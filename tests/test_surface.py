"""Surface-layer tests against a real Chromium and the mock console."""

from __future__ import annotations

import pytest

from cua.surface import (
    AnchorRelativeStrategy, BBoxStrategy, BrowserSurface, ControlError, Controller, CssStrategy,
    LabelTextStrategy, RoleNameStrategy, SessionControl, TableCellStrategy, Target, TargetNotFound, TextStrategy,
    build_target,
)
from tests.conftest import PASSWORD, USER

pytestmark = pytest.mark.timeout(90)


@pytest.fixture
def surface(tmp_path):
    with BrowserSurface(headless=True, trace_dir=tmp_path / "trace") as s:
        yield s


def sign_on(surface: BrowserSurface, base_url: str) -> None:
    surface.navigate(f"{base_url}/login")
    snap = surface.snapshot()
    user = snap.by_role("textbox", "User ID", exact=True)[0]
    pwd = snap.by_role("textbox", "Password", exact=True)[0]
    surface.type(user.ref, USER)
    surface.type(pwd.ref, PASSWORD)
    surface.click(snap.by_role("button", "Sign On", exact=True)[0].ref)
    assert surface.url.endswith("/console")


# ------------------------------------------------------------ perception
def test_login_snapshot_infers_names_for_unlabeled_inputs(surface, mock_server):
    surface.navigate(f"{mock_server.base_url}/login")
    snap = surface.snapshot()
    user = snap.by_role("textbox", "User ID", exact=True)
    pwd = snap.by_role("textbox", "Password", exact=True)
    assert len(user) == 1 and len(pwd) == 1
    assert user[0].name_source == "adjacent_cell"          # no <label for>, name came from the row's label cell
    assert pwd[0].sensitive and pwd[0].value in (None, "")  # nothing typed yet, but flagged regardless
    assert snap.by_role("button", "Sign On", exact=True)[0].name_source == "content"
    rendered = snap.render()
    assert '[e' in rendered and 'textbox "User ID"' in rendered and "--- frame: main ---" in rendered


def test_sensitive_values_are_masked_in_snapshot(surface, mock_server):
    surface.navigate(f"{mock_server.base_url}/login")
    snap = surface.snapshot()
    pwd = snap.by_role("textbox", "Password", exact=True)[0]
    surface.type(pwd.ref, "supersecret")
    snap2 = surface.snapshot()
    pwd2 = snap2.by_role("textbox", "Password", exact=True)[0]
    assert pwd2.sensitive and pwd2.value == "••••••"
    assert "supersecret" not in snap2.render()


def test_sign_on_via_refs_reaches_console(surface, mock_server):
    sign_on(surface, mock_server.base_url)
    snap = surface.snapshot()
    assert snap.by_role("link", "Member Inquiry")
    assert snap.text_visible("Main Menu")


def test_profile_snapshot_includes_iframe_content(surface, mock_server):
    sign_on(surface, mock_server.base_url)
    surface.navigate(f"{mock_server.base_url}/members/12345")
    snap = surface.snapshot()
    assert len(snap.frames) == 2 and snap.frames[1] == 'iframe[name="acctframe"]'
    balance = [e for e in snap.elements if e.name == "$5,432.10"]
    assert balance and balance[0].frame == 'iframe[name="acctframe"]'
    close_links = snap.by_role("link", "Close Account", exact=True, frame='iframe[name="acctframe"]')
    assert len(close_links) == 3
    # iframe elements are reported in page coordinates: below the profile table
    assert balance[0].bbox.y > snap.by_role("cell", "Member Number", exact=True)[0].bbox.y


def test_digest_is_stable_for_same_state_and_changes_on_navigation(surface, mock_server):
    sign_on(surface, mock_server.base_url)
    surface.navigate(f"{mock_server.base_url}/members/search")
    d1 = surface.snapshot().digest()
    d2 = surface.snapshot().digest()
    assert d1 == d2
    surface.navigate(f"{mock_server.base_url}/console")
    assert surface.snapshot().digest() != d1


