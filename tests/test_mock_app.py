"""Behavioural tests for the mock legacy console.

These pin down every runtime condition the replay engine will later have to
detect: happy path, business outcomes (not found), permission denial,
validation errors, application errors, and chaos-injected faults.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from mock_app.app import create_app
from mock_app.chaos import ChaosController
from mock_app.config import Settings
from mock_app.data import MemberStore

USER, PASSWORD = "teller01", "Pa55word!"


def make_settings(**overrides) -> Settings:
    base = dict(
        host="127.0.0.1", port=8000, username=USER, password=PASSWORD,
        session_idle_seconds=0, restricted_members=frozenset({"40403"}),
        crashing_members=frozenset({"50500"}),
    )
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def chaos() -> ChaosController:
    return ChaosController()


@pytest.fixture
def client(chaos) -> TestClient:
    app = create_app(settings=make_settings(), chaos=chaos, store=MemberStore())
    with TestClient(app) as c:
        yield c


def sign_on(client: TestClient) -> None:
    resp = client.post("/login", data={"userid": USER, "passwd": PASSWORD}, follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/console"


# ------------------------------------------------------------------ auth
def test_root_redirects_to_login_when_signed_off(client):
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/login"


def test_login_rejects_bad_credentials(client):
    resp = client.post("/login", data={"userid": USER, "passwd": "wrong"})
    assert resp.status_code == 200
    assert "Invalid user ID or password" in resp.text


def test_login_success_lands_on_console(client):
    sign_on(client)
    resp = client.get("/console")
    assert resp.status_code == 200
    assert "Main Menu" in resp.text and "Member Inquiry" in resp.text


def test_protected_page_redirects_when_not_signed_on(client):
    resp = client.get("/members/search", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/login?reason=login"


def test_sign_off_clears_session(client):
    sign_on(client)
    client.get("/logout", follow_redirects=False)
    resp = client.get("/console", follow_redirects=False)
    assert resp.status_code == 303


# --------------------------------------------------------- member inquiry
def test_search_found_shows_result_row_with_view_link(client):
    sign_on(client)
    resp = client.post("/members/search", data={"q": "12345"})
    assert resp.status_code == 200
    assert "Oyelaran, Marcus" in resp.text
    assert 'href="/members/12345"' in resp.text


def test_search_not_found_is_a_business_outcome_not_an_error(client):
    sign_on(client)
    resp = client.post("/members/search", data={"q": "99999"})
    assert resp.status_code == 200
    assert "No member found for number 99999." in resp.text


def test_search_validation_error(client):
    sign_on(client)
    resp = client.post("/members/search", data={"q": "12ab"})
    assert "must be exactly 5 digits" in resp.text and "VAL-1001" in resp.text


def test_search_restricted_member_is_permission_denied(client):
    sign_on(client)
    resp = client.post("/members/search", data={"q": "40403"})
    assert resp.status_code == 403
    assert "Access Denied" in resp.text and "SEC-0403" in resp.text


def test_member_profile_and_accounts_frame_expose_savings_balance(client):
    sign_on(client)
    profile = client.get("/members/12345")
    assert profile.status_code == 200
    assert "Member Profile" in profile.text
    assert '<iframe name="acctframe" src="/members/12345/accounts"' in profile.text
    assert "***-**-2088" in profile.text  # masked tax id, never the full value

    frame = client.get("/members/12345/accounts")
    assert frame.status_code == 200
    assert "12345-S01" in frame.text and "$5,432.10" in frame.text
    assert "Close Account" in frame.text


def test_direct_unknown_member_url_is_record_not_found(client):
    sign_on(client)
    resp = client.get("/members/99999")
    assert resp.status_code == 404 and "REC-0404" in resp.text


def test_crashing_member_returns_application_error(client):
    sign_on(client)
    resp = client.get("/members/50500")
    assert resp.status_code == 500
    assert "Application Error" in resp.text and "CLK-0500" in resp.text


# ------------------------------------------------------------ sub-account
def test_subaccount_validation_errors(client):
    sign_on(client)
    resp = client.post("/members/12345/subaccount/new",
                       data={"product": "S-VAC", "nickname": "", "deposit": "10"})
    assert resp.status_code == 200
    assert "VAL-2002" in resp.text and "VAL-2004" in resp.text


def test_subaccount_review_then_confirm_creates_account(client):
    sign_on(client)
    resp = client.post("/members/12345/subaccount/new",
                       data={"product": "S-HOL", "nickname": "Holiday Fund", "deposit": "125.50"},
                       follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/members/12345/subaccount/review"

    review = client.get("/members/12345/subaccount/review")
    assert "Review New Sub-Account" in review.text
    assert "Holiday Fund" in review.text and "$125.50" in review.text
    assert 'value="Confirm"' in review.text

    confirmed = client.post("/members/12345/subaccount/confirm")
    assert confirmed.status_code == 200
    assert "Sub-Account Opened" in confirmed.text and "CNF-" in confirmed.text
    assert "12345-S03" in confirmed.text

    frame = client.get("/members/12345/accounts")
    assert "Holiday Fund" in frame.text and "$125.50" in frame.text


def test_review_without_pending_redirects_back_to_form(client):
    sign_on(client)
    resp = client.get("/members/12345/subaccount/review", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"].endswith("/subaccount/new")


# ---------------------------------------------------------- close account
def test_close_account_confirmation_and_execution(client):
    sign_on(client)
    page = client.get("/members/10001/accounts/10001-D01/close")
    assert page.status_code == 200 and 'value="Confirm Close"' in page.text
    done = client.post("/members/10001/accounts/10001-D01/close", follow_redirects=False)
    assert done.status_code == 303
    frame = client.get(done.headers["location"])
    assert "Account 10001-D01 has been closed." in frame.text
    assert "Closed" in frame.text


# ------------------------------------------------------------------ chaos
def test_chaos_session_expiry_is_one_shot(client, chaos):
    sign_on(client)
    chaos.update(expire_session=True)
    bounced = client.get("/console", follow_redirects=False)
    assert bounced.status_code == 303 and bounced.headers["location"] == "/login?reason=expired"
    login = client.get("/login?reason=expired")
    assert "Your session has expired" in login.text
    assert chaos.snapshot()["expire_session"] is False
    # signing on again works and is not bounced a second time
    sign_on(client)
    assert client.get("/console").status_code == 200


def test_chaos_maintenance_dialog_shows_once_on_full_pages_only(client, chaos):
    sign_on(client)
    chaos.update(maintenance_dialog=True)
    frame = client.get("/members/12345/accounts")
    assert "System Maintenance Notice" not in frame.text  # iframe content never consumes it
    page = client.get("/console")
    assert "System Maintenance Notice" in page.text and 'value="OK"' in page.text
    again = client.get("/console")
    assert "System Maintenance Notice" not in again.text


def test_chaos_after_pages_delays_a_one_shot_flag(client, chaos):
    sign_on(client)
    chaos.update(maintenance_dialog=True, after_pages=2)
    assert "System Maintenance Notice" not in client.get("/console").text          # page 1 passes
    assert "System Maintenance Notice" not in client.get("/members/search").text   # page 2 passes
    assert "System Maintenance Notice" in client.get("/console").text              # fires on page 3
    assert "System Maintenance Notice" not in client.get("/console").text          # one-shot, consumed


def test_chaos_sticky_keeps_flags_armed(client, chaos):
    sign_on(client)
    chaos.update(maintenance_dialog=True, sticky=True)
    assert "System Maintenance Notice" in client.get("/console").text
    assert "System Maintenance Notice" in client.get("/console").text
    chaos.reset()
    assert "System Maintenance Notice" not in client.get("/console").text


def test_chaos_app_error_on_next_profile_load(client, chaos):
    sign_on(client)
    chaos.update(app_error=True)
    first = client.get("/members/12345")
    assert first.status_code == 500 and "CLK-0500" in first.text
    second = client.get("/members/12345")
    assert second.status_code == 200 and "Member Profile" in second.text


def test_chaos_slow_delays_pages_but_not_chaos_endpoint(client, chaos):
    sign_on(client)
    chaos.update(slow_ms=300)
    started = time.perf_counter()
    assert client.get("/console").status_code == 200
    assert time.perf_counter() - started >= 0.25
    started = time.perf_counter()
    assert client.get("/__chaos").status_code == 200
    assert time.perf_counter() - started < 0.25


def test_chaos_http_api_roundtrip(client):
    resp = client.post("/__chaos", json={"slow_ms": 50, "expire_session": True})
    assert resp.status_code == 200 and resp.json()["slow_ms"] == 50
    assert client.get("/__chaos").json()["expire_session"] is True
    bad = client.post("/__chaos", json={"nope": 1})
    assert bad.status_code == 400
    assert client.post("/__chaos/reset").json() == {
        "slow_ms": 0, "expire_session": False, "maintenance_dialog": False, "app_error": False, "sticky": False,
        "after_pages": 0,
    }


def test_idle_session_timeout_expires_session():
    app = create_app(settings=make_settings(session_idle_seconds=1), chaos=ChaosController(), store=MemberStore())
    with TestClient(app) as c:
        sign_on(c)
        assert c.get("/console").status_code == 200
        time.sleep(1.2)
        resp = c.get("/console", follow_redirects=False)
        assert resp.status_code == 303 and resp.headers["location"] == "/login?reason=expired"
