"""Tenant overlays: reuse one recorded capability across institutions running the same product.

A capability is recorded once on a base variant. A tenant that runs the same vendor product
with different branding, labels or a slightly different layout gets an *overlay*: a small,
reviewable diff applied at load time. The recorded flow, conditions and contract stay shared;
only what actually differs is overridden.

What an overlay can express, from cheapest to most specific:
  * entry_url            - the tenant's own instance
  * text_substitutions   - relabeling ("Member Number" -> "Member No."), applied to every
                           locator strategy, expectation, checkpoint and condition detector
  * step_overrides       - per step: strategies to try first on this tenant, a replaced target,
                           a different expectation or value, or skip
  * extra / removed conditions - tenant-specific interstitials or messages

The result is validated as a full Capability, so an overlay cannot produce a broken artifact.
"""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from cua.surface.locators import Strategy, Target

from .schema import (
    ActionKind, AllOf, AnyOf, Capability, Condition, ElementPresent, Expectation, TextVisible,
)
from .templating import STRATEGY_TEXT_FIELDS


class StepOverride(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target: Target | None = None
    prepend_strategies: list[Strategy] = Field(default_factory=list)
    expect: Expectation | None = None
    value: str | None = None
    option: str | None = None
    skip: bool = False


class VariantOverlay(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1.0"
    capability_id: str
    variant: str
    description: str = ""
    entry_url: str | None = None
    text_substitutions: dict[str, str] = Field(default_factory=dict)
    step_overrides: dict[str, StepOverride] = Field(default_factory=dict)
    extra_conditions: dict[str, Condition] = Field(default_factory=dict)
    remove_conditions: list[str] = Field(default_factory=list)

    @classmethod
    def load(cls, path: str | Path) -> "VariantOverlay":
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))

    @property
    def filename(self) -> str:
        return f"{self.capability_id}.{self.variant}.json"


def apply_overlay(capability: Capability, overlay: VariantOverlay) -> Capability:
    if overlay.capability_id != capability.id:
        raise ValueError(f"overlay is for '{overlay.capability_id}', capability is '{capability.id}'")
    unknown = set(overlay.step_overrides) - {s.id for s in capability.steps}
    if unknown:
        raise ValueError(f"overlay overrides unknown steps: {sorted(unknown)}")

    cap = capability.model_copy(deep=True)
    changes: list[str] = []
    old_entry = cap.target.entry_url
    cap.target.variant = overlay.variant
    if overlay.entry_url:
        cap.target.entry_url = overlay.entry_url
        for step in cap.steps:
            if step.action is ActionKind.NAVIGATE and step.url == old_entry:
                step.url = overlay.entry_url
        changes.append(f"entry_url -> {overlay.entry_url}")

    subs = overlay.text_substitutions
    if subs:
        for step in cap.steps:
            if step.target is not None:
                _substitute_target(step.target, subs)
            if step.expect is not None:
                _substitute_detector(step.expect.detect, subs)
        _substitute_detector(cap.checkpoint.detect, subs)
        for cond in cap.conditions.values():
            _substitute_detector(cond.detect, subs)
            handler = cond.handler
            if handler is not None and getattr(handler, "target", None) is not None:
                _substitute_target(handler.target, subs)
            for sub_step in getattr(handler, "steps", []) or []:
                if sub_step.target is not None:
                    _substitute_target(sub_step.target, subs)
                if sub_step.expect is not None:
                    _substitute_detector(sub_step.expect.detect, subs)
        changes.append(f"{len(subs)} relabeling(s)")

    kept = []
    for step in cap.steps:
        override = overlay.step_overrides.get(step.id)
        if override is None:
            kept.append(step)
            continue
        if override.skip:
            if step.action is ActionKind.EXTRACT:
                raise ValueError(f"cannot skip extract step '{step.id}': an output depends on it")
            changes.append(f"{step.id} skipped")
            continue
        if override.target is not None:
            step.target = override.target
        if override.prepend_strategies and step.target is not None:
            step.target.strategies = [*override.prepend_strategies, *step.target.strategies]
        if override.expect is not None:
            step.expect = override.expect
        if override.value is not None:
            step.value = override.value
        if override.option is not None:
            step.option = override.option
        changes.append(f"{step.id} overridden")
        kept.append(step)
    cap.steps = kept

    for cid in overlay.remove_conditions:
        cap.conditions.pop(cid, None)
    cap.conditions.update(overlay.extra_conditions)
    if overlay.remove_conditions or overlay.extra_conditions:
        changes.append(f"conditions -{len(overlay.remove_conditions)} +{len(overlay.extra_conditions)}")

    cap.provenance.notes = (cap.provenance.notes + "; " if cap.provenance.notes else "") + \
        f"overlay '{overlay.variant}' applied: " + ", ".join(changes)
    # re-validate every cross-reference after the edits
    return Capability.model_validate(cap.model_dump())


# ------------------------------------------------------------------ helpers
def _substitute_text(value: str, subs: dict[str, str]) -> str:
    if value in subs:
        return subs[value]
    # quoted occurrences inside css selectors / templates: [value="Search"] -> [value="Find"]
    for old, new in subs.items():
        value = value.replace(f'"{old}"', f'"{new}"').replace(f"'{old}'", f"'{new}'")
    return value


def _substitute_target(target: Target, subs: dict[str, str]) -> None:
    target.description = _substitute_text(target.description, subs)
    for strategy in target.strategies:
        for field in STRATEGY_TEXT_FIELDS:
            value = getattr(strategy, field, None)
            if isinstance(value, str) and value:
                setattr(strategy, field, _substitute_text(value, subs))


def _substitute_detector(detector, subs: dict[str, str]) -> None:
    if isinstance(detector, TextVisible):
        detector.text = _substitute_text(detector.text, subs)
    elif isinstance(detector, ElementPresent):
        detector.name = _substitute_text(detector.name, subs)
    elif isinstance(detector, (AnyOf, AllOf)):
        for child in detector.detectors:
            _substitute_detector(child, subs)


def overlay_path(root: Path, capability_id: str, variant: str) -> Path:
    return Path(root) / f"{capability_id}.{variant}.json"
