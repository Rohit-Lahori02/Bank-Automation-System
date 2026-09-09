"""LLM layer tests (no network)."""

from __future__ import annotations

import pytest

from cua.llm import LLMError, client_from_env, extract_json


def test_extract_json_tolerates_prose_and_fences():
    assert extract_json('{"action": "done"}') == {"action": "done"}
    assert extract_json('Sure! ```json\n{"action": "click", "ref": "e3"}\n``` done') == {"action": "click", "ref": "e3"}
    assert extract_json('thinking... {"a": {"b": [1, 2]}} trailing') == {"a": {"b": [1, 2]}}
    with pytest.raises(ValueError):
        extract_json("no json here")
    with pytest.raises(ValueError):
        extract_json("")


def test_client_from_env_selects_provider(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openai_compat")
    monkeypatch.setenv("LLM_MODEL", "meta/llama-3.3-70b-instruct")
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-test-not-real")
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    client = client_from_env()
    assert client.provider == "openai_compat" and client.model == "meta/llama-3.3-70b-instruct"
    assert client.base_url.startswith("https://integrate.api.nvidia.com")

    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("LLM_MODEL", "")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-real")
    client = client_from_env()
    assert client.provider == "anthropic" and client.model == "claude-opus-5"


def test_client_from_env_requires_key(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openai_compat")
    for key in ("NVIDIA_API_KEY", "LLM_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(LLMError, match="NVIDIA_API_KEY"):
        client_from_env()
    monkeypatch.setenv("LLM_PROVIDER", "bogus")
    with pytest.raises(LLMError, match="unknown LLM_PROVIDER"):
        client_from_env()
