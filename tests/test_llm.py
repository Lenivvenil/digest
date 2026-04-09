"""Tests for the LLM httpx wrapper (src/llm.py)."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import httpx
import pytest
import respx

from src.llm import LLMRole, _extract_json, _providers_for_role, complete


def _make_config(providers: list[dict[str, Any]]) -> Any:
    """Build a minimal config-like object with the given providers."""
    from src.config import LLMConfig, ProviderConfig

    provider_objs = [
        ProviderConfig(name=p["name"], model=p["model"], role=p.get("role", []))
        for p in providers
    ]

    class _Cfg:
        llm = LLMConfig(providers=provider_objs)

    return _Cfg()


# ---------------------------------------------------------------------------
# _extract_json
# ---------------------------------------------------------------------------


def test_extract_json_direct() -> None:
    assert _extract_json('{"key": "value"}') == {"key": "value"}


def test_extract_json_array() -> None:
    assert _extract_json("[1, 2, 3]") == [1, 2, 3]


def test_extract_json_code_fence() -> None:
    text = '```json\n{"a": 1}\n```'
    assert _extract_json(text) == {"a": 1}


def test_extract_json_code_fence_no_lang() -> None:
    text = "```\n[1, 2]\n```"
    assert _extract_json(text) == [1, 2]


def test_extract_json_embedded_in_text() -> None:
    text = 'Here is the result: {"x": 42} — done.'
    assert _extract_json(text) == {"x": 42}


def test_extract_json_no_json_raises() -> None:
    with pytest.raises(ValueError, match="No valid JSON"):
        _extract_json("This is just plain text without any JSON.")


def test_extract_json_strips_whitespace() -> None:
    assert _extract_json("  [1]  ") == [1]


# ---------------------------------------------------------------------------
# _providers_for_role
# ---------------------------------------------------------------------------


def test_providers_for_role_matching() -> None:
    config = _make_config([
        {"name": "groq", "model": "llama", "role": ["summarize"]},
        {"name": "deepseek", "model": "ds-chat", "role": ["fallback"]},
    ])
    providers = _providers_for_role(LLMRole.SUMMARIZE, config)
    assert [p.name for p in providers] == ["groq", "deepseek"]


def test_providers_for_role_fallback_only() -> None:
    config = _make_config([
        {"name": "groq", "model": "llama", "role": ["summarize"]},
        {"name": "deepseek", "model": "ds-chat", "role": ["fallback"]},
    ])
    providers = _providers_for_role(LLMRole.GENERATE_QUERIES, config)
    assert [p.name for p in providers] == ["deepseek"]


def test_providers_for_role_deduplication() -> None:
    """Provider in matching should not appear again in fallback list."""
    config = _make_config([
        {"name": "groq", "model": "llama", "role": ["summarize", "fallback"]},
    ])
    providers = _providers_for_role(LLMRole.SUMMARIZE, config)
    names = [p.name for p in providers]
    assert names.count("groq") == 1


def test_providers_for_role_empty_when_no_match() -> None:
    config = _make_config([
        {"name": "groq", "model": "llama", "role": ["summarize"]},
    ])
    providers = _providers_for_role(LLMRole.RANK_SIGNALS, config)
    assert providers == []


# ---------------------------------------------------------------------------
# complete() — OpenAI-compat (Groq)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_complete_openai_compat_success() -> None:
    config = _make_config([
        {"name": "groq", "model": "llama-3.3-70b", "role": ["summarize"]},
    ])
    respx.post("https://api.groq.com/openai/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "Hello from Groq"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )
    )
    messages = [{"role": "user", "content": "Summarize this"}]
    with patch.dict("os.environ", {"GROQ_API_KEY": "test-key"}):
        text, usage = await complete(LLMRole.SUMMARIZE, messages, config)
    assert text == "Hello from Groq"
    assert usage["completion_tokens"] == 5


@pytest.mark.asyncio
@respx.mock
async def test_complete_falls_back_on_500() -> None:
    config = _make_config([
        {"name": "groq", "model": "llama", "role": ["summarize"]},
        {"name": "deepseek", "model": "ds-chat", "role": ["fallback"]},
    ])
    respx.post("https://api.groq.com/openai/v1/chat/completions").mock(
        return_value=httpx.Response(500, text="Internal Server Error")
    )
    respx.post("https://api.deepseek.com/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "DeepSeek fallback"}}],
                "usage": {"completion_tokens": 3},
            },
        )
    )
    messages = [{"role": "user", "content": "test"}]
    with patch.dict("os.environ", {"GROQ_API_KEY": "k1", "DEEPSEEK_API_KEY": "k2"}):
        text, _ = await complete(LLMRole.SUMMARIZE, messages, config)
    assert text == "DeepSeek fallback"


@pytest.mark.asyncio
async def test_complete_skips_missing_api_key() -> None:
    config = _make_config([
        {"name": "groq", "model": "llama", "role": ["summarize"]},
    ])
    messages = [{"role": "user", "content": "test"}]
    with patch.dict("os.environ", {}, clear=True):
        # Remove GROQ_API_KEY from env
        import os
        env = {k: v for k, v in os.environ.items() if k != "GROQ_API_KEY"}
        with patch.dict("os.environ", env, clear=True):
            with pytest.raises(RuntimeError, match="All providers failed"):
                await complete(LLMRole.SUMMARIZE, messages, config)


@pytest.mark.asyncio
async def test_complete_raises_when_no_providers_for_role() -> None:
    config = _make_config([
        {"name": "groq", "model": "llama", "role": ["summarize"]},
    ])
    messages = [{"role": "user", "content": "test"}]
    with pytest.raises(RuntimeError, match="No providers configured"):
        await complete(LLMRole.RANK_SIGNALS, messages, config)


@pytest.mark.asyncio
@respx.mock
async def test_complete_gemini_success() -> None:
    config = _make_config([
        {"name": "gemini", "model": "gemini-2.5-flash", "role": ["generate_queries"]},
    ])
    respx.post(
        url__startswith="https://generativelanguage.googleapis.com"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "candidates": [
                    {"content": {"parts": [{"text": "Gemini response"}]}}
                ],
                "usageMetadata": {
                    "promptTokenCount": 20,
                    "candidatesTokenCount": 10,
                },
            },
        )
    )
    messages = [{"role": "user", "content": "Generate queries"}]
    with patch.dict("os.environ", {"GEMINI_API_KEY": "gemini-key"}):
        text, usage = await complete(LLMRole.GENERATE_QUERIES, messages, config)
    assert text == "Gemini response"
    assert usage["completion_tokens"] == 10


@pytest.mark.asyncio
@respx.mock
async def test_complete_all_providers_fail_raises() -> None:
    config = _make_config([
        {"name": "groq", "model": "llama", "role": ["summarize"]},
        {"name": "deepseek", "model": "ds-chat", "role": ["fallback"]},
    ])
    respx.post("https://api.groq.com/openai/v1/chat/completions").mock(
        return_value=httpx.Response(429, text="Rate limited")
    )
    respx.post("https://api.deepseek.com/v1/chat/completions").mock(
        return_value=httpx.Response(500, text="Error")
    )
    messages = [{"role": "user", "content": "test"}]
    with patch.dict("os.environ", {"GROQ_API_KEY": "k1", "DEEPSEEK_API_KEY": "k2"}):
        with pytest.raises(RuntimeError, match="All providers failed"):
            await complete(LLMRole.SUMMARIZE, messages, config)
