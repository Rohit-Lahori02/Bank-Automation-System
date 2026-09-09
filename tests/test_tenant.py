"""Cross-tenant reuse: one recorded capability, a second variant of the product, an overlay."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cua.artifact import Capability
from cua.artifact.examples import read_savings_balance
from cua.artifact.overlay import StepOverride, VariantOverlay, apply_overlay
from cua.policy import Policy, PolicyEngine, Redactor
from cua.replay import ReplayEngine, ReplayStatus
from cua.surface import BrowserSurface, CssStrategy, RoleNameStrategy, Target
from mock_app.app import create_app
from mock_app.chaos import ChaosController
from mock_app.config import Settings
from mock_app.data import MemberStore
from tests.conftest import PASSWORD, USER

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.timeout(150)
SECRETS = {"app.username": USER, "app.password": PASSWORD}
OVERLAY_PATH = ROOT / "overlays" / "member.read_savings_balance.lakeshore.json"


# ------------------------------------------------------------ the second tenant
def test_variant_renders_the_same_product_with_tenant_differences():
    settings = Settings(host="127.0.0.1", port=1, username=USER, password=PASSWORD, session_idle_seconds=0,
                        restricted_members=frozenset(), crashing_members=frozenset(), variant="lakeshore")
    with TestClient(create_app(settings=settings, chaos=ChaosController(), store=MemberStore())) as c:
        c.post("/login", data={"userid": USER, "passwd": PASSWORD})
        console = c.get("/console").text
        assert "Lakeshore Community Credit Union" in console and "CoreLink v4.3.0" in console
        assert "Member Lookup" in console and "Member Inquiry" not in console
        search = c.get("/members/search").text
        assert "Member No." in search and 'value="Find"' in search and 'name="branch"' in search
        results = c.post("/members/search", data={"q": "12345", "branch": "MAIN"}).text
        assert ">Open<" in results and "Oyelaran, Marcus" in results
        assert "Current Balance" in c.get("/members/12345/accounts").text


# ------------------------------------------------------------ overlays
def test_overlay_relabels_locators_expectations_and_conditions():
    cap = read_savings_balance("http://127.0.0.1:8000/login")
    overlay = VariantOverlay.load(OVERLAY_PATH)
    adapted = apply_overlay(cap, overlay)

    assert adapted.target.variant == "lakeshore" and adapted.target.entry_url == "http://127.0.0.1:8010/login"
    assert adapted.steps[0].url == "http://127.0.0.1:8010/login"       # the navigate step followed
    by_id = {s.id: s for s in adapted.steps}
    assert by_id["go_inquiry"].target.strategies[0].name == "Member Lookup"
    assert by_id["go_inquiry"].expect.detect.text == "Member Lookup"
    assert by_id["enter_member"].target.strategies[0].text == "Member No."
    assert by_id["search"].target.strategies[1].selector == 'input[type="submit"][value="Find"]'
    assert by_id["open_profile"].target.strategies[0].name == "Open"
    assert by_id["read_balance"].target.strategies[0].column_header == "Current Balance"
    assert adapted.checkpoint.detect.detectors[2].name == "Current Balance"
    assert "overlay 'lakeshore' applied" in adapted.provenance.notes
    assert cap.steps[4].target.strategies[0].name == "Member Inquiry"   # the base capability is untouched
    Capability.model_validate_json(adapted.model_dump_json())              # still a valid artifact


def test_overlay_step_overrides_and_validation():
    cap = read_savings_balance()
    extra = RoleNameStrategy(role="button", name="Lookup", rationale="tenant-specific button")
    overlay = VariantOverlay(capability_id=cap.id, variant="x", step_overrides={
        "search": StepOverride(prepend_strategies=[extra]),
        "read_name": StepOverride(target=Target(description="n", role="cell", strategies=[
            CssStrategy(selector="td.name")])),
    })
    adapted = apply_overlay(cap, overlay)
    by_id = {s.id: s for s in adapted.steps}
    assert by_id["search"].target.strategies[0].name == "Lookup" and len(by_id["search"].target.strategies) == 3
    assert by_id["read_name"].target.strategies[0].selector == "td.name"
    with pytest.raises(ValueError, match="overlay is for"):
        apply_overlay(cap, VariantOverlay(capability_id="other.cap", variant="x"))
    with pytest.raises(ValueError, match="unknown steps"):
        apply_overlay(cap, VariantOverlay(capability_id=cap.id, variant="x", step_overrides={"ghost": StepOverride()}))
    with pytest.raises(ValueError, match="cannot skip extract"):
        apply_overlay(cap, VariantOverlay(capability_id=cap.id, variant="x",
                                          step_overrides={"read_balance": StepOverride(skip=True)}))


# ------------------------------------------------------------ replay across tenants
@pytest.fixture
def policy_both(mock_server, mock_server_b) -> PolicyEngine:
    base = Policy.load(ROOT / "policy.yaml")
    return PolicyEngine(base.model_copy(update={"allowed_origins": [mock_server.base_url, mock_server_b.base_url]}))


@pytest.fixture
def surface():
    with BrowserSurface(headless=True) as s:
        yield s


def test_base_capability_breaks_on_the_second_tenant_without_an_overlay(surface, policy_both, mock_server_b, tmp_path):
    cap = read_savings_balance(entry_url=f"{mock_server_b.base_url}/login")   # same recording, other tenant
    engine = ReplayEngine(surface=surface, policy=policy_both, redactor=Redactor([PASSWORD]), evidence_root=tmp_path)
    result = engine.replay(cap, {"member_id": "12345"}, SECRETS)
    assert result.status is ReplayStatus.FAILED and result.failure.code == "TARGET_NOT_FOUND"
    assert result.failure.step_id == "go_inquiry"          # "Member Inquiry" is "Member Lookup" here
    assert any("no visible match" in a for a in result.failure.attempts)


def test_overlay_makes_the_base_recording_work_on_the_second_tenant(surface, policy_both, mock_server_b, tmp_path):
    overlay = VariantOverlay.load(OVERLAY_PATH).model_copy(update={"entry_url": f"{mock_server_b.base_url}/login"})
    cap = apply_overlay(read_savings_balance("http://127.0.0.1:8000/login"), overlay)
    engine = ReplayEngine(surface=surface, policy=policy_both, redactor=Redactor([PASSWORD]), evidence_root=tmp_path)
    result = engine.replay(cap, {"member_id": "12345"}, SECRETS)
    assert result.status is ReplayStatus.SUCCESS, result.one_line()
    assert result.outputs["savings_balance"] == Decimal("5432.10") and result.variant == "lakeshore"
    assert result.drift == []                              # every step resolved on its first-choice strategy
    assert next(r for r in result.steps if r.step_id == "read_balance").strategy == "table_cell#0"
    other = engine.replay(cap, {"member_id": "10001"}, SECRETS)
    assert other.status is ReplayStatus.SUCCESS and other.outputs["savings_balance"] == Decimal("1240.50")


def test_drift_signal_reports_fallback_strategies(surface, policy_both, mock_server, tmp_path):
    cap = read_savings_balance(entry_url=f"{mock_server.base_url}/login")
    step = next(s for s in cap.steps if s.id == "go_inquiry")
    step.target.strategies.insert(0, RoleNameStrategy(role="link", name="Member Enquiry"))   # a stale first choice
    engine = ReplayEngine(surface=surface, policy=policy_both, redactor=Redactor([PASSWORD]), evidence_root=tmp_path)
    result = engine.replay(cap, {"member_id": "12345"}, SECRETS)
    assert result.status is ReplayStatus.SUCCESS
    assert result.drift == [{"step_id": "go_inquiry", "strategy": "role_name", "index": 1}]
    assert '"drift"' in result.to_json()
