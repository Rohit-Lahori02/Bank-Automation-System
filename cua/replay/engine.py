"""Deterministic replay: run a capability without a model in the loop.

Per step:
  1. policy gate (allowlist + risk)                -> denied => failed / risky => escalate
  2. scan conditions on the current screen         -> outcome / hard failure / recover
  3. resolve the target through its strategy chain -> record which strategy hit
  4. act
  5. wait for the step's expectation, scanning conditions while waiting
     - expectation held                             -> next step
     - a condition fired                            -> outcome / hard failure / recover, then re-act
     - timed out                                    -> bounded retry if the step is safe, else fail
Then verify the checkpoint and return the declared outputs.

Nothing here guesses. Every branch is either declared in the artifact or a
hard, explicit failure with expected-vs-observed and a screenshot.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from cua.artifact.schema import (
    ActionKind, Capability, ClickHandler, Condition, ConditionClass, EscalateHandler, InputError, RetryHandler,
    RiskClass, Step, SubflowHandler, parse_output,
)
from cua.artifact.templating import render_target, render_template
from cua.evidence.logger import RunLogger
from cua.policy.engine import PolicyEngine
from cua.policy.redaction import Redactor
from cua.surface.base import Resolved, Surface, TargetNotFound
from cua.surface.locators import Target

from .conditions import describe, detect, fired_conditions, observed
from .result import Escalation, Failure, ReplayResult, ReplayStatus, StepReport


class _Stop(Exception):
    """Internal: carries a terminal result out of the step loop."""

    def __init__(self, result: ReplayResult) -> None:
        self.result = result


@dataclass
class ReplayConfig:
    poll_ms: int = 250
    default_expect_timeout_ms: int = 8000
    checkpoint_timeout_ms: int = 8000
    max_recoveries_per_step: int = 3
    subflow_depth: int = 1
    escalate_on_failure: bool = True   # hand unrecoverable state failures to a human when a handler exists
    before_step: Callable[[str], None] | None = None   # test/demo hook: inject faults before a step


# A handler returns "approved" (automation performs the step), "resumed" (the human did the
# work; continue), "denied", or a dict {"decision": ..., "intervention_id": ..., "human_actions": n}.
EscalationHandler = Callable[[Escalation, "ReplayEngine"], str | dict | None]


class ReplayEngine:
    def __init__(
        self,
        *,
        surface: Surface,
        policy: PolicyEngine,
        redactor: Redactor,
        evidence_root: Path,
        config: ReplayConfig | None = None,
        escalation_handler: EscalationHandler | None = None,
    ) -> None:
        self.surface = surface
        self.policy = policy
        self.redactor = redactor
        self.evidence_root = Path(evidence_root)
        self.cfg = config or ReplayConfig()
        self.escalation_handler = escalation_handler
        self._log: RunLogger | None = None
        self._cap: Capability | None = None
        self._inputs: dict = {}
        self._secrets: dict = {}
        self._reports: list[StepReport] = []
        self._current: StepReport | None = None
        self._interventions: list[dict] = []
        self._suppressed: set[str] = set()      # conditions currently being recovered (not re-detected)
        self._raw_outputs: dict[str, str] = {}
        self._started = 0.0
        self._run_id = ""

    # ------------------------------------------------------------------ api
    def replay(self, capability: Capability, inputs: dict, secrets: dict[str, str], *,
               run_id: str | None = None) -> ReplayResult:
        self._run_id = run_id or time.strftime("%Y%m%dT%H%M%S") + "-replay"
        self._cap, self._secrets = capability, dict(secrets)
        self._reports, self._raw_outputs, self._current, self._suppressed = [], {}, None, set()
        self._interventions = []
        self._started = time.perf_counter()
        for value in secrets.values():
            self.redactor.add_secret(value)
        self._log = RunLogger(self.evidence_root / self._run_id, self.redactor)
        self._log.event("replay_start", capability=capability.id, version=capability.version, inputs=inputs)

        try:
            self._inputs = capability.bind_inputs(inputs)
        except InputError as exc:
            return self._finish(self._failed(None, "INVALID_INPUT", str(exc), expected="inputs matching the contract",
                                             observed=str(inputs)))
        missing = [s for s in capability.secrets if s not in secrets]
        if missing:
            return self._finish(self._failed(None, "MISSING_SECRET", f"secrets not supplied: {missing}"))

        try:
            if capability.steps[0].action is not ActionKind.NAVIGATE:
                # artifacts should start with a navigate; tolerate ones that assume the entry page is open
                verdict = self.policy.check_navigation(capability.target.entry_url)
                if not verdict.allowed:
                    raise _Stop(self._failed(None, "POLICY_DENIED", verdict.reason))
                self.surface.navigate(capability.target.entry_url)
                self._log.event("entry", url=capability.target.entry_url, note="implicit navigate to entry_url")
            for step in capability.steps:
                self._run_step(step, depth=0)
            self._verify_checkpoint()
        except _Stop as stop:
            return self._finish(stop.result)
        except Exception as exc:  # anything unplanned is a hard failure with evidence, never a crash
            step_id = self._current.step_id if self._current else None
            return self._finish(self._failed(step_id, "UNEXPECTED_ERROR",
                                             f"{type(exc).__name__}: {str(exc).splitlines()[0][:300]}",
                                             observed=self._observed()))

        outputs = {}
        for name, spec in capability.outputs.items():
            raw = self._raw_outputs.get(name)
            outputs[name] = parse_output(spec, raw) if raw is not None else None
        self._log.screenshot(self.surface, "final")
        return self._finish(ReplayResult(status=ReplayStatus.SUCCESS, capability_id=capability.id,
                                         capability_version=capability.version, run_id=self._run_id,
                                         inputs=self._inputs, outputs=outputs, checkpoint_verified=True))

    # ------------------------------------------------------------- steps
    def _run_step(self, step: Step, *, depth: int) -> None:
        outer = self._current
        report = StepReport(step_id=step.id, action=step.action.value,
                            subflow_of=next(iter(self._suppressed), "subflow") if depth > 0 else None)
        self._current = report
        started = time.perf_counter()
        try:
            if self.cfg.before_step:
                self.cfg.before_step(step.id)
            if step.wait_before_ms:
                time.sleep(step.wait_before_ms / 1000)
            self._log.event("step_start", step=step.id, action=step.action.value,
                            target=step.target.description if step.target else None, url=self.surface.url)

            human_did_it = self._policy_gate(step, report)
            self._scan_conditions(step, report, depth, phase="before")

            if human_did_it:
                self._after_human(step, report, depth, decision="resumed")
            else:
                self._act_with_retries(step, report, depth)

            self._scan_conditions(step, report, depth, phase="after")
            report.status = report.status or "ok"
            report.duration_ms = int((time.perf_counter() - started) * 1000)
            self._reports.append(report)
            self._log.event("step_end", step=step.id, status=report.status, strategy=report.strategy,
                            attempts=report.attempts, conditions=report.conditions, duration_ms=report.duration_ms)
        finally:
            self._current = outer

    def _act_with_retries(self, step: Step, report: StepReport, depth: int) -> None:
        retry = self._retry_policy()
        attempts = retry.max_attempts if (retry and step.risk is RiskClass.SAFE) else 1
        for attempt in range(1, attempts + 1):
            report.attempts = attempt
            try:
                self._act(step, report)
            except TargetNotFound as exc:
                self._log.event("resolve_failed", step=step.id, attempt=attempt, attempts=exc.attempts)
                if self._recover_if_possible(step, report, depth):
                    continue   # the screen was in a known bad state; it is handled, try again
                if attempt < attempts:
                    time.sleep(retry.backoff_ms / 1000)
                    continue
                self._unrecoverable(step, report, depth, "TARGET_NOT_FOUND",
                                    f"could not locate {step.target.description}",
                                    expected=step.target.description, attempts=exc.attempts)
                return
            except Exception as exc:
                error = f"{type(exc).__name__}: {str(exc).splitlines()[0][:200]}"
                self._log.event("action_error", step=step.id, attempt=attempt, error=error)
                if attempt < attempts:
                    time.sleep(retry.backoff_ms / 1000)
                    continue
                self._unrecoverable(step, report, depth, "ACTION_FAILED", error)
                return

            if self._await_expectation(step, report, depth):
                return
            if attempt < attempts:
                self._log.event("retry", step=step.id, attempt=attempt, reason="expectation timeout")
                report.conditions.append("slow_or_failed_load")
                time.sleep(retry.backoff_ms / 1000)
                continue
            self._unrecoverable(step, report, depth, "EXPECTATION_TIMEOUT",
                                f"step '{step.id}' did not reach its expected state",
                                expected=describe(step.expect.detect))
            return

    def _unrecoverable(self, step: Step, report: StepReport, depth: int, code: str, message: str, *,
                       expected: str = "", attempts: list[str] | None = None) -> None:
        """A state failure the artifact has no answer for: hand it to a human if we can, else fail."""
        if not (self.escalation_handler and self.cfg.escalate_on_failure):
            raise _Stop(self._failed(step.id, code, message, expected=expected, observed=self._observed(),
                                     attempts=attempts or []))
        self._log.screenshot(self.surface, f"failure_{step.id}")
        decision = self._escalate(step, report, kind="unrecoverable", reason=f"[{code}] {message}")
        self._after_human(step, report, depth, decision=decision)

    def _after_human(self, step: Step, report: StepReport, depth: int, *, decision: str) -> None:
        """Continue after a handoff: trust the screen, not the human's word."""
        must_act = step.action is ActionKind.EXTRACT or decision == "approved" or not self._expectation_holds(step)
        if must_act:
            if step.risk is not RiskClass.SAFE and decision != "approved":
                raise _Stop(self._failed(step.id, "HANDOFF_STATE_MISMATCH",
                                         "the human resumed but the risky step's expected state is not on screen",
                                         expected=describe(step.expect.detect) if step.expect else "",
                                         observed=self._observed()))
            self._act(step, report)
            if not self._await_expectation(step, report, depth):
                raise _Stop(self._failed(step.id, "HANDOFF_STATE_MISMATCH",
                                         "after the handoff the step still did not reach its expected state",
                                         expected=describe(step.expect.detect) if step.expect else "",
                                         observed=self._observed()))
        report.status = "recovered"
        report.note = f"human {decision}" + ("" if must_act else " (step performed manually)")

    def _act(self, step: Step, report: StepReport) -> None:
        s = self.surface
        if step.action is ActionKind.NAVIGATE:
            s.navigate(render_template(step.url, self._inputs, self._secrets) or "")
            return
        if step.action is ActionKind.PRESS:
            s.press(step.key or "")
            return
        if step.action is ActionKind.WAIT:
            time.sleep((step.wait_before_ms or 1000) / 1000)
            return
        resolved: Resolved = s.resolve(render_target(step.target, self._inputs, self._secrets))
        report.strategy = f"{resolved.strategy_kind}#{resolved.strategy_index}"
        self._log.event("resolved", step=step.id, strategy=report.strategy, frame=resolved.frame)
        if step.action is ActionKind.CLICK:
            s.click(resolved)
        elif step.action is ActionKind.TYPE:
            s.type(resolved, render_template(step.value, self._inputs, self._secrets) or "")
        elif step.action is ActionKind.SELECT:
            s.select(resolved, render_template(step.option, self._inputs, self._secrets) or "")
        elif step.action is ActionKind.EXTRACT:
            text = s.read_text(resolved)
            self._raw_outputs[step.output] = text
            self._log.event("extracted", step=step.id, output=step.output, value=text)

    # ------------------------------------------------------------ policy
    def _policy_gate(self, step: Step, report: StepReport) -> bool:
        """Returns True when a human performed the step during a risky-action handoff."""
        if step.action is ActionKind.NAVIGATE:
            url = render_template(step.url, self._inputs, self._secrets) or ""
            verdict = self.policy.check_navigation(url)
        else:
            verdict = self.policy.check_action(step.action.value, url=self.surface.url,
                                               target_name=_target_name(step.target),
                                               irreversible=step.irreversible)
        self._log.event("policy", step=step.id, verdict=verdict.model_dump())
        if not verdict.allowed:
            raise _Stop(self._failed(step.id, "POLICY_DENIED", verdict.reason))
        if verdict.needs_human:
            decision = self._escalate(step, report, kind="risky_action",
                                      reason=f"step '{step.id}' ({step.target.description if step.target else step.action.value}) "
                                             f"is irreversible: {verdict.reason}")
            if decision == "approved":
                report.note = "human approved"
                return False
            return True
        if verdict.requires == "flag":
            report.note = "risky action flagged"
        return False

    def _escalate(self, step: Step | None, report: StepReport | None, *, kind: str, reason: str) -> str:
        """Hand the live session to a human. Returns 'approved' or 'resumed'; anything else ends the run."""
        shot = self._log.screenshot(self.surface, f"escalation_{step.id if step else 'run'}")
        snap = self.surface.snapshot()
        esc = Escalation(step_id=step.id if step else None, reason=reason, kind=kind, url=self.surface.url,
                         screenshot=str(shot) if shot else None, screen=snap.render(max_elements=80))
        self._log.event("escalation", step=esc.step_id, escalation_kind=kind, reason=reason)
        raw = self.escalation_handler(esc, self) if self.escalation_handler else None
        details = raw if isinstance(raw, dict) else {"decision": raw}
        decision = details.get("decision") or "denied"
        record = {"step_id": esc.step_id, "kind": kind, "reason": reason, "decision": decision,
                  **{k: v for k, v in details.items() if k != "decision"}}
        self._interventions.append(record)
        self._log.event("escalation_decision", step=esc.step_id, **record)
        if decision in ("approved", "resumed"):
            return decision
        result = ReplayResult(status=ReplayStatus.ESCALATED, capability_id=self._cap.id,
                              capability_version=self._cap.version, run_id=self._run_id, inputs=self._inputs,
                              escalation=esc, steps=list(self._reports))
        raise _Stop(result)

    # --------------------------------------------------------- conditions
    def _retry_policy(self) -> RetryHandler | None:
        for c in self._cap.conditions.values():
            if c.classification is ConditionClass.RECOVERABLE and isinstance(c.handler, RetryHandler):
                return c.handler
        return None

    def _fired(self, snap, url) -> list[Condition]:
        return [c for c in fired_conditions(self._cap, snap, url) if c.id not in self._suppressed]

    def _classify_or_recover(self, cond: Condition, step: Step, report: StepReport, depth: int, snap, *,
                             phase: str) -> None:
        """Terminal conditions raise _Stop; recoverable ones are handled in place."""
        report.conditions.append(cond.id)
        self._log.event("condition", step=step.id, phase=phase, condition=cond.id,
                        classification=cond.classification.value)
        if cond.classification is ConditionClass.BUSINESS_OUTCOME:
            raise _Stop(self._outcome(step, cond))
        if cond.classification is ConditionClass.HARD_FAILURE:
            raise _Stop(self._failed(step.id, cond.code, cond.description,
                                     expected=describe(step.expect.detect) if step.expect else "",
                                     observed=observed(snap)))
        self._handle_recoverable(cond, step, depth)
        report.status = "recovered"

    def _scan_conditions(self, step: Step, report: StepReport, depth: int, *, phase: str) -> None:
        for _ in range(self.cfg.max_recoveries_per_step):
            snap = self.surface.snapshot()
            fired = self._fired(snap, self.surface.url)
            if not fired:
                return
            self._classify_or_recover(_most_severe(fired), step, report, depth, snap, phase=phase)
        raise _Stop(self._failed(step.id, "RECOVERY_EXHAUSTED", "a recoverable condition kept recurring",
                                 observed=self._observed()))

    def _recover_if_possible(self, step: Step, report: StepReport, depth: int) -> bool:
        snap = self.surface.snapshot()
        fired = self._fired(snap, self.surface.url)
        if not fired:
            return False
        self._classify_or_recover(_most_severe(fired), step, report, depth, snap, phase="during")
        return True

    def _handle_recoverable(self, cond: Condition, step: Step, depth: int) -> None:
        handler = cond.handler
        self._log.event("recover", step=step.id, condition=cond.id, handler=handler.kind)
        if isinstance(handler, ClickHandler):
            self.surface.click(self.surface.resolve(handler.target))
        elif isinstance(handler, RetryHandler):
            time.sleep(handler.backoff_ms / 1000)
        elif isinstance(handler, SubflowHandler):
            if depth >= self.cfg.subflow_depth:
                raise _Stop(self._failed(step.id, "RECOVERY_LOOP", f"'{cond.id}' recurred inside its own recovery",
                                         observed=self._observed()))
            self._suppressed.add(cond.id)
            try:
                for sub in handler.steps:
                    self._run_step(sub, depth=depth + 1)
            finally:
                self._suppressed.discard(cond.id)
        elif isinstance(handler, EscalateHandler):
            self._escalate(step, None, kind="unrecoverable", reason=handler.reason)

    def _await_expectation(self, step: Step, report: StepReport, depth: int) -> bool:
        """True when the expectation held (possibly after recovery), False on timeout.

        Outcomes and hard failures raise _Stop. After a recovery the step is re-executed
        (a weak expectation must not be trusted to have been reached by the recovery alone).
        """
        if step.expect is None:
            return True
        timeout = step.expect.timeout_ms or self.cfg.default_expect_timeout_ms
        deadline = time.time() + timeout / 1000
        recoveries = 0
        while True:
            snap = self.surface.snapshot()
            url = self.surface.url
            if detect(step.expect.detect, snap, url):
                return True
            fired = self._fired(snap, url)
            if fired:
                recoveries += 1
                if recoveries > self.cfg.max_recoveries_per_step:
                    raise _Stop(self._failed(step.id, "RECOVERY_EXHAUSTED", f"'{fired[0].id}' kept recurring",
                                             observed=observed(snap)))
                cond = _most_severe(fired)
                self._classify_or_recover(cond, step, report, depth, snap, phase="await")
                if _step_in_subflow(step, cond) and self._expectation_holds(step):
                    return True   # the recovery replayed this very step (e.g. expiry during sign-on)
                if _re_act_after(cond):
                    if step.risk is not RiskClass.SAFE:
                        raise _Stop(self._failed(step.id, "RECOVERY_NEEDS_REPLAY",
                                                 f"'{cond.id}' was recovered but re-running a risky step is not allowed",
                                                 observed=self._observed()))
                    self._act(step, report)
                deadline = time.time() + timeout / 1000
                continue
            if time.time() >= deadline:
                return False
            time.sleep(self.cfg.poll_ms / 1000)

    def _expectation_holds(self, step: Step) -> bool:
        return step.expect is None or detect(step.expect.detect, self.surface.snapshot(), self.surface.url)

    def _verify_checkpoint(self, *, allow_handoff: bool = True) -> None:
        deadline = time.time() + self.cfg.checkpoint_timeout_ms / 1000
        while True:
            snap = self.surface.snapshot()
            if detect(self._cap.checkpoint.detect, snap, self.surface.url):
                self._log.event("checkpoint", verified=True)
                return
            if time.time() >= deadline:
                expected = describe(self._cap.checkpoint.detect)
                if allow_handoff and self.escalation_handler and self.cfg.escalate_on_failure:
                    self._escalate(None, None, kind="unrecoverable",
                                   reason=f"[CHECKPOINT_FAILED] final checkpoint not satisfied; expected {expected}")
                    return self._verify_checkpoint(allow_handoff=False)
                raise _Stop(self._failed(None, "CHECKPOINT_FAILED", "final checkpoint not satisfied",
                                         expected=expected, observed=observed(snap)))
            time.sleep(self.cfg.poll_ms / 1000)

    # ------------------------------------------------------------ results
    def _outcome(self, step: Step, cond: Condition) -> ReplayResult:
        self._log.screenshot(self.surface, "outcome")
        self._log.event("outcome", step=step.id, code=cond.code, condition=cond.id)
        rep = self._current or StepReport(step_id=step.id, action=step.action.value)
        rep.status, rep.note = "outcome", cond.description
        return ReplayResult(status=ReplayStatus.BUSINESS_OUTCOME, capability_id=self._cap.id,
                            capability_version=self._cap.version, run_id=self._run_id, inputs=self._inputs,
                            outcome_code=cond.code, outcome_message=cond.description,
                            steps=[*self._reports, rep])

    def _failed(self, step_id: str | None, code: str, message: str, *, expected: str = "", observed: str = "",
                attempts: list[str] | None = None) -> ReplayResult:
        shot = self._log.screenshot(self.surface, f"failure_{step_id or 'run'}") if self._log else None
        failure = Failure(step_id=step_id, code=code, message=message, expected=expected, observed=observed,
                          screenshot=str(shot) if shot else None, attempts=attempts or [])
        if self._log:
            self._log.event("failure", **failure.model_dump())
        steps = list(self._reports)
        if step_id and self._current and self._current.step_id == step_id:
            self._current.status, self._current.note = "failed", message
            steps.append(self._current)
        elif step_id:
            steps.append(StepReport(step_id=step_id, action="?", status="failed", note=message))
        return ReplayResult(status=ReplayStatus.FAILED, capability_id=self._cap.id if self._cap else "",
                            capability_version=self._cap.version if self._cap else 0, run_id=self._run_id,
                            inputs=self._inputs, failure=failure, steps=steps)

    def _finish(self, result: ReplayResult) -> ReplayResult:
        if not result.steps and self._reports:
            result.steps = list(self._reports)
        result.interventions = list(self._interventions)
        result.variant = self._cap.target.variant if self._cap else "base"
        result.compute_drift()
        result.evidence_dir = str(self._log.run_dir) if self._log else ""
        result.duration_ms = int((time.perf_counter() - self._started) * 1000)
        if self._log:
            self._log.write_json("result.json", result.model_dump(mode="json"))
            self._log.event("replay_end", status=result.status.value, outcome_code=result.outcome_code,
                            failure_code=result.failure.code if result.failure else None)
        return result

    def _observed(self) -> str:
        try:
            return observed(self.surface.snapshot())
        except Exception:
            return f"url={getattr(self.surface, 'url', '?')}"


# ------------------------------------------------------------------ utils
_SEVERITY = {ConditionClass.HARD_FAILURE: 0, ConditionClass.BUSINESS_OUTCOME: 1, ConditionClass.RECOVERABLE: 2}


def _most_severe(conditions: list[Condition]) -> Condition:
    return sorted(conditions, key=lambda c: _SEVERITY[c.classification])[0]


def _re_act_after(cond: Condition) -> bool:
    """Whether the current step must be re-executed after this recovery.

    A dismissed dialog was only covering the page: the action already happened.
    A re-login sub-flow with then=retry_step moved us elsewhere: the step must be redone.
    """
    return isinstance(cond.handler, SubflowHandler) and cond.handler.then == "retry_step"


def _step_in_subflow(step: Step, cond: Condition) -> bool:
    return isinstance(cond.handler, SubflowHandler) and any(s.id == step.id for s in cond.handler.steps)


def _target_name(target: Target | None) -> str | None:
    if target is None:
        return None
    for s in target.strategies:
        name = getattr(s, "name", None) or getattr(s, "text", None)
        if name:
            return name
    return target.description
