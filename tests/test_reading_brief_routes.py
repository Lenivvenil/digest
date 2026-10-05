"""Provider-independent source admission and bounded route recovery contracts."""
from __future__ import annotations

import asyncio
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
from digest.reading_brief_state import BriefState, checksum, load_state, save_state, state_root
from digest.reading_brief_tokens import ESTIMATOR_VERSION
from tests.factories import make_article
from tests.test_reading_brief import config, fetched, payload, response


@pytest.fixture(autouse=True)
def offline_counter() -> Any:
    # Route contracts do not depend on optional assets; tokenizer contracts are separate.
    with patch("digest.source_admission.count_gpt_input", return_value=1000):
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
        with patch("digest.source_admission.count_gpt_input", side_effect=AssertionError("assets unavailable")):
            again = await enrich_selected_cards([], cfg, tmp_path, time.monotonic() + 1000)
            ready_brief_evidence(tmp_path, identity)
        assert again.cards == result.cards and call.call_count == 1 and count.call_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [429, 503])
@pytest.mark.parametrize("interval", [20.0, 90.0])
async def test_known_provider_failure_falls_back_without_resetting_runtime_or_retrying(
    tmp_path: Path, code: int, interval: float,
) -> None:
    cfg = config()
    cfg.llm.min_request_interval_seconds = interval
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
            raise status_error(code)
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
    assert [call.args[1] for call in pace.call_args_list] == [interval, max(interval, 65)]
    state, _ = ready_brief_evidence(tmp_path, next(iter(result.quotations)))
    assert state.route.provider == "gemini" and state.pages[0].route.provider == "groq"
    assert len(state.exact_counts) == len(state.admissions) == 1
    assert [(item.kind, item.route.provider, item.status) for item in state.pages[0].request_attempts] == [
        ("count", "gemini", "accepted"), ("generate", "gemini", "definite_failed"), ("generate", "groq", "accepted"),
    ]
    assert all(item.source_sha256 == state.source_sha256 for item in state.pages[0].request_attempts)


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
        identity = next(state_root(tmp_path).glob("*.json")).stem
        before = load_state(tmp_path, identity)
        same = await enrich_selected_cards([], cfg, tmp_path, time.monotonic() + 1000)
        cfg.reading_brief = replace(cfg.reading_brief, provider=GROQ.name, model=GROQ.model)
        changed = await enrich_selected_cards([], cfg, tmp_path, time.monotonic() + 1000)
        after = load_state(tmp_path, identity)
    assert result.pending == same.pending == changed.pending == 1
    assert not result.cards and not same.cards and not changed.cards and call.call_count == 1
    assert before.pages == after.pages and before.source_sha256 == after.source_sha256
    assert after.pages[0].request_attempts[-1].status == "unknown"
    assert after.error_class == "technical_generation_unknown"


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

    with (patch("digest.source_admission.INPUT_LIMITS", {("gemini", "gemini-3.8-flash"): 115}),
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
            page.pop("request_attempts")
            page.pop("request_history_version")
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


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_json", [True, False])
async def test_accepted_invalid_generation_is_persisted_before_validation_and_never_replayed(
    tmp_path: Path, invalid_json: bool,
) -> None:
    cfg = config()
    cfg.llm.providers = [GROQ]

    async def generate(_role: Any, messages: Any, *_args: Any, **_kwargs: Any) -> Any:
        text, usage = response(messages)
        return ("unparseable private output" if invalid_json else text,
                {**usage, "finish_reason": "STOP" if invalid_json else "MAX_TOKENS", "prompt_tokens": 100,
                 "completion_tokens": 99, "total_tokens": 199, "thoughts": "private thoughts"})

    with (patch("digest.reading_brief.fetch_article", AsyncMock(return_value=fetched("Complete source."))),
          patch("digest.llm.count_gemini_tokens", AsyncMock(return_value=100)) as count,
          patch("digest.llm.complete", side_effect=generate) as call):
        first = await enrich_selected_cards([make_article()], cfg, tmp_path, time.monotonic() + 1000)
        second = await enrich_selected_cards([], cfg, tmp_path, time.monotonic() + 1000)
        cfg.reading_brief = replace(cfg.reading_brief, provider=GROQ.name, model=GROQ.model)
        changed = await enrich_selected_cards([], cfg, tmp_path, time.monotonic() + 1000)
    path = next(state_root(tmp_path).glob("*.json"))
    state = load_state(tmp_path, path.stem)
    attempt = state.pages[0].request_attempts[-1]
    assert first.pending == second.pending == changed.pending == 1 and call.call_count == count.call_count == 1
    assert attempt.kind == "generate" and attempt.status == "accepted"
    assert attempt.finish_reason == ("STOP" if invalid_json else "MAX_TOKENS") and attempt.finished_at is not None
    assert attempt.usage == {"prompt_tokens": 100, "completion_tokens": 99, "total_tokens": 199}
    assert len(attempt.response_sha256) == 64 and state.pages[0].response is None
    assert "private output" not in path.read_text() and "private thoughts" not in path.read_text()


@pytest.mark.asyncio
@pytest.mark.parametrize("interrupt_after_response", [False, True])
async def test_interrupted_generation_intent_prevents_new_invocation(
    tmp_path: Path, interrupt_after_response: bool,
) -> None:
    cfg = config()
    cfg.reading_brief = replace(cfg.reading_brief, provider=GROQ.name, model=GROQ.model)
    cfg.llm.providers = [ProviderConfig("gemini", "gemini-3.8-flash")]

    def save_or_interrupt(state_dir: Path, state: BriefState) -> None:
        if interrupt_after_response and state.pages and any(
            item.kind == "generate" and item.status == "accepted" for item in state.pages[0].request_attempts
        ):
            raise asyncio.CancelledError
        save_state(state_dir, state)

    async def generate(_role: Any, messages: Any, *_args: Any, **_kwargs: Any) -> Any:
        persisted = load_state(tmp_path, next(state_root(tmp_path).glob("*.json")).stem)
        assert persisted.pages[0].request_attempts[-1].status == "reserved"
        if not interrupt_after_response:
            raise asyncio.CancelledError
        return response(messages)

    with (patch("digest.reading_brief.fetch_article", AsyncMock(return_value=fetched("Complete source."))),
          patch("digest.llm.complete", side_effect=generate) as call,
          patch("digest.llm.count_gemini_tokens", AsyncMock()) as count):
        with (patch("digest.reading_brief.save_state", side_effect=save_or_interrupt),
              pytest.raises(asyncio.CancelledError)):
            await enrich_selected_cards([make_article()], cfg, tmp_path, time.monotonic() + 1000)
        cfg.reading_brief = replace(cfg.reading_brief, provider="gemini", model="gemini-3.8-flash")
        resumed = await enrich_selected_cards([], cfg, tmp_path, time.monotonic() + 1000)
    state = load_state(tmp_path, next(state_root(tmp_path).glob("*.json")).stem)
    assert resumed.pending == 1 and not resumed.cards and call.call_count == 1 and count.call_count == 0
    assert state.pages[0].request_attempts[-1].status == "reserved"
    assert state.error_class == "technical_generation_unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
async def test_unknown_count_is_not_repeated_but_supported_local_admission_can_resume(
    tmp_path: Path, legacy: bool,
) -> None:
    cfg = config()

    async def generate(_role: Any, messages: Any, *_args: Any, **kwargs: Any) -> Any:
        assert kwargs["provider_override"].name == "groq"
        return response(messages)

    with (patch("digest.reading_brief.fetch_article", AsyncMock(return_value=fetched("Complete source."))) as fetch,
          patch("digest.llm.count_gemini_tokens", AsyncMock(side_effect=TimeoutError)) as count,
          patch("digest.llm.complete", side_effect=generate) as call):
        first = await enrich_selected_cards([make_article()], cfg, tmp_path, time.monotonic() + 1000)
        if legacy:
            path = next(state_root(tmp_path).glob("*.json"))
            envelope = json.loads(path.read_text())
            for page in envelope["payload"]["pages"]:
                page.pop("request_attempts")
                page.pop("request_history_version")
            envelope["sha256"] = checksum(envelope["payload"])
            path.write_text(json.dumps(envelope))
        second = await enrich_selected_cards([], cfg, tmp_path, time.monotonic() + 1000)
        assert first.pending == second.pending == 1 and count.call_count == 1 and call.call_count == 0
        cfg.llm.providers = [GROQ]
        resumed = await enrich_selected_cards([], cfg, tmp_path, time.monotonic() + 1000)
    assert resumed.pending == 0 and len(resumed.cards) == 1
    assert fetch.call_count == count.call_count == call.call_count == 1
    state, _ = ready_brief_evidence(tmp_path, next(iter(resumed.quotations)))
    expected = [("generate", "accepted")] if legacy else [
        ("count", "unknown"), ("generate", "accepted"),
    ]
    assert [(item.kind, item.status) for item in state.pages[0].request_attempts] == expected
    assert not state.exact_counts and len(state.admissions) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["gemini", "groq"])
