"""Handoff tests: a second client attaches to the SAME live browser over CDP and acts as the human."""

from __future__ import annotations

import json
import socket
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright

from cua.agent import DiscoveryAgent, DiscoveryConfig
from cua.artifact import Expectation, Step, TextVisible
from cua.artifact.examples import read_savings_balance
from cua.evidence.logger import RunLogger
from cua.handoff import (
    RESUME_FILE, DiscoveryHandoff, HandoffController, InterventionRequest, ReplayHandoff, create_console,
    new_intervention_id,
)
from cua.policy import Policy, PolicyEngine, Redactor
from cua.replay import ReplayEngine, ReplayStatus
from cua.surface import BrowserSurface, ControlError, Controller, RoleNameStrategy, SessionControl, Target
from tests.conftest import PASSWORD, USER
from tests.test_agent import HAPPY_PATH, ScriptedLLM, act

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.timeout(180)
SECRETS = {"app.username": USER, "app.password": PASSWORD}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def policy(mock_server) -> PolicyEngine:
    base = Policy.load(ROOT / "policy.yaml")
    return PolicyEngine(base.model_copy(update={"allowed_origins": [mock_server.base_url]}))


@pytest.fixture
def control() -> SessionControl:
    return SessionControl()


@pytest.fixture
def surface(control):
    with BrowserSurface(headless=True, control=control, cdp_port=_free_port()) as s:
        yield s


@pytest.fixture
def controller(surface, control) -> HandoffController:
    return HandoffController(surface=surface, control=control, redactor=Redactor([PASSWORD]), timeout_s=60,
                             poll_s=0.1)


def operator(controller: HandoffController, act_on_page, decision: str = "resumed", *, settle_s: float = 0.8):
    """A human operator in another thread: attaches to the live browser over CDP, acts, decides."""
    errors: list[BaseException] = []

    def run() -> None:
        try:
            deadline = time.time() + 60
            while not controller.pending() and time.time() < deadline:
                time.sleep(0.05)
            request = controller.pending()[0]
            assert controller.control.holder is Controller.PAUSED
            with sync_playwright() as p:
                browser = p.chromium.connect_over_cdp(request.session_url)
                page = browser.contexts[0].pages[0]
                act_on_page(page)
                time.sleep(settle_s)     # let capture events flush through the binding
            controller.decide(request.id, decision, token=request.control_token)
        except BaseException as exc:  # surfaced by the test after join
            errors.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, errors


def sign_on_as_human(page) -> None:
    page.fill("input[name=userid]", USER)
    page.fill("input[name=passwd]", PASSWORD)
    page.click("input[value='Sign On']")
    page.wait_for_url("**/console")


# ------------------------------------------------------------ controller
def test_handoff_cedes_the_live_session_and_records_what_the_human_did(surface, control, controller, mock_server,
                                                                        tmp_path):
    surface.navigate(f"{mock_server.base_url}/login")
    log = RunLogger(tmp_path / "run", Redactor([PASSWORD]))
    thread, errors = operator(controller, sign_on_as_human)
    request = InterventionRequest(id=new_intervention_id(), run_id="r1", run_kind="replay", kind="stuck",
                                  reason="test", url=surface.url, evidence_dir=str(tmp_path / "run"))
    before_token = control.token
    done = controller.escalate(request, log=log)
    thread.join(10)
    assert not errors, errors

    # decision + control transfer
    assert done.status == "resumed" and done.decided_at
    assert control.holder is Controller.AUTOMATION and control.token != before_token
    transitions = [(t.from_holder.value, t.to_holder.value) for t in control.history]
    assert transitions == [("automation", "paused"), ("paused", "human"), ("human", "automation")]
    # the human really operated the SAME session: automation now sees the signed-on console
    assert surface.url.endswith("/console")
    # captured, redacted actions
    rendered = [a.render() for a in done.human_actions]
    assert any('input textbox "User ID" = "teller01"' in r for r in rendered)
    assert any('input textbox "Password" = "••••••"' in r for r in rendered)
    assert any('click button "Sign On"' in r for r in rendered)
    assert PASSWORD not in json.dumps([a.model_dump() for a in done.human_actions])
    # evidence
    events = [e["kind"] for e in log.read_events()]
    assert events[0] == "handoff_start" and "handoff_claimed" in events and "human_action" in events
    assert events[-1] == "handoff_end"
    saved = json.loads((tmp_path / "run" / "intervention.json").read_text(encoding="utf-8"))
    assert saved["status"] == "resumed" and len(saved["human_actions"]) >= 3
    assert PASSWORD not in (tmp_path / "run" / "intervention.json").read_text(encoding="utf-8")


