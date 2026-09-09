"""Provider-agnostic LLM client.

The agent loop only needs one thing from a model: given a system prompt and a
conversation, return text (which the loop parses as a JSON action). That keeps
the loop identical across NVIDIA NIM (free tier, used for development) and
Anthropic (used for the final evidence run). Provider choice is configuration.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


class LLMError(RuntimeError):
    pass


@dataclass
class LLMResponse:
    text: str
    model: str
    provider: str
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    latency_ms: int = 0
    stop_reason: str = ""


@runtime_checkable
class LLMClient(Protocol):
    provider: str
    model: str

    def complete(self, system: str, messages: list[dict], *, max_tokens: int = 2048) -> LLMResponse: ...


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_json(text: str) -> dict:
    """Pull the first JSON object out of a model reply, tolerating prose and code fences."""
    if not text or not text.strip():
        raise ValueError("empty reply")
    candidates = [m.group(1) for m in _FENCE_RE.finditer(text)] + [text]
    decoder = json.JSONDecoder()
    for candidate in candidates:
        start = candidate.find("{")
        while start != -1:
            try:
                obj, _ = decoder.raw_decode(candidate[start:])
            except json.JSONDecodeError:
                start = candidate.find("{", start + 1)
                continue
            if isinstance(obj, dict):
                return obj
            start = candidate.find("{", start + 1)
    raise ValueError("no JSON object found in reply")


def client_from_env() -> LLMClient:
    """Build the configured client. Never logs or prints key material."""
    provider = os.getenv("LLM_PROVIDER", "openai_compat").strip().lower()
    model = os.getenv("LLM_MODEL", "").strip()
    if provider in {"openai_compat", "nvidia", "nim", "openai"}:
        from .openai_compat import OpenAICompatClient

        api_key = os.getenv("LLM_API_KEY") or os.getenv("NVIDIA_API_KEY") or os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise LLMError("no API key: set NVIDIA_API_KEY (or LLM_API_KEY) in .env")
        return OpenAICompatClient(
            model=model or "meta/llama-3.3-70b-instruct",
            api_key=api_key,
            base_url=os.getenv("LLM_BASE_URL", "https://integrate.api.nvidia.com/v1"),
            json_mode=os.getenv("LLM_JSON_MODE", "0") == "1",
        )
    if provider == "anthropic":
        from .anthropic import AnthropicClient

        if not (os.getenv("ANTHROPIC_API_KEY") or os.getenv("LLM_API_KEY")):
            raise LLMError("no API key: set ANTHROPIC_API_KEY in .env")
        return AnthropicClient(
            model=model or "claude-opus-5",
            api_key=os.getenv("LLM_API_KEY") or None,
            effort=os.getenv("LLM_EFFORT", "medium"),
            fallbacks=os.getenv("LLM_FALLBACKS", "1") == "1",
        )
    raise LLMError(f"unknown LLM_PROVIDER '{provider}' (use openai_compat or anthropic)")
