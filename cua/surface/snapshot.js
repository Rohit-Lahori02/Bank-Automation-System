// Snapshot walker: perceives one frame of a page the way an operator sees it.
//
// Produces a compact list of visible controls and text with an inferred role,
// an accessible-name approximation with LEGACY FALLBACKS (adjacent table cell,
// nearest text to the left/above), the current value, a bounding box, and a
// CSS path hint. Each element is tagged with data-cua-ref so the driver can act
// on it within this page state. Nothing here relies on test IDs or clean markup.
//
// Called with { refStart: number, maxText: number }.
(args) => {
  const refStart = args.refStart || 0;
  const maxText = args.maxText || 200;
  // Snapshots tag with data-cua-ref; resolution probes use a separate attribute so
  // they never invalidate refs the agent is still holding from the last snapshot.
  const attrName = args.attrName || "data-cua-ref";

  const norm = (s) => (s || "").replace(/\s+/g, " ").trim();
  const clip = (s, n) => (s.length > n ? s.slice(0, n - 1) + "…" : s);

  function isVisible(el) {
    if (typeof el.checkVisibility === "function") {
      if (!el.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true })) return false;
    } else {
      const cs = getComputedStyle(el);
      if (cs.display === "none" || cs.visibility === "hidden" || parseFloat(cs.opacity) === 0) return false;
    }
    const r = el.getBoundingClientRect();
    return r.width >= 1 && r.height >= 1;
  }

  function directText(el) {
    let t = "";
    for (const n of el.childNodes) if (n.nodeType === 3) t += n.textContent;
    return norm(t);
  }

  function roleOf(el) {
    const explicit = el.getAttribute("role");
    if (explicit) return explicit.toLowerCase();
    const tag = el.tagName;
    const type = (el.getAttribute("type") || "text").toLowerCase();
    if (tag === "A") return el.hasAttribute("href") ? "link" : "text";
    if (tag === "BUTTON") return "button";
    if (tag === "INPUT") {
      if (type === "hidden") return null;
      if (["submit", "button", "reset", "image"].includes(type)) return "button";
      if (type === "checkbox") return "checkbox";
      if (type === "radio") return "radio";
      return "textbox";
    }
    if (tag === "SELECT") return "combobox";
    if (tag === "TEXTAREA") return "textbox";
    if (tag === "DIALOG") return "dialog";
    if (/^H[1-6]$/.test(tag)) return "heading";
    if (tag === "TD" || tag === "TH") return "cell";
    if (tag === "IMG") return "img";
    if (tag === "OPTION" || tag === "SCRIPT" || tag === "STYLE" || tag === "NOSCRIPT") return null;
    if (el.isContentEditable) return "textbox";
    if (el.hasAttribute("onclick")) return "button";
    return "text";
  }

  function cssPath(el) {
    const parts = [];
    let cur = el;
    while (cur && cur.nodeType === 1 && cur.tagName !== "BODY" && parts.length < 10) {
      let seg = cur.tagName.toLowerCase();
      const nameAttr = cur.getAttribute("name");
      if (nameAttr) {
        seg += `[name="${nameAttr}"]`;
      } else {
        let i = 1, sib = cur;
        while ((sib = sib.previousElementSibling)) if (sib.tagName === cur.tagName) i++;
        if (i > 1 || (cur.parentElement && cur.parentElement.querySelectorAll(`:scope > ${cur.tagName.toLowerCase()}`).length > 1)) {
          seg += `:nth-of-type(${i})`;
        }
      }
      parts.unshift(seg);
      cur = cur.parentElement;
    }
    return parts.join(" > ");
  }

  function labelFor(el) {
    if (el.id) {
      const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (l) return norm(l.textContent);
    }
    const wrap = el.closest("label");
    if (wrap) return norm(wrap.textContent);
    return null;
  }

  function adjacentCell(el) {
    const cell = el.closest("td,th");
    if (!cell) return null;
    let prev = cell.previousElementSibling;
    while (prev) {
      const t = norm(prev.textContent);
      if (t && !prev.querySelector("input,select,textarea,button")) return clip(t, 60);
      prev = prev.previousElementSibling;
    }
    return null;
  }

  function overlap(a0, a1, b0, b1) {
    return Math.min(a1, b1) - Math.max(a0, b0);
  }

  function layoutNeighbor(rect, textItems) {
    let best = null;
    for (const t of textItems) {
      const r = t.rect;
      // to the left, vertically overlapping
      if (r.right <= rect.left + 6 && overlap(r.top, r.bottom, rect.top, rect.bottom) > 4) {
        const d = rect.left - r.right;
        if (d < 240 && (!best || d < best.d)) best = { text: t.text, dir: "left", d };
      }
    }
    if (best) return best;
    for (const t of textItems) {
      const r = t.rect;
      if (r.bottom <= rect.top + 6 && overlap(r.left, r.right, rect.left, rect.right) > 4) {
        const d = rect.top - r.bottom;
        if (d < 56 && (!best || d < best.d)) best = { text: t.text, dir: "above", d };
      }
    }
    return best;
  }

  // Tables get an index so cells can be grouped by grid without relying on <th>.
  const tableIndex = new Map();
  document.querySelectorAll("table").forEach((t, i) => tableIndex.set(t, i));
  function groupOf(el, role) {
    if (role !== "cell") return null;
    const tbl = el.closest("table");
    return tbl ? tableIndex.get(tbl) : null;
  }

  // ---- pass 1: collect visible candidates -------------------------------
  const vw = window.innerWidth, vh = window.innerHeight;
  const all = document.body ? document.body.querySelectorAll("*") : [];
  const raw = [];
  const textItems = [];
  for (const el of all) {
    const role = roleOf(el);
    if (!role) continue;
    if (!isVisible(el)) continue;
    const rect = el.getBoundingClientRect();
    const isTextual = role === "text" || role === "cell" || role === "heading";
    let dtext = "";
    if (isTextual) {
      dtext = directText(el);
      if (!dtext && role !== "heading") continue;
      if (!dtext && role === "heading") dtext = norm(el.textContent);
      if (!dtext) continue;
      textItems.push({ text: clip(dtext, 60), rect });
    }
    raw.push({ el, role, rect, dtext });
  }

  // ---- dialogs: overlays that cover a large part of the viewport ---------
  const dialogRoots = [];
  for (const el of all) {
    if (!isVisible(el)) continue;
    const cs = getComputedStyle(el);
    const isDialog = el.tagName === "DIALOG" || el.getAttribute("role") === "dialog" || el.getAttribute("aria-modal") === "true";
    const overlay = (cs.position === "fixed" || cs.position === "absolute") && (parseInt(cs.zIndex, 10) || 0) >= 100;
    if (!isDialog && !overlay) continue;
    const r = el.getBoundingClientRect();
    if (!isDialog && r.width * r.height < 0.3 * vw * vh) continue;
    if (dialogRoots.some((d) => d.contains(el))) continue;
    dialogRoots.push(el);
  }

  // ---- pass 2: name, value, output --------------------------------------
  const out = [];
  let n = refStart;
  let textCount = 0;
  const dialogs = [];

  const dialogInfo = new Map();
  for (const root of dialogRoots) {
    const ref = "e" + (++n);
    root.setAttribute(attrName, ref);
    const r = root.getBoundingClientRect();
    let title = "";
    const cand = root.querySelector("h1,h2,h3,h4,h5,h6,[class*='title'],[class*='head'],b,strong");
    if (cand) title = norm(cand.textContent);
    if (!title) {
      const firstText = Array.from(root.querySelectorAll("*")).map(directText).find((t) => t);
      title = firstText || "dialog";
    }
    dialogInfo.set(root, ref);
    dialogs.push({ ref, name: clip(title, 80) });
    out.push({
      ref, tag: root.tagName.toLowerCase(), role: "dialog", name: clip(title, 80), name_source: "content",
      value: null, sensitive: false, href: null, type: null, name_attr: null, checked: null, disabled: false,
      bbox: { x: r.left + scrollX, y: r.top + scrollY, w: r.width, h: r.height },
      in_viewport: true, css: cssPath(root), in_dialog: false,
    });
  }

  for (const item of raw) {
    const { el, role, rect, dtext } = item;
    if (dialogRoots.includes(el)) continue;
    const isTextual = role === "text" || role === "cell" || role === "heading";
    if (isTextual) {
      if (textCount >= maxText) continue;
      textCount++;
    }

    let name = "", source = "none";
    const aria = el.getAttribute("aria-label");
    const labelledBy = el.getAttribute("aria-labelledby");
    if (aria) { name = norm(aria); source = "aria"; }
    else if (labelledBy) {
      const t = labelledBy.split(/\s+/).map((id) => document.getElementById(id)).filter(Boolean).map((e) => norm(e.textContent)).join(" ");
      if (t) { name = t; source = "aria"; }
    }
    if (!name) {
      if (role === "button") {
        const v = el.tagName === "INPUT" ? el.value : norm(el.textContent);
        if (v) { name = norm(v); source = "content"; }
        else if (el.getAttribute("title")) { name = norm(el.getAttribute("title")); source = "title"; }
      } else if (role === "link") {
        const t = norm(el.textContent);
        if (t) { name = t; source = "content"; }
        else {
          const img = el.querySelector("img[alt]");
          if (img) { name = norm(img.getAttribute("alt")); source = "content"; }
          else if (el.getAttribute("title")) { name = norm(el.getAttribute("title")); source = "title"; }
        }
      } else if (role === "img") {
        name = norm(el.getAttribute("alt") || el.getAttribute("title") || ""); source = name ? "content" : "none";
      } else if (isTextual) {
        name = dtext; source = "content";
      } else {
        const lbl = labelFor(el);
        if (lbl) { name = lbl; source = "label"; }
        else if (el.getAttribute("placeholder")) { name = norm(el.getAttribute("placeholder")); source = "placeholder"; }
        else if (el.getAttribute("title")) { name = norm(el.getAttribute("title")); source = "title"; }
        else {
          const cell = adjacentCell(el);
          if (cell) { name = cell; source = "adjacent_cell"; }
          else {
            const nb = layoutNeighbor(rect, textItems);
            if (nb) { name = nb.text; source = "adjacent_" + nb.dir; }
          }
        }
      }
    }
    name = clip(name, 120);

    const tag = el.tagName.toLowerCase();
    const type = el.getAttribute("type") ? el.getAttribute("type").toLowerCase() : null;
    let value = null, checked = null;
    if (role === "textbox") value = el.isContentEditable ? norm(el.textContent) : (el.value ?? "");
    else if (role === "combobox" && tag === "select") value = el.options[el.selectedIndex] ? norm(el.options[el.selectedIndex].text) : "";
    else if (role === "checkbox" || role === "radio") checked = !!el.checked;
    const sensitive = type === "password";

    const ref = "e" + (++n);
    el.setAttribute(attrName, ref);
    let inDialog = false;
    for (const root of dialogRoots) if (root.contains(el)) { inDialog = true; break; }

    out.push({
      ref, tag, role, name, name_source: source,
      value: sensitive && value ? "••••••" : value,
      sensitive,
      href: role === "link" ? el.getAttribute("href") : null,
      type, name_attr: el.getAttribute("name"), checked,
      disabled: !!el.disabled,
      bbox: { x: rect.left + scrollX, y: rect.top + scrollY, w: rect.width, h: rect.height },
      in_viewport: rect.bottom > 0 && rect.right > 0 && rect.top < vh && rect.left < vw,
      css: cssPath(el), in_dialog: inDialog, group: groupOf(el, role),
    });
  }

  return {
    url: location.href, title: document.title,
    viewport: { w: vw, h: vh }, scroll: { x: scrollX, y: scrollY },
    elements: out, dialogs, next_ref: n,
  };
}
