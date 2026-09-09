"""Replay engine tests: happy path, business outcomes, recoverable conditions, hard failures, escalation."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from cua.artifact import Step
from cua.artifact.examples import read_savings_balance
from cua.policy import Policy, PolicyEngine, Redactor
from cua.replay import ReplayConfig, ReplayEngine, ReplayStatus
from cua.surface import BrowserSurface, RoleNameStrategy, Target
from tests.conftest import PASSWORD, USER

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.timeout(150)
SECRETS = {"app.username": USER, "app.password": PASSWORD}


@pytest.fixture
def policy(mock_server) -> PolicyEngine:
    base = Policy.load(ROOT / "policy.yaml")
    return PolicyEngine(base.model_copy(update={"allowed_origins": [mock_server.base_url]}))


@pytest.fixture
def surface():
    with BrowserSurface(headless=True) as s:
        yield s


@pytest.fixture
def capability(mock_server):
    return read_savings_balance(entry_url=f"{mock_server.base_url}/login")


def make_engine(surface, policy, tmp_path, **cfg) -> ReplayEngine:
    return ReplayEngine(surface=surface, policy=policy, redactor=Redactor([PASSWORD]),
                        evidence_root=tmp_path / "runs", config=ReplayConfig(**cfg))


# ------------------------------------------------------------ success
def test_replay_success_returns_typed_outputs(surface, policy, tmp_path, capability):
    result = make_engine(surface, policy, tmp_path).replay(capability, {"member_id": "12345"}, SECRETS)
    assert result.status is ReplayStatus.SUCCESS, result.one_line()
    assert result.outputs == {"savings_balance": Decimal("5432.10"), "member_name": "Oyelaran, Marcus"}
    assert result.checkpoint_verified and result.exit_code == 0
    by_id = {r.step_id: r for r in result.steps}
    assert by_id["login_user"].strategy == "label_text#0"      # legacy-name inference did the work
    assert by_id["login_submit"].strategy == "role_name#0"
    assert by_id["read_balance"].strategy == "table_cell#0"
    assert all(r.status == "ok" for r in result.steps)
    # evidence: redacted log, result.json, final screenshot
    log = (Path(result.evidence_dir) / "log.jsonl").read_text(encoding="utf-8")
    assert PASSWORD not in log and '"kind": "resolved"' in log
    saved = json.loads((Path(result.evidence_dir) / "result.json").read_text(encoding="utf-8"))
    assert saved["status"] == "success" and saved["outputs"]["savings_balance"] == "5432.10"
    assert (Path(result.evidence_dir) / "final.png").exists()
    json.loads(result.to_json())


def test_replay_is_parameterized_by_inputs(surface, policy, tmp_path, capability):
    engine = make_engine(surface, policy, tmp_path)
    r1 = engine.replay(capability, {"member_id": "10001"}, SECRETS)
    assert r1.status is ReplayStatus.SUCCESS and r1.outputs["savings_balance"] == Decimal("1240.50")
    assert r1.outputs["member_name"] == "Brandt, Alicia"
    r2 = engine.replay(capability, {"member_id": "20077"}, SECRETS)
    assert r2.outputs["savings_balance"] == Decimal("15900.00")


def test_templated_locator_strategies_render_per_invocation(surface, policy, tmp_path, capability):
    """A recorded row anchor like '{{inputs.member_id}}-S01' resolves to the invocation's own row."""
    templated = capability.model_copy(deep=True)
    step = next(s for s in templated.steps if s.id == "read_balance")
    step.target.strategies[0].row_text = "{{inputs.member_id}}-S01"
    result = make_engine(surface, policy, tmp_path).replay(templated, {"member_id": "10001"}, SECRETS)
    assert result.status is ReplayStatus.SUCCESS, result.one_line()
    assert result.outputs["savings_balance"] == Decimal("1240.50")
    assert next(r for r in result.steps if r.step_id == "read_balance").strategy == "table_cell#0"


def test_artifact_without_navigate_step_still_starts_at_entry_url(surface, policy, tmp_path, capability):
    trimmed = capability.model_copy(deep=True)
    trimmed.steps = trimmed.steps[1:]
    result = make_engine(surface, policy, tmp_path).replay(trimmed, {"member_id": "12345"}, SECRETS)
    assert result.status is ReplayStatus.SUCCESS, result.one_line()


