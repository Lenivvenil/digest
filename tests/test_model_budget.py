"""One bounded, durable allowance covers every model attempt in a product cycle."""

from __future__ import annotations

import hashlib
import json
import multiprocessing
from concurrent.futures import ProcessPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from digest import model_budget
from digest.model_budget import (
    ModelBudgetError,
    StageExecution,
    begin_stage,
    finalize_stage,
    inspect_budget,
    reserve_request,
    reserve_request_from_env,
    reserve_stage,
)

NOW = datetime(2026, 10, 4, 12, tzinfo=UTC)
CYCLE = "123456789"


def _begin(cache: Path, stage: str = "prepare", *, limit: int = 10) -> StageExecution:
    claim = reserve_stage(CYCLE, stage, cache, run_attempt=1, limit=limit, now=NOW)
    return begin_stage(CYCLE, stage, claim.claim_sha, run_attempt=1, cache_dir=cache, now=NOW)


def _request(cache: Path, execution: StageExecution, *, kind: str = "generate", provider: str = "primary") -> None:
    reserve_request(
        CYCLE,
        execution.stage,
        execution.claim_sha,
        run_attempt=execution.run_attempt,
        execution_nonce=execution.execution_nonce,
        provider=provider,
        model="vendor/model-v1",
        kind=kind,
        cache_dir=cache,
        now=NOW,
    )


def _finish(cache: Path, execution: StageExecution) -> model_budget.BudgetSnapshot:
    return finalize_stage(
        CYCLE,
        execution.stage,
        execution.claim_sha,
        cache,
        run_attempt=execution.run_attempt,
        execution_nonce=execution.execution_nonce,
        now=NOW,
    )


