// Human-action capture: installed in every document while a human holds control.
// Reports clicks, value changes, submits and Enter presses to the bound function
// window.__cuaHumanEvent. Password values are masked here, before they leave the page.
(() => {
  if (window.__cuaCaptureInstalled) return;
  window.__cuaCaptureInstalled = true;
  const norm = (s) => (s || "").replace(/\s+/g, " ").trim();

  function describe(el) {
    const tag = el.tagName ? el.tagName.toLowerCase() : "?";
    const type = (el.getAttribute && el.getAttribute("type") || "").toLowerCase();
    let role = "element", name = "";
    const label = () => {
      const cell = el.closest && el.closest("td,th");
      const prev = cell && cell.previousElementSibling;
      if (prev && norm(prev.textContent)) return norm(prev.textContent);
      return el.getAttribute("placeholder") || el.getAttribute("aria-label") || el.getAttribute("name") || "";
    };
    if (tag === "a") { role = "link"; name = norm(el.textContent); }
    else if (tag === "button" || (tag === "input" && ["submit", "button", "reset"].includes(type))) {
      role = "button"; name = tag === "input" ? el.value : norm(el.textContent);
    }
    else if (tag === "input" || tag === "textarea") { role = type === "checkbox" ? "checkbox" : "textbox"; name = label(); }
    else if (tag === "select") { role = "combobox"; name = label(); }
    else { name = norm(el.textContent).slice(0, 60); }
    return { tag, role, name: (name || "").slice(0, 80), name_attr: el.getAttribute ? el.getAttribute("name") : null,
             sensitive: type === "password" };
  }

  const send = (payload) => {
    try { window.__cuaHumanEvent(Object.assign({ url: location.href, at: Date.now() / 1000 }, payload)); } catch (e) {}
  };

  document.addEventListener("click", (e) => {
    const el = e.target && e.target.closest ? e.target.closest("a,button,input,select,label,td,div,span") : null;
    if (!el) return;
    send(Object.assign({ type: "click" }, describe(el)));
  }, true);
  document.addEventListener("change", (e) => {
    const el = e.target; if (!el || !el.tagName) return;
    const d = describe(el);
    const value = d.sensitive ? "••••••" : (el.value || "").slice(0, 100);
    send(Object.assign({ type: "input", value }, d));
  }, true);
  document.addEventListener("submit", (e) => {
    const f = e.target;
    send({ type: "submit", tag: "form", role: "form", name: (f.getAttribute && f.getAttribute("action")) || "", sensitive: false });
  }, true);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && e.target && e.target.tagName) send(Object.assign({ type: "key", key: "Enter" }, describe(e.target)));
  }, true);
})();
