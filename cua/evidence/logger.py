"""Structured, redacted evidence for a run.

Every run gets a directory with:
  log.jsonl       one event per line (decisions, policy verdicts, actions, outcomes)
  summary.json    the run's final state
  *.png           screenshots at failure, escalation and completion
  trace.zip       the Playwright trace (written by the surface on stop)
Everything passes through the Redactor before touching disk.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from cua.policy.redaction import Redactor


class RunLogger:
    def __init__(self, run_dir: Path, redactor: Redactor) -> None:
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.redactor = redactor
        self.log_path = self.run_dir / "log.jsonl"
        self._count = 0

    def event(self, kind: str, **data: Any) -> dict:
        record = {"ts": round(time.time(), 3), "seq": self._count, "kind": kind, **self.redactor.redact(_jsonable(data))}
        with open(self.log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        self._count += 1
        return record

    def write_json(self, name: str, obj: Any) -> Path:
        path = self.run_dir / name
        path.write_text(json.dumps(self.redactor.redact(_jsonable(obj)), indent=2, ensure_ascii=False, default=str),
                        encoding="utf-8")
        return path

    def screenshot(self, surface, name: str) -> Path | None:
        try:
            return surface.screenshot(self.run_dir / f"{name}.png")
        except Exception:
            return None

    def read_events(self) -> list[dict]:
        if not self.log_path.exists():
            return []
        return [json.loads(line) for line in self.log_path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _jsonable(obj: Any) -> Any:
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, Path):
        return str(obj)
    return obj
