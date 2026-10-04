"""Provider-independent source admission and bounded route recovery contracts."""
from __future__ import annotations

import json
import time
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from digest import llm
from digest.config import ProviderConfig
from digest.reading_brief import enrich_selected_cards, ready_brief_evidence
from digest.reading_brief_state import checksum, load_state, state_root
from digest.reading_brief_tokens import ESTIMATOR_VERSION
from tests.factories import make_article
from tests.test_reading_brief import config, fetched, payload, response


@pytest.fixture(autouse=True)
def offline_counter() -> Any:
    # Route contracts do not depend on optional assets; tokenizer contracts are separate.
    with patch("digest.reading_brief.count_gpt_input", return_value=1000):
        yield


GROQ = ProviderConfig("groq", "openai/gpt-oss-120b")


def status_error(code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://example.com/mock")
    return httpx.HTTPStatusError("mock status", request=request, response=httpx.Response(code, request=request))


@pytest.mark.asyncio
async def test_groq_full_source_uses_distinct_admission_and_normal_completion(tmp_path: Path) -> None:
    cfg = config()
    cfg.reading_brief = replace(cfg.reading_brief, provider=GROQ.name, model=GROQ.model)
    source = "Claim.\n\nFINAL QUALIFICATION: pilot only."

    async def generate(_role: Any, messages: Any, *_args: Any, **_kwargs: Any) -> Any:
        assert "".join(span["text"] for span in payload(messages)["spans"]) == source
        text, usage = response(messages)
        usage["finish_reason"] = "stop"
        return text, usage

    with (patch("digest.reading_brief.fetch_article", AsyncMock(return_value=fetched(source))),
          patch("digest.llm.count_gemini_tokens", AsyncMock()) as count,
          patch("digest.llm.complete", side_effect=generate) as call):
        result = await enrich_selected_cards([make_article()], cfg, tmp_path, time.monotonic() + 1000)
        assert result.pending == 0 and len(result.cards) == 1
        identity = next(iter(result.quotations))
        state, _ = ready_brief_evidence(tmp_path, identity)
        assert not state.exact_counts and len(state.admissions) == 1
        record = next(iter(state.admissions.values()))
        assert record["method"] == ESTIMATOR_VERSION
        assert record["output_reserve"] == cfg.reading_brief.max_output_tokens
        assert record["request_allowance"] == 8000
        assert "FINAL QUALIFICATION" in result.quotations[identity]
        with patch("digest.reading_brief.count_gpt_input", side_effect=AssertionError("assets unavailable")):
            again = await enrich_selected_cards([], cfg, tmp_path, time.monotonic() + 1000)
            ready_brief_evidence(tmp_path, identity)
        assert again.cards == result.cards and call.call_count == 1 and count.call_count == 0


@pytest.mark.asyncio
async def test_known_provider_failure_falls_back_without_resetting_runtime_or_retrying(tmp_path: Path) -> None:
    cfg = config()
    cfg.llm.providers = [GROQ, ProviderConfig("groq", "qwen/qwen3.8-27b")]
    cfg.llm.max_retries = 3
    llm.set_request_limit(cfg, 3)
    calls = []

    async def count(_messages: Any, call_config: Any, **_kwargs: Any) -> int:
        assert call_config.llm._runtime is cfg.llm._runtime
        llm._reserve_request(llm._request_state(call_config))
        return 100

    async def provider_call(_client: Any, provider: Any, messages: Any, *_args: Any, **_kwargs: Any) -> Any:
        calls.append(provider.model)
        if provider.name == "gemini":
            raise status_error(429)
        text, usage = response(messages)
        usage["finish_reason"] = "stop"
        return text, usage

    with (patch("digest.reading_brief.fetch_article", AsyncMock(return_value=fetched("Complete source."))),
          patch("digest.llm.count_gemini_tokens", side_effect=count),
          patch("digest.llm._call_provider", side_effect=provider_call),
          patch("digest.llm._pace_request", AsyncMock()) as pace):
        result = await enrich_selected_cards([make_article()], cfg, tmp_path, time.monotonic() + 1000)
    assert len(result.cards) == 1 and result.pending == 0
    assert calls == ["gemini-3.8-flash", GROQ.model]
    assert llm.request_budget_remaining(cfg) == 0 and cfg.llm.max_retries == 3
    assert all(call.args[1] == 65 for call in pace.call_args_list)
    state, _ = ready_brief_evidence(tmp_path, next(iter(result.quotations)))
    assert state.route.provider == "gemini" and state.pages[0].route.provider == "groq"
    assert len(state.exact_counts) == len(state.admissions) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [TimeoutError("deadline"), RuntimeError("HTTP 500 code=unknown"),
                                     ValueError("invalid output"), httpx.ReadTimeout("read timeout")])
async def test_unknown_or_ambiguous_failure_does_not_fallback(tmp_path: Path, failure: Exception) -> None:
    cfg = config()
    cfg.llm.providers = [GROQ]
    with (patch("digest.reading_brief.fetch_article", AsyncMock(return_value=fetched("Complete source."))),
          patch("digest.llm.count_gemini_tokens", AsyncMock(return_value=100)),
          patch("digest.llm.complete", AsyncMock(side_effect=failure)) as call):
        result = await enrich_selected_cards([make_article()], cfg, tmp_path, time.monotonic() + 1000)
    assert result.pending == 1 and not result.cards and call.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["length", "tool_calls", "MAX_TOKENS", "error", None])