# ------------------------------------------------------------ dialogs
def test_overlay_dialog_is_detected_and_dismissable(surface, mock_server):
    sign_on(surface, mock_server.base_url)
    mock_server.chaos.update(maintenance_dialog=True)
    surface.navigate(f"{mock_server.base_url}/members/search")
    snap = surface.snapshot()
    assert snap.has_dialog and snap.dialogs[0]["name"] == "System Maintenance Notice"
    ok = [e for e in snap.by_role("button", "OK", exact=True) if e.in_dialog]
    assert ok
    assert "DIALOG PRESENT" in snap.render()
    surface.click(ok[0].ref)
    assert not surface.snapshot().has_dialog


# ------------------------------------------------------------ resolution
def test_resolver_falls_through_strategy_chain(surface, mock_server):
    sign_on(surface, mock_server.base_url)
    surface.navigate(f"{mock_server.base_url}/members/search")
    target = Target(description="member number box", role="textbox", strategies=[
        RoleNameStrategy(role="textbox", name="Member Number"),     # Playwright sees no accessible name -> miss
        CssStrategy(selector="input[name='does-not-exist']"),       # miss
        LabelTextStrategy(role="textbox", text="Member Number"),    # our inference -> hit
    ])
    resolved = surface.resolve(target)
    assert resolved.strategy_index == 2 and resolved.strategy_kind == "label_text"
    surface.type(resolved, "12345")
    surface.click(Target(description="search", role="button", strategies=[RoleNameStrategy(role="button", name="Search")]))
    assert surface.wait_for_text("Oyelaran, Marcus")


def test_anchor_relative_and_bbox_strategies_resolve(surface, mock_server):
    sign_on(surface, mock_server.base_url)
    surface.navigate(f"{mock_server.base_url}/members/search")
    snap = surface.snapshot()
    box = snap.by_role("textbox", "Member Number", exact=True)[0]

    by_anchor = surface.resolve(Target(description="q", role="textbox", strategies=[
        AnchorRelativeStrategy(role="textbox", anchor_text="Member Number", direction="right"),
    ]))
    assert by_anchor.strategy_kind == "anchor_relative"
    assert by_anchor.handle.get_attribute("name") == "q"

    by_bbox = surface.resolve(Target(description="q", role="textbox", strategies=[
        BBoxStrategy(role="textbox", x=box.bbox.x, y=box.bbox.y, w=box.bbox.w, h=box.bbox.h,
                     viewport_w=snap.viewport[0], viewport_h=snap.viewport[1]),
    ]))
    assert by_bbox.handle.get_attribute("name") == "q"


def test_resolver_reports_every_attempt_on_failure(surface, mock_server):
    surface.navigate(f"{mock_server.base_url}/login")
    target = Target(description="ghost", role="button", strategies=[
        RoleNameStrategy(role="button", name="Nope"),
        TextStrategy(text="Nothing here"),
        CssStrategy(selector="button.ghost", frame='iframe[name="missing"]'),
    ])
    with pytest.raises(TargetNotFound) as exc:
        surface.resolve(target)
    assert len(exc.value.attempts) == 3
    assert "frame iframe" in exc.value.attempts[2]


def test_build_target_orders_strategies_by_robustness(surface, mock_server):
    surface.navigate(f"{mock_server.base_url}/login")
    snap = surface.snapshot()
    user = snap.by_role("textbox", "User ID", exact=True)[0]
    target = build_target(user, snap)
    kinds = [s.kind for s in target.strategies]
    # Unlabeled legacy input: no role_name (Playwright would not compute that name),
    # inference first, spatial next, then structural, coordinates last.
    assert kinds == ["label_text", "anchor_relative", "css", "css", "bbox"]
    assert target.strategies[2].selector == 'input[name="userid"]'
    assert all(s.rationale for s in target.strategies)
    resolved = surface.resolve(target)
    assert resolved.strategy_kind == "label_text"

    button = snap.by_role("button", "Sign On", exact=True)[0]
    bt = build_target(button, snap)
    assert [s.kind for s in bt.strategies][0] == "role_name"
    assert surface.resolve(bt).strategy_kind == "role_name"


