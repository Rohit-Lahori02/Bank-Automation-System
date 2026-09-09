"""Who is in control of the live session.

The automation and a human operator share ONE live session. This object is the
seam that makes that safe: every automated action asserts that automation holds
control, and a handoff is an explicit, logged transfer of a control token.
Phase 6 builds the escalation/resume workflow on top of this state machine.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from enum import Enum


class Controller(str, Enum):
    AUTOMATION = "automation"
    HUMAN = "human"
    PAUSED = "paused"


class ControlError(RuntimeError):
    """Raised when automation tries to act while it does not hold control."""


@dataclass
class ControlTransition:
    at: float
    from_holder: Controller
    to_holder: Controller
    reason: str
    token: str


@dataclass
class SessionControl:
    holder: Controller = Controller.AUTOMATION
    token: str = field(default_factory=lambda: secrets.token_hex(8))
    since: float = field(default_factory=time.time)
    history: list[ControlTransition] = field(default_factory=list)

    def assert_automation(self) -> None:
        if self.holder is not Controller.AUTOMATION:
            raise ControlError(f"automation does not hold control (holder={self.holder.value})")

    def transfer(self, to: Controller, reason: str) -> str:
        """Move control to another holder; returns the new control token."""
        if to is self.holder:
            return self.token
        new_token = secrets.token_hex(8)
        self.history.append(ControlTransition(time.time(), self.holder, to, reason, new_token))
        self.holder = to
        self.token = new_token
        self.since = time.time()
        return new_token

    def resume(self, token: str, reason: str = "resume") -> None:
        """Hand control back to automation; the caller must present the current token."""
        if token != self.token:
            raise ControlError("stale control token; someone else changed control since it was issued")
        self.transfer(Controller.AUTOMATION, reason)

    def as_dict(self) -> dict:
        return {
            "holder": self.holder.value,
            "since": self.since,
            "transitions": [
                {"at": t.at, "from": t.from_holder.value, "to": t.to_holder.value, "reason": t.reason}
                for t in self.history
            ],
        }
