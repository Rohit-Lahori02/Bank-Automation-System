"""Policy engine: the allowlist and the risk model, enforced before every action.

Both the discovery agent and the replay engine ask this object before acting.
Nothing not explicitly allowed is permitted:

  * navigation is limited to allowed origins and path patterns (deny wins)
  * only listed action types may be performed
  * an action is RISKY if its target name matches a risky pattern or the step
    is marked irreversible; risky actions are blocked, escalated to a human,
    or flagged according to `risky_action_mode`
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal
from urllib.parse import urljoin, urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field

from cua.artifact.schema import RiskClass


class Policy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = 1
    allowed_origins: list[str]
    allowed_paths: list[str] = Field(default_factory=lambda: [".*"])
    denied_paths: list[str] = Field(default_factory=list)
    allowed_actions: list[str]
    risky_target_patterns: list[str] = Field(default_factory=list)
    risky_action_mode: Literal["block", "escalate", "flag"] = "escalate"
    sensitive_field_patterns: list[str] = Field(default_factory=list)

    @classmethod
    def load(cls, path: str | Path) -> "Policy":
        with open(path, encoding="utf-8") as fh:
            return cls.model_validate(yaml.safe_load(fh) or {})


class Verdict(BaseModel):
    allowed: bool
    risk: RiskClass = RiskClass.SAFE
    requires: Literal["none", "escalate", "flag"] = "none"
    reason: str = ""

    @property
    def needs_human(self) -> bool:
        return self.allowed and self.requires == "escalate"


ALWAYS_SAFE_ACTIONS = {"extract", "wait"}


class PolicyEngine:
    def __init__(self, policy: Policy) -> None:
        self.policy = policy
        self._origins = {o.rstrip("/").lower() for o in policy.allowed_origins}
        self._allowed_paths = [re.compile(p) for p in policy.allowed_paths]
        self._denied_paths = [re.compile(p) for p in policy.denied_paths]
        self._risky = [re.compile(p) for p in policy.risky_target_patterns]
        self._sensitive = [re.compile(p) for p in policy.sensitive_field_patterns]

    # ---- urls ------------------------------------------------------------------
    def url_allowed(self, url: str) -> tuple[bool, str]:
        parts = urlsplit(url)
        if parts.scheme == "about":
            return True, "blank page"
        origin = f"{parts.scheme}://{parts.netloc}".lower()
        if origin not in self._origins:
            return False, f"origin {origin} is not in the allowlist"
        path = parts.path or "/"
        if any(p.search(path) for p in self._denied_paths):
            return False, f"path {path} is explicitly denied"
        if not any(p.search(path) for p in self._allowed_paths):
            return False, f"path {path} matches no allowed pattern"
        return True, "allowed"

    def check_navigation(self, url: str) -> Verdict:
        ok, reason = self.url_allowed(url)
        if not ok:
            return Verdict(allowed=False, reason=reason)
        if "navigate" not in self.policy.allowed_actions:
            return Verdict(allowed=False, reason="navigate is not an allowed action")
        return Verdict(allowed=True, reason=reason)

    # ---- risk ------------------------------------------------------------------
    def classify(self, target_name: str | None, *, irreversible: bool = False) -> RiskClass:
        if irreversible:
            return RiskClass.RISKY
        if target_name and any(p.search(target_name) for p in self._risky):
            return RiskClass.RISKY
        return RiskClass.SAFE

    def is_sensitive_field(self, name: str | None) -> bool:
        return bool(name) and any(p.search(name) for p in self._sensitive)

    # ---- actions ---------------------------------------------------------------
    def check_action(
        self,
        action: str,
        *,
        url: str,
        target_name: str | None = None,
        href: str | None = None,
        irreversible: bool = False,
    ) -> Verdict:
        if action not in self.policy.allowed_actions:
            return Verdict(allowed=False, reason=f"action '{action}' is not allowed by policy")
        ok, reason = self.url_allowed(url)
        if not ok:
            return Verdict(allowed=False, reason=f"current page is outside the allowlist: {reason}")
        if href:
            destination = urljoin(url, href)
            if not destination.lower().startswith(("javascript:", "#")):
                ok, reason = self.url_allowed(destination)
                if not ok:
                    return Verdict(allowed=False, reason=f"link would leave the allowlist: {reason}")
        if action in ALWAYS_SAFE_ACTIONS:
            return Verdict(allowed=True, reason="read-only action")
        risk = self.classify(target_name, irreversible=irreversible)
        if risk is RiskClass.SAFE:
            return Verdict(allowed=True, reason="safe action")
        why = "irreversible step" if irreversible else f"target '{target_name}' matches a risky pattern"
        mode = self.policy.risky_action_mode
        if mode == "block":
            return Verdict(allowed=False, risk=risk, reason=f"risky action blocked by policy ({why})")
        return Verdict(allowed=True, risk=risk, requires=mode, reason=f"risky action requires {mode} ({why})")
