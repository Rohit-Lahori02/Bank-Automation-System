"""Recorder: turn a successful discovery run into a capability artifact.

What the model did is a transcript. What gets saved is a contract:

  * literal input values the model typed become {{inputs.name}} templates
    (URLs too, so /members/12345 -> /members/{{inputs.member_id}})
  * each acted-on element becomes a Target with a ranked strategy chain
  * the model's own "expect" hints, when they held, become step postconditions
  * dialog dismissals the model performed become recoverable conditions rather
    than flow steps, so replay handles the dialog whenever it appears
  * the sign-on steps become the re-login sub-flow for session expiry
  * the app profile contributes the shared failure vocabulary
  * the final screen becomes the checkpoint
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from cua.agent.loop import ActionRecord, DiscoveryRun
from cua.policy.engine import PolicyEngine
from cua.policy.redaction import Redactor
from cua.surface.locators import Target, build_target
from cua.surface.snapshot import Snapshot

from .profiles import conditions_for
from .templating import STRATEGY_TEXT_FIELDS
from .schema import (
    ActionKind, AllOf, AnyOf, Capability, Checkpoint, ClickHandler, Condition, ConditionClass, DialogPresent,
    Expectation, InputParam, OutputParam, ParamType, Provenance, RiskClass, Step, TargetApp, TextVisible, UrlMatches,
)

MONEY_RE = re.compile(r"^-?\$?\s?[\d,]+\.\d{2}$")
INT_RE = re.compile(r"^-?\d+$")
FLOW_ACTIONS = {"click", "type", "select", "press", "navigate", "extract"}


@dataclass
class RecorderSpec:
    capability_id: str
    name: str
    description: str
    app: str
    variant: str = "base"
    input_specs: dict[str, InputParam] = field(default_factory=dict)
    output_types: dict[str, ParamType] = field(default_factory=dict)
    include_profile_conditions: bool = True


class RecordingError(ValueError):
    pass


def record_capability(run: DiscoveryRun, spec: RecorderSpec, *, policy: PolicyEngine,
                      redactor: Redactor | None = None) -> Capability:
    if run.status != "success":
        raise RecordingError(f"only successful runs are recorded (status={run.status})")
    redactor = redactor or Redactor()
    actions = [a for a in run.executed_actions if a.action in FLOW_ACTIONS]
    if not actions:
        raise RecordingError("the run executed no recordable actions")

    steps: list[Step] = [_entry_step(run, actions[0].snapshot)]   # the loop opened the entry url itself
    learned: dict[str, Condition] = {}
    # the screen after an action is the snapshot taken before the next decision
    next_snapshots = {a.index: nxt.snapshot for a, nxt in zip(run.actions, run.actions[1:])}
    for a in actions:
        if a.action == "click" and a.element is not None and a.element.in_dialog:
            cond = _dialog_condition(a)
            learned[cond.id] = cond
            continue
        steps.append(_step(a, run, policy, redactor, len(steps), next_snapshots.get(a.index)))   # s00 is the entry

    if not steps:
        raise RecordingError("no flow steps left after extracting conditions")

    login_steps = _login_subflow(steps, actions)
    conditions = conditions_for(spec.app, login_steps) if spec.include_profile_conditions else {}
    conditions.update(learned)

    inputs = {name: spec.input_specs.get(name) or InputParam(
        type=ParamType.STRING, example=value, description=f"Value of {name} supplied by the caller")
        for name, value in run.inputs.items()}
    outputs = {}
    for s in steps:
        if s.action is ActionKind.EXTRACT:
            sample = run.outputs.get(s.output or "", "")
            outputs[s.output] = OutputParam(type=spec.output_types.get(s.output) or _infer_type(sample),
                                            from_step=s.id, description=s.description)

    final_snapshot = next((a.snapshot for a in reversed(run.actions) if a.action == "done"), None)
    checkpoint, checkpoint_note = _checkpoint(run, final_snapshot)
    provenance = Provenance(
        discovery_run_id=run.run_id, provider=run.provider, model=run.model,
        goal=redactor.redact_text(run.goal), discovery_steps=len(run.actions),
        notes=f"{len(actions)} executed actions; {len(learned)} condition(s) learned from the run; "
              f"{run.llm_calls} model calls, {run.input_tokens + run.output_tokens} tokens"
              + (f"; {checkpoint_note}" if checkpoint_note else ""),
    )
    return Capability(
        id=spec.capability_id, name=spec.name, description=spec.description,
        target=TargetApp(app=spec.app, entry_url=run.entry_url, variant=spec.variant),
        inputs=inputs, outputs=outputs, secrets=list(run.secret_names), steps=steps,
        conditions=conditions, checkpoint=checkpoint, provenance=provenance,
    )


# ----------------------------------------------------------------- pieces
def _entry_step(run: DiscoveryRun, first_snapshot: Snapshot) -> Step:
    """Open the entry URL; expect something distinctive from the first screen the agent saw."""
    landmark = next((e.name for e in first_snapshot.elements if e.role == "heading" and 4 <= len(e.name) <= 40), None)
    if landmark is None:
        landmark = next((t for t in first_snapshot.texts() if 4 <= len(t) <= 40 and not _contains_input(t, run.inputs)),
                        None)
    expect = Expectation(description=f'entry screen shows "{landmark}"', detect=TextVisible(text=landmark)) \
        if landmark else None
    return Step(id="s00_navigate", action=ActionKind.NAVIGATE, url=_parameterize(run.entry_url, run.inputs),
                description="Open the application entry point", expect=expect)


def _step(a: ActionRecord, run: DiscoveryRun, policy: PolicyEngine, redactor: Redactor, n: int,
          next_snapshot: Snapshot | None) -> Step:
    d = a.decision
    step_id = f"s{n:02d}_{d.action}"
    description = redactor.redact_text(_first_sentence(d.reasoning))
    target = _parameterize_target(build_target(a.element, a.snapshot), run.inputs) if a.element is not None else None
    risk = policy.classify(a.element.name if a.element else None)
    expect = _expectation(a, run, next_snapshot)
    common = dict(id=step_id, description=description, target=target, risk=risk,
                  irreversible=risk is RiskClass.RISKY, expect=expect)
    if d.action == "navigate":
        return Step(action=ActionKind.NAVIGATE, url=_parameterize(d.url or "", run.inputs), **common)
    if d.action == "type":
        return Step(action=ActionKind.TYPE, value=_parameterize(d.value or "", run.inputs), **common)
    if d.action == "select":
        return Step(action=ActionKind.SELECT, option=_parameterize(d.option or "", run.inputs), **common)
    if d.action == "press":
        return Step(action=ActionKind.PRESS, key=d.key, **common)
    if d.action == "extract":
        return Step(action=ActionKind.EXTRACT, output=d.output, **common)
    return Step(action=ActionKind.CLICK, **common)


def _expectation(a: ActionRecord, run: DiscoveryRun, next_snapshot: Snapshot | None) -> Expectation | None:
    if a.decision.expect and a.expect_held and not _contains_input(a.decision.expect, run.inputs):
        return Expectation(description=f'"{a.decision.expect}" visible', detect=TextVisible(text=a.decision.expect))
    if a.url_after and a.url_after != a.url_before:
        return Expectation(description="navigated", detect=UrlMatches(pattern=_url_pattern(a.url_after, run.inputs)))
    if a.action in {"click", "press", "select"} and next_snapshot is not None and a.digest_after != a.digest_before:
        # same URL, changed screen (a server-rendered form post): key on text that newly appeared
        before = set(a.snapshot.texts())
        for text in next_snapshot.texts():
            if text in before or not (4 <= len(text) <= 60) or _contains_input(text, run.inputs):
                continue
            return Expectation(description=f'"{text}" appears', detect=TextVisible(text=text))
    return None


def _checkpoint(run: DiscoveryRun, final_snapshot: Snapshot | None) -> tuple[Checkpoint, str]:
    """Checkpoint = final URL pattern + the model's checkpoint text, but only if that text is
    really on the final screen and does not embed an input value (else it would never
    generalize). A model often writes a sentence here; a sentence is not a checkpoint."""
    detectors = []
    note = ""
    if run.final_url:
        detectors.append(UrlMatches(pattern=_url_pattern(run.final_url, run.inputs)))
    text = (run.checkpoint_text or "").strip()
    if text:
        visible = final_snapshot is not None and final_snapshot.text_visible(text)
        if visible and not _contains_input(text, run.inputs):
            detectors.append(TextVisible(text=text))
        else:
            note = "model's checkpoint text was not verifiable on the final screen and was dropped"
            if final_snapshot is not None:
                fragment = _best_fragment(text, final_snapshot, run.inputs)
                if fragment:
                    detectors.append(TextVisible(text=fragment))
                    note = f'model\'s checkpoint text reduced to the visible fragment "{fragment}"'
    if not detectors:
        raise RecordingError("no verifiable checkpoint: neither a final URL nor visible checkpoint text")
    detect = detectors[0] if len(detectors) == 1 else AllOf(detectors=detectors)
    desc = text or "final screen reached"
    return Checkpoint(description=f'goal state: "{desc}"', detect=detect), note


def _best_fragment(sentence: str, snapshot: Snapshot, inputs: dict[str, str]) -> str | None:
    """The visible text sharing the most words with the model's sentence (earliest on screen wins ties)."""
    words = {w for w in re.findall(r"[a-z]{4,}", sentence.casefold())}
    best, best_score = None, 0
    for text in snapshot.texts():
        if not (4 <= len(text) <= 40) or _contains_input(text, inputs):
            continue
        score = len(words & set(re.findall(r"[a-z]{4,}", text.casefold())))
        if score > best_score:
            best, best_score = text, score
    return best