def _rehash(record: dict[str, Any]) -> None:
    body = {key: value for key, value in record.items() if key != "sha256"}
    record["sha256"] = hashlib.sha256(
        json.dumps(
            body,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()


def _process_request(cache: str, claim_sha: str, nonce: str) -> bool:
    try:
        reserve_request(
            CYCLE,
            "prepare",
            claim_sha,
            run_attempt=1,
            execution_nonce=nonce,
            provider="provider",
            model="model",
            kind="generate",
            cache_dir=cache,
        )
        return True
    except ModelBudgetError:
        return False


def test_three_stages_share_ten_including_counting_fallback_and_unknown_outcome(tmp_path: Path) -> None:
    prepare = _begin(tmp_path)
    _request(tmp_path, prepare, kind="count_tokens")
    _request(tmp_path, prepare)  # Even a credential failure or unknown HTTP result is not refunded.
    _request(tmp_path, prepare, provider="fallback")
    result = _finish(tmp_path, prepare)
    assert result.reserved_count == 3 and result.remaining == 7
    irritator = _begin(tmp_path, "irritator")
    assert irritator.remaining == 7
    for _ in range(4):
        _request(tmp_path, irritator)
    assert _finish(tmp_path, irritator).remaining == 3
    comparison = _begin(tmp_path, "comparison")
    assert comparison.remaining == 3
    for _ in range(3):
        _request(tmp_path, comparison)
    with pytest.raises(ModelBudgetError, match="exhausted"):
        _request(tmp_path, comparison)
    final = _finish(tmp_path, comparison)
    assert final.reserved_count == 10 and final.remaining == 0 and final.active_stage is None
    assert [stage.allowance for stage in final.stages] == [10, 7, 3]
    assert list((tmp_path / "model_budgets").iterdir()) == [prepare.path]


def test_cross_process_reservations_are_locked_and_never_exceed_ten(tmp_path: Path) -> None:
    execution = _begin(tmp_path)
    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=6, mp_context=context) as pool:
        futures = [
            pool.submit(_process_request, str(tmp_path), execution.claim_sha, execution.execution_nonce)
            for _ in range(24)
        ]
        assert sum(future.result(timeout=30) for future in futures) == 10
    state = inspect_budget(CYCLE, tmp_path)
    assert state.reserved_count == 10 and state.remaining == 0


@pytest.mark.parametrize("finalized", [False, True])
def test_same_stage_and_cycle_rerun_never_regrant(tmp_path: Path, finalized: bool) -> None:
    execution = _begin(tmp_path)
    if finalized:
        _finish(tmp_path, execution)
    before = execution.path.read_bytes()
    for stage, attempt in (("prepare", 1), ("prepare", 2), ("irritator", 2), ("comparison", 2)):
        with pytest.raises(ModelBudgetError):
            reserve_stage(CYCLE, stage, tmp_path, run_attempt=attempt, now=NOW)
    assert execution.path.read_bytes() == before


def test_active_unknown_prepare_prevents_optional_fresh_allowance(tmp_path: Path) -> None:
    claim = reserve_stage(CYCLE, "prepare", tmp_path, run_attempt=1, now=NOW)
    for stage in ("irritator", "comparison"):
        with pytest.raises(ModelBudgetError, match="active|unknown"):
            reserve_stage(CYCLE, stage, tmp_path, run_attempt=1, now=NOW)
    with pytest.raises(ModelBudgetError, match="journal"):
        finalize_stage(CYCLE, "prepare", claim.claim_sha, tmp_path, run_attempt=1, execution_nonce="1" * 32, now=NOW)
    assert inspect_budget(CYCLE, tmp_path).active_stage == "prepare"


def test_fetched_initial_claim_cannot_finalize_after_local_usage_is_lost(tmp_path: Path) -> None:
    claim = reserve_stage(CYCLE, "prepare", tmp_path, run_attempt=1, now=NOW)
    remote_initial_claim = claim.path.read_bytes()
    execution = begin_stage(CYCLE, "prepare", claim.claim_sha, run_attempt=1, cache_dir=tmp_path, now=NOW)
    _request(tmp_path, execution)
    claim.path.write_bytes(remote_initial_claim)  # A valid remote claim is not an empty execution journal.
    with pytest.raises(ModelBudgetError, match="journal"):
        _finish(tmp_path, execution)
    with pytest.raises(ModelBudgetError, match="journal"):
        _request(tmp_path, execution)
    with pytest.raises(ModelBudgetError):
        reserve_stage(CYCLE, "comparison", tmp_path, run_attempt=1, now=NOW)
    with pytest.raises(ModelBudgetError):
        begin_stage(CYCLE, "prepare", claim.claim_sha, run_attempt=2, cache_dir=tmp_path, now=NOW)


@pytest.mark.parametrize("change", ["checksum", "field", "journal", "nonce", "count", "cycle", "duplicate"])
def test_corrupt_usage_fails_closed(tmp_path: Path, change: str) -> None:
    execution = _begin(tmp_path)
    _request(tmp_path, execution)
    record = json.loads(execution.path.read_text())
    if change == "checksum":
        record["stages"][0]["attempts"][0]["model"] = "modified"
    elif change == "field":
        record["stages"][0]["attempts"][0]["prompt"] = "content-must-never-be-stored"
    elif change == "journal":
        record["stages"][0]["attempts"] = None
    elif change == "nonce":
        record["stages"][0]["execution_nonce"] = None
    elif change == "count":
        record["stages"][0]["allowance"] = True
    elif change == "cycle":
        record["cycle_id"] = "987654321"
    if change != "checksum":
        _rehash(record)
    payload = json.dumps(record)
    if change == "duplicate":
        payload = payload.replace('"schema_version": 1', '"schema_version": 1, "schema_version": 1')
    execution.path.write_text(payload)
    for action in (
        lambda: inspect_budget(CYCLE, tmp_path),
        lambda: _request(tmp_path, execution),
        lambda: _finish(tmp_path, execution),
    ):
        with pytest.raises(ModelBudgetError):
            action()


def test_missing_usage_and_missing_rerun_state_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(ModelBudgetError):
        inspect_budget(CYCLE, tmp_path)
    execution = _begin(tmp_path)
    execution.path.unlink()
    with pytest.raises(ModelBudgetError):
        _request(tmp_path, execution)
    with pytest.raises(ModelBudgetError):
        _finish(tmp_path, execution)
    with pytest.raises(ModelBudgetError):
        reserve_stage(CYCLE, "prepare", tmp_path, run_attempt=2, now=NOW)
    with pytest.raises(ModelBudgetError):
        reserve_stage(CYCLE, "irritator", tmp_path, run_attempt=1, now=NOW)


def test_claim_stage_cycle_attempt_and_nonce_must_match(tmp_path: Path) -> None:
    execution = _begin(tmp_path)
    default: dict[str, Any] = {
        "cycle_id": CYCLE,
        "stage": "prepare",
        "claim_sha": execution.claim_sha,
        "run_attempt": 1,
        "execution_nonce": execution.execution_nonce,
    }
    for key, value in (
        ("cycle_id", "987"),
        ("stage", "comparison"),
        ("claim_sha", "0" * 64),
        ("run_attempt", 2),
        ("run_attempt", True),
        ("execution_nonce", "0" * 32),
    ):
        with pytest.raises(ModelBudgetError):
            reserve_request(
                **{**default, key: value},
                provider="provider",
                model="model",
                kind="generate",
                cache_dir=tmp_path,
                now=NOW,
            )
    assert inspect_budget(CYCLE, tmp_path).reserved_count == 0
    with pytest.raises(ModelBudgetError, match="already began"):
        begin_stage(CYCLE, "prepare", execution.claim_sha, run_attempt=1, cache_dir=tmp_path, now=NOW)


def test_finalized_stage_rejects_late_requests_and_repeated_finalize(tmp_path: Path) -> None:
    execution = _begin(tmp_path)
    _request(tmp_path, execution)
    _finish(tmp_path, execution)
    with pytest.raises(ModelBudgetError):
        _request(tmp_path, execution)
    with pytest.raises(ModelBudgetError):
        _finish(tmp_path, execution)
    comparison = _begin(tmp_path, "comparison")
    assert comparison.remaining == 9
    _finish(tmp_path, comparison)
    with pytest.raises(ModelBudgetError):
        reserve_stage(CYCLE, "irritator", tmp_path, run_attempt=1, now=NOW)


@pytest.mark.parametrize("limit", [0, 4, 10])
def test_allowance_may_be_lower_but_never_higher(tmp_path: Path, limit: int) -> None:
    execution = _begin(tmp_path, limit=limit)
    for _ in range(limit):
        _request(tmp_path, execution)
    with pytest.raises(ModelBudgetError, match="exhausted"):
        _request(tmp_path, execution)
    assert _finish(tmp_path, execution).remaining == 0


@pytest.mark.parametrize("limit", [-1, 11, True, 10.0])
def test_invalid_limit_rejected(tmp_path: Path, limit: Any) -> None:
    with pytest.raises(ModelBudgetError):
        reserve_stage(CYCLE, "prepare", tmp_path, run_attempt=1, limit=limit, now=NOW)


def test_shared_reservation_time_exposes_prior_process_pacing(tmp_path: Path) -> None:
    execution = _begin(tmp_path)
    args = {
        "run_attempt": 1,
        "execution_nonce": execution.execution_nonce,
        "provider": "provider",
        "model": "model",
        "kind": "generate",
        "cache_dir": tmp_path,
    }
    first = reserve_request(CYCLE, "prepare", execution.claim_sha, now=NOW, **args)  # type: ignore[arg-type]
    second = reserve_request(CYCLE, "prepare", execution.claim_sha, now=NOW + timedelta(seconds=2), **args)  # type: ignore[arg-type]
    assert first.previous_reserved_at is None
    assert second.previous_reserved_at == NOW and second.reserved_at == NOW + timedelta(seconds=2)
    assert inspect_budget(CYCLE, tmp_path).last_reserved_at == second.reserved_at


def test_only_safe_metadata_is_persisted(tmp_path: Path) -> None:
    execution = _begin(tmp_path)
    _request(tmp_path, execution)
    record = json.loads(execution.path.read_text())
    attempt = record["stages"][0]["attempts"][0]
    assert set(attempt) == {"provider", "model", "kind", "reserved_at"}
    with pytest.raises(ModelBudgetError, match="identifiers"):
        _request(tmp_path, execution, provider="prompt: secret content\n")
    assert "secret" not in execution.path.read_text()


def test_unsafe_paths_and_cycle_identifiers_are_rejected(tmp_path: Path) -> None:
    for cycle in ("../other", "1/2", "", "01", "1\n", "-1"):
        with pytest.raises(ModelBudgetError):
            reserve_stage(cycle, "prepare", tmp_path, run_attempt=1, now=NOW)
    cache = tmp_path / "link"
    cache.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ModelBudgetError, match="symlinks"):
        reserve_stage(CYCLE, "prepare", cache, run_attempt=1, now=NOW)
    execution = _begin(tmp_path)
    execution.path.with_suffix(".json.tmp").symlink_to(tmp_path / "unrelated")
    with pytest.raises(ModelBudgetError, match="symlinks"):
        _request(tmp_path, execution)


