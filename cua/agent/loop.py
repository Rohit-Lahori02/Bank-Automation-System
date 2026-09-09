"""The discovery loop: observe -> decide -> (policy) -> act, until done or stopped.

The model decides; this loop enforces. Every proposed action passes the policy
engine before it touches the surface, every step is logged with what the agent
saw and why it acted, and the full record of executed actions (with the
snapshot and element behind each one) is what the recorder turns into a
capability artifact afterwards.
"""

from __future__ import annotations

import json
import secrets as _secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from cua.artifact.templating import TemplateError, render_template
from cua.evidence.logger import RunLogger
from cua.llm.client import LLMClient, LLMError, extract_json
from cua.policy.engine import PolicyEngine, Verdict
from cua.policy.redaction import Redactor
from cua.surface.base import Surface
from cua.surface.snapshot import Element, Snapshot

from .prompts import SYSTEM_PROMPT, initial_user_message, step_result_message

ActionName = Literal["click", "type", "select", "press", "navigate", "extract", "wait", "done", "stuck"]


class Decision(BaseModel):
    model_config = ConfigDict(extra="ignore")

    reasoning: str = ""
    action: ActionName
    ref: str | None = None
    value: str | None = None
    option: str | None = None
    key: str | None = None
    url: str | None = None
    output: str | None = None
    expect: str | None = None
    outputs: dict[str, str] | None = None
    checkpoint: str | None = None
    reason: str | None = None

    @model_validator(mode="after")
    def _shape(self) -> "Decision":
        needs_ref = {"click", "type", "select", "extract"}
        if self.action in needs_ref and not self.ref:
            raise ValueError(f"'{self.action}' requires 'ref'")
        if self.ref is not None and not self.ref.startswith("e"):
            raise ValueError("'ref' must look like e12")
        if self.action == "type" and self.value is None:
            raise ValueError("'type' requires 'value'")
        if self.action == "select" and not self.option:
            raise ValueError("'select' requires 'option'")
        if self.action == "press" and not self.key:
            raise ValueError("'press' requires 'key'")
        if self.action == "navigate" and not self.url:
            raise ValueError("'navigate' requires 'url'")
        if self.action == "extract" and not self.output:
            raise ValueError("'extract' requires 'output'")
        if self.action == "stuck" and not self.reason:
            raise ValueError("'stuck' requires 'reason'")
        return self

    def compact(self) -> str:
        return json.dumps({k: v for k, v in self.model_dump().items() if v not in (None, "", {})}, ensure_ascii=False)


@dataclass
class ActionRecord:
    index: int
    decision: Decision
    snapshot: Snapshot
    screen_text: str
    element: Element | None
    url_before: str
    digest_before: str
    started: float = field(default_factory=time.time)
    verdict: Verdict | None = None
    executed: bool = False
    result: str = ""
    url_after: str | None = None
    digest_after: str | None = None
    expect_held: bool | None = None
    extracted: str | None = None
    duration_ms: int = 0
    usage: dict = field(default_factory=dict)

    @property
    def action(self) -> str:
        return self.decision.action


@dataclass
class DiscoveryRun:
    run_id: str
    goal: str
    entry_url: str
    inputs: dict[str, str]
    secret_names: list[str]
    evidence_dir: Path
    provider: str
    model: str
    status: str = "running"     # success | outcome | stuck | no_progress | policy_blocked | max_steps | failed
    actions: list[ActionRecord] = field(default_factory=list)
    outputs: dict[str, str] = field(default_factory=dict)
    checkpoint_text: str | None = None
    final_url: str | None = None
    stop_reason: str | None = None
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    started: float = field(default_factory=time.time)
    finished: float | None = None

    @property
    def executed_actions(self) -> list[ActionRecord]:
        return [a for a in self.actions if a.executed]

    def summary(self) -> dict:
        return {
            "run_id": self.run_id, "goal": self.goal, "entry_url": self.entry_url, "status": self.status,
            "stop_reason": self.stop_reason, "inputs": self.inputs, "secret_names": self.secret_names,
            "outputs": self.outputs, "checkpoint_text": self.checkpoint_text, "final_url": self.final_url,
            "provider": self.provider, "model": self.model, "llm_calls": self.llm_calls,
            "input_tokens": self.input_tokens, "output_tokens": self.output_tokens, "cached_tokens": self.cached_tokens,
            "steps_total": len(self.actions), "steps_executed": len(self.executed_actions),
            "duration_s": round((self.finished or time.time()) - self.started, 1),
            "actions": [
                {"index": a.index, "action": a.action, "target": a.element.name if a.element else None,
                 "role": a.element.role if a.element else None, "value": a.decision.value, "output": a.decision.output,
                 "executed": a.executed, "result": a.result, "expect": a.decision.expect, "expect_held": a.expect_held,
                 "url_before": a.url_before, "url_after": a.url_after, "duration_ms": a.duration_ms}
                for a in self.actions
            ],
        }


