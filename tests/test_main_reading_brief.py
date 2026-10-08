"""Source-work integration preserves the deployed accepted-preparation boundary."""
from __future__ import annotations

import json
import time
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from digest.adapters.models.execution import ModelExecution
from digest.application.preparation import prepare_sources
from digest.candidate_review import CandidateProgress, begin_packet, merge_candidates, plan_packet, reconcile_packet
from digest.config import ReadingBriefConfig
from digest.reading_preparation import prepare_selected_sources, reading_deadline, setup_reading_budget
from digest.review import run_primary_review
from tests.test_candidate_review import NOW, population
from tests.test_reading_brief import fetched, response


async def saved_selection(
    tmp_path: Path, *, execution: ModelExecution, description: str | None = None, all_selected: bool = False,
) -> tuple[Any, Any, Any, Any]:
    config, articles = population(2)
    if description is not None:
        articles["tech"][0].description = description
    config.reading_brief = ReadingBriefConfig(True, "gemini", "gemini-3.8-flash")
    progress = merge_candidates(CandidateProgress(), articles, config, {}, now=NOW)
    packet = plan_packet(progress, config, NOW)
    assert packet is not None
    begin_packet(progress, packet, tmp_path)

    async def select(_role: Any, messages: list[dict[str, str]], *_args: Any, **_kwargs: Any) -> Any:
        items = json.loads(messages[1]["content"])["evidence"]["items"]
        chosen = items if all_selected else items[:1]
        return json.dumps({"selections": [{"evidence_id": item["evidence_id"], "reason": "Useful mechanism",
                                           "quote": item["title"], "confidence": "high"} for item in chosen],
                           "limitations": ["RSS evidence only"]}), {"finish_reason": "stop"}

    with patch("digest.application.review.complete", side_effect=select):
        report = await run_primary_review(articles, config, execution=execution)
    reconcile_packet(progress, packet, report, config, tmp_path)
    return config, progress, packet, report


async def generate(_role: Any, messages: list[dict[str, str]], *_args: Any, **_kwargs: Any) -> Any:
    return response(messages)


@pytest.mark.asyncio
async def test_saved_candidate_selection_becomes_technical_handoff_without_presentation(tmp_path: Path) -> None:
    execution = ModelExecution()
    config, progress, packet, report = await saved_selection(tmp_path, execution=execution)
    with (patch("digest.reading_brief.fetch_article",
                AsyncMock(return_value=fetched("Complete source mechanism."))) as fetch,
          patch("digest.llm.count_gemini_tokens", AsyncMock(return_value=100)) as count,
          patch("digest.llm.complete", side_effect=generate) as model,
          patch("digest.application.review.complete", side_effect=AssertionError("No repeated RSS selection")),
          patch("digest.translation.translate_publication_presentation",
                side_effect=AssertionError("No presentation"))):
        first = await prepare_selected_sources(
            progress, packet, report, config, tmp_path, time.monotonic() + 1000, execution=execution,
        )
        second = await prepare_selected_sources(
            progress, packet, report, config, tmp_path, time.monotonic() + 1000, execution=execution,
        )
    assert first.technical_complete == second.technical_complete == 1
    assert first.status == "semantic_reconciliation_pending"
    assert fetch.call_count == count.call_count == model.call_count == 1
    assert not (tmp_path / "pending_preparation.json").exists()
    assert not (tmp_path / "prepared_edition.json").exists()
    handoff = json.loads(Path(first.handoff_paths[0]).read_text())
    assert handoff["status"] == "technical_complete_semantic_review_pending"
    assert handoff["binding"]["response_sha256"] == report.reviews[0].response_sha256
    assert Path(handoff["source_path"]).exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["excluded", "occurrence", "proof", "newer_decision"])
async def test_current_selection_policy_and_exact_lineage_gate_before_calls(tmp_path: Path, change: str) -> None:
    execution = ModelExecution()
    config, progress, packet, report = await saved_selection(tmp_path, execution=execution)
    identity = report.reviews[0].selections[0].evidence_id
    candidate = progress.candidates[identity]
    if change == "excluded":
        candidate.eligible = False
    elif change == "occurrence":
        candidate.article = replace(candidate.article, description="Different current occurrence")
    elif change == "newer_decision":
        candidate.status = "not_selected"
    else:
        report.reviews[0].response_sha256 = None
    with patch("digest.reading_preparation._advance", side_effect=AssertionError("No unbound request")):
        result = await prepare_selected_sources(
            progress, packet, report, config, tmp_path, time.monotonic() + 1000, execution=execution,
        )
    assert result.technical_complete == 0 and result.outcomes[0].state == "held"


