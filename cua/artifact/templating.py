"""Placeholder rendering for step values: {{inputs.x}} and {{secrets.a.b}}."""

from __future__ import annotations

from typing import Any, Mapping

from .schema import PLACEHOLDER_RE


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


def is_template(text: str | None) -> bool:
    return bool(text) and PLACEHOLDER_RE.search(text) is not None


def references_secret(text: str | None) -> bool:
    return bool(text) and any(m.group(1) == "secrets" for m in PLACEHOLDER_RE.finditer(text))
