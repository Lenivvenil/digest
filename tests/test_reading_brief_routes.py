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
from digest.adapters.models.execution import ModelExecution
from digest.config import ProviderConfig
from digest.reading_brief import _advance, ready_brief_evidence
from digest.reading_brief_state import BriefState, checksum, load_state, save_state, state_root
from digest.reading_brief_tokens import ESTIMATOR_VERSION
from tests.test_reading_brief import config, fetched, payload, response, saved_brief_state


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
    model_execution = ModelExecution()
    cfg = config()
    cfg.reading_brief = replace(cfg.reading_brief, provider=GROQ.name, model=GROQ.model)
    state = saved_brief_state(tmp_path, cfg)
    source = "Claim.\n\nFINAL QUALIFICATION: pilot only."

    async def generate(_role: Any, messages: Any, *_args: Any, **_kwargs: Any) -> Any:
        assert "".join(span["text"] for span in payload(messages)["spans"]) == source
        text, usage = response(messages)
        usage["finish_reason"] = "stop"
        return text, usage

    with (patch("digest.reading_brief.fetch_article", AsyncMock(return_value=fetched(source))),
          patch("digest.llm.count_gemini_tokens", AsyncMock()) as count,
          patch("digest.llm.complete", side_effect=generate) as call):
        await _advance(state, cfg, tmp_path, time.monotonic() + 1000, execution=model_execution)
        identity = state.selection.identity
        state, original = ready_brief_evidence(tmp_path, identity)
        assert state.status == "ready" and state.attempts == 1
        assert original.text == source
        result = state.pages[0].result
        assert result is not None and result.qualification_span_ids == [2]
        assert [original.text[span.start:span.end] for span in original.spans
                if span.id in result.qualification_span_ids] == ["FINAL QUALIFICATION: pilot only."]
        assert not state.exact_counts and len(state.admissions) == 1
        record = next(iter(state.admissions.values()))
        assert record["method"] == ESTIMATOR_VERSION
        assert record["output_reserve"] == cfg.reading_brief.max_output_tokens
        assert record["request_allowance"] == 8000
        path = state_root(tmp_path) / f"{identity}.json"
        persisted = path.read_bytes()
        with patch("digest.source_admission.count_gpt_input", side_effect=AssertionError("assets unavailable")):
            again, checked_source = ready_brief_evidence(tmp_path, identity)
        assert again == state and checked_source == original and path.read_bytes() == persisted
        assert call.call_count == 1 and count.call_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [429, 503])
@pytest.mark.parametrize("interval", [20.0, 90.0])
async def test_known_provider_failure_falls_back_without_resetting_runtime_or_retrying(
    tmp_path: Path, code: int, interval: float,
) -> None:
    model_execution = ModelExecution()
    cfg = config()
    cfg.llm.min_request_interval_seconds = interval
    cfg.llm.providers = [GROQ, ProviderConfig("groq", "qwen/qwen3.8-27b")]
    cfg.llm.max_retries = 3
    llm.set_request_limit(cfg, model_execution, 3)
    state = saved_brief_state(tmp_path, cfg)
    calls = []

    async def count(_messages: Any, call_config: Any, *, execution: ModelExecution, **_kwargs: Any) -> int:
        assert execution is not model_execution
        assert llm._request_state(call_config, execution) is llm._request_state(cfg, model_execution)
        llm._reserve_request(llm._request_state(call_config, execution))
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
        await _advance(state, cfg, tmp_path, time.monotonic() + 1000, execution=model_execution)
    state, _ = ready_brief_evidence(tmp_path, state.selection.identity)
    assert state.status == "ready" and state.attempts == 1
    assert calls == ["gemini-3.8-flash", GROQ.model]
    assert llm.request_budget_remaining(cfg, model_execution) == 0 and cfg.llm.max_retries == 3
    assert [call.args[1] for call in pace.call_args_list] == [interval, max(interval, 65)]
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
    model_execution = ModelExecution()
    cfg = config()
    cfg.llm.providers = [GROQ]
    state = saved_brief_state(tmp_path, cfg)
    identity = state.selection.identity
    with (patch("digest.reading_brief.fetch_article", AsyncMock(return_value=fetched("Complete source."))),
          patch("digest.llm.count_gemini_tokens", AsyncMock(return_value=100)),
          patch("digest.llm.complete", AsyncMock(side_effect=failure)) as call):
        await _advance(state, cfg, tmp_path, time.monotonic() + 1000, execution=model_execution)
        before = load_state(tmp_path, identity)
        state = load_state(tmp_path, identity)
        await _advance(state, cfg, tmp_path, time.monotonic() + 1000, execution=model_execution)
        same = load_state(tmp_path, identity)
        cfg.reading_brief = replace(cfg.reading_brief, provider=GROQ.name, model=GROQ.model)
        state = load_state(tmp_path, identity)
        await _advance(state, cfg, tmp_path, time.monotonic() + 1000, execution=model_execution)
        after = load_state(tmp_path, identity)
    assert before.status == same.status == after.status == "pending" and call.call_count == 1
    assert (before.attempts, same.attempts, after.attempts) == (1, 2, 3)
    assert before.pages == after.pages and before.source_sha256 == after.source_sha256
    assert after.pages[0].request_attempts[-1].status == "unknown"
    assert after.pages[0].result is None
    assert after.error_class == "technical_generation_unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["length", "tool_calls", "MAX_TOKENS", "error", None])
