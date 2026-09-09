"""Anthropic adapter for the final evidence run.

Cost controls baked in: the (stable) system prompt is cached, adaptive thinking
runs at a configurable effort, and server-side refusal fallbacks are enabled so
a single safety decline does not abort a discovery run.
"""

from __future__ import annotations

import time

from .client import LLMError, LLMResponse

EFFORT_CAPABLE_PREFIXES = ("claude-opus-5", "claude-opus-4", "claude-sonnet-5", "claude-fable", "claude-mythos")


class AnthropicClient:
    provider = "anthropic"

    def __init__(
        self,
        *,
        model: str = "claude-opus-5",
        api_key: str | None = None,
        effort: str = "medium",
        fallbacks: bool = True,
    ) -> None:
        import anthropic

        self.model = model
        self.effort = effort
        self.fallbacks = fallbacks
        self._client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()

    def complete(self, system: str, messages: list[dict], *, max_tokens: int = 2048) -> LLMResponse:
        import anthropic

        kwargs: dict = dict(
            model=self.model,
            max_tokens=max_tokens,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=messages,
        )
        if self.model.startswith(EFFORT_CAPABLE_PREFIXES) and self.effort:
            kwargs["output_config"] = {"effort": self.effort}

        started = time.perf_counter()
        try:
            if self.fallbacks:
                resp = self._client.beta.messages.create(
                    betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs,
                )
            else:
                resp = self._client.messages.create(**kwargs)
        except anthropic.RateLimitError as exc:
            raise LLMError("Anthropic rate limit; retry later") from exc
        except anthropic.APIStatusError as exc:
            raise LLMError(f"Anthropic error {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError(f"cannot reach Anthropic: {exc}") from exc

        if resp.stop_reason == "refusal":
            details = getattr(resp, "stop_details", None)
            category = getattr(details, "category", None) if details else None
            raise LLMError(f"model refused the request (category={category})")

        text = "".join(block.text for block in resp.content if getattr(block, "type", "") == "text")
        usage = resp.usage
        return LLMResponse(
            text=text,
            model=resp.model,
            provider=self.provider,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cached_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
            latency_ms=int((time.perf_counter() - started) * 1000),
            stop_reason=resp.stop_reason or "",
        )
