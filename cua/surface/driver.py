"""Playwright implementation of the Surface protocol.

Perception is the injected walker (snapshot.js) run in every frame; action goes
through Playwright locators. Tracing is on for every session so a failure
always leaves a rich artifact behind.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Iterable

from playwright.sync_api import Frame, Locator, Page, TimeoutError as PlaywrightTimeout, sync_playwright

from .base import Resolved, Surface, TargetNotFound
from .locators import (
    AnchorRelativeStrategy, BBoxStrategy, CssStrategy, LabelTextStrategy, RoleNameStrategy, TableCellStrategy,
    Target, TextStrategy,
)
from .session import SessionControl
from .snapshot import Element, Snapshot, TEXTUAL_ROLES

WALKER_JS = (Path(__file__).parent / "snapshot.js").read_text(encoding="utf-8")
REF_ATTR = "data-cua-ref"
PROBE_ATTR = "data-cua-probe"


class BrowserSurface(Surface):
    def __init__(
        self,
        *,
        headless: bool = True,
        trace_dir: Path | None = None,
        viewport: tuple[int, int] = (1280, 820),
        slow_mo: int = 0,
        mask_patterns: Iterable[str] = (r"(?i)password", r"(?i)passwd"),
        control: SessionControl | None = None,
        action_timeout_ms: int = 10_000,
    ) -> None:
        self.headless = headless
        self.trace_dir = Path(trace_dir) if trace_dir else None
        self.viewport = viewport
        self.slow_mo = slow_mo
        self.mask_patterns = [re.compile(p) for p in mask_patterns]
        self.control = control or SessionControl()
        self.action_timeout_ms = action_timeout_ms
        self.js_dialogs: list[dict] = []
        self.last_snapshot: Snapshot | None = None
        self._pw = None
        self._browser = None
        self._context = None
        self._page: Page | None = None
        self._ref_frames: dict[str, str] = {}

    # ------------------------------------------------------------ lifecycle
    def start(self) -> "BrowserSurface":
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=self.headless, slow_mo=self.slow_mo)
        self._context = self._browser.new_context(viewport={"width": self.viewport[0], "height": self.viewport[1]})
        if self.trace_dir:
            self.trace_dir.mkdir(parents=True, exist_ok=True)
            self._context.tracing.start(screenshots=True, snapshots=True, sources=False)
        self._page = self._context.new_page()
        self._page.set_default_timeout(self.action_timeout_ms)
        self._page.on("dialog", self._on_js_dialog)
        return self

    def stop(self, trace_name: str = "trace.zip") -> Path | None:
        trace_path = None
        try:
            if self._context and self.trace_dir:
                trace_path = self.trace_dir / trace_name
                self._context.tracing.stop(path=str(trace_path))
        finally:
            if self._browser:
                self._browser.close()
            if self._pw:
                self._pw.stop()
            self._page = self._context = self._browser = self._pw = None
        return trace_path

    def __enter__(self) -> "BrowserSurface":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    @property
    def page(self) -> Page:
        assert self._page is not None, "surface not started"
        return self._page

    def _on_js_dialog(self, dialog) -> None:
        entry = {"type": dialog.type, "message": dialog.message, "at": time.time()}
        self.js_dialogs.append(entry)
        # Conservative default: never confirm something the recording did not plan for.
        if dialog.type == "alert":
            dialog.accept()
        else:
            dialog.dismiss()

    # ------------------------------------------------------------ navigation
    @property
    def url(self) -> str:
        return self.page.url

    @property
    def title(self) -> str:
        return self.page.title()

    def navigate(self, url: str) -> None:
        self.control.assert_automation()
        self.page.goto(url, wait_until="load")
        self.settle()

    def settle(self, network_idle_ms: int = 1500) -> None:
        try:
            self.page.wait_for_load_state("load", timeout=self.action_timeout_ms)
        except PlaywrightTimeout:
            pass
        try:
            self.page.wait_for_load_state("networkidle", timeout=network_idle_ms)
        except PlaywrightTimeout:
            pass

    # ---------------------------------------------------------------- frames
    def frames(self) -> list[tuple[str, Frame]]:
        """All attached frames with a stable path label, main first."""
        page = self.page
        paths: dict[Frame, str] = {page.main_frame: "main"}
        result: list[tuple[str, Frame]] = [("main", page.main_frame)]
        pending = [f for f in page.frames if f is not page.main_frame]
        for _ in range(len(pending) + 1):
            remaining = []
            for f in pending:
                if f.is_detached():
                    continue
                parent = f.parent_frame
                if parent not in paths:
                    remaining.append(f)
                    continue
                try:
                    handle = f.frame_element()
                    selector = handle.evaluate(
                        "e => e.name ? `iframe[name=\"${e.name}\"]` : e.id ? `iframe#${e.id}` "
                        ": `iframe[src=\"${e.getAttribute('src') || ''}\"]`"
                    )
                except Exception:
                    continue
                path = selector if paths[parent] == "main" else f"{paths[parent]} >> {selector}"
                paths[f] = path
                result.append((path, f))
            pending = remaining
            if not pending:
                break
        return result

    def _frame(self, path: str) -> Frame:
        for p, f in self.frames():
            if p == path:
                return f
        raise LookupError(f"frame not present: {path}")

    def _frame_offset(self, frame: Frame) -> tuple[float, float]:
        if frame is self.page.main_frame:
            return (0.0, 0.0)
        try:
            box = frame.frame_element().bounding_box() or {"x": 0, "y": 0}
            px, py = self._frame_offset(frame.parent_frame) if frame.parent_frame else (0.0, 0.0)
            return (px + box["x"], py + box["y"])
        except Exception:
            return (0.0, 0.0)

    # ------------------------------------------------------------ perception
    def snapshot(self, *, max_text: int = 200) -> Snapshot:
        page = self.page
        elements: list[Element] = []
        dialogs: list[dict] = []
        frame_paths: list[str] = []
        next_ref = 0
        self._ref_frames = {}
        for path, frame in self.frames():
            try:
                data = frame.evaluate(WALKER_JS, {"refStart": next_ref, "maxText": max_text, "attrName": REF_ATTR})
            except Exception:
                continue  # frame navigated away mid-snapshot; skip it
            frame_paths.append(path)
            next_ref = data["next_ref"]
            ox, oy = self._frame_offset(frame)
            for raw in data["elements"]:
                el = Element(frame=path, **raw)
                el.bbox.x += ox
                el.bbox.y += oy
                if self._is_sensitive(el):
                    el.sensitive = True
                    if el.value:
                        el.value = "••••••"
                elements.append(el)
                self._ref_frames[el.ref] = path
            for d in data["dialogs"]:
                dialogs.append({"ref": d["ref"], "name": d["name"], "frame": path})
        snap = Snapshot(
            url=page.url, title=page.title(), viewport=self.viewport,
            frames=frame_paths or ["main"], elements=elements, dialogs=dialogs,
        )
        self.last_snapshot = snap
        return snap

    def _is_sensitive(self, el: Element) -> bool:
        if el.sensitive:
            return True
        haystack = " ".join(filter(None, [el.name, el.name_attr]))
        return any(p.search(haystack) for p in self.mask_patterns)

    def text_visible(self, text: str) -> bool:
        for _, frame in self.frames():
            try:
                if frame.get_by_text(text, exact=False).first.is_visible():
                    return True
            except Exception:
                continue
        return False

    def wait_for_text(self, text: str, timeout_ms: int = 5000) -> bool:
        deadline = time.time() + timeout_ms / 1000
        while time.time() < deadline:
            if self.text_visible(text):
                return True
            time.sleep(0.15)
        return False

    def screenshot(self, path: Path, *, full_page: bool = True) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.page.screenshot(path=str(path), full_page=full_page)
        return path

    # ------------------------------------------------------------ resolution
    def resolve(self, target: Target) -> Resolved:
        attempts: list[str] = []
        for index, strategy in enumerate(target.strategies):
            try:
                frame = self._frame(strategy.frame)
            except LookupError:
                attempts.append(f"{strategy.kind}: frame {strategy.frame} not present")
                continue
            try:
                locator = self._try_strategy(frame, strategy)
            except Exception as exc:  # a broken selector must not abort the chain
                attempts.append(f"{strategy.kind}: error {type(exc).__name__}")
                continue
            if locator is not None:
                return Resolved(frame=strategy.frame, handle=locator, strategy_index=index, strategy_kind=strategy.kind)
            attempts.append(f"{strategy.kind}: no visible match")
        raise TargetNotFound(target, attempts)

    def _try_strategy(self, frame: Frame, s) -> Locator | None:
        if isinstance(s, RoleNameStrategy):
            return self._visible_nth(frame.get_by_role(s.role, name=s.name, exact=s.exact), s.nth)
        if isinstance(s, TextStrategy):
            return self._visible_nth(frame.get_by_text(s.text, exact=s.exact), s.nth)
        if isinstance(s, CssStrategy):
            return self._visible_nth(frame.locator(s.selector), s.nth)
        if isinstance(s, (LabelTextStrategy, AnchorRelativeStrategy, TableCellStrategy, BBoxStrategy)):
            ref = self._probe(frame, s)
            return self._visible_nth(frame.locator(f'[{PROBE_ATTR}="{ref}"]'), 0) if ref else None
        return None

    @staticmethod
    def _visible_nth(locator: Locator, nth: int) -> Locator | None:
        if locator.count() <= nth:
            return None
        candidate = locator.nth(nth)
        return candidate if candidate.is_visible() else None

    def _probe(self, frame: Frame, s) -> str | None:
        """Run the walker with a probe attribute and pick a match for inference-based strategies."""
        data = frame.evaluate(WALKER_JS, {"refStart": 0, "maxText": 400, "attrName": PROBE_ATTR})
        elements = [Element(frame=s.frame, **raw) for raw in data["elements"]]
        if isinstance(s, LabelTextStrategy):
            matches = [e for e in elements if e.role == s.role and e.name.casefold() == s.text.casefold()]
            return matches[s.nth].ref if len(matches) > s.nth else None
        if isinstance(s, AnchorRelativeStrategy):
            anchors = [e for e in elements if e.role in TEXTUAL_ROLES and s.anchor_text.casefold() in e.name.casefold()]
            candidates = [e for e in elements if e.role == s.role]
            best, best_d = None, None
            for a in anchors:
                for c in candidates:
                    if s.direction == "right":
                        ok = c.bbox.x >= a.bbox.right - 6 and _overlap(a.bbox.y, a.bbox.bottom, c.bbox.y, c.bbox.bottom) > 4
                        d = c.bbox.x - a.bbox.right
                    else:
                        ok = c.bbox.y >= a.bbox.bottom - 6 and _overlap(a.bbox.x, a.bbox.right, c.bbox.x, c.bbox.right) > 4
                        d = c.bbox.y - a.bbox.bottom
                    if ok and d < 300 and (best_d is None or d < best_d):
                        best, best_d = c, d
            return best.ref if best else None
        if isinstance(s, TableCellStrategy):
            cells = [e for e in elements if e.role == "cell" and e.group is not None]
            headers = [e for e in cells if e.name.casefold() == s.column_header.casefold()]
            anchors = [e for e in cells if s.row_text.casefold() in e.name.casefold()]
            hits = []
            for h in headers:
                for a in anchors:
                    if a.group != h.group or a is h:
                        continue
                    for c in cells:
                        if c.group != h.group or c is h or c is a:
                            continue
                        same_col = _overlap(c.bbox.x, c.bbox.right, h.bbox.x, h.bbox.right) > min(c.bbox.w, h.bbox.w) * 0.5
                        same_row = _overlap(c.bbox.y, c.bbox.bottom, a.bbox.y, a.bbox.bottom) > min(c.bbox.h, a.bbox.h) * 0.5
                        if same_col and same_row and c.bbox.y > h.bbox.y:
                            hits.append(c)
            unique = list({c.ref: c for c in hits}.values())
            return unique[s.nth].ref if len(unique) > s.nth else None
        if isinstance(s, BBoxStrategy):
            vw, vh = data["viewport"]["w"], data["viewport"]["h"]
            sx = vw / s.viewport_w if s.viewport_w else 1.0
            sy = vh / s.viewport_h if s.viewport_h else 1.0
            cx, cy = (s.x + s.w / 2) * sx, (s.y + s.h / 2) * sy
            tolerance = max(40.0, 0.6 * max(s.w * sx, s.h * sy))
            pool = [e for e in elements if e.role == s.role] or [e for e in elements if e.interactive]
            best, best_d = None, None
            for e in pool:
                ex, ey = e.bbox.center
                d = ((ex - cx) ** 2 + (ey - cy) ** 2) ** 0.5
                if d <= tolerance and (best_d is None or d < best_d):
                    best, best_d = e, d
            return best.ref if best else None
        return None

    def _locator(self, target: str | Target | Resolved) -> Resolved:
        if isinstance(target, Resolved):
            return target
        if isinstance(target, Target):
            return self.resolve(target)
        frame_path = self._ref_frames.get(target)
        if frame_path is None:
            raise LookupError(f"unknown ref {target}; take a snapshot first")
        locator = self._frame(frame_path).locator(f'[{REF_ATTR}="{target}"]')
        if locator.count() == 0:
            raise LookupError(f"ref {target} no longer exists on the page; take a new snapshot")
        return Resolved(frame=frame_path, handle=locator.first, strategy_index=-1, strategy_kind="ref", ref=target)

    # ---------------------------------------------------------------- actions
    def click(self, target: str | Target | Resolved) -> Resolved:
        self.control.assert_automation()
        resolved = self._locator(target)
        resolved.handle.click(timeout=self.action_timeout_ms)
        self.settle()
        return resolved

    def type(self, target: str | Target | Resolved, text: str, *, submit: bool = False) -> Resolved:
        self.control.assert_automation()
        resolved = self._locator(target)
        resolved.handle.fill(text, timeout=self.action_timeout_ms)
        if submit:
            resolved.handle.press("Enter")
            self.settle()
        return resolved

    def select(self, target: str | Target | Resolved, option: str) -> Resolved:
        self.control.assert_automation()
        resolved = self._locator(target)
        try:
            resolved.handle.select_option(label=option, timeout=self.action_timeout_ms)
        except Exception:
            resolved.handle.select_option(value=option, timeout=self.action_timeout_ms)
        self.settle()
        return resolved

    def press(self, key: str) -> None:
        self.control.assert_automation()
        self.page.keyboard.press(key)
        self.settle()


def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return min(a1, b1) - max(a0, b0)