async def test_groq_incomplete_endings_never_emit_or_fallback(tmp_path: Path, ending: str | None) -> None:
    model_execution = ModelExecution()
    cfg = config()
    cfg.reading_brief = replace(cfg.reading_brief, provider=GROQ.name, model=GROQ.model)
    state = saved_brief_state(tmp_path, cfg)

    async def generate(_role: Any, messages: Any, *_args: Any, **_kwargs: Any) -> Any:
        text, usage = response(messages)
        usage["finish_reason"] = ending
        return text, usage

    with (patch("digest.reading_brief.fetch_article", AsyncMock(return_value=fetched("Complete source."))),
          patch("digest.llm.complete", side_effect=generate) as call):
        await _advance(state, cfg, tmp_path, time.monotonic() + 1000, execution=model_execution)
    state = load_state(tmp_path, state.selection.identity)
    assert state.status == "pending" and state.pages[0].result is None and call.call_count == 1
    assert state.pages[0].request_attempts[-1].status == "accepted"


@pytest.mark.asyncio
async def test_changed_primary_resumes_immutable_source_and_keeps_completed_page_binding(tmp_path: Path) -> None:
    model_execution = ModelExecution()
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
        state = saved_brief_state(tmp_path, cfg)
        identity = state.selection.identity
        await _advance(state, cfg, tmp_path, time.monotonic() + 1000, execution=model_execution)
        before = load_state(tmp_path, identity)
        assert before.status == "pending"
        cfg.reading_brief = replace(cfg.reading_brief, provider=GROQ.name, model=GROQ.model)
        state = load_state(tmp_path, identity)
        await _advance(state, cfg, tmp_path, time.monotonic() + 1000, execution=model_execution)
        after, original = ready_brief_evidence(tmp_path, identity)
    assert after.status == "ready" and after.attempts == 2 and fetch.call_count == 1
    assert before.pages[0] == after.pages[0] and before.source_sha256 == after.source_sha256
    assert calls == [("gemini", [1, 1]), ("gemini", [2, 2]), ("groq", [2, 2])]
    assert original.text == source
    result = after.pages[1].result
    assert result is not None and result.qualification_span_ids == [2]
    assert after.pages[1].route is not None and after.pages[1].route.provider == "groq"
    assert [original.text[span.start:span.end] for span in original.spans
            if span.id in result.qualification_span_ids] == ["FINAL QUALIFICATION: pilot only."]