def test_automation_cannot_act_while_human_holds_control(surface, control, controller, mock_server):
    surface.navigate(f"{mock_server.base_url}/login")
    snap = surface.snapshot()
    button = snap.by_role("button", "Sign On", exact=True)[0].ref
    blocked = {}

    def human(page):
        try:
            surface.click(button)      # the automation thread's own surface, while a human holds control
        except ControlError as exc:
            blocked["error"] = str(exc)

    thread, errors = operator(controller, human, "aborted")
    request = InterventionRequest(id=new_intervention_id(), run_id="r", run_kind="replay", kind="stuck",
                                  reason="t", url=surface.url)
    done = controller.escalate(request)
    thread.join(10)
    assert done.status == "aborted" and "does not hold control" in blocked["error"]
    assert control.holder is Controller.AUTOMATION
    surface.click(button)   # allowed again once control is back


def test_resume_signal_from_file(surface, control, controller, tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    def writer():
        time.sleep(0.5)
        token = json.loads((run_dir / "intervention.json").read_text(encoding="utf-8"))["control_token"]
        (run_dir / RESUME_FILE).write_text(json.dumps({"decision": "approved", "token": token}), encoding="utf-8")

    threading.Thread(target=writer, daemon=True).start()
    request = InterventionRequest(id=new_intervention_id(), run_id="r", run_kind="replay", kind="risky_action",
                                  reason="t", evidence_dir=str(run_dir))
    done = controller.escalate(request)
    assert done.status == "approved" and control.holder is Controller.AUTOMATION


def test_stale_token_is_rejected(surface, control, controller):
    request = InterventionRequest(id=new_intervention_id(), run_id="r", run_kind="replay", kind="stuck", reason="t")

    def wrong_then_right():
        while not controller.pending():
            time.sleep(0.05)
        with pytest.raises(PermissionError):
            controller.decide(request.id, "resumed", token="stale")
        controller.decide(request.id, "resumed", token=request.control_token)

    threading.Thread(target=wrong_then_right, daemon=True).start()
    assert controller.escalate(request).status == "resumed"


def test_timeout_returns_control_to_automation(surface, control):
    controller = HandoffController(surface=surface, control=control, redactor=Redactor(), timeout_s=1.0, poll_s=0.1)
    request = InterventionRequest(id=new_intervention_id(), run_id="r", run_kind="replay", kind="stuck", reason="t")
    done = controller.escalate(request)
    assert done.status == "timeout" and control.holder is Controller.AUTOMATION


# ------------------------------------------------------------ replay integration
def _risky_capability(mock_server):
    cap = read_savings_balance(entry_url=f"{mock_server.base_url}/login")
    cap.steps.append(Step(id="close_it", action="click", description="Open the close-account confirmation",
                          target=Target(description='link "Close Account"', role="link", strategies=[
                              RoleNameStrategy(role="link", name="Close Account", frame='iframe[name="acctframe"]')]),
                          expect=Expectation(description="confirmation page", detect=TextVisible(text="Confirm Close"))))
    cap.checkpoint.detect = TextVisible(text="Confirm Close")
    return cap


def test_replay_risky_step_performed_by_human(surface, policy, controller, mock_server, tmp_path):
    def human(page):
        page.frame_locator('iframe[name="acctframe"]').get_by_role("link", name="Close Account").first.click()
        page.wait_for_url("**/close")

    thread, errors = operator(controller, human, "resumed")
    engine = ReplayEngine(surface=surface, policy=policy, redactor=Redactor([PASSWORD]), evidence_root=tmp_path,
                          escalation_handler=ReplayHandoff(controller))
    result = engine.replay(_risky_capability(mock_server), {"member_id": "12345"}, SECRETS)
    thread.join(10)
    assert not errors, errors
    assert result.status is ReplayStatus.SUCCESS, result.one_line()
    step = next(r for r in result.steps if r.step_id == "close_it")
    assert step.status == "recovered" and step.note == "human resumed (step performed manually)"
    assert result.interventions[0]["kind"] == "risky_action" and result.interventions[0]["human_actions"] >= 1
    assert 'click link "Close Account"' in result.interventions[0]["summary"]


def test_replay_risky_step_approved_then_automation_acts(surface, policy, controller, mock_server, tmp_path):
    thread, errors = operator(controller, lambda page: None, "approved")
    engine = ReplayEngine(surface=surface, policy=policy, redactor=Redactor([PASSWORD]), evidence_root=tmp_path,
                          escalation_handler=ReplayHandoff(controller))
    result = engine.replay(_risky_capability(mock_server), {"member_id": "12345"}, SECRETS)
    thread.join(10)
    assert result.status is ReplayStatus.SUCCESS, result.one_line()
    step = next(r for r in result.steps if r.step_id == "close_it")
    assert step.status == "ok" and step.note == "human approved" and step.strategy == "role_name#0"


def test_replay_unrecoverable_state_fixed_by_human(surface, policy, controller, mock_server, tmp_path):
    """A broken locator strands the run; the human navigates the live session; replay verifies and continues."""
    cap = read_savings_balance(entry_url=f"{mock_server.base_url}/login")
    step = next(s for s in cap.steps if s.id == "go_inquiry")
    step.target = Target(description='link "Member Enquiry"', role="link",
                         strategies=[RoleNameStrategy(role="link", name="Member Enquiry")])
    step.expect = Expectation(description="inquiry form", detect=TextVisible(text="Member Number"))

    def human(page):
        page.get_by_role("link", name="Member Inquiry").first.click()
        page.wait_for_url("**/members/search")

    thread, errors = operator(controller, human, "resumed")
    engine = ReplayEngine(surface=surface, policy=policy, redactor=Redactor([PASSWORD]), evidence_root=tmp_path,
                          escalation_handler=ReplayHandoff(controller))
    result = engine.replay(cap, {"member_id": "12345"}, SECRETS)
    thread.join(10)
    assert not errors, errors
    assert result.status is ReplayStatus.SUCCESS, result.one_line()
    fixed = next(r for r in result.steps if r.step_id == "go_inquiry")
    assert fixed.status == "recovered" and fixed.attempts == 3
    assert result.interventions[0]["kind"] == "unrecoverable" and "TARGET_NOT_FOUND" in result.interventions[0]["reason"]
    assert result.outputs["savings_balance"].compare(0) > 0


def test_replay_escalation_aborted_by_human(surface, policy, controller, mock_server, tmp_path):
    thread, errors = operator(controller, lambda page: None, "aborted")
    engine = ReplayEngine(surface=surface, policy=policy, redactor=Redactor([PASSWORD]), evidence_root=tmp_path,
                          escalation_handler=ReplayHandoff(controller))
    result = engine.replay(_risky_capability(mock_server), {"member_id": "12345"}, SECRETS)
    thread.join(10)
    assert result.status is ReplayStatus.ESCALATED and result.escalation.step_id == "close_it"
    assert result.interventions[0]["decision"] == "denied"


# ------------------------------------------------------------ discovery integration
def test_discovery_stuck_then_human_unblocks(surface, policy, controller, mock_server, tmp_path):
    script = [
        {"reasoning": "no idea how to sign on", "action": "stuck", "reason": "cannot find credentials"},
        *HAPPY_PATH[3:],     # after the human signs on, the model continues from Member Inquiry
    ]
    llm = ScriptedLLM(script)
    bridge = DiscoveryHandoff(controller, log_factory=lambda run: RunLogger(run.evidence_dir, Redactor([PASSWORD])))
    agent = DiscoveryAgent(llm=llm, surface=surface, policy=policy, redactor=Redactor([PASSWORD]),
                           evidence_root=tmp_path / "runs", config=DiscoveryConfig(), stuck_handler=bridge.on_stuck)
    thread, errors = operator(controller, sign_on_as_human, "resumed")
    run = agent.run("Look up member 12345 and read their current savings balance",
                    entry_url=f"{mock_server.base_url}/login", inputs={"member_id": "12345"}, secrets=SECRETS)
    thread.join(10)
    assert not errors, errors
    assert run.status == "success", run.stop_reason
    assert run.outputs["savings_balance"] == "$5,432.10"
    assert "human operator took over" in run.actions[0].result
    assert "human operator took over" in llm.calls[1][-1]["content"]      # the model was told what happened
    events = [json.loads(l)["kind"] for l in (run.evidence_dir / "log.jsonl").read_text(encoding="utf-8").splitlines()]
    assert "stuck" in events and "handoff_start" in events and "human_action" in events and "handoff_end" in events


# ------------------------------------------------------------ operator console
def test_operator_console_lists_claims_and_decides(surface, control, controller, mock_server):
    surface.navigate(f"{mock_server.base_url}/login")
    client = TestClient(create_console(controller))
    request = InterventionRequest(id=new_intervention_id(), run_id="r1", run_kind="replay",
                                  capability_id="member.read_savings_balance v1", goal="read balance",
                                  step_id="close_it", kind="risky_action", reason="Close Account is irreversible",
                                  url=surface.url, screen="[e1] link \"Close Account\"")
    seen: dict = {}
    errors: list[BaseException] = []

    def human_via_console():   # the operator uses the console in another thread; Playwright stays on the main thread
        try:
            while not controller.pending():
                time.sleep(0.05)
            index = client.get("/")
            assert index.status_code == 200 and request.id in index.text and "pending" in index.text
            detail = client.get(f"/interventions/{request.id}")
            assert detail.status_code == 200 and "Close Account is irreversible" in detail.text
            seen["holder_after_open"] = control.holder
            api = client.get(f"/api/interventions/{request.id}").json()
            seen["api"] = api
            bad = client.post(f"/interventions/{request.id}/decision", data={"decision": "approved", "token": "stale"})
            seen["bad_status"] = bad.status_code
            ok = client.post(f"/interventions/{request.id}/decision",
                             data={"decision": "approved", "token": api["control_token"]}, follow_redirects=False)
            seen["ok_status"] = ok.status_code
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=human_via_console, daemon=True)
    thread.start()
    done = controller.escalate(request)
    thread.join(10)
    assert not errors, errors
    assert seen["holder_after_open"] is Controller.HUMAN         # opening the request claims the session
    assert seen["api"]["status"] == "claimed" and seen["api"]["control_token"]
    assert seen["bad_status"] == 409 and seen["ok_status"] == 303
    assert done.status == "approved" and control.holder is Controller.AUTOMATION
    assert "approved" in client.get(f"/interventions/{request.id}").text
