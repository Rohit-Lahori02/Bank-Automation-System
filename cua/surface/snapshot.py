"""Snapshot: what the automation perceives on a surface at one instant.

A snapshot is deliberately surface-agnostic. It is a flat list of *elements*,
each with a role, a human-visible name, a value, a bounding box, and the frame
it lives in. A browser produces it from the DOM (see snapshot.js); a desktop
surface could produce the same structure from an accessibility API or from a
screenshot + OCR/grounding model. Everything above this layer (the agent, the
recorder, the replay engine) only ever sees this structure.
"""

from __future__ import annotations

import hashlib
import re
import time
from typing import Iterable

from pydantic import BaseModel, Field

TEXTUAL_ROLES = {"text", "cell", "heading"}
INTERACTIVE_ROLES = {"link", "button", "textbox", "combobox", "checkbox", "radio"}


class BBox(BaseModel):
    x: float
    y: float
    w: float
    h: float

    @property
    def center(self) -> tuple[float, float]:
        return (self.x + self.w / 2, self.y + self.h / 2)

    @property
    def right(self) -> float:
        return self.x + self.w

    @property
    def bottom(self) -> float:
        return self.y + self.h


class Element(BaseModel):
    ref: str
    frame: str = "main"
    tag: str
    role: str
    name: str = ""
    name_source: str = "none"   # aria | label | content | placeholder | title | adjacent_cell | adjacent_left | adjacent_above | none
    value: str | None = None
    sensitive: bool = False
    href: str | None = None
    type: str | None = None
    name_attr: str | None = None
    checked: bool | None = None
    disabled: bool = False
    bbox: BBox
    in_viewport: bool = True
    css: str = ""
    in_dialog: bool = False
    group: int | None = None    # grid/table index for cells, so rows and columns can be related geometrically

    @property
    def interactive(self) -> bool:
        return self.role in INTERACTIVE_ROLES

    def render(self) -> str:
        parts = [f"[{self.ref}] {self.role}"]
        if self.name:
            parts.append(f'"{self.name}"')
        if self.role in {"textbox", "combobox"}:
            parts.append(f'value="{self.value or ""}"')
        elif self.role in {"checkbox", "radio"}:
            parts.append("checked" if self.checked else "unchecked")
        if self.href:
            parts.append(f"href={self.href}")
        flags = []
        if self.disabled:
            flags.append("disabled")
        if self.in_dialog:
            flags.append("in-dialog")
        if not self.in_viewport:
            flags.append("offscreen")
        if flags:
            parts.append("(" + ", ".join(flags) + ")")
        return " ".join(parts)


class Snapshot(BaseModel):
    url: str
    title: str = ""
    taken_at: float = Field(default_factory=time.time)
    viewport: tuple[int, int] = (0, 0)
    frames: list[str] = Field(default_factory=lambda: ["main"])
    elements: list[Element] = Field(default_factory=list)
    dialogs: list[dict] = Field(default_factory=list)

    # ---- lookup ----------------------------------------------------------
    def find(self, ref: str) -> Element | None:
        return next((e for e in self.elements if e.ref == ref), None)

    def by_role(self, role: str, name: str | None = None, *, frame: str | None = None,
                exact: bool = False) -> list[Element]:
        out = []
        for e in self.elements:
            if e.role != role or (frame and e.frame != frame):
                continue
            if name is not None:
                if exact and e.name.casefold() != name.casefold():
                    continue
                if not exact and name.casefold() not in e.name.casefold():
                    continue
            out.append(e)
        return out

    def text_visible(self, text: str, *, frame: str | None = None) -> bool:
        needle = _norm(text).casefold()
        return any(needle in _norm(e.name).casefold() for e in self.elements if not frame or e.frame == frame)

    def texts(self, frame: str | None = None) -> Iterable[str]:
        return (e.name for e in self.elements if e.role in TEXTUAL_ROLES and (not frame or e.frame == frame))

    @property
    def has_dialog(self) -> bool:
        return bool(self.dialogs)

    # ---- identity ----------------------------------------------------------
    def digest(self) -> str:
        """Stable hash of what is on screen, ignoring sensitive values and refs.

        Used for no-progress detection in the agent loop: two consecutive
        snapshots with the same digest mean the last action changed nothing.
        """
        h = hashlib.sha256()
        h.update(re.sub(r"[?#].*$", "", self.url).encode())
        for e in self.elements:
            val = "" if e.sensitive else (e.value or "")
            h.update(f"|{e.frame}|{e.role}|{e.name}|{val}|{e.checked}".encode())
        return h.hexdigest()[:16]

    # ---- rendering for the model / logs -------------------------------------
    def render(self, *, max_elements: int | None = None, include_text: bool = True) -> str:
        lines = [f"URL: {self.url}", f"Title: {self.title}"]
        if self.dialogs:
            names = ", ".join(f'"{d["name"]}"' for d in self.dialogs)
            lines.append(f"DIALOG PRESENT: {names} - it may block interaction until handled.")
        count = 0
        for frame in self.frames:
            frame_elements = [e for e in self.elements if e.frame == frame]
            if not frame_elements:
                continue
            lines.append(f"--- frame: {frame} ---")
            for e in frame_elements:
                if not include_text and e.role in TEXTUAL_ROLES:
                    continue
                if max_elements is not None and count >= max_elements:
                    lines.append(f"... ({len(self.elements) - count} more elements omitted)")
                    return "\n".join(lines)
                lines.append(e.render())
                count += 1
        return "\n".join(lines)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()