def test_env_helper_never_initializes_and_required_missing_claim_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("REQUIRED", "CYCLE", "STAGE", "CLAIM_SHA", "ATTEMPT", "EXECUTION_NONCE"):
        monkeypatch.delenv("DIGEST_MODEL_BUDGET_" + name, raising=False)
    monkeypatch.setenv("GITHUB_RUN_ID", CYCLE)
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    assert reserve_request_from_env(provider="p", model="m", kind="generate", cache_dir=tmp_path) is None
    monkeypatch.setenv("DIGEST_MODEL_BUDGET_REQUIRED", "1")
    with pytest.raises(ModelBudgetError, match="missing"):
        reserve_request_from_env(provider="p", model="m", kind="generate", cache_dir=tmp_path)
    execution = _begin(tmp_path)
    for name, value in {
        "CYCLE": CYCLE,
        "STAGE": "prepare",
        "CLAIM_SHA": execution.claim_sha,
        "ATTEMPT": "1",
        "EXECUTION_NONCE": execution.execution_nonce,
    }.items():
        monkeypatch.setenv("DIGEST_MODEL_BUDGET_" + name, value)
    reservation = reserve_request_from_env(provider="p", model="m", kind="count_tokens", cache_dir=tmp_path, now=NOW)
    assert reservation is not None and reservation.remaining == 9
    assert inspect_budget(CYCLE, tmp_path).reserved_count == 1
    monkeypatch.setenv("GITHUB_RUN_ID", "987654321")
    with pytest.raises(ModelBudgetError, match="GitHub run identity"):
        reserve_request_from_env(provider="p", model="m", kind="generate", cache_dir=tmp_path, now=NOW)
    monkeypatch.setenv("DIGEST_MODEL_BUDGET_REQUIRED", "true")
    with pytest.raises(ModelBudgetError, match="zero or one"):
        reserve_request_from_env(provider="p", model="m", kind="generate", cache_dir=tmp_path, now=NOW)