async def test_legacy_admitted_unfinished_page_cannot_replay_its_unrecorded_generation(
    tmp_path: Path, provider: str,
) -> None:
    cfg = config()
    if provider == "groq":
        cfg.reading_brief = replace(cfg.reading_brief, provider=GROQ.name, model=GROQ.model)
    with (patch("digest.reading_brief.fetch_article", AsyncMock(return_value=fetched("Complete source."))) as fetch,
          patch("digest.llm.count_gemini_tokens", AsyncMock(return_value=100)) as count,
          patch("digest.llm.complete", AsyncMock(side_effect=TimeoutError)) as call):
        first = await enrich_selected_cards([make_article()], cfg, tmp_path, time.monotonic() + 1000)
        path = next(state_root(tmp_path).glob("*.json"))
        envelope = json.loads(path.read_text())
        for page in envelope["payload"]["pages"]:
            page.pop("request_attempts")
            page.pop("request_history_version")
        envelope["sha256"] = checksum(envelope["payload"])
        path.write_text(json.dumps(envelope))
        original = load_state(tmp_path, path.stem)
        repeated = await enrich_selected_cards([], cfg, tmp_path, time.monotonic() + 1000)
        cfg.reading_brief = replace(cfg.reading_brief, provider=GROQ.name, model=GROQ.model)
        changed = await enrich_selected_cards([], cfg, tmp_path, time.monotonic() + 1000)
    held = load_state(tmp_path, path.stem)
    assert first.pending == repeated.pending == changed.pending == 1 and call.call_count == fetch.call_count == 1
    assert count.call_count == (1 if provider == "gemini" else 0)
    assert held.pages == original.pages and held.source_sha256 == original.source_sha256
    assert held.error_class == "technical_generation_unknown" and not held.pages[0].request_attempts


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["gemini", "groq"])
async def test_new_page_with_no_request_intent_can_resume_after_budget_deferral(tmp_path: Path, provider: str) -> None:
    cfg = config()
    if provider == "groq":
        cfg.reading_brief = replace(cfg.reading_brief, provider=GROQ.name, model=GROQ.model)
    llm.set_request_limit(cfg, 0)

    async def generate(_role: Any, messages: Any, *_args: Any, **_kwargs: Any) -> Any:
        return response(messages)

    with (patch("digest.reading_brief.fetch_article", AsyncMock(return_value=fetched("Complete source."))) as fetch,
          patch("digest.llm.count_gemini_tokens", AsyncMock(return_value=100)) as count,
          patch("digest.llm.complete", side_effect=generate) as call):
        first = await enrich_selected_cards([make_article()], cfg, tmp_path, time.monotonic() + 1000)
        state = load_state(tmp_path, next(state_root(tmp_path).glob("*.json")).stem)
        assert first.pending == 1 and count.call_count == call.call_count == 0
        assert state.pages[0].request_history_version == 1 and not state.pages[0].request_attempts
        llm.set_request_limit(cfg, 2)
        resumed = await enrich_selected_cards([], cfg, tmp_path, time.monotonic() + 1000)
    assert resumed.pending == 0 and len(resumed.cards) == 1 and fetch.call_count == call.call_count == 1
    assert count.call_count == (1 if provider == "gemini" else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["hash", "finish", "usage", "missing"])
