"""Offline checks for shared, actual-request-bound source admission."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import asdict
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from digest import llm, source_admission
from digest.adapters.models.execution import ModelExecution
from digest.config import ProviderConfig
from digest.reading_brief_tokens import ESTIMATOR_VERSION, GPT_HASH, TokenProfileUnavailable
from digest.source_admission import admit_request, estimate_record, route_profile, wire_request
from scripts.review_fixture import fixture_config

MESSAGES = [{"role": "system", "content": "Inspect all source passages."},
            {"role": "user", "content": "Opening claim.\nLate qualification: not guaranteed. Ω"}]
GEMINI = ProviderConfig("gemini", "gemini-3.8-flash")
GROQ = ProviderConfig("groq", "openai/gpt-oss-120b")
COUNT_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.8-flash:countTokens"


@pytest.fixture(autouse=True)
def offline_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "fixture-key")


def test_profiles_preserve_source_limits_and_output_reserve() -> None:
    gemini = route_profile(GEMINI.name, GEMINI.model, 2048)
    groq = route_profile(GROQ.name, GROQ.model, 2048)
    assert gemini is not None and gemini.input_tokens == 1_048_576
    assert groq is not None and groq.input_tokens == 5952
    assert estimate_record(groq, 1001) == {
        "method": ESTIMATOR_VERSION, "tokenizer_sha256": GPT_HASH,
        "local_input_count": 1001, "input_estimate": 1458,
        "framing_reserve": 256, "output_reserve": 2048, "request_allowance": 8000,
    }
    assert route_profile(GROQ.name, GROQ.model, 8000) is None
    assert route_profile("groq", "unverified-model", 2048) is None
    assert route_profile("deepseek", "deepseek-chat", 2048) is None


@pytest.mark.asyncio
@respx.mock
async def test_exact_admission_binds_wire_model_output_and_consumes_shared_counter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_execution = ModelExecution()
    config = fixture_config()
    config.llm.max_retries = 3
    llm.set_request_limit(config, model_execution, 2)
    generate = AsyncMock(side_effect=AssertionError("Admission cannot generate"))
    monkeypatch.setattr(llm, "complete", generate)
    count_route = respx.post(COUNT_URL).respond(200, json={"totalTokens": 47})
    admitted = await admit_request(MESSAGES, config,
                                   execution=model_execution, provider_override=GEMINI,
                                   temperature=0.23, max_output_tokens=3456, deadline=time.monotonic() + 30)
    assert admitted.admitted and admitted.status == "admitted" and admitted.method == "exact"
    assert admitted.provider == GEMINI.name and admitted.model == GEMINI.model
    assert admitted.exact_count == 47 and admitted.input_estimate is None
    assert admitted.output_reserve == 3456 and admitted.input_limit == 1_048_576
    assert "prompt_version" not in asdict(admitted)
    counted = json.loads(count_route.calls.last.request.content)["generateContentRequest"]
    assert counted.pop("model") == f"models/{GEMINI.model}"
    route = route_profile(GEMINI.name, GEMINI.model, 3456)
    assert route is not None
    assert counted == wire_request(route, MESSAGES, temperature=0.23)
    body_bytes = httpx.Request("POST", "https://request.invalid", json=counted).content
    header = json.dumps([GEMINI.name, GEMINI.model], separators=(",", ":")).encode()
    assert admitted.request_sha256 == hashlib.sha256(header + b"\n" + body_bytes).hexdigest()
    assert llm.request_budget_remaining(config, model_execution) == 1
    assert count_route.call_count == 1
    generate.assert_not_called()


@pytest.mark.asyncio
async def test_binding_changes_with_actual_model_temperature_output_and_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_execution = ModelExecution()
    count = AsyncMock(return_value=47)
    monkeypatch.setattr(llm, "count_gemini_tokens", count)
    monkeypatch.setitem(source_admission.INPUT_LIMITS, ("gemini", "fixture-alternate-model"), 1_048_576)
    config = fixture_config()
    results = []
    for provider, temperature, output, messages in [
        (GEMINI, 0.1, 2048, MESSAGES),
        (ProviderConfig("gemini", "fixture-alternate-model"), 0.1, 2048, MESSAGES),
        (GEMINI, 0.2, 2048, MESSAGES),
        (GEMINI, 0.1, 2049, MESSAGES),
        (GEMINI, 0.1, 2048, [*MESSAGES, {"role": "user", "content": "Another qualification"}]),
    ]:
        results.append(await admit_request(messages, config,
                                           execution=model_execution, provider_override=provider,
                                           temperature=temperature, max_output_tokens=output,
                                           deadline=time.monotonic() + 30))
    assert all(result.admitted for result in results)
    assert len({result.request_sha256 for result in results}) == len(results)
    assert [call.kwargs["provider_override"].model for call in count.await_args_list] == [
        GEMINI.model, "fixture-alternate-model", GEMINI.model, GEMINI.model, GEMINI.model,
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(("local_count", "status"), [(4000, "admitted"), (6000, "oversized")])
async def test_local_estimate_reserves_output_and_retains_nonexact_evidence(
    monkeypatch: pytest.MonkeyPatch, local_count: int, status: str,
) -> None:
    model_execution = ModelExecution()
    monkeypatch.setattr(source_admission, "count_gpt_input", lambda messages: local_count)
    count = AsyncMock(side_effect=AssertionError("Local counting cannot call a provider"))
    generate = AsyncMock(side_effect=AssertionError("Admission cannot generate"))
    monkeypatch.setattr(llm, "count_gemini_tokens", count)
    monkeypatch.setattr(llm, "complete", generate)
    admission = await admit_request(MESSAGES, fixture_config(),
                                    execution=model_execution, provider_override=GROQ,
                                    temperature=0.1, max_output_tokens=2048, deadline=time.monotonic() + 30)
    assert admission.status == status
    assert admission.method == "estimated" and admission.exact_count is None
    assert admission.input_estimate == (local_count * 6 + 4) // 5 + 256
    assert admission.input_limit == 8000 - 2048
    assert admission.output_reserve == 2048
    assert admission.evidence["local_input_count"] == local_count
    assert admission.evidence["output_reserve"] == 2048
    count.assert_not_called()
    generate.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(("provider", "error"), [
    (ProviderConfig("deepseek", "deepseek-chat"), "technical_unknown_profile"),
    (GROQ, "technical_tokenizer_profile"),
])
async def test_unsupported_or_missing_tokenizer_fails_closed_without_remote_calls(
    monkeypatch: pytest.MonkeyPatch, provider: ProviderConfig, error: str,
) -> None:
    model_execution = ModelExecution()
    def missing(messages: list[dict[str, str]]) -> int:
        raise TokenProfileUnavailable("fixture missing asset")

    monkeypatch.setattr(source_admission, "count_gpt_input", missing)
    count = AsyncMock(side_effect=AssertionError("No count allowed"))
    generate = AsyncMock(side_effect=AssertionError("No generation allowed"))
    monkeypatch.setattr(llm, "count_gemini_tokens", count)
    monkeypatch.setattr(llm, "complete", generate)
    admission = await admit_request(MESSAGES, fixture_config(),
                                    execution=model_execution, provider_override=provider,
                                    temperature=0.1, max_output_tokens=2048, deadline=time.monotonic() + 30)
    assert not admission.admitted and admission.status == "unverified"
    assert admission.error_class == error
    assert admission.exact_count is None and admission.input_estimate is None
    count.assert_not_called()
    generate.assert_not_called()


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize("response", [httpx.Response(503, json={"error": "SECRET_RESPONSE"}),
                                       httpx.Response(200, json={"totalTokens": "unknown"}),
                                       httpx.Response(200, json={"totalTokens": 1_048_577})])
async def test_count_failure_unknown_or_overlimit_never_retries_falls_back_or_generates(
    response: httpx.Response, monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_execution = ModelExecution()
    config = fixture_config()
    config.llm.max_retries = 3
    config.llm.providers = [GEMINI, GROQ]
    generate = AsyncMock(side_effect=AssertionError("No generation allowed"))
    monkeypatch.setattr(llm, "complete", generate)
    route = respx.post(COUNT_URL).mock(return_value=response)
    admission = await admit_request(MESSAGES, config,
                                    execution=model_execution, provider_override=GEMINI,
                                    temperature=0.1, max_output_tokens=2048, deadline=time.monotonic() + 30)
    assert not admission.admitted and admission.method == "exact"
    assert admission.request_sha256 and admission.output_reserve == 2048
    if response.json().get("totalTokens") == 1_048_577:
        assert admission.status == "oversized" and admission.exact_count == 1_048_577
        assert admission.error_class == "technical_admission_capacity"
    else:
        assert admission.status == "unverified" and admission.exact_count is None
        assert admission.error_class == "technical_count_unknown"
    assert "SECRET" not in repr(admission)
    assert route.call_count == 1 and len(respx.calls) == 1
    generate.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("remaining", [0, 1])
async def test_no_count_without_budget_for_count_and_generation(
    monkeypatch: pytest.MonkeyPatch, remaining: int,
) -> None:
    model_execution = ModelExecution()
    config = fixture_config()
    llm.set_request_limit(config, model_execution, remaining)
    count = AsyncMock(side_effect=AssertionError("No count allowed"))
    monkeypatch.setattr(llm, "count_gemini_tokens", count)
    admission = await admit_request(MESSAGES, config,
                                    execution=model_execution, provider_override=GEMINI,
                                    temperature=0.1, max_output_tokens=2048, deadline=time.monotonic() + 30)
    assert admission.error_class == "technical_request_budget" and not admission.admitted
    assert llm.request_budget_remaining(config, model_execution) == remaining
    count.assert_not_called()


@pytest.mark.asyncio
async def test_spent_groq_request_budget_holds_before_tokenizer_work(monkeypatch: pytest.MonkeyPatch) -> None:
    model_execution = ModelExecution()
    config = fixture_config()
    llm.set_request_limit(config, model_execution, 1)
    monkeypatch.setattr(llm, "_call_provider", AsyncMock(return_value=("Already spent", {})))
    await llm.complete(llm.LLMRole.REVIEW_EVIDENCE, MESSAGES, config,
                       execution=model_execution, provider_override=GROQ)

    def unexpected_tokenizer(messages: list[dict[str, str]]) -> int:
        raise AssertionError("An exhausted request allowance must hold before tokenizer work")

    monkeypatch.setattr(source_admission, "count_gpt_input", unexpected_tokenizer)
    admission = await admit_request(MESSAGES, config,
                                    execution=model_execution, provider_override=GROQ,
                                    temperature=0.1, max_output_tokens=2048, deadline=time.monotonic() + 30)
    assert admission.status == "unverified" and not admission.admitted
    assert admission.error_class == "technical_request_budget"
    assert admission.input_estimate is None and admission.exact_count is None
    assert llm.request_budget_remaining(config, model_execution) == 0


@pytest.mark.asyncio
async def test_deadline_accounts_for_existing_shared_pacing(monkeypatch: pytest.MonkeyPatch) -> None:
    model_execution = ModelExecution()
    config = fixture_config()
    config.llm.min_request_interval_seconds = 65
    runtime = llm._request_state(config, model_execution)
    runtime.last_request_at = time.monotonic()
    count = AsyncMock(side_effect=AssertionError("No count allowed"))
    monkeypatch.setattr(llm, "count_gemini_tokens", count)
    admission = await admit_request(MESSAGES, config,
                                    execution=model_execution, provider_override=GEMINI,
                                    temperature=0.1, max_output_tokens=2048, deadline=time.monotonic() + 30)
    assert admission.error_class == "technical_deadline" and admission.exact_count is None
    count.assert_not_called()


@pytest.mark.asyncio
async def test_count_deadline_cancels_wait_and_preserves_outer_cancellation(monkeypatch: pytest.MonkeyPatch) -> None:
    model_execution = ModelExecution()
    async def stalled(*args: Any, **kwargs: Any) -> int:
        await asyncio.Future()
        raise AssertionError("Unreachable")

    monkeypatch.setattr(llm, "count_gemini_tokens", stalled)
    monkeypatch.setattr(source_admission, "COUNT_SECONDS", 0.01)
    result = await admit_request(MESSAGES, fixture_config(),
                                 execution=model_execution, provider_override=GEMINI,
                                 temperature=0.1, max_output_tokens=2048, deadline=time.monotonic() + 0.02)
    assert result.error_class == "technical_deadline" and result.exact_count is None
    monkeypatch.setattr(source_admission, "COUNT_SECONDS", 10)
    task = asyncio.create_task(admit_request(MESSAGES, fixture_config(),
                                             execution=model_execution, provider_override=GEMINI,
                                            temperature=0.1, max_output_tokens=2048, deadline=time.monotonic() + 30))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
