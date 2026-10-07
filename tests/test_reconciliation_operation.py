"""Fake HTTP proofs of local operation boundaries, not remote durability or fidelity."""

from __future__ import annotations

import asyncio
import copy
import json
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import httpx
import pytest

from digest import llm, model_budget, source_admission
from digest import reconciliation_operation as operation
from digest.config import Config, ProviderConfig
from digest.reading_brief_state import BriefState, Source, checksum
from digest.reading_brief_tokens import TokenProfileUnavailable
from digest.reading_reconciliation import ReconciliationInput, build_reconciliation_input
from tests.test_reading_brief import config
from tests.test_reading_reconciliation import saved_synthetic as saved_synthetic


@dataclass
class Harness:
    root: Path
    source: Source
    state: BriefState
    value: ReconciliationInput
    config: Config
    prepared: operation.PreparedReconciliation
    checkpoint: operation.ExactIntentCheckpoint
    execution: model_budget.StageExecution
    calls: list[httpx.Request]
    intervals: list[float]
    raw: str

    @property
    def path(self) -> Path:
        return self.root / "article_reconciliation" / f"{self.value.selection.identity}.json"

    def record(self) -> dict[str, Any]:
        data: dict[str, Any] = json.loads(self.path.read_text())["record"]
        return data

    async def run(self, **kwargs: Any) -> operation.OperationResult:
        arguments = {"deadline": time.monotonic() + 1000, "checkpoint": self.checkpoint, **kwargs}
        return await operation.execute_reconciliation_operation(
            self.value,
            self.source,
            self.state,
            self.config,
            self.root,
            **arguments,
        )


def bind_execution(
    monkeypatch: pytest.MonkeyPatch, cycle: str = "123456789", limit: int = 10
) -> model_budget.StageExecution:
    claim = model_budget.reserve_stage(cycle, "prepare", run_attempt=1, limit=limit)
    execution = model_budget.begin_stage(cycle, "prepare", claim.claim_sha, run_attempt=1)
    values = {
        "REQUIRED": "1",
        "CYCLE": cycle,
        "STAGE": "prepare",
        "CLAIM_SHA": execution.claim_sha,
        "ATTEMPT": "1",
        "EXECUTION_NONCE": execution.execution_nonce,
    }
    for key, value in values.items():
        monkeypatch.setenv("DIGEST_MODEL_BUDGET_" + key, value)
    monkeypatch.setenv("GITHUB_RUN_ID", cycle)
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    return execution


@pytest.fixture
def harness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    saved_synthetic: tuple[Source, BriefState],
) -> Harness:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GEMINI_API_KEY", "fake-test-key")
    monkeypatch.setenv("GROQ_API_KEY", "fake-test-key")
    execution = bind_execution(monkeypatch)
    settings = config()
    settings.llm.min_request_interval_seconds = 20
    settings.llm.max_retries = 4  # The operation must disable retries on its copy only.
    settings.llm.providers = [ProviderConfig("groq", "openai/gpt-oss-120b")]
    source, state = saved_synthetic
    value = build_reconciliation_input(source, state)
    prepared = operation.prepare_reconciliation_operation(value, source, state, settings, tmp_path, claim=execution)
    checkpoint = operation.ExactIntentCheckpoint(
        prepared.manifest_sha256, execution.cycle_id, execution.stage, execution.claim_sha, "verified-test-commit"
    )
    raw = json.dumps(
        {
            "selected_span_ids": [value.evidence[0].id],
            "qualification_span_ids": [value.evidence[-1].id],
            "reading_angle": {
                "text": "The reported result applies to pilot clients.",
                "span_ids": [value.evidence[0].id, value.evidence[-1].id],
            },
            "abstain": False,
        }
    )
    result = Harness(tmp_path, source, state, value, settings, prepared, checkpoint, execution, [], [], raw)
    monkeypatch.setattr(
        source_admission,
        "estimate_request",
        lambda route, _messages: source_admission.estimate_record(
            route,
            100,
        ),
    )

    async def pace(_state: Any, interval: float) -> None:
        result.intervals.append(interval)

    monkeypatch.setattr(llm, "_pace_request", pace)
    # Exercise actual request reservations without real waiting. Separate tests
    # inspect route spacing and the operation's absolute deadline preflight.
    monkeypatch.setattr(llm, "request_wait_seconds", lambda _config: 0.0)
    original_client = httpx.AsyncClient

    def client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        return original_client(
            *args, **kwargs, transport=httpx.MockTransport(lambda request: transport(result, request)), trust_env=False
        )

    monkeypatch.setattr(httpx, "AsyncClient", client)
    return result


