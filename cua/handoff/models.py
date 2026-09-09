"""Intervention request: everything a human needs to take over, and the record of what they did."""

from __future__ import annotations

import time
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class Decision(str, Enum):
    APPROVED = "approved"     # automation may perform the held (risky) step itself
    RESUMED = "resumed"       # the human performed the manual work; automation continues from here
    ABORTED = "aborted"       # the human declined; the run ends as escalated
    TIMEOUT = "timeout"       # no operator responded in time


class HumanAction(BaseModel):
    at: float
    type: str                       # click | input | submit | key
    role: str = ""
    name: str = ""
    tag: str = ""
    value: str | None = None        # masked for sensitive fields
    url: str = ""
    frame: str = "main"

    def render(self) -> str:
        what = f'{self.type} {self.role} "{self.name}"'.strip()
        if self.value is not None and self.type == "input":
            what += f' = "{self.value}"'
        return what


class InterventionRequest(BaseModel):
    id: str
    run_id: str
    run_kind: Literal["replay", "discovery"]
    capability_id: str = ""
    goal: str = ""
    step_id: str | None = None
    kind: str                       # risky_action | unrecoverable | stuck
    reason: str
    url: str = ""
    screenshot: str | None = None
    screen: str = ""
    session_url: str = ""           # CDP endpoint of the live browser, for a remote operator client
    evidence_dir: str = ""
    created_at: float = Field(default_factory=time.time)
    control_token: str = ""
    status: str = "pending"         # pending | claimed | approved | resumed | aborted | timeout
    decided_at: float | None = None
    human_actions: list[HumanAction] = Field(default_factory=list)

    @property
    def decision(self) -> Decision | None:
        try:
            return Decision(self.status)
        except ValueError:
            return None

    def summary(self) -> str:
        acts = "; ".join(a.render() for a in self.human_actions) or "no actions captured"
        return f"{self.status}: {acts}"