def test_value_cell_target_is_value_independent(surface, mock_server):
    """An extracted value (a balance) must be re-locatable when the value differs."""
    sign_on(surface, mock_server.base_url)
    surface.navigate(f"{mock_server.base_url}/members/12345")
    snap = surface.snapshot()
    frame = 'iframe[name="acctframe"]'
    balance = [e for e in snap.elements if e.name == "$5,432.10"][0]
    target = build_target(balance, snap)
    first = target.strategies[0]
    assert isinstance(first, TableCellStrategy)
    assert first.row_text == "12345-S01" and first.column_header == "Balance" and first.frame == frame

    # Same recorded target, different member: the row anchor is parameterized by the recorder
    # (Phase 4); here we substitute it by hand and expect the other member's balance.
    generic = Target(description="savings balance", role="cell", strategies=[
        TableCellStrategy(frame=frame, row_text="Primary Share", column_header="Balance"),
    ])
    surface.navigate(f"{mock_server.base_url}/members/10001")
    resolved = surface.resolve(generic)
    assert resolved.strategy_kind == "table_cell"
    assert resolved.handle.inner_text().strip() == "$1,240.50"
    surface.navigate(f"{mock_server.base_url}/members/12345")
    assert surface.resolve(generic).handle.inner_text().strip() == "$5,432.10"


def test_value_next_to_a_label_is_anchored_on_the_label(surface, mock_server):
    """A value in a key/value layout (profile name, a confirmation number) must not depend on its own text."""
    sign_on(surface, mock_server.base_url)
    surface.navigate(f"{mock_server.base_url}/members/12345")
    snap = surface.snapshot()
    name_cell = snap.by_role("cell", "Oyelaran, Marcus", exact=True)[0]
    target = build_target(name_cell, snap)
    anchored = next(s for s in target.strategies if s.kind == "anchor_relative")
    assert anchored.anchor_text == "Name" and anchored.direction == "right"
    assert target.strategies.index(anchored) < [s.kind for s in target.strategies].index("text")
    # the same target, with the value-specific text strategy removed, reads another member's name
    generic = Target(description="name", role="cell", strategies=[anchored])
    surface.navigate(f"{mock_server.base_url}/members/10001")
    assert surface.resolve(generic).handle.inner_text().strip() == "Brandt, Alicia"


def test_targets_round_trip_through_json(surface, mock_server):
    surface.navigate(f"{mock_server.base_url}/login")
    snap = surface.snapshot()
    target = build_target(snap.by_role("textbox", "User ID", exact=True)[0], snap)
    restored = Target.model_validate_json(target.model_dump_json())
    assert restored == target


# ------------------------------------------------------------ control + evidence
def test_actions_blocked_while_human_holds_control(mock_server, tmp_path):
    control = SessionControl()
    with BrowserSurface(headless=True, control=control) as surface:
        surface.navigate(f"{mock_server.base_url}/login")
        snap = surface.snapshot()
        token = control.transfer(Controller.HUMAN, "operator takeover")
        with pytest.raises(ControlError):
            surface.click(snap.by_role("button", "Sign On", exact=True)[0].ref)
        with pytest.raises(ControlError):
            control.resume("wrong-token")
        control.resume(token, "operator done")
        assert control.holder is Controller.AUTOMATION
        surface.click(snap.by_role("button", "Sign On", exact=True)[0].ref)  # now allowed
        assert len(control.history) == 2


def test_trace_is_written_on_stop(mock_server, tmp_path):
    surface = BrowserSurface(headless=True, trace_dir=tmp_path / "trace").start()
    surface.navigate(f"{mock_server.base_url}/login")
    trace = surface.stop()
    assert trace is not None and trace.exists() and trace.stat().st_size > 1000
