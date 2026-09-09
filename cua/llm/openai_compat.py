"""OpenAI-compatible chat adapter (NVIDIA NIM, or any compatible endpoint).

Free-tier endpoints rate-limit aggressively, so this adapter owns its own
retry/backoff with respect for Retry-After, and keeps temperature at 0 for
reproducible action selection.
"""

from __future__ import annotations

import time

from .client import LLMError, LLMResponse


class OpenAICompatClient:
    provider = "openai_compat"

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str,
        json_mode: bool = False,
        temperature: float = 0.0,
        max_retries: int = 5,
        timeout: float = 120.0,
    ) -> None:
        from openai import OpenAI

        self.model = model
        self.base_url = base_url
        self.json_mode = json_mode
        self.temperature = temperature
        self.max_retries = max_retries
        self._client = OpenAI(api_key=api_key, base_url=base_url, max_retries=0, timeout=timeout)

    def complete(self, system: str, messages: list[dict], *, max_tokens: int = 2048) -> LLMResponse:
        import openai

        payload = [{"role": "system", "content": system}, *messages]
        kwargs: dict = dict(model=self.model, messages=payload, max_tokens=max_tokens, temperature=self.temperature)
        if self.json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        delay = 2.0
        for attempt in range(self.max_retries + 1):
            started = time.perf_counter()
            try:
                resp = self._client.chat.completions.create(**kwargs)
            except openai.RateLimitError as exc:
                retry_after = _retry_after(exc)
                if attempt == self.max_retries:
                    raise LLMError(f"rate limited by {self.base_url} after {attempt} retries") from exc
                time.sleep(retry_after or delay)
                delay = min(delay * 2, 30)
                continue
            except openai.APIStatusError as exc:
                if exc.status_code >= 500 and attempt < self.max_retries:
                    time.sleep(delay)
                    delay = min(delay * 2, 30)
                    continue
                raise LLMError(f"{self.provider} error {exc.status_code}: {exc.message}") from exc
            except openai.APIConnectionError as exc:
                if attempt < self.max_retries:
                    time.sleep(delay)
                    delay = min(delay * 2, 30)
                    continue
                raise LLMError(f"cannot reach {self.base_url}: {exc}") from exc

            choice = resp.choices[0]
            usage = getattr(resp, "usage", None)
            return LLMResponse(
                text=choice.message.content or "",
                model=resp.model or self.model,
                provider=self.provider,
                input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                output_tokens=getattr(usage, "completion_tokens", 0) or 0,
                latency_ms=int((time.perf_counter() - started) * 1000),
                stop_reason=choice.finish_reason or "",
            )
        raise LLMError("unreachable")


def _retry_after(exc) -> float | None:
    try:
        value = exc.response.headers.get("retry-after")
        return float(value) if value else None
    except Exception:
        return None
