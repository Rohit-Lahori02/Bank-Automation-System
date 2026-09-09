"""The capability artifact: a typed, versioned, reviewable contract.

A Capability is what an AI agent invokes in production. It is NOT a transcript
of what the model did; it is a declarative description of:

  * what the capability needs   -> typed `inputs` and named `secrets`
  * what it returns             -> typed `outputs`, each tied to an extract step
  * what it does                -> ordered `steps`, each with a Target whose
                                   locator strategies carry their own rationale
  * what can legitimately go    -> `conditions`, each classified as a business
    wrong                          outcome, a recoverable condition, or a hard
                                   failure, with a handler where recovery exists
  * how success is verified     -> per-step `expect` postconditions and a final
                                   `checkpoint`
  * where it came from          -> `provenance` (run id, model, redaction)

Design rules:
  - Values that vary per invocation are templates: {{inputs.member_id}}.
    Credentials are never stored; steps reference {{secrets.app.password}} and
    the value is supplied at replay time.
  - Anything that would make the artifact tenant-specific lives in `target`
    (app id, variant) so one recording can be reused or overridden per tenant.
  - The schema is strict: unknown fields are rejected, references are checked.
"""

from __future__ import annotations

import re
import time
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cua.surface.locators import Target

SCHEMA_VERSION = "1.0"
PLACEHOLDER_RE = re.compile(r"\{\{\s*(inputs|secrets)\.([A-Za-z_][\w.]*)\s*\}\}")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------- params
class ParamType(str, Enum):
    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"
    MONEY = "money"


class InputError(ValueError):
    def __init__(self, name: str, message: str) -> None:
        self.name = name
        super().__init__(f"input '{name}': {message}")


class InputParam(_Strict):
    type: ParamType = ParamType.STRING
    description: str = ""
    required: bool = True
    pattern: str | None = None
    example: str | None = None
    default: str | None = None
    sensitive: bool = False     # redacted from logs/evidence even though it is an input

    def coerce(self, name: str, raw: Any) -> Any:
        if raw is None:
            if self.default is not None:
                raw = self.default
            elif self.required:
                raise InputError(name, "required")
            else:
                return None
        text = str(raw).strip()
        if self.pattern and not re.fullmatch(self.pattern, text):
            raise InputError(name, f"value does not match pattern {self.pattern}")
        try:
            if self.type is ParamType.INTEGER:
                return int(text)
            if self.type is ParamType.NUMBER:
                return float(text)
            if self.type is ParamType.BOOLEAN:
                if text.lower() in {"true", "1", "yes"}:
                    return True
                if text.lower() in {"false", "0", "no"}:
                    return False
                raise ValueError
            if self.type is ParamType.MONEY:
                return Decimal(text.replace("$", "").replace(",", "")).quantize(Decimal("0.01"))
        except (ValueError, InvalidOperation):
            raise InputError(name, f"expected {self.type.value}, got {text!r}") from None
        return text


class OutputParam(_Strict):
    type: ParamType = ParamType.STRING
    description: str = ""
    from_step: str


def parse_output(param: OutputParam, text: str) -> Any:
    """Turn extracted screen text into the declared output type."""
    text = (text or "").strip()
    if param.type is ParamType.MONEY:
        cleaned = re.sub(r"[^\d.\-]", "", text)
        return Decimal(cleaned).quantize(Decimal("0.01")) if cleaned else None
    if param.type is ParamType.INTEGER:
        digits = re.sub(r"[^\d\-]", "", text)
        return int(digits) if digits else None
    if param.type is ParamType.NUMBER:
        cleaned = re.sub(r"[^\d.\-]", "", text)
        return float(cleaned) if cleaned else None
    if param.type is ParamType.BOOLEAN:
        return text.lower() in {"true", "yes", "y", "1", "open", "active"}
    return text


# ------------------------------------------------------------------ detectors
class TextVisible(_Strict):
    kind: Literal["text_visible"] = "text_visible"
    text: str
    frame: str | None = None


class UrlMatches(_Strict):
    kind: Literal["url_matches"] = "url_matches"
    pattern: str


class ElementPresent(_Strict):
    kind: Literal["element_present"] = "element_present"
    role: str
    name: str
    frame: str | None = None


class DialogPresent(_Strict):
    kind: Literal["dialog_present"] = "dialog_present"
    name_contains: str | None = None


class AnyOf(_Strict):
    kind: Literal["any_of"] = "any_of"
    detectors: list["Detector"]


class AllOf(_Strict):
    kind: Literal["all_of"] = "all_of"
    detectors: list["Detector"]


Detector = Annotated[
    Union[TextVisible, UrlMatches, ElementPresent, DialogPresent, AnyOf, AllOf],
    Field(discriminator="kind"),
]
AnyOf.model_rebuild()
AllOf.model_rebuild()