def test_begin_and_finalize_write_failures_leave_allowance_held(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claim = reserve_stage(CYCLE, "prepare", tmp_path, run_attempt=1, now=NOW)
    before = claim.path.read_bytes()
    original_write = model_budget.atomic_json_write

    def fail_write(path: Path, data: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(model_budget, "atomic_json_write", fail_write)
    with pytest.raises(ModelBudgetError):
        begin_stage(CYCLE, "prepare", claim.claim_sha, run_attempt=1, cache_dir=tmp_path, now=NOW)
    assert claim.path.read_bytes() == before
    monkeypatch.setattr(model_budget, "atomic_json_write", original_write)
    execution = begin_stage(CYCLE, "prepare", claim.claim_sha, run_attempt=1, cache_dir=tmp_path, now=NOW)
    _request(tmp_path, execution)
    monkeypatch.setattr(model_budget, "atomic_json_write", fail_write)
    with pytest.raises(ModelBudgetError):
        _finish(tmp_path, execution)
    snapshot = inspect_budget(CYCLE, tmp_path)
    assert snapshot.active_stage == "prepare" and snapshot.reserved_count == 1
    with pytest.raises(ModelBudgetError, match="active"):
        reserve_stage(CYCLE, "comparison", tmp_path, run_attempt=1, now=NOW)


def test_cli_claim_begin_finalize_and_inspect_outputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("GITHUB_RUN_ID", CYCLE)
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    common = ["--cache-dir", str(tmp_path), "--stage", "prepare"]
    assert model_budget.main(["reserve", *common]) == 0
    claim = inspect_budget(CYCLE, tmp_path)
    assert f"claim_sha={claim.claim_sha}\n" in output.read_text()
    assert f"path={claim.path}\n" in output.read_text() and "remaining=10\n" in output.read_text()
    assert model_budget.main(["begin", *common, "--claim-sha", claim.claim_sha]) == 0
    nonce = inspect_budget(CYCLE, tmp_path).stages[-1].execution_nonce
    assert nonce is not None and f"execution_nonce={nonce}\n" in output.read_text()
    assert model_budget.main(["finalize", *common, "--claim-sha", claim.claim_sha, "--execution-nonce", nonce]) == 0
    assert model_budget.main(["inspect", "--cache-dir", str(tmp_path)]) == 0
    assert model_budget.main(["reserve", *common]) == 1