@pytest.mark.asyncio
async def test_legacy_v1_exact_cache_without_new_fields_remains_readable(tmp_path: Path) -> None:
    model_execution = ModelExecution()
    cfg = config()
    state = saved_brief_state(tmp_path, cfg)
    async def generate(_role: Any, messages: Any, *_args: Any, **_kwargs: Any) -> Any:
        return response(messages)

    with (patch("digest.reading_brief.fetch_article", AsyncMock(return_value=fetched("Complete source."))),
          patch("digest.llm.count_gemini_tokens", AsyncMock(return_value=100)),
          patch("digest.llm.complete", side_effect=generate) as call):
        await _advance(state, cfg, tmp_path, time.monotonic() + 1000, execution=model_execution)
        identity = state.selection.identity
        first, original = ready_brief_evidence(tmp_path, identity)
        path = state_root(tmp_path) / f"{identity}.json"
        envelope = json.loads(path.read_text())
        envelope["payload"].pop("admissions")
        for page in envelope["payload"]["pages"]:
            page.pop("route")
            page.pop("request_attempts")
            page.pop("request_history_version")
        envelope["sha256"] = checksum(envelope["payload"])
        path.write_text(json.dumps(envelope))
        persisted = path.read_bytes()
        again, checked_source = ready_brief_evidence(tmp_path, identity)
    assert again.status == first.status == "ready" and again.attempts == first.attempts == 1
    assert again.pages[0].result == first.pages[0].result and checked_source == original
    assert again.pages[0].request_history_version == 0 and not again.pages[0].request_attempts
    assert path.read_bytes() == persisted and call.call_count == 1


@pytest.mark.asyncio
async def test_unknown_profile_hold_can_resume_after_explicit_supported_primary_change(tmp_path: Path) -> None:
    from digest.reading_preparation import prepare_selected_sources
    from tests.test_main_reading_brief import saved_selection

    model_execution = ModelExecution()
    cfg, progress, packet, report = await saved_selection(tmp_path, execution=model_execution)
    cfg.reading_brief = replace(cfg.reading_brief, provider="groq", model="qwen/qwen3.8-27b")
    identity = report.reviews[0].selections[0].evidence_id

    async def generate(_role: Any, messages: Any, *_args: Any, **_kwargs: Any) -> Any:
        text, usage = response(messages)
        usage["finish_reason"] = "stop"
        return text, usage

    with (patch("digest.reading_brief.fetch_article", AsyncMock(return_value=fetched("Complete source."))) as fetch,
          patch("digest.llm.complete", side_effect=generate) as call):
        first = await prepare_selected_sources(
            progress, packet, report, cfg, tmp_path, time.monotonic() + 1000, execution=model_execution,
        )
        held = load_state(tmp_path, identity)
        assert first.pending == 1 and first.technical_complete == 0
        assert held.status == "pending" and held.attempts == 0 and held.source_sha256 is None
        assert fetch.call_count == call.call_count == 0
        cfg.reading_brief = replace(cfg.reading_brief, model=GROQ.model)
        resumed = await prepare_selected_sources(
            progress, packet, report, cfg, tmp_path, time.monotonic() + 1000, execution=model_execution,
        )
    assert resumed.pending == 0 and resumed.technical_complete == 1
    assert fetch.call_count == call.call_count == 1
    state, _ = ready_brief_evidence(tmp_path, identity)
    assert state.route.model == "qwen/qwen3.8-27b" and state.pages[0].route.model == GROQ.model


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_json", [True, False])
async def test_accepted_invalid_generation_is_persisted_before_validation_and_never_replayed(
    tmp_path: Path, invalid_json: bool,
) -> None:
    model_execution = ModelExecution()
    cfg = config()
    cfg.llm.providers = [GROQ]
    state = saved_brief_state(tmp_path, cfg)
    identity = state.selection.identity

    async def generate(_role: Any, messages: Any, *_args: Any, **_kwargs: Any) -> Any:
        text, usage = response(messages)
        return ("unparseable private output" if invalid_json else text,
                {**usage, "finish_reason": "STOP" if invalid_json else "MAX_TOKENS", "prompt_tokens": 100,
                 "completion_tokens": 99, "total_tokens": 199, "thoughts": "private thoughts"})

    with (patch("digest.reading_brief.fetch_article", AsyncMock(return_value=fetched("Complete source."))),
          patch("digest.llm.count_gemini_tokens", AsyncMock(return_value=100)) as count,
          patch("digest.llm.complete", side_effect=generate) as call):
        await _advance(state, cfg, tmp_path, time.monotonic() + 1000, execution=model_execution)
        first = load_state(tmp_path, identity)
        state = load_state(tmp_path, identity)
        await _advance(state, cfg, tmp_path, time.monotonic() + 1000, execution=model_execution)
        second = load_state(tmp_path, identity)
        cfg.reading_brief = replace(cfg.reading_brief, provider=GROQ.name, model=GROQ.model)
        state = load_state(tmp_path, identity)
        await _advance(state, cfg, tmp_path, time.monotonic() + 1000, execution=model_execution)
    path = state_root(tmp_path) / f"{identity}.json"
    state = load_state(tmp_path, identity)
    attempt = state.pages[0].request_attempts[-1]
    assert first.status == second.status == state.status == "pending" and call.call_count == count.call_count == 1
    assert (first.attempts, second.attempts, state.attempts) == (1, 2, 3)
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
    model_execution = ModelExecution()
    cfg = config()
    cfg.reading_brief = replace(cfg.reading_brief, provider=GROQ.name, model=GROQ.model)
    cfg.llm.providers = [ProviderConfig("gemini", "gemini-3.8-flash")]
    state = saved_brief_state(tmp_path, cfg)
    identity = state.selection.identity

    def save_or_interrupt(state_dir: Path, state: BriefState) -> None:
        if interrupt_after_response and state.pages and any(
            item.kind == "generate" and item.status == "accepted" for item in state.pages[0].request_attempts
        ):
            raise asyncio.CancelledError
        save_state(state_dir, state)

    async def generate(_role: Any, messages: Any, *_args: Any, **_kwargs: Any) -> Any:
        persisted = load_state(tmp_path, identity)
        assert persisted.pages[0].request_attempts[-1].status == "reserved"
        if not interrupt_after_response:
            raise asyncio.CancelledError
        return response(messages)

    with (patch("digest.reading_brief.fetch_article", AsyncMock(return_value=fetched("Complete source."))),
          patch("digest.llm.complete", side_effect=generate) as call,
          patch("digest.llm.count_gemini_tokens", AsyncMock()) as count):
        with (patch("digest.reading_brief.save_state", side_effect=save_or_interrupt),
              pytest.raises(asyncio.CancelledError)):
            await _advance(state, cfg, tmp_path, time.monotonic() + 1000, execution=model_execution)
        cfg.reading_brief = replace(cfg.reading_brief, provider="gemini", model="gemini-3.8-flash")
        state = load_state(tmp_path, identity)
        await _advance(state, cfg, tmp_path, time.monotonic() + 1000, execution=model_execution)
    state = load_state(tmp_path, identity)
    assert state.status == "pending" and state.pages[0].result is None
    assert call.call_count == 1 and count.call_count == 0
    assert state.pages[0].request_attempts[-1].status == "reserved"
    assert state.error_class == "technical_generation_unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