# ------------------------------------------------------------ business outcomes
def test_not_found_is_a_business_outcome_not_a_failure(surface, policy, tmp_path, capability):
    result = make_engine(surface, policy, tmp_path).replay(capability, {"member_id": "99999"}, SECRETS)
    assert result.status is ReplayStatus.BUSINESS_OUTCOME
    assert result.outcome_code == "MEMBER_NOT_FOUND" and result.failure is None and result.outputs == {}
    assert result.steps[-1].step_id == "search" and result.steps[-1].status == "outcome"
    assert result.exit_code == 10
    assert (Path(result.evidence_dir) / "outcome.png").exists()


def test_permission_denied_is_a_business_outcome(surface, policy, tmp_path, capability):
    result = make_engine(surface, policy, tmp_path).replay(capability, {"member_id": "40403"}, SECRETS)
    assert result.status is ReplayStatus.BUSINESS_OUTCOME and result.outcome_code == "PERMISSION_DENIED"


def test_invalid_input_is_rejected_before_touching_the_ui(surface, policy, tmp_path, capability):
    result = make_engine(surface, policy, tmp_path).replay(capability, {"member_id": "12ab"}, SECRETS)
    assert result.status is ReplayStatus.FAILED and result.failure.code == "INVALID_INPUT"
    assert result.failure.step_id is None and result.steps == []
    assert "does not match pattern" in result.failure.message


def test_missing_secret_is_reported(surface, policy, tmp_path, capability):
    result = make_engine(surface, policy, tmp_path).replay(capability, {"member_id": "12345"}, {"app.username": USER})
    assert result.status is ReplayStatus.FAILED and result.failure.code == "MISSING_SECRET"


# ------------------------------------------------------------ hard failures
def test_application_error_is_a_hard_failure_with_evidence(surface, policy, tmp_path, capability):
    result = make_engine(surface, policy, tmp_path).replay(capability, {"member_id": "50500"}, SECRETS)
    assert result.status is ReplayStatus.FAILED and result.exit_code == 20
    f = result.failure
    assert f.code == "APP_ERROR" and f.step_id == "open_profile"
    assert "Member Profile" in f.expected and "Application Error" in f.observed
    assert f.screenshot and Path(f.screenshot).exists()


def test_bad_credentials_are_a_hard_failure(surface, policy, tmp_path, capability):
    result = make_engine(surface, policy, tmp_path).replay(capability, {"member_id": "12345"},
                                                           {"app.username": USER, "app.password": "wrong"})
    assert result.status is ReplayStatus.FAILED and result.failure.code == "AUTH_FAILED"
    assert result.failure.step_id == "login_submit"


def test_unresolvable_target_reports_every_strategy_tried(surface, policy, tmp_path, capability):
    broken = capability.model_copy(deep=True)
    step = next(s for s in broken.steps if s.id == "go_inquiry")
    step.target = Target(description='link "Member Enquiry"', role="link", strategies=[
        RoleNameStrategy(role="link", name="Member Enquiry"), RoleNameStrategy(role="link", name="Members"),
    ])
    step.expect = None
    result = make_engine(surface, policy, tmp_path).replay(broken, {"member_id": "12345"}, SECRETS)
    assert result.status is ReplayStatus.FAILED and result.failure.code == "TARGET_NOT_FOUND"
    assert result.failure.step_id == "go_inquiry" and len(result.failure.attempts) == 2
    assert result.steps[-1].attempts == 3      # bounded retry from the app profile's retry policy


# ------------------------------------------------------------ recoverable conditions
def test_maintenance_dialog_is_dismissed_and_replay_continues(surface, policy, tmp_path, capability, mock_server):
    mock_server.chaos.update(maintenance_dialog=True)
    result = make_engine(surface, policy, tmp_path).replay(capability, {"member_id": "12345"}, SECRETS)
    assert result.status is ReplayStatus.SUCCESS, result.one_line()
    fired = [(r.step_id, r.conditions) for r in result.steps if r.conditions]
    assert fired and fired[0][1] == ["maintenance_dialog"]


def test_session_expiry_mid_flow_triggers_relogin_and_retry(surface, policy, tmp_path, capability, mock_server):
    def arm(step_id: str) -> None:
        if step_id == "go_inquiry":
            mock_server.chaos.update(expire_session=True)

    result = make_engine(surface, policy, tmp_path, before_step=arm).replay(capability, {"member_id": "12345"}, SECRETS)
    assert result.status is ReplayStatus.SUCCESS, result.one_line()
    inquiry = next(r for r in result.steps if r.step_id == "go_inquiry")
    assert "session_expired" in inquiry.conditions and inquiry.status == "recovered"
    log = (Path(result.evidence_dir) / "log.jsonl").read_text(encoding="utf-8")
    assert '"handler": "subflow"' in log
    assert result.outputs["savings_balance"] == Decimal("5432.10")


