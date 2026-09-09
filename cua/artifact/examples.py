"""A hand-authored reference capability for the mock console.

This is what the recorder (Phase 4) is expected to produce for the goal
"look up a member and read their current savings balance". It exists so the
replay engine, the CLI, and the docs have a concrete, reviewable artifact
before a discovery run has been recorded, and as a readability check on the
schema itself.
"""

from __future__ import annotations

from cua.surface.locators import (
    AnchorRelativeStrategy, BBoxStrategy, CssStrategy, LabelTextStrategy, RoleNameStrategy, TableCellStrategy,
    Target, TextStrategy,
)

from .schema import (
    AllOf, AnyOf, Capability, Checkpoint, ClickHandler, Condition, ConditionClass, DialogPresent, ElementPresent,
    Expectation, InputParam, OutputParam, ParamType, Provenance, RetryHandler, Step, SubflowHandler, TargetApp,
    TextVisible, UrlMatches,
)

ACCT_FRAME = 'iframe[name="acctframe"]'


def _field(label: str, name_attr: str) -> Target:
    """Unlabeled legacy text input identified by the label cell next to it."""
    return Target(description=f'textbox "{label}"', role="textbox", strategies=[
        LabelTextStrategy(role="textbox", text=label,
                          rationale="label inferred from the adjacent table cell; the input has no <label for>"),
        AnchorRelativeStrategy(role="textbox", anchor_text=label, direction="right",
                               rationale="nearest textbox to the right of the label text"),
        CssStrategy(selector=f'input[name="{name_attr}"]',
                    rationale="server-rendered form field name; precise but invisible to the operator"),
    ])


def _button(name: str) -> Target:
    return Target(description=f'button "{name}"', role="button", strategies=[
        RoleNameStrategy(role="button", name=name, rationale="accessible button name from its value/content"),
        CssStrategy(selector=f'input[type="submit"][value="{name}"]', rationale="structural fallback"),
    ])


def _link(name: str) -> Target:
    return Target(description=f'link "{name}"', role="link", strategies=[
        RoleNameStrategy(role="link", name=name, rationale="accessible link name from visible text"),
        TextStrategy(text=name, rationale="visible link text"),
    ])


LOGIN_STEPS = [
    Step(id="login_user", action="type", description="Enter operator user id",
         target=_field("User ID", "userid"), value="{{secrets.app.username}}"),
    Step(id="login_pass", action="type", description="Enter operator password",
         target=_field("Password", "passwd"), value="{{secrets.app.password}}"),
    Step(id="login_submit", action="click", description="Sign on", target=_button("Sign On"),
         expect=Expectation(description="main menu shown", detect=UrlMatches(pattern=r"/console$")),
         on_conditions=["invalid_credentials"]),
]