def _parameterize_target(target: Target, inputs: dict[str, str]) -> Target:
    """Replace literal input values inside locator strategies (row anchors, hrefs, names)."""
    for strategy in target.strategies:
        for field in STRATEGY_TEXT_FIELDS:
            value = getattr(strategy, field, None)
            if isinstance(value, str) and value:
                setattr(strategy, field, _parameterize(value, inputs))
    return target


def _contains_input(text: str, inputs: dict[str, str]) -> bool:
    return any(len(str(v)) >= 2 and str(v) in text for v in inputs.values())


def _dialog_condition(a: ActionRecord) -> Condition:
    name = next((d["name"] for d in a.snapshot.dialogs), "dialog")
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:40] or "dialog"
    return Condition(
        id=f"dialog_{slug}", classification=ConditionClass.RECOVERABLE,
        description=f'A "{name}" dialog appeared during discovery; the agent dismissed it. Replay does the same.',
        detect=DialogPresent(name_contains=name[:40]),
        handler=ClickHandler(description=f'Press "{a.element.name}"', target=build_target(a.element, a.snapshot)),
    )


def _login_subflow(steps: list[Step], actions: list[ActionRecord]) -> list[Step]:
    """Steps from the first secret-typing step through the click that leaves the sign-on page."""
    start = next((i for i, s in enumerate(steps) if s.value and "{{secrets." in s.value), None)
    if start is None:
        return []
    for j in range(start, len(steps)):
        s = steps[j]
        if s.action is ActionKind.CLICK and s.expect is not None:
            return steps[start:j + 1]
    return []


# ----------------------------------------------------------------- utils
def _parameterize(text: str, inputs: dict[str, str]) -> str:
    """Replace literal input values with {{inputs.name}} (longest values first)."""
    if not text:
        return text
    for name, value in sorted(inputs.items(), key=lambda kv: -len(str(kv[1]))):
        value = str(value)
        if len(value) >= 2 and value in text:
            text = text.replace(value, f"{{{{inputs.{name}}}}}")
    return text


def _url_pattern(url: str, inputs: dict[str, str]) -> str:
    """Regex matching the URL's path with input values generalized."""
    path = urlsplit(url).path or "/"
    pattern = re.escape(path)
    for value in sorted((str(v) for v in inputs.values()), key=len, reverse=True):
        if len(value) >= 2:
            pattern = pattern.replace(re.escape(value), r"[^/]+")
    return pattern + "$"


def _infer_type(sample: str | None) -> ParamType:
    sample = (sample or "").strip()
    if MONEY_RE.match(sample):
        return ParamType.MONEY
    if INT_RE.match(sample):
        return ParamType.INTEGER
    return ParamType.STRING


def _first_sentence(text: str, limit: int = 160) -> str:
    text = (text or "").strip()
    cut = re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0]
    return cut[:limit]
