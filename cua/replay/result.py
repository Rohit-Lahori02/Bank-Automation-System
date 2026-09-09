"""The replay result contract: what a calling agent gets back.

Four terminal states, deliberately distinct:

  success           the checkpoint held; `outputs` carries the declared values
  business_outcome  the application answered with a legitimate result that is
                    not the happy path ("no such member", "access denied");
                    `outcome_code` says which. Not an error.
  failed            something went wrong; `failure` says at which step, what
                    was expected, what was observed, and where the evidence is
  escalated         a human must decide (risky step, unrecoverable state);
                    `escalation` carries the context that was handed over
"""

from __future__ import annotations

import json
from decimal import Decimal
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class ReplayStatus(str, Enum):
    SUCCESS = "success"
    BUSINESS_OUTCOME = "business_outcome"
    FAILED = "failed"
    ESCALATED = "escalated"


class StepReport(BaseModel):
    step_id: str
    action: str
    status: str = ""                  # ok | recovered | outcome | failed | held | skipped (set when the step ends)
    strategy: str | None = None       # which locator strategy resolved the target, e.g. "label_text#2"
    attempts: int = 1
    duration_ms: int = 0
    conditions: list[str] = Field(default_factory=list)   # condition ids that fired during this step
    note: str = ""


class Failure(BaseModel):
    step_id: str | None
    code: str
    message: str
    expected: str = ""
    observed: str = ""
    screenshot: str | None = None
    attempts: list[str] = Field(default_factory=list)


class Escalation(BaseModel):
    step_id: str | None
    reason: str
    kind: str                         # risky_action | unrecoverable | stuck
    url: str = ""
    screenshot: str | None = None
    screen: str = ""


class ReplayResult(BaseModel):
    status: ReplayStatus
    capability_id: str
    capability_version: int
    run_id: str
    inputs: dict[str, Any] = Field(default_factory=dict)
    outputs: dict[str, Any] = Field(default_factory=dict)
    outcome_code: str | None = None
    outcome_message: str | None = None
    failure: Failure | None = None
    escalation: Escalation | None = None
    steps: list[StepReport] = Field(default_factory=list)
    checkpoint_verified: bool = False
    evidence_dir: str = ""
    duration_ms: int = 0

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.model_dump(mode="json"), indent=indent, default=_json_default, ensure_ascii=False)

    @property
    def exit_code(self) -> int:
        return {ReplayStatus.SUCCESS: 0, ReplayStatus.BUSINESS_OUTCOME: 10,
                ReplayStatus.FAILED: 20, ReplayStatus.ESCALATED: 30}[self.status]

    def one_line(self) -> str:
        if self.status is ReplayStatus.SUCCESS:
            return f"success: outputs={self.outputs}"
        if self.status is ReplayStatus.BUSINESS_OUTCOME:
            return f"business outcome {self.outcome_code}: {self.outcome_message}"
        if self.status is ReplayStatus.FAILED and self.failure:
            return f"failed at {self.failure.step_id} [{self.failure.code}]: {self.failure.message}"
        if self.status is ReplayStatus.ESCALATED and self.escalation:
            return f"escalated at {self.escalation.step_id}: {self.escalation.reason}"
        return self.status.value


def _json_default(obj: Any):
    if isinstance(obj, Decimal):
        return str(obj)
    return str(obj)