async def test_unknown_count_is_not_repeated_but_supported_local_admission_can_resume(
    tmp_path: Path, legacy: bool,
) -> None:
    model_execution = ModelExecution()
    cfg = config()
    state = saved_brief_state(tmp_path, cfg)
    identity = state.selection.identity

    async def generate(_role: Any, messages: Any, *_args: Any, **kwargs: Any) -> Any:
        assert kwargs["provider_override"].name == "groq"
        return response(messages)

    with (patch("digest.reading_brief.fetch_article", AsyncMock(return_value=fetched("Complete source."))) as fetch,
          patch("digest.llm.count_gemini_tokens", AsyncMock(side_effect=TimeoutError)) as count,
          patch("digest.llm.complete", side_effect=generate) as call):
        await _advance(state, cfg, tmp_path, time.monotonic() + 1000, execution=model_execution)
        first = load_state(tmp_path, identity)
        if legacy:
            path = state_root(tmp_path) / f"{identity}.json"
            envelope = json.loads(path.read_text())
            for page in envelope["payload"]["pages"]:
                page.pop("request_attempts")
                page.pop("request_history_version")
            envelope["sha256"] = checksum(envelope["payload"])
            path.write_text(json.dumps(envelope))
        state = load_state(tmp_path, identity)
        await _advance(state, cfg, tmp_path, time.monotonic() + 1000, execution=model_execution)
        second = load_state(tmp_path, identity)
        assert first.status == second.status == "pending" and count.call_count == 1 and call.call_count == 0
        cfg.llm.providers = [GROQ]
        state = load_state(tmp_path, identity)
        await _advance(state, cfg, tmp_path, time.monotonic() + 1000, execution=model_execution)
    state, _ = ready_brief_evidence(tmp_path, identity)
    assert state.status == "ready" and state.attempts == 3
    assert fetch.call_count == count.call_count == call.call_count == 1
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
    model_execution = ModelExecution()
    cfg = config()
    if provider == "groq":
        cfg.reading_brief = replace(cfg.reading_brief, provider=GROQ.name, model=GROQ.model)
    state = saved_brief_state(tmp_path, cfg)
    identity = state.selection.identity
    with (patch("digest.reading_brief.fetch_article", AsyncMock(return_value=fetched("Complete source."))) as fetch,
          patch("digest.llm.count_gemini_tokens", AsyncMock(return_value=100)) as count,
          patch("digest.llm.complete", AsyncMock(side_effect=TimeoutError)) as call):
        await _advance(state, cfg, tmp_path, time.monotonic() + 1000, execution=model_execution)
        first = load_state(tmp_path, identity)
        path = state_root(tmp_path) / f"{identity}.json"
        envelope = json.loads(path.read_text())
        for page in envelope["payload"]["pages"]:
            page.pop("request_attempts")
            page.pop("request_history_version")
        envelope["sha256"] = checksum(envelope["payload"])
        path.write_text(json.dumps(envelope))
        original = load_state(tmp_path, path.stem)
        state = load_state(tmp_path, identity)
        await _advance(state, cfg, tmp_path, time.monotonic() + 1000, execution=model_execution)
        repeated = load_state(tmp_path, identity)
        cfg.reading_brief = replace(cfg.reading_brief, provider=GROQ.name, model=GROQ.model)
        state = load_state(tmp_path, identity)
        await _advance(state, cfg, tmp_path, time.monotonic() + 1000, execution=model_execution)
    held = load_state(tmp_path, path.stem)
    assert first.status == repeated.status == held.status == "pending" and call.call_count == fetch.call_count == 1
    assert count.call_count == (1 if provider == "gemini" else 0)
    assert held.pages == original.pages and held.source_sha256 == original.source_sha256
    assert held.error_class == "technical_generation_unknown" and not held.pages[0].request_attempts


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["gemini", "groq"])
async def test_new_page_with_no_request_intent_can_resume_after_budget_deferral(tmp_path: Path, provider: str) -> None:
    model_execution = ModelExecution()
    cfg = config()
    if provider == "groq":
        cfg.reading_brief = replace(cfg.reading_brief, provider=GROQ.name, model=GROQ.model)
    llm.set_request_limit(cfg, model_execution, 0)
    state = saved_brief_state(tmp_path, cfg)
    identity = state.selection.identity

    async def generate(_role: Any, messages: Any, *_args: Any, **_kwargs: Any) -> Any:
        return response(messages)

    with (patch("digest.reading_brief.fetch_article", AsyncMock(return_value=fetched("Complete source."))) as fetch,
          patch("digest.llm.count_gemini_tokens", AsyncMock(return_value=100)) as count,
          patch("digest.llm.complete", side_effect=generate) as call):
        await _advance(state, cfg, tmp_path, time.monotonic() + 1000, execution=model_execution)
        state = load_state(tmp_path, identity)
        assert state.status == "pending" and count.call_count == call.call_count == 0
        assert state.pages[0].request_history_version == 1 and not state.pages[0].request_attempts
        llm.set_request_limit(cfg, model_execution, 2)
        state = load_state(tmp_path, identity)
        await _advance(state, cfg, tmp_path, time.monotonic() + 1000, execution=model_execution)
    state, _ = ready_brief_evidence(tmp_path, identity)
    assert state.status == "ready" and state.attempts == 2 and fetch.call_count == call.call_count == 1
    assert count.call_count == (1 if provider == "gemini" else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["hash", "finish", "usage", "missing"])