@dataclass
class DiscoveryConfig:
    max_steps: int = 25
    max_no_progress: int = 3
    max_consecutive_blocked: int = 3
    max_parse_retries: int = 2
    history_screens: int = 2          # how many past screens stay verbatim in the context
    max_elements: int = 220
    expect_timeout_ms: int = 4000
    wait_action_ms: int = 1500
    max_tokens: int = 1024


RiskyHandler = Callable[[ActionRecord, Verdict, "DiscoveryRun"], bool]
StuckHandler = Callable[["DiscoveryRun", ActionRecord | None], bool | str]   # truthy = human intervened, resume


class DiscoveryAgent:
    def __init__(
        self,
        *,
        llm: LLMClient,
        surface: Surface,
        policy: PolicyEngine,
        redactor: Redactor,
        evidence_root: Path,
        config: DiscoveryConfig | None = None,
        risky_handler: RiskyHandler | None = None,
        stuck_handler: StuckHandler | None = None,
    ) -> None:
        self.llm = llm
        self.surface = surface
        self.policy = policy
        self.redactor = redactor
        self.evidence_root = Path(evidence_root)
        self.cfg = config or DiscoveryConfig()
        self.risky_handler = risky_handler
        self.stuck_handler = stuck_handler

    # ------------------------------------------------------------------ run
    @staticmethod
    def new_run_id() -> str:
        return time.strftime("%Y%m%dT%H%M%S") + "-" + _secrets.token_hex(3)

    def run(self, goal: str, *, entry_url: str, inputs: dict[str, str], secrets: dict[str, str],
            run_id: str | None = None) -> DiscoveryRun:
        run_id = run_id or self.new_run_id()
        run = DiscoveryRun(run_id=run_id, goal=goal, entry_url=entry_url, inputs=dict(inputs),
                           secret_names=sorted(secrets), evidence_dir=self.evidence_root / run_id,
                           provider=self.llm.provider, model=self.llm.model)
        for value in secrets.values():
            self.redactor.add_secret(value)
        log = RunLogger(run.evidence_dir, self.redactor)
        log.event("run_start", run_id=run_id, goal=goal, entry_url=entry_url, inputs=inputs,
                  secret_names=run.secret_names, provider=run.provider, model=run.model,
                  config=self.cfg.__dict__)

        verdict = self.policy.check_navigation(entry_url)
        if not verdict.allowed:
            run.status, run.stop_reason = "policy_blocked", f"entry url denied: {verdict.reason}"
            return self._finish(run, log)
        try:
            self.surface.navigate(entry_url)
        except Exception as exc:
            run.status, run.stop_reason = "failed", f"could not open entry url: {exc}"
            return self._finish(run, log)

        no_progress = 0
        blocked_streak = 0
        for step_no in range(1, self.cfg.max_steps + 1):
            snap = self.surface.snapshot()
            screen = snap.render(max_elements=self.cfg.max_elements)
            if run.actions and run.actions[-1].executed and run.actions[-1].digest_after is None:
                run.actions[-1].digest_after = snap.digest()
                run.actions[-1].url_after = self.surface.url

            decision, usage = self._decide(run, screen, log)
            if decision is None:
                run.status, run.stop_reason = "failed", "model did not produce a valid action"
                log.screenshot(self.surface, "failed")
                break

            record = ActionRecord(index=len(run.actions) + 1, decision=decision, snapshot=snap, screen_text=screen,
                                  element=snap.find(decision.ref) if decision.ref else None,
                                  url_before=self.surface.url, digest_before=snap.digest(), usage=usage)
            run.actions.append(record)
            log.event("decision", step=record.index, decision=decision.model_dump(exclude_none=True),
                      url=record.url_before, usage=usage)

            if decision.action == "done":
                if self._accept_done(run, record, log):
                    break
                continue
            if decision.action == "stuck":
                run.status, run.stop_reason = "stuck", decision.reason
                record.result = "stuck"
                log.screenshot(self.surface, "stuck")
                log.event("stuck", step=record.index, reason=decision.reason, url=self.surface.url)
                outcome = self.stuck_handler(run, record) if self.stuck_handler else None
                if outcome:
                    run.status, run.stop_reason = "running", None
                    what = outcome if isinstance(outcome, str) else "manual steps performed"
                    record.result = (f"a human operator took over the session and intervened ({what}). "
                                     "Control is back with you: re-read the screen and continue toward the goal.")
                    record.executed = False
                    no_progress = 0
                    continue
                break

            if decision.ref and record.element is None:
                record.result = f"error: ref {decision.ref} is not on the current screen; use a ref from the listing"
                log.event("action", step=record.index, result=record.result)
                continue

            # ---- policy gate --------------------------------------------------
            if decision.action == "navigate":
                verdict = self.policy.check_navigation(decision.url or "")
            else:
                verdict = self.policy.check_action(
                    decision.action, url=self.surface.url,
                    target_name=record.element.name if record.element else None,
                    href=record.element.href if record.element else None,
                )
            record.verdict = verdict
            if not verdict.allowed:
                blocked_streak += 1
                record.result = f"blocked by policy: {verdict.reason}. Do not retry this action."
                log.event("policy", step=record.index, verdict=verdict.model_dump(), result="blocked")
                if blocked_streak >= self.cfg.max_consecutive_blocked:
                    run.status, run.stop_reason = "policy_blocked", "repeated policy violations"
                    log.screenshot(self.surface, "policy_blocked")
                    break
                continue
            if verdict.needs_human:
                approved = bool(self.risky_handler and self.risky_handler(record, verdict, run))
                log.event("policy", step=record.index, verdict=verdict.model_dump(),
                          result="approved" if approved else "held")
                if not approved:
                    blocked_streak += 1
                    name = record.element.name if record.element else decision.action
                    record.result = (f"blocked: '{name}' is an irreversible action that requires human approval, "
                                     "which was not granted. Do not retry it; call done if the goal is otherwise met.")
                    if blocked_streak >= self.cfg.max_consecutive_blocked:
                        run.status, run.stop_reason = "policy_blocked", "risky action repeatedly attempted"
                        break
                    continue
            elif verdict.requires == "flag":
                log.event("policy", step=record.index, verdict=verdict.model_dump(), result="flagged")
            blocked_streak = 0

            # ---- execute -----------------------------------------------------
            started = time.perf_counter()
            try:
                self._execute(record, run.inputs, secrets)
                record.executed = True
                record.result = "ok"
            except TemplateError as exc:
                record.result = f"error: {exc}"
            except Exception as exc:  # surface-level failure: report to the model, keep going
                record.result = f"error: {type(exc).__name__}: {str(exc).splitlines()[0][:200]}"
                log.screenshot(self.surface, f"step{record.index:02d}_error")
            record.duration_ms = int((time.perf_counter() - started) * 1000)

            if record.executed and decision.expect:
                record.expect_held = self.surface.wait_for_text(decision.expect, self.cfg.expect_timeout_ms)
                if not record.expect_held:
                    record.result += f' (expected text "{decision.expect}" is NOT visible)'
            if record.executed and decision.action == "extract":
                record.result = f'ok: extracted {decision.output} = "{record.extracted}"'

            if record.executed and decision.action not in {"extract", "wait"}:
                post = self.surface.snapshot()
                record.digest_after, record.url_after = post.digest(), self.surface.url
                if record.digest_after == record.digest_before:
                    no_progress += 1
                    record.result += " (the screen did not change)"
                    if no_progress >= self.cfg.max_no_progress:
                        run.status, run.stop_reason = "no_progress", "screen unchanged after repeated actions"
                        log.screenshot(self.surface, "no_progress")
                        log.event("action", step=record.index, result=record.result, executed=record.executed)
                        break
                else:
                    no_progress = 0
            log.event("action", step=record.index, action=decision.action, result=record.result,
                      executed=record.executed, target=record.element.name if record.element else None,
                      url_before=record.url_before, url_after=record.url_after, duration_ms=record.duration_ms,
                      expect=decision.expect, expect_held=record.expect_held)
        else:
            run.status, run.stop_reason = "max_steps", "step budget exhausted"
            log.screenshot(self.surface, "max_steps")

        return self._finish(run, log)

    # ---------------------------------------------------------------- helpers
    def _finish(self, run: DiscoveryRun, log: RunLogger) -> DiscoveryRun:
        run.finished = time.time()
        try:
            run.final_url = self.surface.url
        except Exception:
            pass
        log.write_json("summary.json", run.summary())
        log.write_json("transcript.json", self._transcript(run))
        log.event("run_end", status=run.status, stop_reason=run.stop_reason, outputs=run.outputs,
                  llm_calls=run.llm_calls, input_tokens=run.input_tokens, output_tokens=run.output_tokens)
        return run

    def _transcript(self, run: DiscoveryRun) -> list[dict]:
        out = []
        for a in run.actions:
            out.append({"step": a.index, "screen": a.screen_text, "decision": a.decision.model_dump(exclude_none=True),
                        "result": a.result})
        return out

    def _build_messages(self, run: DiscoveryRun, screen: str) -> list[dict]:
        keep_from = max(0, len(run.actions) - self.cfg.history_screens)
        messages: list[dict] = []
        for i, a in enumerate(run.actions):
            prior_screen = a.screen_text if i >= keep_from else None
            if i == 0:
                messages.append({"role": "user", "content": initial_user_message(
                    goal=run.goal, entry_url=run.entry_url, allowed_origins=self.policy.policy.allowed_origins,
                    inputs=run.inputs, secret_names=run.secret_names, max_steps=self.cfg.max_steps,
                    screen=prior_screen if prior_screen is not None else f"(screen omitted; url={a.url_before})")})
            else:
                prev = run.actions[i - 1]
                messages.append({"role": "user", "content": step_result_message(prev.index, prev.result, prior_screen, a.url_before)})
            messages.append({"role": "assistant", "content": a.decision.compact()})
        if not run.actions:
            messages.append({"role": "user", "content": initial_user_message(
                goal=run.goal, entry_url=run.entry_url, allowed_origins=self.policy.policy.allowed_origins,
                inputs=run.inputs, secret_names=run.secret_names, max_steps=self.cfg.max_steps, screen=screen)})
        else:
            last = run.actions[-1]
            messages.append({"role": "user", "content": step_result_message(last.index, last.result, screen, self.surface.url)})
        return messages

    def _decide(self, run: DiscoveryRun, screen: str, log: RunLogger) -> tuple[Decision | None, dict]:
        messages = self._build_messages(run, screen)
        usage_total = {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0, "latency_ms": 0, "calls": 0}
        for attempt in range(self.cfg.max_parse_retries + 1):
            try:
                resp = self.llm.complete(SYSTEM_PROMPT, messages, max_tokens=self.cfg.max_tokens)
            except LLMError as exc:
                log.event("llm_error", error=str(exc))
                return None, usage_total
            run.llm_calls += 1
            run.input_tokens += resp.input_tokens
            run.output_tokens += resp.output_tokens
            run.cached_tokens += resp.cached_tokens
            for key, val in (("input_tokens", resp.input_tokens), ("output_tokens", resp.output_tokens),
                             ("cached_tokens", resp.cached_tokens), ("latency_ms", resp.latency_ms), ("calls", 1)):
                usage_total[key] += val
            try:
                decision = Decision.model_validate(extract_json(resp.text))
                return decision, usage_total
            except (ValueError, ValidationError) as exc:
                problem = str(exc).splitlines()[0][:300]
                log.event("parse_error", attempt=attempt, error=problem, reply=resp.text[:500])
                messages.append({"role": "assistant", "content": resp.text})
                messages.append({"role": "user", "content":
                                 f"Your reply was not a valid action: {problem}. Reply with exactly one JSON object "
                                 "in the required format and nothing else."})
        return None, usage_total

    def _accept_done(self, run: DiscoveryRun, record: ActionRecord, log: RunLogger) -> bool:
        decision = record.decision
        extracted = {a.decision.output: a.extracted for a in run.executed_actions
                     if a.action == "extract" and a.decision.output}
        claimed = decision.outputs or {}
        missing = [name for name in claimed if name not in extracted]
        if missing:
            record.result = (f"rejected: outputs {missing} were never extracted. Use the extract action on the "
                             "element containing each value, then call done again.")
            log.event("done_rejected", step=record.index, missing=missing)
            return False
        run.outputs = {name: extracted[name] for name in extracted}
        run.checkpoint_text = decision.checkpoint
        run.status = "success"
        run.stop_reason = "goal reported complete"
        record.executed = True
        record.result = "done"
        log.screenshot(self.surface, "final")
        log.event("done", step=record.index, outputs=run.outputs, checkpoint=decision.checkpoint, url=self.surface.url)
        return True

    def _execute(self, record: ActionRecord, inputs: dict, secrets: dict) -> None:
        d = record.decision
        s = self.surface
        if d.action == "click":
            s.click(d.ref)
        elif d.action == "type":
            s.type(d.ref, render_template(d.value, inputs, secrets) or "")
        elif d.action == "select":
            s.select(d.ref, render_template(d.option, inputs, secrets) or "")
        elif d.action == "press":
            s.press(d.key)
        elif d.action == "navigate":
            s.navigate(render_template(d.url, inputs, secrets) or "")
        elif d.action == "extract":
            record.extracted = s.read_text(d.ref)
        elif d.action == "wait":
            time.sleep(self.cfg.wait_action_ms / 1000)
