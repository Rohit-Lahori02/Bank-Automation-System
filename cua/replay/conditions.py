"""Evaluate artifact detectors against a live snapshot."""

from __future__ import annotations

import re

from cua.artifact.schema import (
    AllOf, AnyOf, Capability, Condition, DialogPresent, ElementPresent, TextVisible, UrlMatches,
)
from cua.surface.snapshot import Snapshot


def detect(detector, snapshot: Snapshot, url: str) -> bool:
    if isinstance(detector, TextVisible):
        return bool(detector.text) and snapshot.text_visible(detector.text, frame=detector.frame)
    if isinstance(detector, UrlMatches):
        return re.search(detector.pattern, url) is not None
    if isinstance(detector, ElementPresent):
        return bool(snapshot.by_role(detector.role, detector.name, frame=detector.frame, exact=True))
    if isinstance(detector, DialogPresent):
        if not snapshot.dialogs:
            return False
        if not detector.name_contains:
            return True
        needle = detector.name_contains.casefold()
        return any(needle in d["name"].casefold() for d in snapshot.dialogs)
    if isinstance(detector, AnyOf):
        return any(detect(d, snapshot, url) for d in detector.detectors)
    if isinstance(detector, AllOf):
        # An empty AllOf is "never detected by scanning": such conditions are applied by the
        # engine itself (e.g. the retry policy for expectation timeouts).
        return bool(detector.detectors) and all(detect(d, snapshot, url) for d in detector.detectors)
    return False


def describe(detector) -> str:
    if isinstance(detector, TextVisible):
        where = f" in {detector.frame}" if detector.frame else ""
        return f'text "{detector.text}" visible{where}'
    if isinstance(detector, UrlMatches):
        return f"url matches /{detector.pattern}/"
    if isinstance(detector, ElementPresent):
        where = f" in {detector.frame}" if detector.frame else ""
        return f'{detector.role} "{detector.name}" present{where}'
    if isinstance(detector, DialogPresent):
        return f'dialog "{detector.name_contains or "*"}" present'
    if isinstance(detector, AnyOf):
        return "any of [" + "; ".join(describe(d) for d in detector.detectors) + "]"
    if isinstance(detector, AllOf):
        return "all of [" + "; ".join(describe(d) for d in detector.detectors) + "]"
    return str(detector)


def fired_conditions(capability: Capability, snapshot: Snapshot, url: str) -> list[Condition]:
    return [c for c in capability.conditions.values() if detect(c.detect, snapshot, url)]


def observed(snapshot: Snapshot, limit: int = 8) -> str:
    """A compact description of what is on screen, for failure reports."""
    parts = [f"url={snapshot.url}", f'title="{snapshot.title}"']
    if snapshot.dialogs:
        parts.append("dialogs=" + ", ".join(f'"{d["name"]}"' for d in snapshot.dialogs))
    texts = [t for t in snapshot.texts() if len(t) > 3][:limit]
    if texts:
        parts.append("texts=" + " | ".join(f'"{t[:60]}"' for t in texts))
    return "; ".join(parts)
