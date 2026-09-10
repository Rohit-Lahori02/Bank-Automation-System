"""Runtime fault injection for the mock console.

The real environment's interesting failures are runtime conditions, not layout
drift. This controller lets a test (or the CLI) make the app misbehave on
demand so the replay engine's error taxonomy can be exercised deterministically.

Flags:
  slow_ms             - delay every page response by N milliseconds (sticky by nature)
  expire_session      - the next authenticated request is bounced to /login (one-shot)
  maintenance_dialog  - the next full page render shows a modal notice (one-shot)
  app_error           - the next member profile load returns an application error (one-shot)
  sticky              - when true, one-shot flags are NOT cleared after firing
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from threading import Lock


@dataclass
class ChaosState:
    slow_ms: int = 0
    expire_session: bool = False
    maintenance_dialog: bool = False
    app_error: bool = False
    sticky: bool = False
    after_pages: int = 0        # let this many full-page renders pass before a one-shot flag fires


class ChaosController:
    ONE_SHOT_FLAGS = ("expire_session", "maintenance_dialog", "app_error")

    def __init__(self) -> None:
        self._state = ChaosState()
        self._lock = Lock()

    def snapshot(self) -> dict:
        with self._lock:
            return asdict(self._state)

    def update(self, **changes) -> dict:
        valid = {f.name for f in fields(ChaosState)}
        unknown = set(changes) - valid
        if unknown:
            raise ValueError(f"unknown chaos flags: {sorted(unknown)}")
        with self._lock:
            for key, value in changes.items():
                if key in ("slow_ms", "after_pages"):
                    value = max(0, int(value))
                else:
                    value = _as_bool(value)
                setattr(self._state, key, value)
            return asdict(self._state)

    def reset(self) -> dict:
        with self._lock:
            self._state = ChaosState()
            return asdict(self._state)

    def consume(self, flag: str) -> bool:
        """Read a one-shot flag and clear it unless sticky mode is on.

        With `after_pages` > 0 the flag is armed but held back: each consume attempt lets one
        page pass and decrements the countdown, so a fault can be made to appear mid-flow.
        """
        if flag not in self.ONE_SHOT_FLAGS:
            raise ValueError(f"{flag} is not a one-shot flag")
        with self._lock:
            value = getattr(self._state, flag)
            if value and self._state.after_pages > 0:
                self._state.after_pages -= 1
                return False
            if value and not self._state.sticky:
                setattr(self._state, flag, False)
            return value

    @property
    def slow_ms(self) -> int:
        with self._lock:
            return self._state.slow_ms


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "on"}
