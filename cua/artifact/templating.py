"""Placeholder rendering for step values: {{inputs.x}} and {{secrets.a.b}}."""

from __future__ import annotations

from typing import Any, Mapping

from cua.surface.locators import Target

from .schema import PLACEHOLDER_RE

# Strategy fields that may carry {{inputs.x}} templates (e.g. a row anchor "{{inputs.member_id}}-S01")
STRATEGY_TEXT_FIELDS = ("name", "text", "row_text", "column_header", "anchor_text", "selector")


class TemplateError(KeyError):
    pass


def render_template(text: str | None, inputs: Mapping[str, Any], secrets: Mapping[str, str]) -> str | None:
    if text is None:
        return None

    def sub(match) -> str:
        scope, name = match.group(1), match.group(2)
        pool = inputs if scope == "inputs" else secrets
        if name not in pool or pool[name] is None:
            raise TemplateError(f"no value supplied for {{{{{scope}.{name}}}}}")
        return str(pool[name])

    return PLACEHOLDER_RE.sub(sub, text)


def render_target(target: Target, inputs: Mapping[str, Any], secrets: Mapping[str, str]) -> Target:
    """Return a copy of the target with templated strategy fields rendered for this invocation."""
    if not any(is_template(getattr(s, f, None)) for s in target.strategies for f in STRATEGY_TEXT_FIELDS):
        return target
    rendered = target.model_copy(deep=True)
    for strategy in rendered.strategies:
        for field in STRATEGY_TEXT_FIELDS:
            value = getattr(strategy, field, None)
            if is_template(value):
                setattr(strategy, field, render_template(value, inputs, secrets))
    return rendered


def is_template(text: str | None) -> bool:
    return bool(text) and PLACEHOLDER_RE.search(text) is not None


def references_secret(text: str | None) -> bool:
    return bool(text) and any(m.group(1) == "secrets" for m in PLACEHOLDER_RE.finditer(text))
