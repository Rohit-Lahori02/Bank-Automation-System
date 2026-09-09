"""Handoff controller: pause automation, cede the LIVE session to a human, take it back.

The control-transfer model:

    automation ──escalate──▶ paused ──claim──▶ human ──decision──▶ automation
                                 (awaiting operator)   (operating the same browser)

* One live session. The human uses the very browser the automation drives
  (headed window on this machine; a remote operator would get the same page
  over CDP screencast - see REPORT.md). Nothing is re-created.
* Control is a token held by exactly one party (`SessionControl`). Every
  automated action asserts it; while a human holds it, automation cannot act.
* Everything the human does is captured (redacted) and lands in the run's
  evidence next to the automation's own steps, so the record is continuous.
* Resume is a signal carrying the token: from the operator console, from the
  `cua resume` command (a file), or in-process. A decision transfers control
  back and the caller (replay engine or discovery loop) continues.
"""

from __future__ import annotations

import json
import secrets
import threading
import time
from pathlib import Path
from typing import Callable

from cua.evidence.logger import RunLogger
from cua.policy.redaction import Redactor
from cua.surface.session import Controller, SessionControl

from .models import Decision, HumanAction, InterventionRequest

RESUME_FILE = "resume.json"
INTERVENTION_FILE = "intervention.json"