async def test_groq_incomplete_endings_never_emit_or_fallback(tmp_path: Path, ending: str | None) -> None:
    cfg = config()
    cfg.reading_brief = replace(cfg.reading_brief, provider=GROQ.name, model=GROQ.model)

    async def generate(_role: Any, messages: Any, *_args: Any, **_kwargs: Any) -> Any:
        text, usage = response(messages)
        usage["finish_reason"] = ending
        return text, usage

    with (patch("digest.reading_brief.fetch_article", AsyncMock(return_value=fetched("Complete source."))),
          patch("digest.llm.complete", side_effect=generate) as call):
        result = await enrich_selected_cards([make_article()], cfg, tmp_path, time.monotonic() + 1000)
    assert result.pending == 1 and not result.cards and call.call_count == 1


@pytest.mark.asyncio
async def test_changed_primary_resumes_immutable_source_and_keeps_completed_page_binding(tmp_path: Path) -> None:
    cfg = config()
    source = "Claim.\n\nFINAL QUALIFICATION: pilot only."
    calls = []

    async def count(messages: Any, *_args: Any, **_kwargs: Any) -> int:
        return 100 + len(payload(messages)["spans"]) * 10

    async def generate(_role: Any, messages: Any, *_args: Any, **kwargs: Any) -> Any:
        calls.append((kwargs["provider_override"].name, payload(messages)["span_range"]))
        if calls == [("gemini", [1, 1]), ("gemini", [2, 2])]:
            raise RuntimeError("HTTP 503 code=UNAVAILABLE")
        text, usage = response(messages)
        usage["finish_reason"] = "stop" if kwargs["provider_override"].name == "groq" else "STOP"
        return text, usage

    with (patch("digest.reading_brief.INPUT_LIMITS", {("gemini", "gemini-3.8-flash"): 115}),
          patch("digest.reading_brief.fetch_article", AsyncMock(return_value=fetched(source))) as fetch,
          patch("digest.llm.count_gemini_tokens", side_effect=count),
          patch("digest.llm.complete", side_effect=generate)):
        first = await enrich_selected_cards([make_article()], cfg, tmp_path, time.monotonic() + 1000)
        assert first.pending == 1
        identity = next(state_root(tmp_path).glob("*.json")).stem
        before = load_state(tmp_path, identity)
        cfg.reading_brief = replace(cfg.reading_brief, provider=GROQ.name, model=GROQ.model)
        second = await enrich_selected_cards([], cfg, tmp_path, time.monotonic() + 1000)
        after, _ = ready_brief_evidence(tmp_path, identity)
    assert second.pending == 0 and len(second.cards) == 1 and fetch.call_count == 1
    assert before.pages[0] == after.pages[0] and before.source_sha256 == after.source_sha256
    assert calls == [("gemini", [1, 1]), ("gemini", [2, 2]), ("groq", [2, 2])]
    assert "FINAL QUALIFICATION" in second.quotations[identity]


@pytest.mark.asyncio
async def test_legacy_v1_exact_cache_without_new_fields_remains_readable(tmp_path: Path) -> None:
    async def generate(_role: Any, messages: Any, *_args: Any, **_kwargs: Any) -> Any:
        return response(messages)

    with (patch("digest.reading_brief.fetch_article", AsyncMock(return_value=fetched("Complete source."))),
          patch("digest.llm.count_gemini_tokens", AsyncMock(return_value=100)),
          patch("digest.llm.complete", side_effect=generate) as call):
        first = await enrich_selected_cards([make_article()], config(), tmp_path, time.monotonic() + 1000)
        identity = next(iter(first.quotations))
        path = state_root(tmp_path) / f"{identity}.json"
        envelope = json.loads(path.read_text())
        envelope["payload"].pop("admissions")
        for page in envelope["payload"]["pages"]:
            page.pop("route")
        envelope["sha256"] = checksum(envelope["payload"])
        path.write_text(json.dumps(envelope))
        again = await enrich_selected_cards([], config(), tmp_path, time.monotonic() + 1000)
    assert again.cards == first.cards and call.call_count == 1


@pytest.mark.asyncio
async def test_unknown_profile_hold_can_resume_after_explicit_supported_primary_change(tmp_path: Path) -> None:
    cfg = config()
    cfg.reading_brief = replace(cfg.reading_brief, provider="groq", model="qwen/qwen3.8-27b")

    async def generate(_role: Any, messages: Any, *_args: Any, **_kwargs: Any) -> Any:
        text, usage = response(messages)
        usage["finish_reason"] = "stop"
        return text, usage

    with (patch("digest.reading_brief.fetch_article", AsyncMock(return_value=fetched("Complete source."))) as fetch,
          patch("digest.llm.complete", side_effect=generate) as call):
        first = await enrich_selected_cards([make_article()], cfg, tmp_path, time.monotonic() + 1000)
        assert first.pending == 1 and fetch.call_count == call.call_count == 0
        cfg.reading_brief = replace(cfg.reading_brief, model=GROQ.model)
        resumed = await enrich_selected_cards([], cfg, tmp_path, time.monotonic() + 1000)
    assert resumed.pending == 0 and len(resumed.cards) == 1 and fetch.call_count == call.call_count == 1
    state, _ = ready_brief_evidence(tmp_path, next(iter(resumed.quotations)))
    assert state.route.model == "qwen/qwen3.8-27b" and state.pages[0].route.model == GROQ.model
