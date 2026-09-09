"""Glue between the handoff controller and the two things that can get stuck.

ReplayHandoff   - implements the replay engine's escalation_handler protocol
DiscoveryHandoff - implements the discovery agent's stuck_handler / risky_handler
Both build an InterventionRequest with full context, block on the controller,
and translate the human's decision into what the caller understands.
"""

from __future__ import annotations

from cua.agent.loop import ActionRecord, DiscoveryRun
from cua.evidence.logger import RunLogger
from cua.policy.engine import Verdict
from cua.replay.result import Escalation

from .controller import HandoffController, new_intervention_id
from .models import Decision, InterventionRequest


class ReplayHandoff:
    def __init__(self, controller: HandoffController) -> None:
        self.controller = controller

    def __call__(self, esc: Escalation, engine) -> dict:
        cap = engine._cap
        request = InterventionRequest(
            id=new_intervention_id(), run_id=engine._run_id, run_kind="replay",
            capability_id=f"{cap.id} v{cap.version}", goal=cap.description, step_id=esc.step_id, kind=esc.kind,
            reason=esc.reason, url=esc.url, screenshot=esc.screenshot, screen=esc.screen,
            evidence_dir=str(engine._log.run_dir) if engine._log else "",
        )
        resume_when = getattr(engine, "_resume_check", None) if getattr(engine.cfg, "auto_resume", True) else None
        done = self.controller.escalate(request, log=engine._log, resume_when=resume_when)
        decision = done.decision or Decision.TIMEOUT
        return {"decision": "denied" if decision in (Decision.ABORTED, Decision.TIMEOUT) else decision.value,
                "intervention_id": done.id, "human_actions": len(done.human_actions), "summary": done.summary(),
                "auto_resumed": done.auto_resumed}


class DiscoveryHandoff:
    def __init__(self, controller: HandoffController, *, log_factory=None) -> None:
        self.controller = controller
        self.log_factory = log_factory

    def _log(self, run: DiscoveryRun) -> RunLogger | None:
        return self.log_factory(run) if self.log_factory else None

    def on_stuck(self, run: DiscoveryRun, record: ActionRecord | None) -> str | bool:
        request = InterventionRequest(
            id=new_intervention_id(), run_id=run.run_id, run_kind="discovery", goal=run.goal,
            step_id=f"step {record.index}" if record else None, kind="stuck",
            reason=record.decision.reason if record and record.decision.reason else "agent reported it is stuck",
            url=record.url_before if record else run.entry_url, screen=record.screen_text if record else "",
            screenshot=str(run.evidence_dir / "stuck.png"), evidence_dir=str(run.evidence_dir),
        )
        done = self.controller.escalate(request, log=self._log(run))
        if done.decision in (Decision.RESUMED, Decision.APPROVED):
            return done.summary()
        return False

    def on_risky(self, record: ActionRecord, verdict: Verdict, run: DiscoveryRun | None = None) -> bool:
        name = record.element.name if record.element else record.decision.action
        request = InterventionRequest(
            id=new_intervention_id(), run_id=run.run_id if run else "discovery", run_kind="discovery",
            goal=run.goal if run else "", step_id=f"step {record.index}", kind="risky_action",
            reason=f"agent wants to {record.decision.action} '{name}': {verdict.reason}",
            url=record.url_before, screen=record.screen_text,
            evidence_dir=str(run.evidence_dir) if run else "",
        )
        done = self.controller.escalate(request, log=self._log(run) if run else None)
        return done.decision is Decision.APPROVED