def read_savings_balance(entry_url: str = "http://127.0.0.1:8000/login") -> Capability:
    return Capability(
        id="member.read_savings_balance",
        name="Read member savings balance",
        description="Look up a member by member number and return the balance of their primary savings share.",
        target=TargetApp(app="corelink-member-servicing", entry_url=entry_url, variant="base"),
        inputs={
            "member_id": InputParam(type=ParamType.STRING, pattern=r"\d{5}", example="12345",
                                    description="5-digit member number"),
        },
        outputs={
            "savings_balance": OutputParam(type=ParamType.MONEY, from_step="read_balance",
                                           description="Current balance of the primary savings share"),
            "member_name": OutputParam(type=ParamType.STRING, from_step="read_name",
                                       description="Member name as shown on the profile (Last, First)"),
        },
        secrets=["app.username", "app.password"],
        steps=[
            Step(id="open", action="navigate", url=entry_url,
                 description="Open the console sign-on page",
                 expect=Expectation(description="sign-on form visible", detect=TextVisible(text="Operator Sign On"))),
            *LOGIN_STEPS,
            Step(id="go_inquiry", action="click", description="Open Member Inquiry", target=_link("Member Inquiry"),
                 expect=Expectation(description="inquiry form", detect=TextVisible(text="Member Inquiry"))),
            Step(id="enter_member", action="type", description="Enter the member number",
                 target=_field("Member Number", "q"), value="{{inputs.member_id}}"),
            Step(id="search", action="click", description="Run the search", target=_button("Search"),
                 expect=Expectation(description="a result or a not-found message", detect=AnyOf(detectors=[
                     TextVisible(text="Search Results"), TextVisible(text="No member found"),
                     TextVisible(text="Access Denied"), TextVisible(text="VAL-"),
                 ])),
                 on_conditions=["member_not_found", "permission_denied", "invalid_member_number"]),
            Step(id="open_profile", action="click", description="Open the member profile", target=_link("View"),
                 expect=Expectation(description="profile page", detect=AnyOf(detectors=[
                     TextVisible(text="Member Profile"), TextVisible(text="Application Error"),
                 ])),
                 on_conditions=["application_error"]),
            Step(id="read_name", action="extract", description="Read the member name", output="member_name",
                 target=Target(description="name value cell", role="cell", strategies=[
                     AnchorRelativeStrategy(role="cell", anchor_text="Name", direction="right",
                                            rationale="value cell to the right of the 'Name' label in the profile "
                                                      "key/value grid; a two-column grid has no column header to key on"),
                 ])),
            Step(id="read_balance", action="extract", description="Read the primary savings balance",
                 output="savings_balance",
                 target=Target(description="Balance of the Primary Share row in the accounts grid", role="cell",
                               strategies=[
                                   TableCellStrategy(frame=ACCT_FRAME, row_text="Primary Share", column_header="Balance",
                                                     rationale="grid cell by row anchor + column header; independent "
                                                               "of the balance value, which differs per member"),
                                   BBoxStrategy(frame=ACCT_FRAME, role="cell", x=707, y=534, w=119, h=40,
                                                viewport_w=1280, viewport_h=820,
                                                rationale="coordinates from the recording viewport; last resort"),
                               ])),
        ],
        conditions={
            "session_expired": Condition(
                id="session_expired", classification=ConditionClass.RECOVERABLE,
                description="The app bounced us to sign-on with an expiry notice; sign on again and retry the step.",
                detect=UrlMatches(pattern=r"/login\?reason=expired"),
                handler=SubflowHandler(steps=LOGIN_STEPS, then="retry_step"),
            ),
            "maintenance_dialog": Condition(
                id="maintenance_dialog", classification=ConditionClass.RECOVERABLE,
                description="A modal maintenance notice is covering the page; dismiss it and continue.",
                detect=DialogPresent(name_contains="Maintenance"),
                handler=ClickHandler(description="Press OK on the notice", target=Target(
                    description='button "OK" in the dialog', role="button", strategies=[
                        RoleNameStrategy(role="button", name="OK", rationale="the dialog's only button"),
                    ])),
            ),
            "slow_or_failed_load": Condition(
                id="slow_or_failed_load", classification=ConditionClass.RECOVERABLE,
                description="The page did not reach the expected state in time; wait and retry a bounded number of times.",
                detect=AllOf(detectors=[]),   # applied by the engine when an expectation times out
                handler=RetryHandler(max_attempts=3, backoff_ms=1500),
            ),
            "invalid_credentials": Condition(
                id="invalid_credentials", classification=ConditionClass.HARD_FAILURE, code="AUTH_FAILED",
                description="The operator credentials were rejected.",
                detect=TextVisible(text="Invalid user ID or password"),
            ),
            "member_not_found": Condition(
                id="member_not_found", classification=ConditionClass.BUSINESS_OUTCOME, code="MEMBER_NOT_FOUND",
                description="No member exists with the supplied number. A legitimate answer, not an error.",
                detect=TextVisible(text="No member found"),
            ),
            "permission_denied": Condition(
                id="permission_denied", classification=ConditionClass.BUSINESS_OUTCOME, code="PERMISSION_DENIED",
                description="The operator role may not view this member.",
                detect=TextVisible(text="Access Denied"),
            ),
            "invalid_member_number": Condition(
                id="invalid_member_number", classification=ConditionClass.BUSINESS_OUTCOME, code="INVALID_INPUT",
                description="The app rejected the member number format.",
                detect=TextVisible(text="VAL-1001"),
            ),
            "application_error": Condition(
                id="application_error", classification=ConditionClass.HARD_FAILURE, code="APP_ERROR",
                description="The application returned an error page while loading the profile.",
                detect=TextVisible(text="Application Error"),
            ),
        },
        checkpoint=Checkpoint(description="On the member profile with the accounts grid loaded", detect=AllOf(detectors=[
            UrlMatches(pattern=r"/members/\d{5}$"),
            TextVisible(text="Member Profile"),
            ElementPresent(role="cell", name="Balance", frame=ACCT_FRAME),
        ])),
        provenance=Provenance(discovery_run_id="hand-authored", provider="none", model="none",
                              goal="Look up a member by number and read their current savings balance",
                              notes="Reference artifact written by hand to the schema; the recorder must produce "
                                    "an equivalent from a real discovery run."),
    )