def test_session_expiry_during_sign_on_does_not_replay_the_sign_on(surface, policy, tmp_path, capability, mock_server):
    """Expiry armed before sign-on fires on the first authenticated request; the re-login subflow
    reaches the console, and the sign-on step must not be re-clicked on a page without the button."""
    armed = []

    def arm(step_id: str) -> None:
        if step_id == "login_submit" and not armed:   # once: the re-login subflow runs a step with the same id
            armed.append(True)
            mock_server.chaos.update(expire_session=True)

    result = make_engine(surface, policy, tmp_path, before_step=arm).replay(capability, {"member_id": "12345"}, SECRETS)
    assert result.status is ReplayStatus.SUCCESS, result.one_line()
    login = next(r for r in result.steps if r.step_id == "login_submit" and not r.subflow_of)
    assert "session_expired" in login.conditions and login.status == "recovered"
    inner = [r for r in result.steps if r.subflow_of == "session_expired"]
    assert [r.step_id for r in inner] == ["login_user", "login_pass", "login_submit"]   # the re-login, as evidence


def test_slow_backend_is_tolerated(surface, policy, tmp_path, capability, mock_server):
    mock_server.chaos.update(slow_ms=1200)
    try:
        result = make_engine(surface, policy, tmp_path).replay(capability, {"member_id": "12345"}, SECRETS)
    finally:
        mock_server.chaos.update(slow_ms=0)
    assert result.status is ReplayStatus.SUCCESS, result.one_line()


def test_transient_app_error_recovers_via_retry_when_it_clears(surface, policy, tmp_path, capability, mock_server):
    """A one-shot app error on the profile is a HARD failure by the artifact's contract - no silent retry."""
    def arm(step_id: str) -> None:
        if step_id == "open_profile":
            mock_server.chaos.update(app_error=True)

    result = make_engine(surface, policy, tmp_path, before_step=arm).replay(capability, {"member_id": "12345"}, SECRETS)
    assert result.status is ReplayStatus.FAILED and result.failure.code == "APP_ERROR"


# ------------------------------------------------------------ escalation + policy
def test_risky_step_escalates_by_default(surface, policy, tmp_path, capability):
    risky = capability.model_copy(deep=True)
    risky.steps.append(Step(id="close_it", action="click", description="Close the primary account",
                            target=Target(description='link "Close Account"', role="link", strategies=[
                                RoleNameStrategy(role="link", name="Close Account", frame='iframe[name="acctframe"]')])))
    result = make_engine(surface, policy, tmp_path).replay(risky, {"member_id": "12345"}, SECRETS)
    assert result.status is ReplayStatus.ESCALATED, result.one_line()
    assert result.exit_code == 30
    esc = result.escalation
    assert esc.step_id == "close_it" and esc.kind == "risky_action" and "irreversible" in esc.reason
    assert esc.screenshot and Path(esc.screenshot).exists() and "Member Profile" in esc.screen
    assert len(result.steps) == len(capability.steps)      # everything before it completed


def test_escalation_handler_can_approve(surface, policy, tmp_path, capability):
    risky = capability.model_copy(deep=True)
    risky.steps.append(Step(id="close_it", action="click", description="Open close-account confirmation",
                            target=Target(description='link "Close Account"', role="link", strategies=[
                                RoleNameStrategy(role="link", name="Close Account", frame='iframe[name="acctframe"]')]),
                            expect=None))
    risky.checkpoint.detect.detectors = [risky.checkpoint.detect.detectors[1]]  # relax checkpoint for the test
    risky.checkpoint.detect.detectors[0].text = "Close Account"
    seen = []
    engine = ReplayEngine(surface=surface, policy=policy, redactor=Redactor([PASSWORD]), evidence_root=tmp_path,
                          escalation_handler=lambda esc, eng: seen.append(esc.step_id) or "approved")
    result = engine.replay(risky, {"member_id": "12345"}, SECRETS)
    assert seen == ["close_it"]
    assert result.status is ReplayStatus.SUCCESS
    assert next(r for r in result.steps if r.step_id == "close_it").note == "human approved"


def test_policy_denies_navigation_outside_allowlist(surface, policy, tmp_path, capability):
    bad = capability.model_copy(deep=True)
    bad.steps[0].url = "https://evil.example.com/login"
    bad.target.entry_url = bad.steps[0].url
    result = make_engine(surface, policy, tmp_path).replay(bad, {"member_id": "12345"}, SECRETS)
    assert result.status is ReplayStatus.FAILED and result.failure.code == "POLICY_DENIED"
    assert result.failure.step_id == "open"