async def test_completed_generation_must_match_its_accepted_attempt(tmp_path: Path, damage: str) -> None:
    async def generate(_role: Any, messages: Any, *_args: Any, **_kwargs: Any) -> Any:
        return response(messages)

    with (patch("digest.reading_brief.fetch_article", AsyncMock(return_value=fetched("Complete source."))),
          patch("digest.llm.count_gemini_tokens", AsyncMock(return_value=100)),
          patch("digest.llm.complete", side_effect=generate) as call):
        first = await enrich_selected_cards([make_article()], config(), tmp_path, time.monotonic() + 1000)
        identity = next(iter(first.quotations))
        state = load_state(tmp_path, identity)
        attempt = state.pages[0].request_attempts[-1]
        if damage == "hash":
            attempt.response_sha256 = "0" * 64
        elif damage == "finish":
            attempt.finish_reason = "MAX_TOKENS"
        elif damage == "usage":
            attempt.usage = {"prompt_tokens": 999}
        else:
            state.pages[0].request_attempts.pop()
        save_state(tmp_path, state)
        with pytest.raises(ValueError, match="result_attempt_binding_mismatch"):
            ready_brief_evidence(tmp_path, identity)
        held = await enrich_selected_cards([], config(), tmp_path, time.monotonic() + 1000)
    assert held.pending == 1 and not held.cards and call.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["deadline", "definite_failed"])
