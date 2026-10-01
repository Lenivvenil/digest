"""Tests for the LLM httpx wrapper (src/llm.py)."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import httpx
import pytest
import respx

from digest.llm import LLMRole, _extract_json, _providers_for_role, complete


def _make_config(providers: list[dict[str, Any]]) -> Any:
    """Build a minimal config-like object with the given providers."""
    from digest.config import LLMConfig, ProviderConfig

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
async def test_complete_openai_compat_preserves_text_usage_and_stop_status() -> None:
    config = _make_config([
        {"name": "groq", "model": "llama-3.3-70b", "role": ["summarize"]},
    ])
    respx.post("https://api.groq.com/openai/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "Hello from Groq"}, "finish_reason": "length"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )
    )
    messages = [{"role": "user", "content": "Summarize this"}]
    with patch.dict("os.environ", {"GROQ_API_KEY": "test-key"}):
        text, usage = await complete(LLMRole.SUMMARIZE, messages, config)
    assert text == "Hello from Groq"
    assert usage["completion_tokens"] == 5
    assert usage["finish_reason"] == "length"


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
async def test_complete_gemini_preserves_text_usage_and_stop_status() -> None:
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
                    {"content": {"parts": [{"text": "Gemini response"}]}, "finishReason": "MAX_TOKENS"}
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
    assert usage["finish_reason"] == "MAX_TOKENS"


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


@pytest.mark.asyncio
async def test_transient_error_retries_with_server_delay() -> None:
    from unittest.mock import AsyncMock

    cfg = _make_config([{"name": "groq", "model": "openai/gpt-oss-120b", "role": ["summarize"]}])
    cfg.llm.max_retries = 1
    response = httpx.Response(503, headers={"retry-after": "3"}, request=httpx.Request("POST", "https://example.com"))
    error = httpx.HTTPStatusError("unavailable", request=response.request, response=response)
    with (
        patch("digest.llm._call_provider", AsyncMock(side_effect=[error, ("Recovered", {})])) as call,
        patch("digest.llm.asyncio.sleep", AsyncMock()) as sleep,
    ):
        text, _ = await complete(LLMRole.SUMMARIZE, [], cfg)
    assert text == "Recovered"
    assert call.await_count == 2
    sleep.assert_awaited_once_with(3.0)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 402, 403, 404])
async def test_permanent_errors_fall_back_without_retry_and_redact_body(
    status: int, caplog: pytest.LogCaptureFixture,
) -> None:
    from unittest.mock import AsyncMock

    cfg = _make_config([
        {"name": "groq", "model": "old", "role": ["summarize"]},
        {"name": "gemini", "model": "working", "role": ["fallback"]},
    ])
    cfg.llm.max_retries = 2
    response = httpx.Response(status, json={"error": {"code": "model_not_found", "message": "SECRET_PROMPT"}},
                              request=httpx.Request("POST", "https://example.com"))
    error = httpx.HTTPStatusError("SECRET_CREDENTIAL", request=response.request, response=response)
    with (
        patch("digest.llm._call_provider", AsyncMock(side_effect=[error, ("Fallback", {})])) as call,
        patch("digest.llm.asyncio.sleep", AsyncMock()) as sleep,
    ):
        text, _ = await complete(LLMRole.SUMMARIZE, [], cfg)
    assert text == "Fallback"
    assert call.await_count == 2
    sleep.assert_not_awaited()
    assert "model_not_found" in caplog.text
    assert "SECRET" not in caplog.text


@pytest.mark.asyncio
async def test_excessive_retry_after_falls_back_without_shortening_server_wait() -> None:
    from unittest.mock import AsyncMock

    cfg = _make_config([
        {"name": "groq", "model": "busy", "role": ["summarize"]},
        {"name": "gemini", "model": "working", "role": ["fallback"]},
    ])
    cfg.llm.max_retries = 2
    response = httpx.Response(429, headers={"retry-after": "3600"}, request=httpx.Request("POST", "https://example.com"))
    error = httpx.HTTPStatusError("limited", request=response.request, response=response)
    with (
        patch("digest.llm._call_provider", AsyncMock(side_effect=[error, ("Fallback", {})])) as call,
        patch("digest.llm.asyncio.sleep", AsyncMock()) as sleep,
    ):
        assert (await complete(LLMRole.SUMMARIZE, [], cfg))[0] == "Fallback"
    assert call.await_count == 2
    sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_parallel_pipeline_calls_respect_shared_concurrency_limit() -> None:
    import asyncio

    cfg = _make_config([{"name": "groq", "model": "model", "role": ["fallback"]}])
    cfg.llm.max_concurrent_requests = 1
    active = 0
    high_water = 0

    async def provider(*args: object) -> tuple[str, dict]:
        nonlocal active, high_water
        active += 1
        high_water = max(high_water, active)
        await asyncio.sleep(0)
        active -= 1
        return "ok", {}

    with patch("digest.llm._call_provider", side_effect=provider):
        results = await asyncio.gather(*(
            complete(role, [], cfg) for role in [LLMRole.SUMMARIZE, LLMRole.GENERATE_QUERIES, LLMRole.RANK_SIGNALS]
        ))
    assert high_water == 1
    assert len(results) == 3


@pytest.mark.asyncio
async def test_request_spacing_is_shared_and_deterministic() -> None:
    import asyncio

    from digest.llm import _pace_request, _RequestState

    state = _RequestState(asyncio.get_running_loop(), asyncio.Semaphore(1))
    now = 100.0
    waits: list[float] = []

    async def sleep(delay: float) -> None:
        nonlocal now
        waits.append(delay)
        now += delay

    with patch("digest.llm.time.monotonic", side_effect=lambda: now), patch("digest.llm.asyncio.sleep", sleep):
        await _pace_request(state, 20.0)
        await _pace_request(state, 20.0)
        await _pace_request(state, 20.0)
    assert waits == [20.0, 20.0]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [402, 404, 429])
async def test_provider_failures_are_shared_across_subsequent_calls(status: int) -> None:
    from unittest.mock import AsyncMock

    cfg = _make_config([
        {"name": "groq", "model": "unavailable", "role": ["summarize"]},
        {"name": "gemini", "model": "working", "role": ["fallback"]},
    ])
    cfg.llm.max_retries = 1
    response = httpx.Response(status, headers={"retry-after": "3600"},
                              request=httpx.Request("POST", "https://example.com"))
    error = httpx.HTTPStatusError("unavailable", request=response.request, response=response)
    with patch("digest.llm._call_provider", AsyncMock(side_effect=[error, ("first", {}), ("second", {})])) as call:
        await complete(LLMRole.SUMMARIZE, [], cfg)
        await complete(LLMRole.SUMMARIZE, [], cfg)
    assert [c.args[1].name for c in call.await_args_list] == ["groq", "gemini", "gemini"]


def test_machine_error_code_does_not_echo_arbitrary_response_strings() -> None:
    from digest.llm import _safe_provider_error

    response = httpx.Response(404, json={"error": {"code": "sk-secret-token"}},
                              request=httpx.Request("POST", "https://example.com"))
    error = httpx.HTTPStatusError("secret", request=response.request, response=response)
    assert _safe_provider_error(error) == "HTTP 404 code=unknown"


@pytest.mark.asyncio
async def test_queued_call_rechecks_cooldown_after_waiting_for_request_slot() -> None:
    from unittest.mock import AsyncMock

    from digest.llm import _request_state

    cfg = _make_config([
        {"name": "groq", "model": "busy", "role": ["summarize"]},
        {"name": "gemini", "model": "working", "role": ["fallback"]},
    ])
    state = _request_state(cfg)
    calls = 0

    async def pace(*args: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            # Another in-flight request received backoff while this call queued.
            state.unavailable_until[("groq", "busy")] = float("inf")

    with (
        patch("digest.llm._pace_request", side_effect=pace),
        patch("digest.llm._call_provider", AsyncMock(return_value=("Fallback", {}))) as provider,
    ):
        assert (await complete(LLMRole.SUMMARIZE, [], cfg))[0] == "Fallback"
    assert [call.args[1].name for call in provider.await_args_list] == ["gemini"]


@pytest.mark.asyncio
async def test_explicit_review_model_never_uses_configured_fallback() -> None:
    from unittest.mock import AsyncMock

    from digest.config import ProviderConfig

    config = _make_config([
        {"name": "gemini", "model": "primary", "role": ["fallback"]},
        {"name": "deepseek", "model": "paid", "role": ["fallback"]},
    ])
    with patch("digest.llm._call_provider", AsyncMock(side_effect=ValueError("invalid response"))) as call:
        with pytest.raises(RuntimeError):
            await complete(LLMRole.REVIEW_EVIDENCE, [], config,
                           provider_override=ProviderConfig("groq", "openai/gpt-oss-120b"))
    assert call.await_count == 1
    assert call.await_args.args[1].name == "groq"


@pytest.mark.asyncio
async def test_review_output_budget_reaches_all_provider_transports() -> None:
    import json

    from digest.llm import _anthropic_call, _gemini_call, _openai_compat_call

    with respx.mock:
        groq = respx.post("https://api.groq.com/openai/v1/chat/completions").mock(
            return_value=httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]}))
        gemini = respx.post("https://generativelanguage.googleapis.com/v1beta/models/fixture:generateContent").mock(
            return_value=httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}))
        anthropic = respx.post("https://api.anthropic.com/v1/messages").mock(
            return_value=httpx.Response(200, json={"content": [{"text": "ok"}]}))
        async with httpx.AsyncClient() as client:
            await _openai_compat_call(client, "https://api.groq.com/openai/v1", "fake", "fixture", [], 0.2, 2048)
            await _gemini_call(client, "fake", "fixture", [], 0.2, 2048)
            await _anthropic_call(client, "fake", "fixture", [], 0.2, 2048)
    assert json.loads(groq.calls.last.request.content)["max_completion_tokens"] == 2048
    assert json.loads(gemini.calls.last.request.content)["generationConfig"]["maxOutputTokens"] == 2048
    assert json.loads(anthropic.calls.last.request.content)["max_tokens"] == 2048


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["groq", "gemini"])
@pytest.mark.parametrize("empty_content", ["", None])
@respx.mock
async def test_review_retains_empty_output_exhaustion_for_resumable_processing(
    provider: str, empty_content: str | None,
) -> None:
    config = _make_config([{"name": provider, "model": "fixture", "role": ["review_evidence"]}])
    if provider == "groq":
        response = {"choices": [{"message": {"content": empty_content}, "finish_reason": "length"}],
                    "usage": {"completion_tokens": 2000}}
        respx.post("https://api.groq.com/openai/v1/chat/completions").respond(200, json=response)
    else:
        response = {"candidates": [{"content": {"parts": [{"text": None}] if empty_content is None else []},
                                    "finishReason": "MAX_TOKENS"}],
                    "usageMetadata": {"candidatesTokenCount": 2000}}
        respx.post(url__startswith="https://generativelanguage.googleapis.com").respond(200, json=response)
    legacy = _make_config([{"name": provider, "model": "fixture", "role": ["summarize"]}])
    legacy.llm.max_retries = 0
    with patch.dict("os.environ", {"GROQ_API_KEY": "synthetic", "GEMINI_API_KEY": "synthetic"}):
        text, usage = await complete(LLMRole.REVIEW_EVIDENCE, [], config)
        with pytest.raises(RuntimeError, match="All providers failed"):
            await complete(LLMRole.SUMMARIZE, [], legacy)
    assert text == ""
    assert usage["finish_reason"] in {"length", "MAX_TOKENS"}
    assert usage["completion_tokens"] == 2000