@pytest.mark.asyncio
async def test_accepted_preparation_resume_never_reenters_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution = ModelExecution()
    config, _, _, _ = await saved_selection(tmp_path, execution=execution)
    monkeypatch.chdir(tmp_path)
    from digest.edition_runtime import ExistingEdition

    expected = ExistingEdition("ready", "fixture-ready-sha")
    with (patch("digest.edition_runtime.recover_preparation", return_value=expected) as resume,
          patch("digest.reading_preparation.prepare_selected_sources", side_effect=AssertionError("No source work"))):
        assert (await prepare_sources(
            config, "config.yaml", verbose=False, feedback_precollected=True, publication_date=None,
            started_at=time.monotonic(), execution=execution,
        )).ready_sha256 == expected.ready_sha256
    resume.assert_called_once()


@pytest.mark.asyncio
async def test_reading_budget_keeps_existing_spend_and_dispatch_reserve(tmp_path: Path) -> None:
    execution = ModelExecution()
    from digest import llm

    config, _, _, _ = await saved_selection(tmp_path, execution=execution)
    state = llm._request_state(config, execution=execution)
    state.requests_attempted = 2
    setup_reading_budget(config, execution=execution)
    assert llm.request_budget_remaining(config, execution=execution) == 8
    config.translation = replace(config.translation, enabled=True, timeout_seconds=90)
    assert reading_deadline(config, 100) == 325
    state.requests_attempted += 1
    setup_reading_budget(config, execution=execution)
    assert llm.request_budget_remaining(config, execution=execution) == 7


