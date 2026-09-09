"""The surface seam.

Everything above this protocol (agent loop, recorder, replay engine, handoff)
talks to a surface only through these operations on Snapshot/Target/ref values.
A browser implements it with Playwright (driver.py). A legacy web app is the
same implementation with the frame-aware walker. A native desktop app would
implement the same protocol over an accessibility API (UIA/AT-SPI) or over
screenshots + a grounding model, producing the same Snapshot structure and
consuming the same Target strategies (role/name, anchor-relative, bbox).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from .locators import Target
from .snapshot import Snapshot


@dataclass
class Resolved:
    """A target resolved on the live surface, with provenance of how."""

    frame: str
    handle: Any                 # surface-specific handle (Playwright Locator for the browser)
    strategy_index: int
    strategy_kind: str
    ref: str | None = None

    def describe(self) -> str:
        return f"{self.strategy_kind}#{self.strategy_index} in {self.frame}"


class TargetNotFound(LookupError):
    def __init__(self, target: Target, attempts: list[str]) -> None:
        self.target = target
        self.attempts = attempts
        super().__init__(f"could not resolve {target.description}; tried: " + "; ".join(attempts))


@runtime_checkable
class Surface(Protocol):
    @property
    def url(self) -> str: ...

    def navigate(self, url: str) -> None: ...
    def snapshot(self) -> Snapshot: ...
    def resolve(self, target: Target) -> Resolved: ...
    def click(self, target: str | Target | Resolved) -> Resolved: ...
    def type(self, target: str | Target | Resolved, text: str, *, submit: bool = False) -> Resolved: ...
    def select(self, target: str | Target | Resolved, option: str) -> Resolved: ...
    def press(self, key: str) -> None: ...
    def wait_for_text(self, text: str, timeout_ms: int = 5000) -> bool: ...
    def screenshot(self, path: Path) -> Path: ...