def transport(harness: Harness, request: httpx.Request) -> httpx.Response:
    harness.calls.append(request)
    record = harness.record()
    kind = "count" if request.url.path.endswith(":countTokens") else "generate"
    assert record["attempts"][-1]["kind"] == kind and record["attempts"][-1]["status"] == "reserved"
    budget = model_budget.inspect_budget(harness.execution.cycle_id)
    assert budget.stages[-1].attempts[-1].kind == kind  # type: ignore[index]
    if kind == "count":
        return httpx.Response(200, json={"totalTokens": 500})
    if request.url.host == "api.groq.com":
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": harness.raw}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 111, "completion_tokens": 22},
            },
        )
    return httpx.Response(
        200,
        json={
            "candidates": [{"content": {"parts": [{"text": harness.raw}]}, "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 500, "candidatesTokenCount": 22},
        },
    )


@pytest.mark.asyncio
async def test_completed_exact_cache_and_shared_physical_charges(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = json.dumps(harness.state.__dict__, default=str, sort_keys=True)
    result = await harness.run()
    assert result.status == "completed" and not result.cached and result.response is not None
    assert result.response.reading_angle == "The reported result applies to pilot clients."
    assert len(harness.calls) == 2 and harness.intervals == [20, 20]
    assert model_budget.inspect_budget(harness.execution.cycle_id).reserved_count == 2
    assert llm.request_budget_remaining(harness.config) == 8
    assert harness.config.llm.max_retries == 4 and harness.config.llm.min_request_interval_seconds == 20
    assert [attempt["status"] for attempt in harness.record()["attempts"]] == ["completed", "completed"]
    assert before == json.dumps(harness.state.__dict__, default=str, sort_keys=True)
    for key in ("REQUIRED", "CYCLE", "STAGE", "CLAIM_SHA", "ATTEMPT", "EXECUTION_NONCE"):
        monkeypatch.delenv("DIGEST_MODEL_BUDGET_" + key)
    again = await harness.run(checkpoint=None, deadline=0)
    assert again.status == "completed" and again.cached and len(harness.calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("checkpoint", [None, "mismatched"])
async def test_missing_or_mismatched_checkpoint_never_dispatches(harness: Harness, checkpoint: str | None) -> None:
    assertion = None if checkpoint is None else replace(harness.checkpoint, manifest_sha256="0" * 64)
    result = await harness.run(checkpoint=assertion)
    assert result.status == "pending" and not harness.calls and not harness.record()["attempts"]
    assert model_budget.inspect_budget(harness.execution.cycle_id).reserved_count == 0


@pytest.mark.asyncio
async def test_missing_record_is_not_created_by_execution(harness: Harness) -> None:
    harness.path.unlink()
    result = await harness.run()
    assert result.error_class == "technical_operation_missing" and not harness.path.exists() and not harness.calls


@pytest.mark.asyncio
async def test_no_active_shared_execution_cannot_grant_allowance(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key in ("REQUIRED", "CYCLE", "STAGE", "CLAIM_SHA", "ATTEMPT", "EXECUTION_NONCE"):
        monkeypatch.delenv("DIGEST_MODEL_BUDGET_" + key)
    assert (await harness.run()).error_class == "technical_request_budget"
    assert not harness.calls and not harness.record()["attempts"]


@pytest.mark.asyncio
async def test_counts_need_generation_capacity_and_deadline(harness: Harness) -> None:
    llm.set_request_limit(harness.config, 1)
    assert (await harness.run()).status == "pending" and not harness.calls
    llm.set_request_limit(harness.config, 10)
    assert (await harness.run(deadline=time.monotonic() + 1)).error_class == "technical_deadline"
    assert not harness.calls and not harness.record()["attempts"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["count_unknown", "generation_unknown", "generation_rejected", "invalid"])
async def test_failure_outcomes_and_alternate_boundaries(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    original = transport

    def handler(current: Harness, request: httpx.Request) -> httpx.Response:
        count = request.url.path.endswith(":countTokens")
        if request.url.host != "api.groq.com" and (
            failure == "count_unknown"
            and count
            or failure in {"generation_unknown", "generation_rejected"}
            and not count
        ):
            current.calls.append(request)
            if failure == "generation_rejected":
                return httpx.Response(429, json={"error": "fake explicit rejection"})
            raise httpx.ReadTimeout("fake timeout", request=request)
        if failure == "invalid":
            current.raw = '{"not_the_protocol": true}'
        return original(current, request)

    monkeypatch.setattr("tests.test_reconciliation_operation.transport", handler)
    result = await harness.run()
    record = harness.record()
    if failure in {"count_unknown", "generation_rejected"}:
        assert result.status == "completed" and result.response is not None
        assert result.response.completion.provider == "groq"
        assert record["attempts"][0 if failure == "count_unknown" else 1]["status"] == (
            "unknown" if failure == "count_unknown" else "definite_failed"
        )
        assert harness.intervals == ([20, 65] if failure == "count_unknown" else [20, 20, 65])
        assert model_budget.inspect_budget(harness.execution.cycle_id).reserved_count == len(harness.calls)
    else:
        assert result.status == "pending" and len(harness.calls) == 2
        assert record["attempts"][-1]["status"] == (
            "unknown" if failure == "generation_unknown" else "accepted_invalid"
        )
        assert record["raw_response"] is None
        assert (await harness.run()).error_class == "technical_generation_held"
        assert len(harness.calls) == 2 and model_budget.inspect_budget(harness.execution.cycle_id).remaining == 8


@pytest.mark.asyncio
async def test_exact_count_is_reused_after_a_pre_dispatch_deadline_hold(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = operation._generation_timeout

    def expired(_config: Config, _deadline: float) -> float:
        raise TimeoutError("technical_deadline")

    monkeypatch.setattr(operation, "_generation_timeout", expired)
    assert (await harness.run()).error_class == "technical_deadline" and len(harness.calls) == 1
    monkeypatch.setattr(operation, "_generation_timeout", original)
    assert (await harness.run()).status == "completed" and len(harness.calls) == 2
    assert [item["kind"] for item in harness.record()["attempts"]] == ["count", "generate"]


@pytest.mark.asyncio
async def test_changed_input_or_route_cannot_bypass_stable_operation(harness: Harness) -> None:
    harness.value = build_reconciliation_input(harness.source, harness.state, extra_context_span_ids=(2,))
    assert (await harness.run()).status == "pending" and not harness.calls
    with pytest.raises(ValueError, match="technical_operation_exists"):
        operation.prepare_reconciliation_operation(
            harness.value, harness.source, harness.state, harness.config, harness.root, claim=harness.execution
        )
    harness.value = build_reconciliation_input(harness.source, harness.state)
    harness.config.reading_brief = replace(harness.config.reading_brief, provider="groq", model="openai/gpt-oss-120b")
    assert (await harness.run()).status == "pending" and not harness.calls


@pytest.mark.asyncio
async def test_foreign_cycle_is_checkpoint_transition_hold_not_generation_unknown(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unknown_count(*_args: Any, **_kwargs: Any) -> int:
        raise RuntimeError("fake count unknown")

    def unavailable(*_args: Any, **_kwargs: Any) -> dict[str, int | str]:
        raise TokenProfileUnavailable("fake tokenizer unavailable")

    monkeypatch.setattr(llm, "count_gemini_tokens", unknown_count)
    monkeypatch.setattr(source_admission, "estimate_request", unavailable)
    assert (await harness.run()).status == "pending"
    assert harness.record()["attempts"][0]["status"] == "unknown"
    execution = bind_execution(monkeypatch, cycle="987654321")
    harness.checkpoint = replace(harness.checkpoint, cycle_id=execution.cycle_id, claim_sha=execution.claim_sha)
    result = await harness.run()
    assert result.error_class == "technical_checkpoint_transition_required"
    assert harness.record()["attempts"][0]["kind"] == "count" and not harness.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("crash_after_return", [False, True])
async def test_interrupted_generation_keeps_intent_and_prevents_replay(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
    crash_after_return: bool,
) -> None:
    original = operation._save

    def save(path: Path, record: operation.OperationRecord) -> None:
        if record.attempts and record.attempts[-1].kind == "generate":
            if crash_after_return and record.attempts[-1].status == "accepted":
                raise OSError("fake runner disk failure")
            if not crash_after_return and record.attempts[-1].status == "reserved":
                original(path, record)
                raise asyncio.CancelledError
        original(path, record)

    monkeypatch.setattr(operation, "_save", save)
    if crash_after_return:
        assert (await harness.run()).status == "pending"
    else:
        with pytest.raises(asyncio.CancelledError):
            await harness.run()
    monkeypatch.setattr(operation, "_save", original)
    assert harness.record()["attempts"][-1]["status"] == "reserved"
    calls = len(harness.calls)
    assert (await harness.run()).error_class == "technical_generation_held" and len(harness.calls) == calls


@pytest.mark.asyncio
async def test_corrupt_completed_cache_is_held(harness: Harness) -> None:
    assert (await harness.run()).status == "completed"
    payload = json.loads(harness.path.read_text())
    payload["record"]["raw_response"] = "changed cached response"
    harness.path.write_text(json.dumps(payload))
    assert (await harness.run()).status == "pending" and len(harness.calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["count", "generate"])
async def test_elapsed_adapter_timeout_retains_kind_specific_uncertainty(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    original = transport

    def handler(current: Harness, request: httpx.Request) -> httpx.Response:
        count = request.url.path.endswith(":countTokens")
        if request.url.host != "api.groq.com" and count == (kind == "count"):
            current.calls.append(request)
            raise TimeoutError("fake adapter timeout before absolute deadline")
        return original(current, request)

    monkeypatch.setattr("tests.test_reconciliation_operation.transport", handler)
    result = await harness.run()
    if kind == "count":
        assert result.status == "completed" and result.response is not None
        assert result.response.completion.provider == "groq"
        assert harness.record()["attempts"][0]["status"] == "unknown"
    else:
        assert result.error_class == "technical_generation_unknown"
        assert len(harness.calls) == 2 and harness.record()["attempts"][-1]["status"] == "unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize("malformed", ["text", "finish", "truncated"])
async def test_accepted_invalid_adapter_output_holds_without_repair(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
    malformed: str,
) -> None:
    original = llm.complete

    async def generate(*args: Any, **kwargs: Any) -> Any:
        text, usage = await original(*args, **kwargs)
        if malformed == "text":
            return ["invalid non-string content"], usage
        return text, {**usage, "finish_reason": ["STOP"] if malformed == "finish" else "MAX_TOKENS"}

    monkeypatch.setattr(llm, "complete", generate)
    assert (await harness.run()).status == "pending"
    record = harness.record()
    assert record["attempts"][-1]["status"] == "accepted_invalid" and record["raw_response"] is None
    assert (await harness.run()).error_class == "technical_generation_held"
    assert len(harness.calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("earlier", ["reserved", "unknown", "accepted_invalid"])
async def test_rehashed_contradictory_completed_cache_cannot_hide_prior_generation(
    harness: Harness,
    earlier: str,
) -> None:
    assert (await harness.run()).status == "completed"
    payload = json.loads(harness.path.read_text())
    original = payload["record"]["attempts"][-1]
    later = copy.deepcopy(original)
    later["request_index"] = 1
    original["status"] = earlier
    original["finished_at"] = None if earlier == "reserved" else original["finished_at"]
    if earlier != "accepted_invalid":
        original["response_sha256"] = None
        original["finish_reason"] = None
        original["usage"] = {}
    original["error_class"] = "technical_invalid_output" if earlier == "accepted_invalid" else None
    payload["record"]["attempts"].append(later)
    payload["sha256"] = checksum(payload["record"])
    harness.path.write_text(json.dumps(payload))
    result = await harness.run()
    assert result.error_class == "technical_operation_integrity" and len(harness.calls) == 2


@pytest.mark.asyncio
async def test_concurrent_caller_holds_while_original_dispatches(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()
    finish = asyncio.Event()
    original = llm.count_gemini_tokens

    async def count(*args: Any, **kwargs: Any) -> int:
        started.set()
        await finish.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(llm, "count_gemini_tokens", count)
    running = asyncio.create_task(harness.run())
    await started.wait()
    assert (await harness.run()).error_class == "technical_operation_active"
    finish.set()
    assert (await running).status == "completed" and len(harness.calls) == 2


@pytest.mark.asyncio
async def test_restored_prepared_checkpoint_cannot_look_new_under_a_later_cycle(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remote_prepared_only = harness.path.read_bytes()
    assert (await harness.run()).status == "completed"
    harness.path.write_bytes(remote_prepared_only)  # Local attempts/results were lost with the runner.
    next_execution = bind_execution(monkeypatch, cycle="987654321")
    harness.checkpoint = replace(
        harness.checkpoint,
        cycle_id=next_execution.cycle_id,
        claim_sha=next_execution.claim_sha,
        checkpoint_reference="another-verified-commit",
    )
    before = len(harness.calls)
    assert (await harness.run()).error_class == "technical_checkpoint_transition_required"
    assert len(harness.calls) == before
    assert model_budget.inspect_budget(next_execution.cycle_id).reserved_count == 0
    assert harness.record()["prepared"]["origin"]["cycle_id"] == harness.execution.cycle_id


@pytest.mark.asyncio
async def test_oversized_complete_envelope_is_retained_without_generation_or_recount(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = transport

    def handler(current: Harness, request: httpx.Request) -> httpx.Response:
        response = original(current, request)
        if request.url.path.endswith(":countTokens"):
            return httpx.Response(
                200, json={"totalTokens": source_admission.INPUT_LIMITS["gemini", "gemini-3.8-flash"] + 1}
            )
        return response

    monkeypatch.setattr("tests.test_reconciliation_operation.transport", handler)
    monkeypatch.setattr(
        source_admission, "estimate_request", lambda route, _messages: source_admission.estimate_record(route, 10_000)
    )
    original_input = harness.record()["prepared"]["input_json"]
    assert (await harness.run()).error_class == "technical_admission_capacity"
    assert len(harness.calls) == 1 and harness.record()["prepared"]["input_json"] == original_input
    assert (await harness.run()).error_class == "technical_admission_capacity" and len(harness.calls) == 1


@pytest.mark.asyncio
async def test_existing_physical_reservations_are_not_reset(harness: Harness) -> None:
    for _ in range(9):
        model_budget.reserve_request_from_env(provider="gemini", model="gemini-3.8-flash", kind="generate")
    result = await harness.run()
    assert result.error_class == "technical_request_budget" and not harness.calls
    assert model_budget.inspect_budget(harness.execution.cycle_id).reserved_count == 9


@pytest.mark.asyncio
async def test_shared_pacing_wait_stays_inside_absolute_deadline(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(llm, "request_wait_seconds", lambda _config: 20.0 if harness.calls else 0.0)
    result = await harness.run(deadline=time.monotonic() + 40)
    assert result.error_class == "technical_deadline" and len(harness.calls) == 1
    assert harness.record()["attempts"][0]["kind"] == "count"
    assert harness.record()["attempts"][0]["status"] == "completed"


@pytest.mark.asyncio
async def test_local_primary_can_resume_after_alternate_count_when_tokenizer_recovers(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness.path.unlink()  # Replace the untouched test fixture before any invocation.
    harness.config.reading_brief = replace(harness.config.reading_brief, provider="groq", model="openai/gpt-oss-120b")
    harness.config.llm.providers = [ProviderConfig("gemini", "gemini-3.8-flash")]
    harness.prepared = operation.prepare_reconciliation_operation(
        harness.value,
        harness.source,
        harness.state,
        harness.config,
        harness.root,
        claim=harness.execution,
    )
    harness.checkpoint = replace(harness.checkpoint, manifest_sha256=harness.prepared.manifest_sha256)
    original_estimate = source_admission.estimate_request
    original_timeout = operation._generation_timeout

    def unavailable(*_args: Any, **_kwargs: Any) -> dict[str, int | str]:
        raise TokenProfileUnavailable("fake tokenizer unavailable")

    def deadline(_config: Config, _deadline: float) -> float:
        raise TimeoutError("fake remaining time insufficient after count")

    monkeypatch.setattr(source_admission, "estimate_request", unavailable)
    monkeypatch.setattr(operation, "_generation_timeout", deadline)
    assert (await harness.run()).error_class == "technical_deadline"
    assert [item["request_index"] for item in harness.record()["attempts"]] == [1]
    monkeypatch.setattr(source_admission, "estimate_request", original_estimate)
    monkeypatch.setattr(operation, "_generation_timeout", original_timeout)
    result = await harness.run()
    assert result.status == "completed" and result.response is not None
    assert result.response.completion.provider == "groq"
    assert [item["request_index"] for item in harness.record()["attempts"]] == [1, 0]
    assert (await harness.run()).cached and len(harness.calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["origin", "nonce", "checkpoint"])
async def test_completed_cache_requires_consistent_original_execution(harness: Harness, damage: str) -> None:
    assert (await harness.run()).status == "completed"
    payload = json.loads(harness.path.read_text())
    execution = payload["record"]["execution"]
    if damage == "origin":
        execution["cycle_id"] = "987654321"
    elif damage == "nonce":
        execution["execution_nonce"] = None
    else:
        execution["checkpoint_reference"] = ""
    payload["sha256"] = checksum(payload["record"])
    harness.path.write_text(json.dumps(payload))
    assert (await harness.run()).error_class == "technical_operation_integrity"
    assert len(harness.calls) == 2