@pytest.mark.asyncio
async def test_real_prepare_path_advances_after_source_handoff_without_accepting_prose(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from digest.main import _run
    from digest.radar.collector import SourceCollectionOutcome, _capture_candidates

    monkeypatch.chdir(tmp_path)
    config, articles = population(2)
    config.review.review_led_only = True
    config.reading_brief = ReadingBriefConfig(True, "gemini", "gemini-3.8-flash")

    async def collect(_config: Any, **kwargs: Any) -> Any:
        inventory = kwargs["inventory"]
        inventory.sources = [SourceCollectionOutcome("A", "https://a.example/feed", "tech", 2)]
        _capture_candidates(inventory, config.enabled_sources, [articles["tech"]], {}, NOW, [], {})
        return articles, {}

    async def select(_role: Any, messages: list[dict[str, str]], *_args: Any, **_kwargs: Any) -> Any:
        item = json.loads(messages[1]["content"])["evidence"]["items"][0]
        return json.dumps({"selections": [{"evidence_id": item["evidence_id"], "reason": "Useful mechanism",
                                           "quote": item["title"], "confidence": "high"}],
                           "limitations": ["RSS evidence only"]}), {"finish_reason": "stop"}

    with (patch("digest.application.candidate_review._instant", return_value=NOW),
          patch("digest.config.load_config", return_value=config),
          patch("digest.radar.collect", side_effect=collect),
          patch("digest.application.review.complete", side_effect=select) as selection,
          patch("digest.reading_brief.fetch_article",
                AsyncMock(return_value=fetched("Full source evidence."))) as fetch,
          patch("digest.llm.count_gemini_tokens", AsyncMock(return_value=100)) as count,
          patch("digest.llm.complete", side_effect=generate) as model,
          patch("digest.edition_runtime.finish_preparation", side_effect=AssertionError("No semantic acceptance"))):
        first = await _run("config.yaml", False, False, False, prepare_only=True, feedback_precollected=True)
        second = await _run("config.yaml", False, False, False, prepare_only=True, feedback_precollected=True)
        third = await _run("config.yaml", False, False, False, prepare_only=True, feedback_precollected=True)
    assert third.new_articles == 0
    assert first.edition_status == second.edition_status == "semantic_reconciliation_pending"
    assert selection.call_count == fetch.call_count == count.call_count == model.call_count == 2
    assert not (tmp_path / ".cache" / "pending_preparation.json").exists()
    assert not first.telegram_sent and not second.telegram_sent


@pytest.mark.asyncio
async def test_changed_occurrence_cannot_bypass_prior_unknown_generation(tmp_path: Path) -> None:
    execution = ModelExecution()
    config, progress, packet, report = await saved_selection(tmp_path, execution=execution)
    with (patch("digest.reading_brief.fetch_article",
                AsyncMock(return_value=fetched("Complete public source."))) as fetch,
          patch("digest.llm.count_gemini_tokens", AsyncMock(return_value=100)),
          patch("digest.llm.complete", side_effect=TimeoutError("fixture uncertainty")) as model):
        initial = await prepare_selected_sources(
            progress, packet, report, config, tmp_path, time.monotonic() + 1000, execution=execution,
        )
    assert initial.pending == 1 and model.call_count == fetch.call_count == 1
    config, progress, packet, report = await saved_selection(
        tmp_path, description="Changed RSS description only", execution=execution,
    )
    with (patch("digest.reading_brief.fetch_article", side_effect=AssertionError("No new acquisition")),
          patch("digest.llm.complete", side_effect=AssertionError("No unknown replay"))):
        held = await prepare_selected_sources(
            progress, packet, report, config, tmp_path, time.monotonic() + 1000, execution=execution,
        )
    assert held.pending == 1 and held.outcomes[0].state == "held"


@pytest.mark.asyncio
async def test_matching_legacy_completed_evidence_has_explicit_current_adoption(tmp_path: Path) -> None:
    execution = ModelExecution()
    from digest.reading_brief_state import load_state

    config, progress, packet, report = await saved_selection(tmp_path, execution=execution)
    with (patch("digest.reading_brief.fetch_article", AsyncMock(return_value=fetched("Complete public source."))),
          patch("digest.llm.count_gemini_tokens", AsyncMock(return_value=100)),
          patch("digest.llm.complete", side_effect=generate)):
        first = await prepare_selected_sources(
            progress, packet, report, config, tmp_path, time.monotonic() + 1000, execution=execution,
        )
    identity = report.reviews[0].selections[0].evidence_id
    source_sha = load_state(tmp_path, identity).source_sha256
    (tmp_path / "reading_bindings" / f"{identity}.json").unlink()
    with patch("digest.reading_preparation._advance", side_effect=AssertionError("No completed-page replay")):
        adopted = await prepare_selected_sources(
            progress, packet, report, config, tmp_path, time.monotonic() + 1000, execution=execution,
        )
    assert first.technical_complete == adopted.technical_complete == 1
    handoff = json.loads(Path(adopted.handoff_paths[0]).read_text())
    assert handoff["binding"]["evidence_origin"] == "legacy_selection_match_historical_feed_binding_unknown"
    assert handoff["source_sha256"] == source_sha
    assert list((tmp_path / "reading_bindings" / "revisions").glob("*.json"))


@pytest.mark.asyncio
async def test_unsupported_source_invocation_fails_before_feedback_collection_or_models(tmp_path: Path) -> None:
    from digest.main import _run

    config, _ = population(1)
    config.reading_brief = ReadingBriefConfig(True, "gemini", "gemini-3.8-flash")
    with (patch("digest.config.load_config", return_value=config),
          patch("digest.application.run_state.collect_run_feedback", side_effect=AssertionError("No feedback I/O")),
          patch("digest.radar.collect", side_effect=AssertionError("No feed I/O")),
          patch("digest.application.review.complete", side_effect=AssertionError("No model I/O"))):
        with pytest.raises(ValueError, match="candidate-bound --prepare-edition"):
            await _run("config.yaml", True, True, False)


@pytest.mark.asyncio
async def test_lost_bound_state_never_restarts_source_or_generation(tmp_path: Path) -> None:
    execution = ModelExecution()
    config, progress, packet, report = await saved_selection(tmp_path, execution=execution)
    with (patch("digest.reading_brief.fetch_article", AsyncMock(return_value=fetched("Complete public source."))),
          patch("digest.llm.count_gemini_tokens", AsyncMock(return_value=100)),
          patch("digest.llm.complete", side_effect=generate)):
        await prepare_selected_sources(
            progress, packet, report, config, tmp_path, time.monotonic() + 1000, execution=execution,
        )
    identity = report.reviews[0].selections[0].evidence_id
    (tmp_path / "reading_briefs" / f"{identity}.json").unlink()
    with patch("digest.reading_preparation._advance", side_effect=AssertionError("No lost-state replay")):
        held = await prepare_selected_sources(
            progress, packet, report, config, tmp_path, time.monotonic() + 1000, execution=execution,
        )
    assert held.pending == 1 and held.outcomes[0].reason == "technical_missing_reading_state"


@pytest.mark.asyncio
@pytest.mark.parametrize("state_kind", ["complete", "unknown", "missing_handoff"])
async def test_source_recovery_filter_retains_proofs_and_exposes_later_candidates(
    tmp_path: Path, state_kind: str,
) -> None:
    execution = ModelExecution()
    from digest.candidate_review import pending_completed_report
    from digest.reading_preparation import deferred_source_reports

    config, progress, packet, report = await saved_selection(tmp_path, execution=execution)
    with (patch("digest.reading_brief.fetch_article", AsyncMock(return_value=fetched("Complete public source."))),
          patch("digest.llm.count_gemini_tokens", AsyncMock(return_value=100)),
          patch("digest.llm.complete", side_effect=TimeoutError if state_kind == "unknown" else generate)):
        result = await prepare_selected_sources(
            progress, packet, report, config, tmp_path, time.monotonic() + 1000, execution=execution,
        )
    if state_kind == "missing_handoff":
        Path(result.handoff_paths[0]).unlink()
    before = json.dumps([item.report.__dict__ for item in progress.packets], default=str)
    deferred = deferred_source_reports(progress, tmp_path, config)
    recovered = pending_completed_report(progress, skip_reports=deferred)
    assert (recovered is None) == (state_kind != "missing_handoff")
    assert before == json.dumps([item.report.__dict__ for item in progress.packets], default=str)
    assert not packet.handed_to_preparation
    assert all(item.status != "delivered" for item in progress.candidates.values())


@pytest.mark.asyncio
async def test_managed_preparation_deadline_includes_setup_and_barrier_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution = ModelExecution()
    config, _, _, _ = await saved_selection(tmp_path, execution=execution)
    monkeypatch.setenv("PREPARATION_DEADLINE", "1100")
    with patch("digest.reading_preparation.time.time", return_value=1000), patch(
        "digest.reading_preparation.time.monotonic", return_value=500,
    ):
        assert reading_deadline(config, 490) == 555  # 100 seconds left, less45 for persistence.


@pytest.mark.asyncio
async def test_complete_empty_source_packet_retires_resolved_metadata_without_fake_preparation(tmp_path: Path) -> None:
    execution = ModelExecution()
    from digest.candidate_dispositions import CandidateDispositionCapture
    from digest.candidate_review import load_candidate_progress, save_candidate_progress
    from digest.candidate_storage import load_candidate
    from digest.reading_preparation import deferred_source_reports

    config, articles = population(2)
    config.reading_brief = ReadingBriefConfig(True, "gemini", "gemini-3.8-flash")
    progress = merge_candidates(CandidateProgress(), articles, config, {}, now=NOW)
    packet = plan_packet(progress, config, NOW)
    assert packet is not None
    begin_packet(progress, packet, tmp_path)

    async def abstain(_role: Any, messages: Any, *_args: Any, **_kwargs: Any) -> Any:
        items = json.loads(messages[1]["content"])["evidence"]["items"]
        return json.dumps({"selections": [], "limitations": ["Metadata judgment only"],
                           "dispositions": [{"evidence_id": item["evidence_id"], "status": "not_selected",
                                             "reason": "This fixture has no role-relevant technical mechanism."}
                                            for item in items]}), {"finish_reason": "stop"}

    capture = CandidateDispositionCapture()
    with patch("digest.application.review.complete", side_effect=abstain):
        report = await run_primary_review(articles, config, disposition_capture=capture, execution=execution)
    reconcile_packet(progress, packet, report, config, tmp_path, disposition_capture=capture)
    identities = set(progress.candidates)
    deferred = deferred_source_reports(progress, tmp_path, config)
    assert deferred
    save_candidate_progress(progress, tmp_path, skipped_empty_reports=deferred)
    restored = load_candidate_progress(tmp_path)
    assert not restored.candidates and not restored.packets and not packet.handed_to_preparation
    assert all(load_candidate(identity, tmp_path).status == "not_selected" for identity in identities)
    assert not (tmp_path / "pending_preparation.json").exists()
    merge_candidates(restored, articles, config, {}, now=NOW, cache_dir=tmp_path)
    assert restored.packets and all(not item.handed_to_preparation for item in restored.packets)
    assert deferred_source_reports(restored, tmp_path, config)


@pytest.mark.asyncio
async def test_mixed_complete_and_resumable_source_packet_is_not_skipped(tmp_path: Path) -> None:
    execution = ModelExecution()
    from digest.candidate_review import pending_completed_report
    from digest.reading_preparation import deferred_source_reports

    config, progress, packet, report = await saved_selection(tmp_path, all_selected=True, execution=execution)
    calls = 0

    async def partial(_role: Any, messages: Any, *_args: Any, **_kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls > 1:
            raise RuntimeError("HTTP 503")
        return response(messages)

    with (patch("digest.reading_brief.fetch_article", AsyncMock(return_value=fetched("Complete public source."))),
          patch("digest.llm.count_gemini_tokens", AsyncMock(return_value=100)),
          patch("digest.llm.complete", side_effect=partial)):
        result = await prepare_selected_sources(
            progress, packet, report, config, tmp_path, time.monotonic() + 1000, execution=execution,
        )
    assert result.technical_complete == result.pending == 1
    assert not deferred_source_reports(progress, tmp_path, config)
    assert pending_completed_report(progress) == report
