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


# ---------------------------------------------------------------------------
# Bounded, synthetic provider-failure observability regressions
# ---------------------------------------------------------------------------


def test_failure_diagnostics_parse_finite_headers_and_duration_resets() -> None:
    from datetime import UTC, datetime, timedelta

    from digest.llm import provider_failure_diagnostics

    observed = datetime(2026, 10, 1, 5, tzinfo=UTC)
    response = httpx.Response(429, headers={
        "retry-after": "12.5",
        "x-ratelimit-limit-requests": "30",
        "x-ratelimit-limit-tokens": "6000",
        "x-ratelimit-remaining-requests": "0",
        "x-ratelimit-remaining-tokens": "125.5",
        "x-ratelimit-reset-requests": "1m30s",
        "x-ratelimit-reset-tokens": "2h3m",
        "x-request-id": "SECRET_IGNORED_HEADER",
    }, json={"error": {"code": "rate_limit_exceeded"}},
        request=httpx.Request("POST", "https://example.com"))
    error = httpx.HTTPStatusError("limited", request=response.request, response=response)
    diagnostics = provider_failure_diagnostics(error, observed_at=observed)
    assert diagnostics is not None
    assert diagnostics.status_code == 429
    assert diagnostics.error_code == "rate_limit_exceeded"
    assert diagnostics.quota_axis == "unknown"
    assert diagnostics.quota_axis_source == "unknown"
    assert datetime.fromisoformat(diagnostics.observed_at) == observed
    assert diagnostics.numeric_headers == {
        "x-ratelimit-limit-requests": 30.0,
        "x-ratelimit-limit-tokens": 6000.0,
        "x-ratelimit-remaining-requests": 0.0,
        "x-ratelimit-remaining-tokens": 125.5,
    }
    assert diagnostics.retry_after is not None
    assert diagnostics.retry_after.seconds == 12.5
    assert diagnostics.retry_after.format == "seconds"
    assert datetime.fromisoformat(diagnostics.retry_after.server_retry_at) == observed + timedelta(seconds=12.5)
    assert set(diagnostics.reset_headers) == {"x-ratelimit-reset-requests", "x-ratelimit-reset-tokens"}
    for name, seconds in [("x-ratelimit-reset-requests", 90.0), ("x-ratelimit-reset-tokens", 7380.0)]:
        timing = diagnostics.reset_headers[name]
        assert timing.seconds == seconds
        assert timing.format == "duration"
        assert datetime.fromisoformat(timing.server_retry_at) == observed + timedelta(seconds=seconds)


@pytest.mark.parametrize("axis_field", ["quota_axis", "type"])
def test_failure_diagnostics_parse_http_date_and_explicit_machine_axis(axis_field: str) -> None:
    from datetime import UTC, datetime, timedelta

    from digest.llm import provider_failure_diagnostics

    observed = datetime(2026, 10, 1, 5, tzinfo=UTC)
    response = httpx.Response(429, headers={
        "retry-after": "Thu, 01 Oct 2026 05:02:00 GMT",
        "x-ratelimit-reset-requests": "90",
    }, json={"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", axis_field: "tokens_per_minute"}},
        request=httpx.Request("POST", "https://example.com"))
    error = httpx.HTTPStatusError("limited", request=response.request, response=response)
    diagnostics = provider_failure_diagnostics(error, observed_at=observed)
    assert diagnostics is not None
    assert diagnostics.quota_axis == "tokens_per_minute"
    assert diagnostics.error_code == "RESOURCE_EXHAUSTED"
    assert diagnostics.quota_axis_source == f"error_{axis_field}"
    assert diagnostics.retry_after is not None
    assert diagnostics.retry_after.seconds == 120.0
    assert diagnostics.retry_after.format == "http_date"
    assert datetime.fromisoformat(diagnostics.retry_after.server_retry_at) == observed + timedelta(seconds=120)
    reset = diagnostics.reset_headers["x-ratelimit-reset-requests"]
    assert reset.seconds == 90.0
    assert reset.format == "seconds"


