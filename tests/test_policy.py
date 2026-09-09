"""Policy engine and redaction tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from cua.artifact import RiskClass
from cua.policy import Policy, PolicyEngine, RedactionError, Redactor

ROOT = Path(__file__).resolve().parents[1]
BASE = "http://127.0.0.1:8000"


@pytest.fixture
def policy() -> Policy:
    return Policy.load(ROOT / "policy.yaml")


@pytest.fixture
def engine(policy) -> PolicyEngine:
    return PolicyEngine(policy)


# ------------------------------------------------------------------ allowlist
def test_repo_policy_loads_with_expected_shape(policy):
    assert policy.risky_action_mode == "escalate"
    assert "click" in policy.allowed_actions and "navigate" in policy.allowed_actions


def test_url_allowlist(engine):
    assert engine.url_allowed(f"{BASE}/login")[0]
    assert engine.url_allowed(f"{BASE}/members/12345/subaccount/review")[0]
    assert engine.url_allowed("http://localhost:8000/console")[0]
    ok, reason = engine.url_allowed("https://evil.example.com/login")
    assert not ok and "origin" in reason
    ok, reason = engine.url_allowed(f"{BASE}/__chaos")
    assert not ok and "explicitly denied" in reason
    ok, reason = engine.url_allowed(f"{BASE}/admin/users")
    assert not ok
    ok, reason = engine.url_allowed(f"{BASE}/reports")
    assert not ok and "matches no allowed pattern" in reason


def test_navigation_verdicts(engine):
    assert engine.check_navigation(f"{BASE}/members/search").allowed
    v = engine.check_navigation("https://evil.example.com/")
    assert not v.allowed and v.risk is RiskClass.SAFE


# --------------------------------------------------------------------- actions
def test_disallowed_action_type(engine):
    v = engine.check_action("upload", url=f"{BASE}/console")
    assert not v.allowed and "not allowed" in v.reason


def test_action_from_outside_allowlist_is_denied(engine):
    v = engine.check_action("click", url="https://evil.example.com/", target_name="View")
    assert not v.allowed and "outside the allowlist" in v.reason


def test_link_that_leaves_allowlist_is_denied(engine):
    v = engine.check_action("click", url=f"{BASE}/console", target_name="Help", href="https://vendor.example.com/help")
    assert not v.allowed and "leave the allowlist" in v.reason
    v = engine.check_action("click", url=f"{BASE}/console", target_name="Chaos", href="/__chaos")
    assert not v.allowed
    assert engine.check_action("click", url=f"{BASE}/console", target_name="Inquiry", href="/members/search").allowed
    assert engine.check_action("click", url=f"{BASE}/console", target_name="OK", href="javascript:void(0)").allowed


def test_safe_action_is_allowed_without_conditions(engine):
    v = engine.check_action("type", url=f"{BASE}/members/search", target_name="Member Number")
    assert v.allowed and v.risk is RiskClass.SAFE and v.requires == "none"


def test_risky_target_requires_escalation_by_default(engine):
    v = engine.check_action("click", url=f"{BASE}/members/12345/subaccount/review", target_name="Confirm")
    assert v.allowed and v.risk is RiskClass.RISKY and v.requires == "escalate" and v.needs_human
    v = engine.check_action("click", url=f"{BASE}/members/12345/accounts", target_name="Close Account")
    assert v.needs_human
    v = engine.check_action("click", url=f"{BASE}/members/12345", target_name="Open Sub-Account", irreversible=True)
    assert v.needs_human and "irreversible" in v.reason


def test_risky_modes_block_and_flag(policy):
    blocking = PolicyEngine(policy.model_copy(update={"risky_action_mode": "block"}))
    v = blocking.check_action("click", url=f"{BASE}/members/12345/subaccount/review", target_name="Confirm Close")
    assert not v.allowed and v.risk is RiskClass.RISKY
    flagging = PolicyEngine(policy.model_copy(update={"risky_action_mode": "flag"}))
    v = flagging.check_action("click", url=f"{BASE}/members/12345/subaccount/review", target_name="Submit")
    assert v.allowed and v.requires == "flag" and not v.needs_human


def test_read_only_actions_never_risky(engine):
    v = engine.check_action("extract", url=f"{BASE}/members/12345", target_name="Confirm")
    assert v.allowed and v.risk is RiskClass.SAFE


def test_sensitive_field_detection(engine):
    assert engine.is_sensitive_field("Password")
    assert engine.is_sensitive_field("tax_id_last4")
    assert not engine.is_sensitive_field("Member Number")


# ------------------------------------------------------------------- redaction
def test_secret_values_are_replaced_longest_first():
    r = Redactor(["Pa55word!", "Pa55"])
    assert r.redact_text("typed Pa55word! then Pa55") == "typed [REDACTED:SECRET] then [REDACTED:SECRET]"
    r.add_secret("nvapi-abcdefghijklmnopqrstuvwxyz")
    assert "[REDACTED:SECRET]" in r.redact_text("key nvapi-abcdefghijklmnopqrstuvwxyz")


def test_pattern_scrubbing():
    r = Redactor()
    assert r.redact_text("SSN 123-45-6789") == "SSN [REDACTED:SSN]"
    assert r.redact_text("card 4111 1111 1111 1111 ok") == "card [REDACTED:CARD] ok"
    assert r.redact_text("mail a.brandt@example.com") == "mail [REDACTED:EMAIL]"
    assert r.redact_text("call (218) 555-0142 now") == "call [REDACTED:PHONE] now"
    assert r.redact_text("Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123") == "Authorization: [REDACTED:TOKEN]"
    assert r.redact_text("key sk-abcdefghijklmnopqrstuvwxyz") == "key [REDACTED:API_KEY]"


def test_card_redaction_requires_luhn_checksum():
    r = Redactor()
    assert r.redact_text("4111 1111 1111 1112") == "4111 1111 1111 1112"     # not a valid card number
    assert r.redact_text("run 20260908T141522-5b1995") == "run 20260908T141522-5b1995"
    assert r.redact({"input_tokens": 100, "api_token": "abc", "secret_names": ["a"]}) == \
        {"input_tokens": 100, "api_token": "[REDACTED]", "secret_names": ["a"]}


def test_business_identifiers_survive_redaction():
    r = Redactor()
    assert r.redact_text("member 12345 balance $5,432.10 account 12345-S01") == \
        "member 12345 balance $5,432.10 account 12345-S01"
    assert r.redact_text("Tax ID (last 4): ***-**-2088") == "Tax ID (last 4): ***-**-2088"


def test_structured_redaction_masks_sensitive_keys():
    r = Redactor(["Pa55word!"])
    obj = {"userid": "teller01", "passwd": "Pa55word!", "note": "pw was Pa55word!", "nested": [{"token": "abc"}],
           "phone": "(218) 555-0142", "count": 3}
    out = r.redact(obj)
    assert out == {"userid": "teller01", "passwd": "[REDACTED]", "note": "pw was [REDACTED:SECRET]",
                   "nested": [{"token": "[REDACTED]"}], "phone": "[REDACTED:PHONE]", "count": 3}
    assert obj["passwd"] == "Pa55word!"  # input not mutated


def test_assert_clean():
    r = Redactor(["Pa55word!"])
    r.assert_clean('{"value": "{{secrets.app.password}}"}')
    with pytest.raises(RedactionError):
        r.assert_clean('{"value": "Pa55word!"}', context="artifact")