# ---------------------------------------------------------------- conditions
class ConditionClass(str, Enum):
    BUSINESS_OUTCOME = "business_outcome"   # legitimate result the caller needs ("no such member")
    RECOVERABLE = "recoverable"             # known interstitial / transient state with a handler
    HARD_FAILURE = "hard_failure"           # stop; surface a debuggable error


class ClickHandler(_Strict):
    kind: Literal["click"] = "click"
    target: Target
    description: str = ""


class RetryHandler(_Strict):
    kind: Literal["retry"] = "retry"
    max_attempts: int = 3
    backoff_ms: int = 1000


class SubflowHandler(_Strict):
    kind: Literal["subflow"] = "subflow"
    steps: list["Step"]
    then: Literal["retry_step", "continue"] = "retry_step"


class EscalateHandler(_Strict):
    kind: Literal["escalate"] = "escalate"
    reason: str


Handler = Annotated[
    Union[ClickHandler, RetryHandler, SubflowHandler, EscalateHandler],
    Field(discriminator="kind"),
]


class Condition(_Strict):
    id: str
    description: str = ""
    detect: Detector
    classification: ConditionClass
    code: str | None = None          # required for business_outcome / hard_failure
    handler: Handler | None = None   # required for recoverable

    @model_validator(mode="after")
    def _shape(self) -> "Condition":
        if self.classification is ConditionClass.RECOVERABLE and self.handler is None:
            raise ValueError(f"condition '{self.id}' is recoverable but has no handler")
        if self.classification is not ConditionClass.RECOVERABLE and not self.code:
            raise ValueError(f"condition '{self.id}' needs a result code")
        return self


# --------------------------------------------------------------------- steps
class ActionKind(str, Enum):
    NAVIGATE = "navigate"
    CLICK = "click"
    TYPE = "type"
    SELECT = "select"
    PRESS = "press"
    EXTRACT = "extract"
    WAIT = "wait"


class RiskClass(str, Enum):
    SAFE = "safe"       # reversible / read-only
    RISKY = "risky"     # irreversible or externally visible (confirm, submit, transfer)


class Expectation(_Strict):
    description: str = ""
    detect: Detector
    timeout_ms: int = 8000


class Step(_Strict):
    id: str
    action: ActionKind
    description: str = ""
    target: Target | None = None
    value: str | None = None       # for type: may contain {{inputs.x}} / {{secrets.x}}
    option: str | None = None      # for select
    url: str | None = None         # for navigate; may contain placeholders
    key: str | None = None         # for press
    output: str | None = None      # for extract: output name
    risk: RiskClass = RiskClass.SAFE
    irreversible: bool = False
    expect: Expectation | None = None
    on_conditions: list[str] = Field(default_factory=list)   # condition ids to check after this step
    wait_before_ms: int = 0

    @model_validator(mode="after")
    def _shape(self) -> "Step":
        needs_target = {ActionKind.CLICK, ActionKind.TYPE, ActionKind.SELECT, ActionKind.EXTRACT}
        if self.action in needs_target and self.target is None:
            raise ValueError(f"step '{self.id}': {self.action.value} requires a target")
        if self.action is ActionKind.NAVIGATE and not self.url:
            raise ValueError(f"step '{self.id}': navigate requires url")
        if self.action is ActionKind.TYPE and self.value is None:
            raise ValueError(f"step '{self.id}': type requires value")
        if self.action is ActionKind.SELECT and self.option is None:
            raise ValueError(f"step '{self.id}': select requires option")
        if self.action is ActionKind.PRESS and not self.key:
            raise ValueError(f"step '{self.id}': press requires key")
        if self.action is ActionKind.EXTRACT and not self.output:
            raise ValueError(f"step '{self.id}': extract requires output")
        if self.irreversible:
            self.risk = RiskClass.RISKY
        return self

    def placeholders(self) -> list[tuple[str, str]]:
        found: list[tuple[str, str]] = []
        for text in (self.value, self.url, self.option):
            if text:
                found.extend((m.group(1), m.group(2)) for m in PLACEHOLDER_RE.finditer(text))
        return found


SubflowHandler.model_rebuild()
Condition.model_rebuild()


# ---------------------------------------------------------------- capability
class Checkpoint(_Strict):
    description: str = ""
    detect: Detector


class TargetApp(_Strict):
    app: str                 # vendor product id, e.g. "corelink-member-servicing"
    entry_url: str
    variant: str = "base"    # tenant/version variant this recording was made on


class Provenance(_Strict):
    discovery_run_id: str
    recorded_at: float = Field(default_factory=time.time)
    provider: str
    model: str
    surface: str = "browser-playwright"
    goal: str
    discovery_steps: int = 0
    redaction_applied: bool = True
    notes: str = ""


class CapabilityStatus(str, Enum):
    DRAFT = "draft"
    APPROVED = "approved"
    DEPRECATED = "deprecated"


