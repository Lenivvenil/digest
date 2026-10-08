"""Review-only controls reach the real HTTP adapter without changing other calls."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from digest import llm
from digest.adapters.models.execution import ModelExecution
from digest.adapters.models.review import groq_review_response_format
from digest.config import LLMConfig, ProviderConfig

MESSAGES = [
    {"role": "system", "content": "Return evidence review JSON."},
    {"role": "user", "content": "Supplied evidence packet."},
]
RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "evidence_review",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {"limitations": {"type": "array", "items": {"type": "string"}}},
            "required": ["limitations"],
            "additionalProperties": False,
        },
    },
}


def _config(provider: ProviderConfig) -> SimpleNamespace:
    return SimpleNamespace(llm=LLMConfig(providers=[provider]))


def _response(headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(200, headers=headers, json={
        "choices": [{"message": {"content": '{"limitations":[]}'}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 123, "completion_tokens": 45},
        "model": "openai/gpt-oss-120b",
    })


@pytest.mark.asyncio
async def test_review_controls_reach_actual_http_request_without_changing_cap_or_messages() -> None:
    execution = ModelExecution()
    provider = ProviderConfig("groq", "openai/gpt-oss-120b", ["review_evidence"])
    config = _config(provider)
    response_format = groq_review_response_format()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _response({
            "x-ratelimit-limit-requests": "30",
            "x-ratelimit-remaining-requests": "0",
            "x-ratelimit-limit-tokens": "8000",
            "x-ratelimit-remaining-tokens": "7824",
            "x-ratelimit-reset-tokens": "7s",
            "x-unrelated-secret": "must-not-be-retained",
        })

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False)
    llm.set_request_limit(config, execution, 2)
    with (patch("digest.llm.httpx.AsyncClient", return_value=client),
          patch.dict("os.environ", {"GROQ_API_KEY": "fixture-key"})):
        text, usage = await llm.complete(
            llm.LLMRole.REVIEW_EVIDENCE, MESSAGES, config, temperature=0.2,
            provider_override=provider, max_output_tokens=4096,
            reasoning_effort="low", response_format=response_format,
            execution=execution,
        )

    assert len(requests) == 1
    request = requests[0]
    assert str(request.url) == "https://api.groq.com/openai/v1/chat/completions"
    assert json.loads(request.content) == {
        "model": provider.model, "messages": MESSAGES, "temperature": 0.2,
        "max_completion_tokens": 4096, "reasoning_effort": "low", "response_format": response_format,
    }
    assert set(request.extensions["timeout"].values()) == {60.0}
    assert text == '{"limitations":[]}'
    assert usage == {
        "prompt_tokens": 123, "completion_tokens": 45,
        "finish_reason": "stop", "resolved_model": provider.model,
        "rate_limit_limit_requests": 30, "rate_limit_remaining_requests": 0,
        "rate_limit_limit_tokens": 8000, "rate_limit_remaining_tokens": 7824,
    }
    assert llm.request_budget_remaining(config, execution) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("role", list(llm.LLMRole))
async def test_omitted_controls_keep_all_groq_roles_wire_unchanged(role: llm.LLMRole) -> None:
    execution = ModelExecution()
    provider = ProviderConfig("groq", "openai/gpt-oss-120b", [role.value])
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _response({"x-ratelimit-remaining-tokens": "10"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False)
    with (patch("digest.llm.httpx.AsyncClient", return_value=client),
          patch.dict("os.environ", {"GROQ_API_KEY": "fixture-key"})):
        _, usage = await llm.complete(role, MESSAGES, _config(provider), max_output_tokens=4096, execution=execution)
    assert len(requests) == 1
    assert json.loads(requests[0].content) == {
        "model": provider.model, "messages": MESSAGES, "temperature": 0.3, "max_completion_tokens": 4096,
    }
    assert not any(key.startswith("rate_limit_") for key in usage)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["deepseek", "mistral", "gemini", "anthropic"])
async def test_default_controls_do_not_reach_other_providers(name: str) -> None:
    execution = ModelExecution()
    provider = ProviderConfig(name, "fixture-model", ["review_evidence"])
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "ok"}}],
            "candidates": [{"content": {"parts": [{"text": "ok"}]}}],
            "content": [{"text": "ok"}],
        })

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False)
    with (patch("digest.llm.httpx.AsyncClient", return_value=client),
          patch.dict("os.environ", {f"{name.upper()}_API_KEY": "fixture-key"})):
        assert (await llm.complete(
            llm.LLMRole.REVIEW_EVIDENCE, MESSAGES, _config(provider),
            provider_override=provider, max_output_tokens=4096, reasoning_effort=None, response_format=None,
         execution=execution))[0] == "ok"
    assert len(bodies) == 1
    body = bodies[0]
    assert "reasoning_effort" not in body and "response_format" not in body
    if name == "gemini":
        assert body["generationConfig"] == {"temperature": 0.3, "maxOutputTokens": 4096}
    else:
        assert body["max_tokens"] == 4096


@pytest.mark.asyncio
@pytest.mark.parametrize(("role", "provider", "controls"), [
    (llm.LLMRole.SUMMARIZE, ProviderConfig("groq", "openai/gpt-oss-120b"), {"reasoning_effort": "low"}),
    (llm.LLMRole.REVIEW_EVIDENCE, None, {"reasoning_effort": "low"}),
    (llm.LLMRole.REVIEW_EVIDENCE, ProviderConfig("groq", "openai/gpt-oss-20b"), {"reasoning_effort": "low"}),
    (llm.LLMRole.REVIEW_EVIDENCE, ProviderConfig("gemini", "fixture"), {"response_format": RESPONSE_FORMAT}),
    (llm.LLMRole.REVIEW_EVIDENCE, ProviderConfig("deepseek", "openai/gpt-oss-120b"), {"reasoning_effort": "low"}),
    (llm.LLMRole.REVIEW_EVIDENCE, ProviderConfig("groq", "openai/gpt-oss-120b"), {"reasoning_effort": "medium"}),
    (llm.LLMRole.REVIEW_EVIDENCE, ProviderConfig("groq", "openai/gpt-oss-120b"), {"reasoning_effort": True}),
    (llm.LLMRole.REVIEW_EVIDENCE, ProviderConfig("groq", "openai/gpt-oss-120b"), {"response_format": {}}),
    (llm.LLMRole.REVIEW_EVIDENCE, ProviderConfig("groq", "openai/gpt-oss-120b"),
     {"response_format": {"type": "json_object"}}),
    (llm.LLMRole.REVIEW_EVIDENCE, ProviderConfig("groq", "openai/gpt-oss-120b"),
     {"response_format": {"type": "json_schema", "json_schema": {"name": "review", "schema": {}}}}),
])
async def test_unsupported_controls_fail_before_reservation_or_client(
    role: llm.LLMRole, provider: ProviderConfig | None, controls: dict[str, Any],
) -> None:
    execution = ModelExecution()
    config = _config(ProviderConfig("groq", "openai/gpt-oss-120b", ["review_evidence"]))
    with (patch("digest.llm.httpx.AsyncClient", side_effect=AssertionError("HTTP forbidden")) as client,
          patch("digest.llm._reserve_request", side_effect=AssertionError("Reservation forbidden")) as reserve):
        with pytest.raises(ValueError, match="Review"):
            await llm.complete(role, MESSAGES, config, provider_override=provider, **controls, execution=execution)
    client.assert_not_called()
    reserve.assert_not_called()
    assert execution._state is None


@pytest.mark.asyncio
async def test_invalid_or_absent_rate_limit_headers_stay_unknown() -> None:
    execution = ModelExecution()
    provider = ProviderConfig("groq", "openai/gpt-oss-120b", ["review_evidence"])
    response = _response({
        "x-ratelimit-limit-requests": "-1",
        "x-ratelimit-remaining-requests": "1.2",
        "x-ratelimit-limit-tokens": "9" * 5000,
    })
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: response), trust_env=False)
    with (patch("digest.llm.httpx.AsyncClient", return_value=client),
          patch.dict("os.environ", {"GROQ_API_KEY": "fixture-key"})):
        _, usage = await llm.complete(
            llm.LLMRole.REVIEW_EVIDENCE, MESSAGES, _config(provider),
            provider_override=provider, reasoning_effort="low",
            execution=execution,
        )
    assert not any(key.startswith("rate_limit_") for key in usage)


@pytest.mark.asyncio
async def test_schema_rejection_does_not_retry_or_fall_back() -> None:
    execution = ModelExecution()
    provider = ProviderConfig("groq", "openai/gpt-oss-120b", ["review_evidence"])
    config = _config(provider)
    config.llm.providers.append(ProviderConfig("gemini", "fixture", ["fallback"]))
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(400, json={"error": {"code": "invalid_schema", "message": "private error body"}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False)
    with (patch("digest.llm.httpx.AsyncClient", return_value=client),
          patch.dict("os.environ", {"GROQ_API_KEY": "fixture-key", "GEMINI_API_KEY": "fixture-key"})):
        with pytest.raises(RuntimeError, match="HTTP 400 code=unknown"):
            await llm.complete(
                llm.LLMRole.REVIEW_EVIDENCE, MESSAGES, config, provider_override=provider,
                max_output_tokens=4096, reasoning_effort="low", response_format=RESPONSE_FORMAT,
                execution=execution,
            )
    assert len(requests) == 1
    assert json.loads(requests[0].content)["response_format"] == RESPONSE_FORMAT