def test_failure_diagnostics_drop_nonfinite_malformed_and_arbitrary_secrets() -> None:
    import json
    from dataclasses import asdict
    from datetime import UTC, datetime

    from digest.llm import provider_failure_diagnostics

    request = httpx.Request("POST", "https://example.com/?key=SECRET_QUERY",
                           headers={"Authorization": "Bearer SECRET_API_KEY"}, content="SECRET_PROMPT")
    response = httpx.Response(429, headers={
        "retry-after": "NaN",
        "x-ratelimit-limit-requests": "NaN",
        "x-ratelimit-limit-tokens": "inf",
        "x-ratelimit-remaining-requests": "-inf",
        "x-ratelimit-remaining-tokens": "SECRET_HEADER",
        "x-ratelimit-reset-requests": "1e309s",
        "x-ratelimit-reset-tokens": "1mSECRET_RESET",
        "authorization": "SECRET_RESPONSE_HEADER",
    }, json={"error": {
        "code": "SECRET_CODE", "quota_axis": "SECRET_AXIS", "type": "SECRET_TYPE",
        "message": "Daily request quota exhausted: SECRET_RESPONSE_BODY",
    }}, request=request)
    error = httpx.HTTPStatusError("SECRET_EXCEPTION", request=request, response=response)
    diagnostics = provider_failure_diagnostics(error, observed_at=datetime(2026, 10, 1, 5, tzinfo=UTC))
    assert diagnostics is not None
    assert diagnostics.error_code == "unknown"
    assert diagnostics.quota_axis == "unknown"
    assert diagnostics.quota_axis_source == "unknown"
    assert diagnostics.numeric_headers == {}
    assert diagnostics.reset_headers == {}
    assert diagnostics.retry_after is None
    serialized = json.dumps(asdict(diagnostics), allow_nan=False)
    assert "SECRET" not in serialized
    assert "Daily request" not in serialized


@pytest.mark.asyncio
@respx.mock
async def test_failure_diagnostics_survive_exhaustion_without_changing_retries(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from dataclasses import asdict
    from unittest.mock import AsyncMock

    from digest.llm import LLMProviderError, _request_state

    caplog.set_level("INFO", logger="digest.llm")
    config = _make_config([{"name": "groq", "model": "fixture", "role": ["summarize"]}])
    config.llm.max_retries = 1
    route = respx.post("https://api.groq.com/openai/v1/chat/completions").mock(side_effect=[
        httpx.Response(503, headers={"retry-after": "3"}, json={"error": {"code": "UNAVAILABLE"}}),
        httpx.Response(429, headers={
            "retry-after": "3600",
            "x-ratelimit-remaining-requests": "0",
            "x-ratelimit-remaining-tokens": "250",
            "x-request-id": "SECRET_RESPONSE_HEADER",
        }, json={"error": {"code": "rate_limit_exceeded", "message": "SECRET_RESPONSE_BODY"}}),
    ])
    state = _request_state(config)
    key = ("groq", "fixture")
    with (
        patch.dict("os.environ", {"GROQ_API_KEY": "SECRET_API_KEY"}),
        patch("digest.llm.asyncio.sleep", AsyncMock()) as sleep,
    ):
        with pytest.raises(RuntimeError, match="HTTP 429 code=rate_limit_exceeded") as failed:
            await complete(LLMRole.SUMMARIZE, [{"role": "user", "content": "SECRET_PROMPT"}], config)
        saved = state.failure_diagnostics[key]
        assert isinstance(failed.value, LLMProviderError)
        assert failed.value.diagnostics is saved
        assert saved.status_code == 429
        assert saved.error_code == "rate_limit_exceeded"
        assert saved.quota_axis == "unknown"
        assert saved.retry_after is not None
        assert saved.retry_after.seconds == 3600.0
        assert route.call_count == 2
        sleep.assert_awaited_once_with(3.0)
        assert "SECRET" not in str(asdict(saved)) + str(failed.value) + caplog.text

        # A skipped cooldown preserves the last actual response for inspection.
        with pytest.raises(LLMProviderError, match="All providers failed") as skipped:
            await complete(LLMRole.SUMMARIZE, [], config)
        assert skipped.value.diagnostics is None
        assert state.failure_diagnostics[key] is saved
        assert route.call_count == 2
        sleep.assert_awaited_once_with(3.0)

        # A new non-HTTP failure must not inherit the old response diagnostics.
        state.unavailable_until[key] = 0.0
        config.llm.max_retries = 0
        route.mock(side_effect=httpx.ConnectError("SECRET_TRANSPORT_ERROR"))
        with pytest.raises(LLMProviderError, match="ConnectError") as transport_failed:
            await complete(LLMRole.SUMMARIZE, [], config)
        assert transport_failed.value.diagnostics is None
    assert route.call_count == 3
    assert key not in state.failure_diagnostics
    assert "SECRET" not in caplog.text