async def test_completed_generation_must_match_its_accepted_attempt(tmp_path: Path, damage: str) -> None:
    model_execution = ModelExecution()
    cfg = config()
    state = saved_brief_state(tmp_path, cfg)
    async def generate(_role: Any, messages: Any, *_args: Any, **_kwargs: Any) -> Any:
        return response(messages)

    with (patch("digest.reading_brief.fetch_article", AsyncMock(return_value=fetched("Complete source."))),
          patch("digest.llm.count_gemini_tokens", AsyncMock(return_value=100)),
          patch("digest.llm.complete", side_effect=generate) as call):
        await _advance(state, cfg, tmp_path, time.monotonic() + 1000, execution=model_execution)
        identity = state.selection.identity
        state, _ = ready_brief_evidence(tmp_path, identity)
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
        path = state_root(tmp_path) / f"{identity}.json"
        persisted = path.read_bytes()
        with pytest.raises(ValueError, match="result_attempt_binding_mismatch"):
            ready_brief_evidence(tmp_path, identity)
    assert path.read_bytes() == persisted and call.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["deadline", "definite_failed"])
async def test_legacy_count_binding_survives_local_route_progress_and_failures(tmp_path: Path, failure: str) -> None:
    model_execution = ModelExecution()
    cfg = config()
    state = saved_brief_state(tmp_path, cfg)
    identity = state.selection.identity

    async def generate(_role: Any, messages: Any, *_args: Any, **_kwargs: Any) -> Any:
        if failure == "definite_failed":
            raise status_error(503)
        return response(messages)

    with (patch("digest.reading_brief.fetch_article", AsyncMock(return_value=fetched("Complete source."))),
          patch("digest.llm.count_gemini_tokens", AsyncMock(side_effect=TimeoutError)) as count,
          patch("digest.llm.complete", side_effect=generate) as call):
        await _advance(state, cfg, tmp_path, time.monotonic() + 1000, execution=model_execution)
        path = state_root(tmp_path) / f"{identity}.json"
        envelope = json.loads(path.read_text())
        for page in envelope["payload"]["pages"]:
            page.pop("request_attempts")
            page.pop("request_history_version")
            page.pop("legacy_count_request_sha256")
        original_prompt = envelope["payload"]["pages"][0]["prompt_sha256"]
        envelope["sha256"] = checksum(envelope["payload"])
        path.write_text(json.dumps(envelope))
        cfg.llm.providers = [GROQ]
        state = load_state(tmp_path, identity)
        await _advance(state, cfg, tmp_path, time.monotonic() + (20 if failure == "deadline" else 1000),
                       execution=model_execution)
        saved = load_state(tmp_path, path.stem)
        assert saved.status == "pending" and saved.pages[0].request_history_version == 1
        assert saved.pages[0].legacy_count_request_sha256 == original_prompt
        assert saved.pages[0].prompt_sha256 != original_prompt and saved.pages[0].route.provider == "groq"
        state = load_state(tmp_path, identity)
        await _advance(state, cfg, tmp_path, time.monotonic() + 1000, execution=model_execution)
        final = load_state(tmp_path, identity)
    assert count.call_count == 1
    if failure == "deadline":
        final, _ = ready_brief_evidence(tmp_path, identity)
        assert final.status == "ready" and call.call_count == 1
    else:
        assert final.status == "pending" and final.pages[0].result is None and call.call_count == 2
        assert final.error_class == "technical_count_unknown"