class Capability(_Strict):
    schema_version: str = SCHEMA_VERSION
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$")
    name: str
    version: int = 1
    status: CapabilityStatus = CapabilityStatus.DRAFT
    description: str
    target: TargetApp
    inputs: dict[str, InputParam] = Field(default_factory=dict)
    outputs: dict[str, OutputParam] = Field(default_factory=dict)
    secrets: list[str] = Field(default_factory=list)
    steps: list[Step]
    conditions: dict[str, Condition] = Field(default_factory=dict)
    checkpoint: Checkpoint
    provenance: Provenance

    # ---- integrity ---------------------------------------------------------
    @model_validator(mode="after")
    def _integrity(self) -> "Capability":
        ids = [s.id for s in self.steps]
        dupes = {i for i in ids if ids.count(i) > 1}
        if dupes:
            raise ValueError(f"duplicate step ids: {sorted(dupes)}")
        if not self.steps:
            raise ValueError("a capability needs at least one step")

        for key, cond in self.conditions.items():
            if cond.id != key:
                raise ValueError(f"condition key '{key}' does not match its id '{cond.id}'")

        all_steps = list(self.steps)
        for cond in self.conditions.values():
            if isinstance(cond.handler, SubflowHandler):
                all_steps.extend(cond.handler.steps)

        for step in all_steps:
            for scope, name in step.placeholders():
                if scope == "inputs" and name not in self.inputs:
                    raise ValueError(f"step '{step.id}' references undeclared input '{name}'")
                if scope == "secrets" and name not in self.secrets:
                    raise ValueError(f"step '{step.id}' references undeclared secret '{name}'")
            for cid in step.on_conditions:
                if cid not in self.conditions:
                    raise ValueError(f"step '{step.id}' references unknown condition '{cid}'")

        by_id = {s.id: s for s in self.steps}
        for name, out in self.outputs.items():
            step = by_id.get(out.from_step)
            if step is None or step.action is not ActionKind.EXTRACT or step.output != name:
                raise ValueError(f"output '{name}' must come from an extract step that produces it")
        for step in self.steps:
            if step.action is ActionKind.EXTRACT and step.output not in self.outputs:
                raise ValueError(f"step '{step.id}' extracts undeclared output '{step.output}'")
        return self

    # ---- invocation contract ---------------------------------------------------
    def bind_inputs(self, provided: dict[str, Any]) -> dict[str, Any]:
        unknown = set(provided) - set(self.inputs)
        if unknown:
            raise InputError(sorted(unknown)[0], "not a declared input")
        return {name: spec.coerce(name, provided.get(name)) for name, spec in self.inputs.items()}

    def risky_steps(self) -> list[Step]:
        return [s for s in self.steps if s.risk is RiskClass.RISKY]

    @property
    def filename(self) -> str:
        return f"{self.id}.v{self.version}.json"

    # ---- human review ----------------------------------------------------------
    def describe(self) -> str:
        lines = [
            f"{self.name}  [{self.id} v{self.version}, {self.status.value}]",
            f"  {self.description}",
            f"  target: {self.target.app} ({self.target.variant}) @ {self.target.entry_url}",
            "  inputs:",
        ]
        for n, p in self.inputs.items():
            req = "required" if p.required else "optional"
            lines.append(f"    - {n}: {p.type.value} ({req}){' pattern=' + p.pattern if p.pattern else ''}  {p.description}")
        if not self.inputs:
            lines.append("    (none)")
        lines.append("  outputs:")
        for n, o in self.outputs.items():
            lines.append(f"    - {n}: {o.type.value} from step {o.from_step}  {o.description}")
        if not self.outputs:
            lines.append("    (none)")
        if self.secrets:
            lines.append(f"  secrets (supplied at replay, never stored): {', '.join(self.secrets)}")
        lines.append("  steps:")
        for s in self.steps:
            tgt = f" -> {s.target.description}" if s.target else ""
            val = f" = {s.value}" if s.value is not None else ""
            opt = f" = {s.option}" if s.option is not None else ""
            url = f" {s.url}" if s.url else ""
            risk = "  [RISKY]" if s.risk is RiskClass.RISKY else ""
            lines.append(f"    {s.id}: {s.action.value}{url}{tgt}{val}{opt}{risk}")
            if s.expect:
                lines.append(f"        expect: {s.expect.description or s.expect.detect.kind}")
            if s.on_conditions:
                lines.append(f"        checks: {', '.join(s.on_conditions)}")
        lines.append("  conditions:")
        for c in self.conditions.values():
            extra = f" -> {c.code}" if c.code else f" -> handler:{c.handler.kind}"
            lines.append(f"    - {c.id} [{c.classification.value}]{extra}  {c.description}")
        lines.append(f"  checkpoint: {self.checkpoint.description or self.checkpoint.detect.kind}")
        lines.append(f"  provenance: {self.provenance.provider}/{self.provenance.model}, run {self.provenance.discovery_run_id}")
        return "\n".join(lines)
