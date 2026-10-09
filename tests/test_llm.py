"""Tests for the LLM httpx wrapper (src/llm.py)."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import httpx
import pytest
import respx

from digest.adapters.models.execution import ModelExecution
from digest.llm import (
    LLMRole,
    _extract_json,
    _providers_for_role,
    complete,
    count_gemini_tokens,
    request_budget_remaining,
    request_wait_seconds,
    set_request_limit,
)


def _make_config(providers: list[dict[str, Any]]) -> Any:
    """Build a minimal config-like object with the given providers."""
    from digest.config import LLMConfig, ProviderConfig

    provider_objs = [ProviderConfig(name=p["name"], model=p["model"], role=p.get("role", [])) for p in providers]

    class _Cfg:
        llm = LLMConfig(providers=provider_objs)

    return _Cfg()


# ---------------------------------------------------------------------------
# _extract_json
# ---------------------------------------------------------------------------


def test_extract_json_direct() -> None:
    assert _extract_json('{"key": "value"}') == {"key": "value"}


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
    config = _make_config(
        [
            {"name": "groq", "model": "llama", "role": ["summarize", "fallback"]},
            {"name": "deepseek", "model": "ds-chat", "role": ["fallback"]},
        ]
    )
    providers = _providers_for_role(LLMRole.SUMMARIZE, config)
    assert [p.name for p in providers] == ["groq", "deepseek"]


def test_providers_for_role_fallback_only() -> None:
    config = _make_config(
        [
            {"name": "groq", "model": "llama", "role": ["summarize"]},
            {"name": "deepseek", "model": "ds-chat", "role": ["fallback"]},
        ]
    )
    providers = _providers_for_role(LLMRole.GENERATE_QUERIES, config)
    assert [p.name for p in providers] == ["deepseek"]


# ---------------------------------------------------------------------------
# complete() — OpenAI-compat (Groq)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_complete_falls_back_on_500() -> None:
    model_execution = ModelExecution()
    config = _make_config(
        [
            {"name": "groq", "model": "llama", "role": ["summarize"]},
            {"name": "deepseek", "model": "ds-chat", "role": ["fallback"]},
        ]
    )
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
    rejections = []
    with patch.dict("os.environ", {"GROQ_API_KEY": "k1", "DEEPSEEK_API_KEY": "k2"}):
        text, usage = await complete(
            LLMRole.SUMMARIZE, messages, config, execution=model_execution, http_rejections=rejections,
        )
    assert text == "DeepSeek fallback"

    assert usage == {"completion_tokens": 3} and [item.http_status for item in rejections] == [500]


@pytest.mark.asyncio
async def test_complete_skips_missing_api_key() -> None:
    model_execution = ModelExecution()
    config = _make_config(
        [
            {"name": "groq", "model": "llama", "role": ["summarize"]},
        ]
    )
    messages = [{"role": "user", "content": "test"}]
    with patch.dict("os.environ", {}, clear=True):
        # Remove GROQ_API_KEY from env
        import os

        env = {k: v for k, v in os.environ.items() if k != "GROQ_API_KEY"}
        with patch.dict("os.environ", env, clear=True):
            with pytest.raises(RuntimeError, match="All providers failed"):
                await complete(LLMRole.SUMMARIZE, messages, config, execution=model_execution)


@pytest.mark.asyncio
async def test_complete_raises_when_no_providers_for_role() -> None:
    model_execution = ModelExecution()
    config = _make_config(
        [
            {"name": "groq", "model": "llama", "role": ["summarize"]},
        ]
    )
    messages = [{"role": "user", "content": "test"}]
    with pytest.raises(RuntimeError, match="No providers configured"):
        await complete(LLMRole.RANK_SIGNALS, messages, config, execution=model_execution)


@pytest.mark.asyncio
@respx.mock
async def test_complete_all_providers_fail_raises() -> None:
    model_execution = ModelExecution()
    config = _make_config(
        [
            {"name": "groq", "model": "llama", "role": ["summarize"]},
            {"name": "deepseek", "model": "ds-chat", "role": ["fallback"]},
        ]
    )
    respx.post("https://api.groq.com/openai/v1/chat/completions").mock(
        return_value=httpx.Response(429, text="Rate limited")
    )
    respx.post("https://api.deepseek.com/v1/chat/completions").mock(return_value=httpx.Response(500, text="Error"))
    messages = [{"role": "user", "content": "test"}]
    with patch.dict("os.environ", {"GROQ_API_KEY": "k1", "DEEPSEEK_API_KEY": "k2"}):
        with pytest.raises(RuntimeError, match="All providers failed"):
            await complete(LLMRole.SUMMARIZE, messages, config, execution=model_execution)


@pytest.mark.asyncio
async def test_transient_error_retries_with_server_delay() -> None:
    model_execution = ModelExecution()
    from unittest.mock import AsyncMock

    cfg = _make_config([{"name": "groq", "model": "openai/gpt-oss-120b", "role": ["summarize"]}])
    cfg.llm.max_retries = 1
    response = httpx.Response(503, headers={"retry-after": "3"}, request=httpx.Request("POST", "https://example.com"))
    error = httpx.HTTPStatusError("unavailable", request=response.request, response=response)
    rejections = []
    with (
        patch("digest.llm._call_provider", AsyncMock(side_effect=[error, ("Recovered", {})])) as call,
        patch("digest.llm.asyncio.sleep", AsyncMock()) as sleep,
    ):
        text, _ = await complete(LLMRole.SUMMARIZE, [], cfg, execution=model_execution, http_rejections=rejections)
    assert text == "Recovered"
    assert call.await_count == 2
    sleep.assert_awaited_once_with(3.0)

    assert len(rejections) == 1 and rejections[0].http_status == 503 and rejections[0].retry_after_seconds == 3
    assert model_execution.request_state(cfg.llm).requests_attempted == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403])
async def test_permanent_errors_fall_back_without_retry_and_redact_body(
    status: int,
    caplog: pytest.LogCaptureFixture,
) -> None:
    model_execution = ModelExecution()
    from unittest.mock import AsyncMock

    cfg = _make_config(
        [
            {"name": "groq", "model": "old", "role": ["summarize"]},
            {"name": "gemini", "model": "working", "role": ["fallback"]},
        ]
    )
    cfg.llm.max_retries = 2
    response = httpx.Response(
        status,
        json={"error": {"code": "model_not_found", "message": "SECRET_PROMPT"}},
        request=httpx.Request("POST", "https://example.com"),
    )
    error = httpx.HTTPStatusError("SECRET_CREDENTIAL", request=response.request, response=response)
    with (
        patch("digest.llm._call_provider", AsyncMock(side_effect=[error, ("Fallback", {})])) as call,
        patch("digest.llm.asyncio.sleep", AsyncMock()) as sleep,
    ):
        text, _ = await complete(LLMRole.SUMMARIZE, [], cfg, execution=model_execution)
    assert text == "Fallback"
    assert call.await_count == 2
    sleep.assert_not_awaited()
    assert "model_not_found" in caplog.text
    assert "SECRET" not in caplog.text


@pytest.mark.asyncio
async def test_parallel_pipeline_calls_respect_shared_concurrency_limit() -> None:
    model_execution = ModelExecution()
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
        results = await asyncio.gather(
            *(
                complete(role, [], cfg, execution=model_execution)
                for role in [LLMRole.SUMMARIZE, LLMRole.GENERATE_QUERIES, LLMRole.RANK_SIGNALS]
            )
        )
    assert high_water == 1
    assert len(results) == 3


@pytest.mark.asyncio
async def test_request_spacing_is_shared_and_deterministic() -> None:
    import asyncio

    from digest.adapters.models.execution import RequestState
    from digest.llm import _pace_request

    state = RequestState(asyncio.get_running_loop(), asyncio.Semaphore(1))
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
async def test_provider_failures_are_shared_across_subsequent_calls(
    status: int,
    caplog: pytest.LogCaptureFixture,
) -> None:
    model_execution = ModelExecution()
    from unittest.mock import AsyncMock

    cfg = _make_config(
        [
            {"name": "groq", "model": "unavailable", "role": ["summarize"]},
            {"name": "gemini", "model": "working", "role": ["fallback"]},
        ]
    )
    cfg.llm.max_retries = 2
    response = httpx.Response(
        status,
        headers={"retry-after": "3600"} if status == 429 else {},
        json={"error": {"code": "model_not_found", "message": "SECRET_PROMPT"}},
        request=httpx.Request("POST", "https://example.com"),
    )
    error = httpx.HTTPStatusError("SECRET_CREDENTIAL", request=response.request, response=response)
    with (
        patch("digest.llm._call_provider", AsyncMock(side_effect=[error, ("first", {}), ("second", {})])) as call,
        patch("digest.llm.asyncio.sleep", AsyncMock()) as sleep,
    ):
        text, _ = await complete(LLMRole.SUMMARIZE, [], cfg, execution=model_execution)
        assert text == "first"
        assert call.await_count == 2
        sleep.assert_not_awaited()
        assert "model_not_found" in caplog.text
        assert "SECRET" not in caplog.text
        await complete(LLMRole.SUMMARIZE, [], cfg, execution=model_execution)
    sleep.assert_not_awaited()
    assert [c.args[1].name for c in call.await_args_list] == ["groq", "gemini", "gemini"]


def test_machine_error_code_does_not_echo_arbitrary_response_strings() -> None:
    from digest.llm import _safe_provider_error

    response = httpx.Response(
        404, json={"error": {"code": "sk-secret-token"}}, request=httpx.Request("POST", "https://example.com")
    )
    error = httpx.HTTPStatusError("secret", request=response.request, response=response)
    assert _safe_provider_error(error) == "HTTP 404 code=unknown"


@pytest.mark.asyncio
async def test_queued_call_rechecks_cooldown_after_waiting_for_request_slot() -> None:
    model_execution = ModelExecution()
    from unittest.mock import AsyncMock

    from digest.llm import _request_state

    cfg = _make_config(
        [
            {"name": "groq", "model": "busy", "role": ["summarize"]},
            {"name": "gemini", "model": "working", "role": ["fallback"]},
        ]
    )
    state = _request_state(cfg, model_execution)
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
        assert (await complete(LLMRole.SUMMARIZE, [], cfg, execution=model_execution))[0] == "Fallback"
    assert [call.args[1].name for call in provider.await_args_list] == ["gemini"]


@pytest.mark.asyncio
async def test_explicit_review_model_never_uses_configured_fallback() -> None:
    model_execution = ModelExecution()
    from unittest.mock import AsyncMock

    from digest.config import ProviderConfig

    config = _make_config(
        [
            {"name": "gemini", "model": "primary", "role": ["fallback"]},
            {"name": "deepseek", "model": "paid", "role": ["fallback"]},
        ]
    )
    with patch("digest.llm._call_provider", AsyncMock(side_effect=ValueError("invalid response"))) as call:
        with pytest.raises(RuntimeError):
            await complete(
                LLMRole.REVIEW_EVIDENCE,
                [],
                config,
                execution=model_execution,
                provider_override=ProviderConfig("groq", "openai/gpt-oss-120b"),
            )
    assert call.await_count == 1
    assert call.await_args.args[1].name == "groq"


@pytest.mark.asyncio
async def test_review_output_budget_reaches_all_provider_transports() -> None:
    import json

    from digest.llm import _anthropic_call, _gemini_call, _openai_compat_call

    with respx.mock:
        groq = respx.post("https://api.groq.com/openai/v1/chat/completions").mock(
            return_value=httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
        )
        gemini = respx.post("https://generativelanguage.googleapis.com/v1beta/models/fixture:generateContent").mock(
            return_value=httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "ok"}]}}]})
        )
        anthropic = respx.post("https://api.anthropic.com/v1/messages").mock(
            return_value=httpx.Response(200, json={"content": [{"text": "ok"}]})
        )
        async with httpx.AsyncClient() as client:
            await _openai_compat_call(client, "https://api.groq.com/openai/v1", "fake", "fixture", [], 0.2, 2048)
            await _gemini_call(client, "fake", "fixture", [], 0.2, 2048)
            await _anthropic_call(client, "fake", "fixture", [], 0.2, 2048)
    assert json.loads(groq.calls.last.request.content)["max_completion_tokens"] == 2048
    assert json.loads(gemini.calls.last.request.content)["generationConfig"]["maxOutputTokens"] == 2048
    assert json.loads(anthropic.calls.last.request.content)["max_tokens"] == 2048


@pytest.mark.asyncio
@respx.mock
async def test_gemini_preflight_counts_the_exact_generation_request() -> None:
    model_execution = ModelExecution()
    import json

    config = _make_config([{"name": "gemini", "model": "configured-model", "role": ["fallback"]}])
    provider = config.llm.providers[0]
    count_route = respx.post(
        "https://generativelanguage.googleapis.com/v1beta/models/configured-model:countTokens",
    ).respond(200, json={"totalTokens": 123})
    generate_route = respx.post(
        "https://generativelanguage.googleapis.com/v1beta/models/configured-model:generateContent",
    ).respond(200, json={"candidates": [{"content": {"parts": [{"text": "ok"}]}}]})
    messages = [
        {"role": "system", "content": "First instruction"},
        {"role": "user", "content": "First message"},
        {"role": "assistant", "content": "Earlier answer"},
        {"role": "system", "content": "Second instruction"},
        {"role": "user", "content": "Full source text without truncation"},
    ]
    with patch.dict("os.environ", {"GEMINI_API_KEY": "fixture-key"}):
        assert (
            await count_gemini_tokens(
                messages,
                config,
                execution=model_execution,
                provider_override=provider,
                temperature=0.15,
                max_output_tokens=3456,
            )
            == 123
        )
        await complete(
            LLMRole.REVIEW_EVIDENCE,
            messages,
            config,
            execution=model_execution,
            provider_override=provider,
            temperature=0.15,
            max_output_tokens=3456,
        )
    generated = json.loads(generate_route.calls.last.request.content)
    counted = json.loads(count_route.calls.last.request.content)
    assert counted == {"generateContentRequest": {**generated, "model": "models/configured-model"}}
    assert generated == {
        "contents": [
            {"role": "user", "parts": [{"text": "First message"}]},
            {"role": "model", "parts": [{"text": "Earlier answer"}]},
            {"role": "user", "parts": [{"text": "Full source text without truncation"}]},
        ],
        "systemInstruction": {"parts": [{"text": "First instruction"}, {"text": "Second instruction"}]},
        "generationConfig": {"temperature": 0.15, "maxOutputTokens": 3456},
    }
    request = count_route.calls.last.request
    assert request.headers["x-goog-api-key"] == "fixture-key"
    assert request.url.query == b""
    assert set(request.extensions["timeout"].values()) == {10.0}


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize(
    "body",
    [
        None,
        [],
        {},
        {"totalTokens": 0},
        {"totalTokens": -1},
        {"totalTokens": True},
        {"totalTokens": "12"},
        {"totalTokens": 1.5},
    ],
)
async def test_gemini_preflight_rejects_invalid_count_without_retry(body: Any) -> None:
    model_execution = ModelExecution()
    config = _make_config([{"name": "gemini", "model": "fixture", "role": ["fallback"]}])
    config.llm.max_retries = 3
    route = respx.post(
        "https://generativelanguage.googleapis.com/v1beta/models/fixture:countTokens",
    ).respond(200, json=body)
    set_request_limit(config, model_execution, 2)
    with patch.dict("os.environ", {"GEMINI_API_KEY": "fixture-key"}):
        with pytest.raises(RuntimeError, match="Gemini token preflight failed: (ValueError|JSONDecodeError)"):
            await count_gemini_tokens(
                [{"role": "user", "content": "Nonempty source"}],
                config,
                execution=model_execution,
                provider_override=config.llm.providers[0],
            )
    assert route.call_count == 1
    assert request_budget_remaining(config, model_execution) == 1


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize("status", [404, 429, 503])
async def test_gemini_preflight_failure_has_no_retry_fallback_or_sensitive_error(
    status: int,
    caplog: pytest.LogCaptureFixture,
) -> None:
    model_execution = ModelExecution()
    config = _make_config(
        [
            {"name": "gemini", "model": "fixture", "role": ["summarize"]},
            {"name": "deepseek", "model": "fallback", "role": ["fallback"]},
        ]
    )
    config.llm.max_retries = 3
    route = respx.post(
        "https://generativelanguage.googleapis.com/v1beta/models/fixture:countTokens",
    ).respond(status, json={"error": {"message": "SECRET_BODY", "code": "SECRET_CODE"}})
    with patch.dict("os.environ", {"GEMINI_API_KEY": "SECRET_KEY"}):
        with pytest.raises(RuntimeError) as error:
            await count_gemini_tokens(
                [{"role": "user", "content": "SECRET_PROMPT"}],
                config,
                execution=model_execution,
                provider_override=config.llm.providers[0],
            )
        assert str(error.value) == f"Gemini token preflight failed: HTTP {status} code=unknown"
        if status in {404, 429}:
            with pytest.raises(RuntimeError, match="All providers failed"):
                await complete(
                    LLMRole.SUMMARIZE, [], config, execution=model_execution, provider_override=config.llm.providers[0]
                )
    assert route.call_count == 1
    assert len(respx.calls) == 1
    assert "SECRET" not in caplog.text


@pytest.mark.asyncio
@respx.mock
async def test_count_and_generation_share_the_request_cap_under_concurrency() -> None:
    model_execution = ModelExecution()
    import asyncio

    config = _make_config([{"name": "gemini", "model": "fixture", "role": ["fallback"]}])
    provider = config.llm.providers[0]
    count_route = respx.post(
        "https://generativelanguage.googleapis.com/v1beta/models/fixture:countTokens",
    ).respond(200, json={"totalTokens": 7})
    generate_route = respx.post(
        "https://generativelanguage.googleapis.com/v1beta/models/fixture:generateContent",
    ).respond(200, json={"candidates": [{"content": {"parts": [{"text": "ok"}]}}]})
    set_request_limit(config, model_execution, 3)
    messages = [{"role": "user", "content": "source"}]
    with patch.dict("os.environ", {"GEMINI_API_KEY": "fixture-key"}):
        results = await asyncio.gather(
            count_gemini_tokens(messages, config, execution=model_execution, provider_override=provider),
            complete(LLMRole.SUMMARIZE, messages, config, execution=model_execution),
            complete(LLMRole.REVIEW_EVIDENCE, messages, config, execution=model_execution, provider_override=provider),
            count_gemini_tokens(messages, config, execution=model_execution, provider_override=provider),
            complete(LLMRole.REVIEW_EVIDENCE, messages, config, execution=model_execution, provider_override=provider),
            return_exceptions=True,
        )
    assert count_route.call_count + generate_route.call_count == 3
    failures = [result for result in results if isinstance(result, Exception)]
    assert len(failures) == 2
    assert all(isinstance(error, RuntimeError) and "budget exhausted" in str(error) for error in failures)
    assert request_budget_remaining(config, model_execution) == 0
    set_request_limit(config, model_execution, 3)
    assert request_budget_remaining(config, model_execution) == 0


@pytest.mark.asyncio
@respx.mock
async def test_request_cap_counts_failed_attempts_and_prevents_retry_and_fallback() -> None:
    model_execution = ModelExecution()
    from unittest.mock import AsyncMock

    config = _make_config(
        [
            {"name": "groq", "model": "fixture", "role": ["summarize"]},
            {"name": "gemini", "model": "fallback", "role": ["fallback"]},
        ]
    )
    config.llm.max_retries = 2
    route = respx.post("https://api.groq.com/openai/v1/chat/completions").respond(503)
    set_request_limit(config, model_execution, 2)
    with patch.dict("os.environ", {"GROQ_API_KEY": "fixture-key"}), patch("digest.llm.asyncio.sleep", AsyncMock()):
        with pytest.raises(RuntimeError, match="budget exhausted"):
            await complete(LLMRole.SUMMARIZE, [], config, execution=model_execution)
    assert route.call_count == 2
    assert len(respx.calls) == 2
    assert request_budget_remaining(config, model_execution) == 0


@pytest.mark.asyncio
@respx.mock
async def test_legacy_calls_are_unlimited_by_default() -> None:
    model_execution = ModelExecution()
    config = _make_config([{"name": "gemini", "model": "fixture", "role": ["fallback"]}])
    route = respx.post(
        "https://generativelanguage.googleapis.com/v1beta/models/fixture:generateContent",
    ).respond(200, json={"candidates": [{"content": {"parts": [{"text": "ok"}]}}]})
    assert request_budget_remaining(config, model_execution) is None
    with patch.dict("os.environ", {"GEMINI_API_KEY": "fixture-key"}):
        for _ in range(13):
            assert (await complete(LLMRole.SUMMARIZE, [], config, execution=model_execution))[0] == "ok"
    assert route.call_count == 13
    assert request_budget_remaining(config, model_execution) is None


@pytest.mark.asyncio
@respx.mock
async def test_gemini_preflight_shares_semaphore_and_rechecks_cooldown_after_pacing() -> None:
    model_execution = ModelExecution()
    import asyncio
    from unittest.mock import AsyncMock

    from digest.llm import _request_state

    config = _make_config([{"name": "gemini", "model": "fixture", "role": ["fallback"]}])
    config.llm.max_concurrent_requests = 1
    config.llm.min_request_interval_seconds = 0.5
    state = _request_state(config, model_execution)
    set_request_limit(config, model_execution, 2)

    async def pace(*args: object) -> None:
        state.unavailable_until[("gemini", "fixture")] = float("inf")

    with patch("digest.llm._pace_request", AsyncMock(side_effect=pace)) as pacing:
        async with state.semaphore:
            task = asyncio.create_task(
                count_gemini_tokens(
                    [{"role": "user", "content": "source"}],
                    config,
                    execution=model_execution,
                    provider_override=config.llm.providers[0],
                )
            )
            await asyncio.sleep(0)
            pacing.assert_not_awaited()
        with pytest.raises(RuntimeError, match="unavailable"):
            await task
    pacing.assert_awaited_once_with(state, 0.5)
    assert request_budget_remaining(config, model_execution) == 2
    assert len(respx.calls) == 0


@pytest.mark.asyncio
@respx.mock
async def test_gemini_preflight_rejects_missing_credentials_and_empty_or_wrong_provider_requests() -> None:
    model_execution = ModelExecution()
    config = _make_config(
        [
            {"name": "gemini", "model": "fixture", "role": ["summarize"]},
            {"name": "groq", "model": "fixture", "role": ["fallback"]},
        ]
    )
    set_request_limit(config, model_execution, 1)
    messages = [{"role": "user", "content": "source"}]
    with patch.dict("os.environ", {}, clear=True):
        with pytest.raises(ValueError, match="nonempty request"):
            await count_gemini_tokens([], config, execution=model_execution, provider_override=config.llm.providers[0])
        with pytest.raises(ValueError, match="configured Gemini"):
            await count_gemini_tokens(
                messages, config, execution=model_execution, provider_override=config.llm.providers[1]
            )
        assert request_budget_remaining(config, model_execution) == 1
        with pytest.raises(RuntimeError, match="GEMINI_API_KEY not set"):
            await count_gemini_tokens(
                messages, config, execution=model_execution, provider_override=config.llm.providers[0]
            )
        assert request_budget_remaining(config, model_execution) == 0
        with pytest.raises(RuntimeError, match="budget exhausted"):
            await complete(LLMRole.SUMMARIZE, messages, config, execution=model_execution)
    assert len(respx.calls) == 0


@pytest.mark.parametrize("limit", [-1, True, 1.5, "3"])
def test_request_limit_requires_a_nonnegative_integer(limit: Any) -> None:
    model_execution = ModelExecution()
    with pytest.raises(ValueError, match="nonnegative integer"):
        set_request_limit(_make_config([]), model_execution, limit)


@pytest.mark.asyncio
async def test_request_wait_seconds_reports_only_current_pacing_delay() -> None:
    model_execution = ModelExecution()
    from digest.llm import _request_state

    config = _make_config([])
    state = _request_state(config, model_execution)
    with patch("digest.llm.time.monotonic", return_value=100.0):
        assert request_wait_seconds(config, model_execution) == 0.0
        state.next_request_at = 103.5
        assert request_wait_seconds(config, model_execution) == 3.5
        state.next_request_at = 99.0
        assert request_wait_seconds(config, model_execution) == 0.0


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize(
    ("name", "url", "default"),
    [
        ("groq", "https://api.groq.com/openai/v1/chat/completions", 60.0),
        ("gemini", "https://generativelanguage.googleapis.com/v1beta/models/fixture:generateContent", 120.0),
        ("anthropic", "https://api.anthropic.com/v1/messages", 120.0),
    ],
)
@pytest.mark.parametrize("requested", [None, 37.25, 999.0])
async def test_request_timeout_reaches_transport_and_never_extends_default(
    name: str,
    url: str,
    default: float,
    requested: float | None,
) -> None:
    model_execution = ModelExecution()
    config = _make_config([{"name": name, "model": "fixture", "role": ["summarize"]}])
    route = respx.post(url).respond(
        200,
        json={
            "choices": [{"message": {"content": "ok"}}],
            "candidates": [{"content": {"parts": [{"text": "ok"}]}}],
            "content": [{"text": "ok"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            "usageMetadata": {"promptTokenCount": 20, "candidatesTokenCount": 10},
        },
    )
    kwargs = {"request_timeout_seconds": requested} if requested is not None else {}
    with patch.dict("os.environ", {f"{name.upper()}_API_KEY": "fixture-key"}):
        text, usage = await complete(
            LLMRole.SUMMARIZE,
            [{"role": "user", "content": "Summarize this"}],
            config,
            execution=model_execution,
            **kwargs,
        )
    assert text == "ok"
    if name in {"groq", "gemini"}:
        assert usage["completion_tokens"] == (5 if name == "groq" else 10)
    expected = default if requested is None else min(default, requested)
    assert set(route.calls.last.request.extensions["timeout"].values()) == {expected}


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize("requested", [0.0, -1.0, float("inf"), float("nan"), True])
async def test_invalid_request_timeout_is_rejected_before_http(requested: float) -> None:
    model_execution = ModelExecution()
    config = _make_config([{"name": "gemini", "model": "fixture", "role": ["summarize"]}])
    with pytest.raises(ValueError, match="finite and greater than zero"):
        await complete(LLMRole.SUMMARIZE, [], config, execution=model_execution, request_timeout_seconds=requested)
    assert len(respx.calls) == 0


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize("requested", [None, 30.0])
async def test_explicit_request_timeout_prevents_retry_while_legacy_timeout_still_retries(
    requested: float | None,
) -> None:
    model_execution = ModelExecution()
    from unittest.mock import AsyncMock

    config = _make_config([{"name": "gemini", "model": "fixture", "role": ["summarize"]}])
    config.llm.max_retries = 1
    route = respx.post(
        "https://generativelanguage.googleapis.com/v1beta/models/fixture:generateContent",
    ).mock(
        side_effect=[
            httpx.ReadTimeout("fixture timeout"),
            httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}),
        ]
    )
    rejections = []
    with (
        patch.dict("os.environ", {"GEMINI_API_KEY": "fixture-key"}),
        patch("digest.llm.asyncio.sleep", AsyncMock()) as sleep,
    ):
        if requested is None:
            assert (await complete(LLMRole.SUMMARIZE, [], config, execution=model_execution,
                                   http_rejections=rejections))[0] == "ok"
            assert route.call_count == 2
            sleep.assert_awaited_once()
        else:
            with pytest.raises(RuntimeError, match="ReadTimeout"):
                await complete(
                    LLMRole.SUMMARIZE,
                    [],
                    config,
                    execution=model_execution,
                    provider_override=config.llm.providers[0],
                    request_timeout_seconds=requested,
                    http_rejections=rejections,
                )
            assert route.call_count == 1
            sleep.assert_not_awaited()

    assert rejections == []


@pytest.mark.asyncio
async def test_gemini_preserves_all_final_parts_and_omits_hidden_metadata() -> None:
    from digest.llm import _gemini_call

    payload = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {"text": "synthetic hidden content", "thought": True},
                        {"text": "First statement. ", "thoughtSignature": "synthetic-opaque-signature"},
                        {"thoughtSignature": "synthetic-metadata-only"},
                        {"text": "Only enrolled customers qualify.", "partMetadata": {"fixture": True}},
                    ]
                },
                "finishReason": "STOP",
            }
        ],
        "modelVersion": "fixture-model",
        "usageMetadata": {
            "promptTokenCount": 12,
            "candidatesTokenCount": 8,
        },
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)),
        trust_env=False,
    ) as client:
        text, usage = await _gemini_call(client, "synthetic", "fixture", [], 0.1, 2048)
    assert text == "First statement. Only enrolled customers qualify."
    assert usage == {
        "prompt_tokens": 12,
        "completion_tokens": 8,
        "finish_reason": "STOP",
        "resolved_model": "fixture-model",
    }
    assert "hidden" not in text and "signature" not in text


@pytest.mark.parametrize(
    "parts",
    [
        None,
        [],
        [{}],
        ["invalid"],
        [{"text": None}],
        [{"text": " "}],
        [{"text": "synthetic hidden content", "thought": True}],
        [{"text": "partial"}, {"functionCall": {"name": "do_not_execute"}}],
        [{"text": "partial"}, {"inlineData": {"data": "synthetic"}}],
        [{"text": "partial", "thought": "false"}],
    ],
)
def test_gemini_final_parts_fail_closed_without_disclosing_response(parts) -> None:
    from digest.llm import _gemini_final_text

    with pytest.raises(ValueError) as error:
        _gemini_final_text({"content": {"parts": parts}})
    assert "synthetic" not in str(error.value) and "do_not_execute" not in str(error.value)
    assert "partial" not in str(error.value)


@pytest.mark.asyncio
async def test_stricter_stage_pacing_covers_previous_start_and_deadline_without_reset() -> None:
    model_execution = ModelExecution()
    import copy

    from digest import llm
    from digest.reading_brief import _generation_timeout

    config = _make_config([{"name": "gemini", "model": "fixture", "role": ["summarize"]}])
    config.llm.min_request_interval_seconds = 20
    llm.set_request_limit(config, model_execution, 2)
    state = llm._request_state(config, model_execution)
    stage_execution = model_execution.share_initialized(config.llm)
    stage = copy.copy(config)
    stage.llm = copy.copy(config.llm)
    stage.llm.min_request_interval_seconds = 65
    clock = [1000.0]
    waits = []

    async def sleep(delay: float) -> None:
        waits.append(delay)
        clock[0] += delay

    with (
        patch("digest.llm.time.monotonic", side_effect=lambda: clock[0]),
        patch("digest.llm.asyncio.sleep", side_effect=sleep),
    ):
        await llm._pace_request(state, 20)
        llm._reserve_request(state)
        clock[0] = 1005
        assert llm.request_wait_seconds(stage, stage_execution) == 60
        with pytest.raises(TimeoutError, match="technical_deadline"):
            _generation_timeout(stage, 1090, execution=stage_execution)
        assert not waits and llm.request_budget_remaining(stage, stage_execution) == 1
        await llm._pace_request(state, 65)
        assert clock[0] == state.last_request_at == 1065 and waits == [60]
        assert state.next_request_at == 1130 and llm.request_budget_remaining(config, model_execution) == 1


@pytest.mark.parametrize("retry,quota,expected", [
    (" \t86400.0\t ", "9223372036854775807", (86400.0, 2**63 - 1)),
    ("86400.1", "9223372036854775808", (None, None)),
    ("1e2", "１２", (None, None)),
    ("9" * 33, "9" * 20, (None, None)),
    ("\u00a01\u00a0", "\u00a01\u00a0", (None, None)),
])
def test_rejection_metadata_is_bounded_and_invalid_values_stay_unknown(retry, quota, expected) -> None:
    from dataclasses import FrozenInstanceError

    from digest.llm import _http_rejection

    response = httpx.Response(429, json={"error": {"code": "HOSTILE", "message": "HOSTILE"}}, headers={
        "retry-after": retry.encode("utf-8"), "x-ratelimit-limit-tokens": quota.encode("utf-8"),
        "x-ratelimit-remaining-requests": "0",
    })
    facts = _http_rejection(response)
    assert (facts.retry_after_seconds, facts.rate_limit_limit_tokens) == expected
    assert facts.http_status == 429 and facts.provider_code is None and facts.rate_limit_remaining_requests == 0
    assert facts.rate_limit_limit_requests is facts.rate_limit_remaining_tokens is None
    with pytest.raises(FrozenInstanceError):
        facts.http_status = 200


async def test_rejection_projector_failure_preserves_terminal_error_and_cooldown() -> None:
    from unittest.mock import AsyncMock

    cfg = _make_config([{"name": "groq", "model": "model", "role": ["summarize"]}])
    execution = ModelExecution()
    response = httpx.Response(403, json={"error": {"code": "permission_denied"}},
                              request=httpx.Request("POST", "https://example.com"))
    error = httpx.HTTPStatusError("HOSTILE", request=response.request, response=response)
    rejections = []
    with patch("digest.llm._call_provider", AsyncMock(side_effect=error)) as call, patch(
        "digest.llm._http_rejection", side_effect=ValueError("projection failed")
    ):
        with pytest.raises(RuntimeError) as caught:
            await complete(LLMRole.SUMMARIZE, [], cfg, execution=execution, http_rejections=rejections)
    assert type(caught.value) is RuntimeError
    assert str(caught.value) == "All providers failed for role 'summarize'. Last error: HTTP 403 code=permission_denied"
    assert rejections == [] and call.await_count == execution.request_state(cfg.llm).requests_attempted == 1
    assert execution.request_state(cfg.llm).unavailable_until[("groq", "model")] == float("inf")
