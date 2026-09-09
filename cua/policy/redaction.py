"""Redaction: keep secrets and regulated data out of artifacts, logs and evidence.

Two mechanisms, applied to every string that leaves the process:

  1. Known secret values (credentials supplied at runtime) are replaced
     wherever they appear, longest first, so partial overlaps cannot leak.
  2. Pattern-based scrubbing for data classes we must never persist: SSNs,
     card numbers, API keys / bearer tokens, e-mail addresses, phone numbers.

Dictionary keys matching sensitive field patterns have their values replaced
outright. `assert_clean` is the last line of defence before an artifact is
written to disk.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

DEFAULT_PATTERNS: list[tuple[str, str]] = [
    ("API_KEY", r"\b(?:sk|nvapi|ghp|xox[abp])-[A-Za-z0-9_\-]{16,}\b"),
    ("TOKEN", r"(?i)\bbearer\s+[A-Za-z0-9\-_\.=]{16,}"),
    ("CARD", r"\b\d(?:[ -]?\d){12,18}\b"),
    ("SSN", r"\b\d{3}-\d{2}-\d{4}\b"),
    ("EMAIL", r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"),
    ("PHONE", r"(?<![\w-])(?:\+1[ .-]?)?\(?\d{3}\)?[ .-]?\d{3}[ .-]?\d{4}(?![\w-])"),
]

DEFAULT_SENSITIVE_FIELDS = [
    r"(?i)password", r"(?i)passwd", r"(?i)\bssn\b", r"(?i)tax[_ ]?id",
    r"(?i)token(?!s)",      # api_token / access_token, but not input_tokens
    r"(?i)secret(?!_names)",
]


def luhn_valid(digits: str) -> bool:
    """Luhn checksum, so timestamps and reference numbers are not mistaken for card numbers."""
    total, parity = 0, len(digits) % 2
    for i, ch in enumerate(digits):
        d = int(ch)
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


class RedactionError(ValueError):
    pass


class Redactor:
    def __init__(
        self,
        secret_values: Iterable[str] = (),
        *,
        sensitive_field_patterns: Iterable[str] = DEFAULT_SENSITIVE_FIELDS,
        patterns: Iterable[tuple[str, str]] = DEFAULT_PATTERNS,
        min_secret_length: int = 4,
    ) -> None:
        self._secrets = sorted({s for s in secret_values if s and len(s) >= min_secret_length}, key=len, reverse=True)
        self._fields = [re.compile(p) for p in sensitive_field_patterns]
        self._patterns = [(label, re.compile(p)) for label, p in patterns]

    def add_secret(self, value: str) -> None:
        if value and value not in self._secrets:
            self._secrets.append(value)
            self._secrets.sort(key=len, reverse=True)

    # ---- strings -------------------------------------------------------------
    def redact_text(self, text: str) -> str:
        if not text:
            return text
        for secret in self._secrets:
            text = text.replace(secret, "[REDACTED:SECRET]")
        for label, pattern in self._patterns:
            if label == "CARD":
                text = pattern.sub(lambda m: "[REDACTED:CARD]" if luhn_valid(re.sub(r"\D", "", m.group(0))) else m.group(0), text)
            else:
                text = pattern.sub(f"[REDACTED:{label}]", text)
        return text

    def is_sensitive_field(self, name: str | None) -> bool:
        return bool(name) and any(p.search(name) for p in self._fields)

    # ---- structures ----------------------------------------------------------
    def redact(self, obj: Any) -> Any:
        if isinstance(obj, str):
            return self.redact_text(obj)
        if isinstance(obj, dict):
            out = {}
            for key, value in obj.items():
                if self.is_sensitive_field(str(key)) and isinstance(value, (str, int, float)):
                    out[key] = "[REDACTED]"
                else:
                    out[key] = self.redact(value)
            return out
        if isinstance(obj, (list, tuple)):
            return type(obj)(self.redact(v) for v in obj)
        return obj

    # ---- guard ---------------------------------------------------------------
    def assert_clean(self, text: str, *, context: str = "output") -> None:
        for secret in self._secrets:
            if secret in text:
                raise RedactionError(f"{context} contains a secret value; refusing to persist")
