"""Application profiles: runtime conditions that belong to an APP, not to one capability.

"Session expired", "maintenance notice", "record not found" and "application
error" are properties of the vendor product. Every capability recorded on that
product needs the same detectors and the same handlers, and every tenant
running that product shares them. Keeping them here (keyed by app id) is what
lets a capability recorded once be reused across tenants: the flow is recorded,
the app's failure vocabulary is shared, and a tenant variant only overrides
what actually differs.
"""

from __future__ import annotations

from typing import Callable

from cua.surface.locators import RoleNameStrategy, Target

from .schema import (
    AllOf, ClickHandler, Condition, ConditionClass, DialogPresent, RetryHandler, Step, SubflowHandler, TextVisible,
    UrlMatches,
)

ConditionFactory = Callable[[list[Step]], dict[str, Condition]]


def corelink_conditions(login_steps: list[Step]) -> dict[str, Condition]:
    """Runtime conditions of the CoreLink member-servicing console."""
    conditions: dict[str, Condition] = {}
    if login_steps:
        conditions["session_expired"] = Condition(
            id="session_expired", classification=ConditionClass.RECOVERABLE,
            description="The app bounced us to sign-on with an expiry notice; sign on again and retry the step.",
            detect=UrlMatches(pattern=r"/login\?reason=expired"),
            handler=SubflowHandler(steps=login_steps, then="retry_step"),
        )
    conditions.update({
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
            detect=AllOf(detectors=[]),   # engine-applied when a step expectation times out
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
        "validation_error": Condition(
            id="validation_error", classification=ConditionClass.BUSINESS_OUTCOME, code="VALIDATION_ERROR",
            description="The app rejected the submitted form values (VAL-2xxx).",
            detect=TextVisible(text="Please correct the following"),
        ),
        "application_error": Condition(
            id="application_error", classification=ConditionClass.HARD_FAILURE, code="APP_ERROR",
            description="The application returned an error page.",
            detect=TextVisible(text="Application Error"),
        ),
    })
    return conditions


PROFILES: dict[str, ConditionFactory] = {
    "corelink-member-servicing": corelink_conditions,
}


def conditions_for(app: str, login_steps: list[Step]) -> dict[str, Condition]:
    factory = PROFILES.get(app)
    return factory(login_steps) if factory else {}
