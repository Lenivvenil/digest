"""Actual adapter entry points consume the same persisted cycle allowance."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
import respx

from digest import llm, model_budget
from digest.adapters.models.execution import ModelExecution
from tests.test_llm import _make_config

CYCLE = "456789"


def stage(monkeypatch: pytest.MonkeyPatch, name: str, *, limit: int = 10) -> model_budget.StageExecution:
    claim = model_budget.reserve_stage(CYCLE, name, run_attempt=1, limit=limit)
    execution = model_budget.begin_stage(CYCLE, name, claim.claim_sha, run_attempt=1)
    for key, value in {
        "REQUIRED": "1",
        "CYCLE": CYCLE,
        "STAGE": name,
        "ATTEMPT": "1",
        "CLAIM_SHA": claim.claim_sha,
        "EXECUTION_NONCE": execution.execution_nonce,
    }.items():
        monkeypatch.setenv("DIGEST_MODEL_BUDGET_" + key, value)
    monkeypatch.setenv("GITHUB_RUN_ID", CYCLE)
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    return execution


def finish(execution: model_budget.StageExecution) -> None:
    model_budget.finalize_stage(
        CYCLE, execution.stage, execution.claim_sha, run_attempt=1, execution_nonce=execution.execution_nonce
    )


@pytest.mark.asyncio
@respx.mock
async def test_count_fallback_and_new_process_config_share_remaining_slots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_execution = ModelExecution()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GEMINI_API_KEY", "fixture")
    monkeypatch.setenv("GROQ_API_KEY", "fixture")
    execution = stage(monkeypatch, "prepare", limit=4)
    config = _make_config(
        [
            {"name": "gemini", "model": "fixture", "role": ["summarize"]},
            {"name": "groq", "model": "fixture", "role": ["fallback"]},
        ]
    )
    config.llm.max_retries = 0
    count = respx.post("https://generativelanguage.googleapis.com/v1beta/models/fixture:countTokens").respond(
        200, json={"totalTokens": 100}
    )
    primary = respx.post("https://generativelanguage.googleapis.com/v1beta/models/fixture:generateContent").respond(503)
    fallback = respx.post("https://api.groq.com/openai/v1/chat/completions").respond(
        200, json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}], "usage": {}}
    )
    messages = [{"role": "user", "content": "Public fixture source"}]
    assert (
        await llm.count_gemini_tokens(
            messages, config, execution=model_execution, provider_override=config.llm.providers[0]
        )
        == 100
    )
    assert (await llm.complete(llm.LLMRole.SUMMARIZE, messages, config, execution=model_execution))[0] == "ok"
    assert count.call_count == primary.call_count == fallback.call_count == 1
    assert llm.request_budget_remaining(config, model_execution) == 1
    finish(execution)
    stage(monkeypatch, "irritator")
    fresh_execution = ModelExecution()
    fresh = _make_config([{"name": "groq", "model": "fixture", "role": ["summarize"]}])
    await llm.complete(llm.LLMRole.SUMMARIZE, messages, fresh, execution=fresh_execution)
    with pytest.raises(model_budget.ModelBudgetError, match="exhausted"):
        await llm.complete(llm.LLMRole.SUMMARIZE, messages, fresh, execution=fresh_execution)
    snapshot = model_budget.inspect_budget(CYCLE)
    assert snapshot.reserved_count == 4 and snapshot.remaining == 0
    assert [item.kind for item in snapshot.stages[0].attempts or ()] == ["count", "generate", "generate"]
    assert fallback.call_count == 2


@pytest.mark.asyncio
async def test_missing_usage_or_failed_write_stops_before_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_execution = ModelExecution()
    monkeypatch.chdir(tmp_path)
    execution = stage(monkeypatch, "prepare")
    before = execution.path.read_bytes()
    config = _make_config([{"name": "groq", "model": "fixture", "role": ["summarize"]}])
    with (
        patch("digest.model_budget.atomic_json_write", side_effect=OSError("fixture failure")),
        patch("digest.llm._call_provider", side_effect=AssertionError("No HTTP")),
    ):
        with pytest.raises(model_budget.ModelBudgetError, match="persist"):
            await llm.complete(
                llm.LLMRole.SUMMARIZE, [{"role": "user", "content": "fixture"}], config, execution=model_execution
            )
    assert execution.path.read_bytes() == before
    assert model_budget.inspect_budget(CYCLE).reserved_count == 0
    execution.path.unlink()
    with patch("digest.llm._call_provider", side_effect=AssertionError("No HTTP")):
        with pytest.raises(model_budget.ModelBudgetError):
            await llm.complete(llm.LLMRole.SUMMARIZE, [], config, execution=model_execution)


@pytest.mark.asyncio
async def test_new_process_pacing_reads_previous_stage_reservation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_execution = ModelExecution()
    monkeypatch.chdir(tmp_path)
    earlier = datetime.now(UTC) - timedelta(seconds=20)
    claim = model_budget.reserve_stage(CYCLE, "prepare", run_attempt=1, now=earlier)
    execution = model_budget.begin_stage(CYCLE, "prepare", claim.claim_sha, run_attempt=1, now=earlier)
    model_budget.reserve_request(
        CYCLE,
        "prepare",
        claim.claim_sha,
        run_attempt=1,
        execution_nonce=execution.execution_nonce,
        provider="groq",
        model="fixture",
        kind="generate",
        now=earlier,
    )
    finish(execution)
    stage(monkeypatch, "irritator")
    config = _make_config([{"name": "groq", "model": "fixture"}])
    config.llm.min_request_interval_seconds = 65
    assert 43 < llm.request_wait_seconds(config, model_execution) <= 45


@pytest.mark.asyncio
async def test_budget_failure_does_not_block_accepted_presentation_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_execution = ModelExecution()
    from digest.application.preparation import prepare_edition
    from digest.edition_runtime import ExistingEdition
    from scripts.review_fixture import fixture_config

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DIGEST_MODEL_BUDGET_REQUIRED", "1")
    config = fixture_config()
    expected = ExistingEdition("ready", "fixture-ready-sha")
    with patch("digest.edition_runtime.recover_preparation", return_value=expected):
        assert (
            await prepare_edition(
                config,
                "config.yaml",
                execution=model_execution,
                verbose=False,
                feedback_precollected=True,
                publication_date=None,
                started_at=0,
            )
        ).ready_sha256 == expected.ready_sha256