class HandoffController:
    def __init__(
        self,
        *,
        surface,
        control: SessionControl,
        redactor: Redactor,
        timeout_s: float = 600.0,
        poll_s: float = 0.5,
        on_escalate: Callable[[InterventionRequest], None] | None = None,
        on_human_action: Callable[[InterventionRequest, HumanAction], None] | None = None,
        on_decision: Callable[[InterventionRequest], None] | None = None,
    ) -> None:
        self.surface = surface
        self.control = control
        self.redactor = redactor
        self.timeout_s = timeout_s
        self.poll_s = poll_s
        self.on_escalate = on_escalate
        self.on_human_action = on_human_action
        self.on_decision = on_decision
        self.interventions: dict[str, InterventionRequest] = {}
        self._events: dict[str, threading.Event] = {}
        self._lock = threading.Lock()
        self._log: RunLogger | None = None

    # ------------------------------------------------------------ requests
    def escalate(self, request: InterventionRequest, *, log: RunLogger | None = None,
                 resume_when: Callable[[], bool] | None = None) -> InterventionRequest:
        """Route an intervention request and block until a human decides (or the timeout hits).

        `resume_when` is an optional check of the step's expected state. Once at least one human
        action has been captured and the check holds, the handoff resolves itself as "resumed":
        the human did the step, the screen proves it, and automation takes control back without
        a second round-trip. Anything else still needs an explicit decision.
        """
        self._log = log
        run_dir = Path(request.evidence_dir) if request.evidence_dir else None
        if not request.session_url:
            request.session_url = getattr(self.surface, "cdp_url", None) or ""
        request.control_token = self.control.transfer(Controller.PAUSED, f"escalation: {request.reason}")
        with self._lock:
            self.interventions[request.id] = request
            self._events[request.id] = threading.Event()
        self._write(request)
        if log:
            log.event("handoff_start", intervention=request.id, step=request.step_id, kind=request.kind,
                      reason=request.reason, control=self.control.as_dict())
        if self.on_escalate:
            self.on_escalate(request)

        self.surface.start_human_capture(lambda payload, frame: self._on_human_action(request, payload, frame))
        try:
            self._wait(request, run_dir, resume_when)
        finally:
            self.surface.stop_human_capture()
            if request.status == "pending" or request.status == "claimed":
                request.status = Decision.TIMEOUT.value
            request.decided_at = time.time()
            # hand control back to automation whatever happened; the caller decides what to do next
            self.control.transfer(Controller.AUTOMATION, f"handoff ended: {request.status}")
            self._write(request)
            if log:
                log.event("handoff_end", intervention=request.id, decision=request.status,
                          auto_resumed=request.auto_resumed,
                          human_actions=[a.model_dump() for a in request.human_actions],
                          control=self.control.as_dict())
            if self.on_decision:
                try:
                    self.on_decision(request)
                except Exception:
                    pass
        return request

    def decide(self, intervention_id: str, decision: str | Decision, *, token: str | None = None) -> InterventionRequest:
        """Deliver a decision for a pending intervention (from the console, the CLI, or a test)."""
        decision = Decision(decision)
        with self._lock:
            request = self.interventions[intervention_id]
            if token is not None and token != request.control_token:
                raise PermissionError("stale control token")
            if request.status not in ("pending", "claimed"):
                return request
            request.status = decision.value
            self._events[intervention_id].set()
        return request

    def claim(self, intervention_id: str) -> InterventionRequest:
        """A human has picked the request up and is now operating the session."""
        with self._lock:
            request = self.interventions[intervention_id]
            if request.status == "pending":
                request.status = "claimed"
                if self.control.holder is Controller.PAUSED:
                    self.control.transfer(Controller.HUMAN, "operator claimed the session")
                    if self._log:
                        self._log.event("handoff_claimed", intervention=intervention_id, control=self.control.as_dict())
        return request

    def pending(self) -> list[InterventionRequest]:
        return [r for r in self.interventions.values() if r.status in ("pending", "claimed")]

    # ------------------------------------------------------------- internals
    def _wait(self, request: InterventionRequest, run_dir: Path | None,
              resume_when: Callable[[], bool] | None = None) -> None:
        event = self._events[request.id]
        deadline = time.time() + self.timeout_s
        pump = getattr(self.surface, "pump", None)
        while time.time() < deadline:
            if pump:
                pump()   # deliver captured human actions while we wait
            if event.wait(self.poll_s):
                if pump:
                    pump()
                return
            if resume_when is not None and request.human_actions and request.status in ("pending", "claimed"):
                try:
                    reached = resume_when()
                except Exception:
                    reached = False
                if reached:
                    request.auto_resumed = True
                    if self._log:
                        self._log.event("handoff_auto_resume", intervention=request.id,
                                        note="step's expected state observed after human action")
                    self.decide(request.id, Decision.RESUMED)
                    return
            if run_dir is not None:
                signal = run_dir / RESUME_FILE
                if signal.exists():
                    try:
                        payload = json.loads(signal.read_text(encoding="utf-8"))
                        self.decide(request.id, payload.get("decision", "resumed"), token=payload.get("token"))
                        return
                    except (ValueError, PermissionError, KeyError):
                        signal.unlink(missing_ok=True)

    def _on_human_action(self, request: InterventionRequest, payload: dict, frame: str) -> None:
        if request.status == "pending":
            self.claim(request.id)
        name = str(payload.get("name") or "")
        value = payload.get("value")
        if value is not None and (payload.get("sensitive") or self.redactor.is_sensitive_field(name)):
            value = "••••••"
        action = HumanAction(
            at=float(payload.get("at") or time.time()), type=str(payload.get("type") or "?"),
            role=str(payload.get("role") or ""), name=self.redactor.redact_text(name),
            tag=str(payload.get("tag") or ""), value=self.redactor.redact_text(value) if value else value,
            url=str(payload.get("url") or ""), frame=frame,
        )
        request.human_actions.append(action)
        if self._log:
            self._log.event("human_action", intervention=request.id, **action.model_dump())
        if self.on_human_action:
            try:
                self.on_human_action(request, action)
            except Exception:
                pass

    def _write(self, request: InterventionRequest) -> None:
        if not request.evidence_dir:
            return
        path = Path(request.evidence_dir) / INTERVENTION_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        data = self.redactor.redact(request.model_dump(mode="json"))
        data["control_token"] = request.control_token   # the resume signal must present it; it is not a secret
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def new_intervention_id() -> str:
    return "int-" + secrets.token_hex(4)