@pytest.mark.asyncio
async def test_shared_budget_predispatch_failure_remains_resumable(tmp_path: Path) -> None:
    model_execution = ModelExecution()
    from digest.model_budget import ModelBudgetError

    cfg = config()
    cfg.reading_brief = replace(cfg.reading_brief, provider=GROQ.name, model=GROQ.model)
    state = saved_brief_state(tmp_path, cfg)
    identity = state.selection.identity
    with (patch("digest.reading_brief.fetch_article", AsyncMock(return_value=fetched("Complete source."))),
          patch("digest.llm.complete", side_effect=ModelBudgetError("Local usage write failed before dispatch"))):
        await _advance(state, cfg, tmp_path, time.monotonic() + 1000, execution=model_execution)
    state = load_state(tmp_path, identity)
    assert state.status == "pending" and state.error_class == "technical_quota_or_budget"
    assert state.pages[0].request_attempts[-1].status == "definite_failed"

    async def generate(_role: Any, messages: Any, *_args: Any, **_kwargs: Any) -> Any:
        return response(messages)

    with patch("digest.llm.complete", side_effect=generate) as call:
        state = load_state(tmp_path, identity)
        await _advance(state, cfg, tmp_path, time.monotonic() + 1000, execution=model_execution)
    state, _ = ready_brief_evidence(tmp_path, identity)
    assert state.status == "ready" and state.attempts == 2 and call.call_count == 1
