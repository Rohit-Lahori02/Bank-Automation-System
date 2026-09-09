"""File-backed capability store: one JSON file per (id, version)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .schema import Capability

if TYPE_CHECKING:  # avoid an import cycle with cua.policy at runtime
    from cua.policy.redaction import Redactor


@dataclass(frozen=True)
class StoredCapability:
    id: str
    version: int
    status: str
    name: str
    path: Path


class ArtifactStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def path_for(self, capability: Capability) -> Path:
        return self.root / capability.filename

    def save(self, capability: Capability, *, redactor: Redactor | None = None, overwrite: bool = False) -> Path:
        """Persist an artifact. Refuses to write anything containing a known secret value."""
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.path_for(capability)
        if path.exists() and not overwrite:
            raise FileExistsError(f"{path.name} already exists; bump the version or pass overwrite=True")
        text = capability.model_dump_json(indent=2, exclude_none=True)
        if redactor is not None:
            redactor.assert_clean(text, context=path.name)
        path.write_text(text, encoding="utf-8")
        return path

    def load(self, ref: str | Path, version: int | None = None) -> Capability:
        path = Path(ref)
        if path.suffix == ".json" and path.exists():
            return Capability.model_validate_json(path.read_text(encoding="utf-8"))
        candidates = [c for c in self.list() if c.id == str(ref)]
        if version is not None:
            candidates = [c for c in candidates if c.version == version]
        if not candidates:
            raise FileNotFoundError(f"no capability '{ref}'{f' v{version}' if version else ''} in {self.root}")
        latest = max(candidates, key=lambda c: c.version)
        return Capability.model_validate_json(latest.path.read_text(encoding="utf-8"))

    def list(self) -> list[StoredCapability]:
        if not self.root.exists():
            return []
        out = []
        for path in sorted(self.root.glob("*.json")):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                out.append(StoredCapability(raw["id"], int(raw["version"]), raw.get("status", "draft"), raw.get("name", ""), path))
            except (ValueError, KeyError):
                continue
        return out
