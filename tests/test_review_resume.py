"""Scheduled report-only resume tests; no live network or delivery is permitted."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from digest.adapters.models.execution import ModelExecution
from digest.review import run_blind_review
from digest.review_resume import execute_resume, prepare_resume
from scripts.review_fixture import fixture_articles, fixture_config, fixture_response

NOW = datetime(2026, 9, 30, 12, tzinfo=UTC)


@pytest.fixture(autouse=True)
def isolated_resume(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    config = fixture_config()
    with (
        patch("digest.review_resume.load_config", side_effect=lambda _: deepcopy(config)),
        patch("httpx.AsyncClient", side_effect=AssertionError("No live HTTP")),
        patch("digest.delivery.send_article_cards", side_effect=AssertionError("No delivery")),
        patch("digest.radar.collect", AsyncMock(side_effect=AssertionError("No collection"))),
        patch("digest.radar.save_dedup_cache", side_effect=AssertionError("No dedup writes")),
    ):
        yield


async def _checkpoint(
    path: Path, *, age: timedelta = timedelta(minutes=10), failed: tuple[str, ...] = ("secondary",),
) -> dict[str, Any]:
    execution = ModelExecution()
    with patch("digest.review.complete", side_effect=fixture_response):
        report = await run_blind_review(fixture_articles(), fixture_config(), execution=execution)
    for review in report.reviews:
        review.generated_at = review.attempted_at = (NOW - age).isoformat()
        if review.slot in failed:
            review.status = "unavailable"
            review.selections = []
            review.limitations = []
            review.generated_at = None
    report.status = "incomplete" if failed else "complete"
    report.reviews = report.reviews[:2]
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = asdict(report)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return payload


def _marker(checkpoint: Path) -> Path:
    return checkpoint.with_name(checkpoint.name.removesuffix(".review.json") + ".resume-attempted.json")


@pytest.mark.asyncio
async def test_prepare_selects_newest_eligible_report_and_persists_safe_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    reports = tmp_path / "digests"
    older = reports / "older.review.json"
    newest = reports / "newest.review.json"
    await _checkpoint(older, age=timedelta(hours=3))
    payload = await _checkpoint(newest)
    await _checkpoint(reports / "complete.review.json", failed=(), age=timedelta(minutes=1))
    await _checkpoint(reports / "stale.review.json", age=timedelta(days=2))
    skipped = reports / "already.review.json"
    await _checkpoint(skipped, age=timedelta(minutes=1))
    _marker(skipped).write_text("{}")
    github_output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(github_output))
    with patch("digest.review.complete", AsyncMock()) as complete:
        chosen = prepare_resume(Path("config.yaml"), reports, NOW)
    complete.assert_not_called()
    assert chosen == newest
    marker = json.loads(_marker(newest).read_text())
    assert marker["checkpoint"] == "digests/newest.review.json"
    assert marker["bundle_id"] == payload["evidence"]["bundle_id"]
    assert marker["checkpoint_sha256"] == hashlib.sha256(newest.read_bytes()).hexdigest()
    assert marker["execution_started_at"] is None
    assert github_output.read_text() == "checkpoint=digests/newest.review.json\n"
    assert not _marker(older).exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["stale", "boundary", "future", "complete", "marked", "unknown_age", "corrupt"])
async def test_prepare_skips_ineligible_reports(kind: str, tmp_path: Path) -> None:
    path = tmp_path / "digests/day.review.json"
    age = {"stale": timedelta(days=2), "boundary": timedelta(days=1), "future": timedelta(minutes=-1)}.get(
        kind, timedelta(minutes=10),
    )
    payload = await _checkpoint(path, age=age, failed=() if kind == "complete" else ("secondary",))
    if kind == "marked":
        _marker(path).write_text("{}")
    if kind == "unknown_age":
        for review in payload["reviews"]:
            review["generated_at"] = review["attempted_at"] = None
    if kind == "corrupt":
        payload["evidence"]["items"][0]["excerpt"] = "tampered"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with patch("digest.review.complete", AsyncMock()) as complete:
        assert prepare_resume(Path("config.yaml"), path.parent, NOW) is None
    complete.assert_not_called()


@pytest.mark.asyncio
async def test_resume_calls_only_missing_slots_and_keeps_production_evidence(tmp_path: Path) -> None:
    execution = ModelExecution()
    path = tmp_path / "digests/day.review.json"
    payload = await _checkpoint(path)
    before = path.read_bytes()
    assert prepare_resume(Path("config.yaml"), path.parent, NOW) == path
    calls = []

    async def response(role: Any, messages: list[dict[str, str]], config: Any, **kwargs: Any) -> tuple:
        calls.append(kwargs["provider_override"].model)
        assert json.loads(_marker(path).read_text())["execution_started_at"]
        assert config.llm.max_retries == 0
        assert config.llm.max_concurrent_requests == 1
        assert config.llm.min_request_interval_seconds == 65.0
        assert config.review.max_evidence_articles == 20
        assert config.review.max_excerpt_chars == 500
        assert config.review.max_output_tokens <= 4096
        assert not config.telegram.enabled and not config.obsidian.enabled and not config.adaptive.enabled
        return await fixture_response(role, messages, config, **kwargs)

    with patch("digest.review.complete", side_effect=response):
        assert await execute_resume(Path("config.yaml"), path, execution=execution) == 0
    assert calls == ["openai/gpt-oss-120b", "qwen/qwen3.8-27b"]
    assert path.read_bytes() == before
    resumed = json.loads((path.parent / "day.review-resumed.json").read_text())
    assert resumed["evidence"] == json.loads(before)["evidence"]
    assert resumed["reviews"][0] == {**payload["reviews"][0], "reused_from_checkpoint": True}
    assert (path.parent / "day.review-resumed.md").exists()
    with patch("digest.review.complete", AsyncMock()) as complete:
        with pytest.raises(ValueError):
            await execute_resume(Path("config.yaml"), path, execution=execution)
    complete.assert_not_called()


@pytest.mark.asyncio
async def test_both_main_failures_recover_with_two_calls_and_explicit_escalation_budget(tmp_path: Path) -> None:
    execution = ModelExecution()
    path = tmp_path / "digests/day.review.json"
    await _checkpoint(path, failed=("primary", "secondary"))
    assert prepare_resume(Path("config.yaml"), path.parent, NOW) == path
    with patch("digest.review.complete", side_effect=fixture_response) as complete:
        assert await execute_resume(Path("config.yaml"), path, execution=execution) == 2
    assert complete.call_count == 2
    resumed = json.loads((path.parent / "day.review-resumed.json").read_text())
    assert [review["slot"] for review in resumed["reviews"]] == ["primary", "secondary"]
    assert resumed["third_model_reason"] == "resume_request_budget_exhausted"
    assert resumed["status"] == "incomplete"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["missing", "changed_bytes", "changed_bundle", "already_started"])
async def test_execute_rejects_missing_or_mismatched_markers_before_model_calls(kind: str, tmp_path: Path) -> None:
    execution = ModelExecution()
    path = tmp_path / "digests/day.review.json"
    await _checkpoint(path)
    if kind != "missing":
        assert prepare_resume(Path("config.yaml"), path.parent, NOW) == path
        marker = json.loads(_marker(path).read_text())
        if kind == "changed_bytes":
            path.write_text(path.read_text() + "\n")
        elif kind == "changed_bundle":
            marker["bundle_id"] = "different"
        else:
            marker["execution_started_at"] = NOW.isoformat()
        _marker(path).write_text(json.dumps(marker))
    with patch("digest.review.complete", AsyncMock()) as complete:
        with pytest.raises(ValueError):
            await execute_resume(Path("config.yaml"), path, execution=execution)
    complete.assert_not_called()
    assert not (path.parent / "day.review-resumed.json").exists()


@pytest.mark.asyncio
async def test_prepare_rejects_unsafe_paths_and_skips_symlinked_checkpoints(tmp_path: Path) -> None:
    path = tmp_path / "digests/day.review.json"
    await _checkpoint(path)
    with pytest.raises(ValueError):
        prepare_resume(Path("config.yaml"), tmp_path.parent, NOW)
    with pytest.raises(ValueError):
        prepare_resume(Path("config.yaml"), Path("digests\ncheckpoint=unsafe"), NOW)
    link = tmp_path / "links/alias.review.json"
    link.parent.mkdir()
    link.symlink_to(path)
    assert prepare_resume(Path("config.yaml"), link.parent, NOW) is None


@pytest.mark.asyncio
async def test_model_outside_approved_lineup_is_rejected_without_requests(tmp_path: Path) -> None:
    from digest.config import ReviewModelConfig

    config = fixture_config()
    config.review.primary = ReviewModelConfig("other", "paid-model")
    with (
        patch("digest.review_resume.load_config", return_value=config),
        patch("digest.review.complete", AsyncMock()) as complete,
    ):
        with pytest.raises(ValueError, match="approved"):
            prepare_resume(Path("config.yaml"), tmp_path / "digests", NOW)
    complete.assert_not_called()


@pytest.mark.asyncio
async def test_resume_preserves_separate_source_provenance_without_promoting_rss_review(tmp_path: Path) -> None:
    execution = ModelExecution()
    checkpoint = tmp_path / "digests/day.review.json"
    payload = await _checkpoint(checkpoint)
    provenance = {"full_source_required": True, "full_source_error": "ValueError",
                  "reading_brief_status": {"pending": 2, "abstained": 0, "oldest_pending": "2026-09-29"}}
    payload.update(provenance)
    checkpoint.write_text(json.dumps(payload))
    assert prepare_resume(Path("fixture.yaml"), checkpoint.parent, NOW) == checkpoint
    with patch("digest.review.complete", side_effect=fixture_response):
        await execute_resume(Path("fixture.yaml"), checkpoint, execution=execution)
    resumed = json.loads(checkpoint.with_name("day.review-resumed.json").read_text())
    assert {key: resumed[key] for key in provenance} == provenance
    assert resumed["evidence"] == json.loads(json.dumps(payload["evidence"]))
    assert "full_source_evidence" not in resumed