async def test_legacy_count_binding_survives_local_route_progress_and_failures(tmp_path: Path, failure: str) -> None:
    cfg = config()

    async def generate(_role: Any, messages: Any, *_args: Any, **_kwargs: Any) -> Any:
        if failure == "definite_failed":
            raise status_error(503)
        return response(messages)

    with (patch("digest.reading_brief.fetch_article", AsyncMock(return_value=fetched("Complete source."))),
          patch("digest.llm.count_gemini_tokens", AsyncMock(side_effect=TimeoutError)) as count,
          patch("digest.llm.complete", side_effect=generate) as call):
        await enrich_selected_cards([make_article()], cfg, tmp_path, time.monotonic() + 1000)
        path = next(state_root(tmp_path).glob("*.json"))
        envelope = json.loads(path.read_text())
        for page in envelope["payload"]["pages"]:
            page.pop("request_attempts")
            page.pop("request_history_version")
            page.pop("legacy_count_request_sha256")
        original_prompt = envelope["payload"]["pages"][0]["prompt_sha256"]
        envelope["sha256"] = checksum(envelope["payload"])
        path.write_text(json.dumps(envelope))
        cfg.llm.providers = [GROQ]
        second = await enrich_selected_cards([], cfg, tmp_path,
                                             time.monotonic() + (20 if failure == "deadline" else 1000))
        saved = load_state(tmp_path, path.stem)
        assert second.pending == 1 and saved.pages[0].request_history_version == 1
        assert saved.pages[0].legacy_count_request_sha256 == original_prompt
        assert saved.pages[0].prompt_sha256 != original_prompt and saved.pages[0].route.provider == "groq"
        final = await enrich_selected_cards([], cfg, tmp_path, time.monotonic() + 1000)
    assert count.call_count == 1
    if failure == "deadline":
        assert final.pending == 0 and len(final.cards) == 1 and call.call_count == 1
    else:
        assert final.pending == 1 and not final.cards and call.call_count == 2
        assert load_state(tmp_path, path.stem).error_class == "technical_count_unknown"


@pytest.mark.asyncio
async def test_shared_budget_predispatch_failure_remains_resumable(tmp_path: Path) -> None:
    from digest.model_budget import ModelBudgetError

    cfg = config()
    cfg.reading_brief = replace(cfg.reading_brief, provider=GROQ.name, model=GROQ.model)
    with (patch("digest.reading_brief.fetch_article", AsyncMock(return_value=fetched("Complete source."))),
          patch("digest.llm.complete", side_effect=ModelBudgetError("Local usage write failed before dispatch"))):
        held = await enrich_selected_cards([make_article()], cfg, tmp_path, time.monotonic() + 1000)
    from digest.radar.collector import article_hash

    identity = article_hash(make_article().title, make_article().link)
    state = load_state(tmp_path, identity)
    assert held.pending == 1 and state.error_class == "technical_quota_or_budget"
    assert state.pages[0].request_attempts[-1].status == "definite_failed"

    async def generate(_role: Any, messages: Any, *_args: Any, **_kwargs: Any) -> Any:
        return response(messages)

    with patch("digest.llm.complete", side_effect=generate) as call:
        resumed = await enrich_selected_cards([], cfg, tmp_path, time.monotonic() + 1000)
    assert resumed.pending == 0 and call.call_count == 1
