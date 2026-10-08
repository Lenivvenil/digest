"""Count holds free the scheduler only when exact current routes cannot advance."""
from __future__ import annotations

import time
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from digest.adapters.models.execution import ModelExecution
from digest.application.preparation import _candidate_inputs
from digest.config import ProviderConfig
from digest.radar.collector import CollectionInventory, SourceCollectionOutcome, _capture_candidates, article_hash
from digest.reading_brief_state import load_state, save_state
from digest.reading_brief_tokens import TokenProfileUnavailable
from digest.reading_preparation import deferred_source_reports, prepare_selected_sources
from tests.test_candidate_review import NOW, population
from tests.test_main_reading_brief import generate, saved_selection
from tests.test_reading_brief import fetched


@pytest.mark.asyncio
@pytest.mark.parametrize("fallback", [False, True])
@pytest.mark.parametrize("legacy", [False, True])
async def test_count_held_packet_releases_planning_only_without_a_configured_fallback(
    tmp_path: Path, fallback: bool, legacy: bool,
) -> None:
    model_execution = ModelExecution()
    config, progress, packet, report = await saved_selection(tmp_path,
                                                             execution=model_execution)
    config.llm.providers = []
    identity = report.reviews[0].selections[0].evidence_id
    with (patch("digest.reading_brief.fetch_article", AsyncMock(return_value=fetched("Complete source."))) as fetch,
          patch("digest.llm.count_gemini_tokens", AsyncMock(side_effect=TimeoutError)) as count,
          patch("digest.llm.complete", side_effect=generate) as complete,
          patch("digest.source_admission.count_gpt_input", return_value=1000)):
        result = await prepare_selected_sources(progress, packet, report, config, tmp_path, time.monotonic() + 1000,
                                                execution=model_execution)
        assert result.pending == 1 and count.call_count == 1 and complete.call_count == 0
        if legacy:
            state = load_state(tmp_path, identity)
            state.pages[0].request_attempts = []
            state.pages[0].request_history_version = 0
            save_state(tmp_path, state)
        retained = {path: path.read_bytes() for root in ("reading_briefs", "reading_bindings")
                    for path in (tmp_path / root).rglob("*.json")}
        report_before = asdict(report)
        if fallback:
            config.llm.providers = [ProviderConfig("groq", "openai/gpt-oss-120b")]
        _, articles = population(3)
        inventory = CollectionInventory(sources=[SourceCollectionOutcome("A", "https://a.example/feed", "tech", 3)])
        _capture_candidates(inventory, config.enabled_sources, [articles["tech"]], {}, NOW, [], {})
        new_identity = article_hash(articles["tech"][2].title, articles["tech"][2].link)
        for _ in range(2):
            with patch("digest.application.candidate_review._instant", return_value=NOW):
                next_packet, next_report, _ = _candidate_inputs(
                    progress, inventory, config, config, {}, {}, str(tmp_path), False, {})
            assert next_packet is not None
            if fallback:
                assert next_packet == packet and next_report == report
            else:
                assert next_report is None
                assert new_identity in {item.evidence_id for item in next_packet.evidence.items}
                assert identity not in {item.evidence_id for item in next_packet.evidence.items}
        assert retained == {path: path.read_bytes() for path in retained}
        assert asdict(report) == report_before and not packet.handed_to_preparation
        assert progress.candidates[identity].status == "selected"
        assert fetch.call_count == count.call_count == 1 and complete.call_count == 0
        if fallback:
            resumed = await prepare_selected_sources(
                progress, packet, report, config, tmp_path, time.monotonic() + 1000,
                execution=model_execution)
            assert resumed.technical_complete == 1 and resumed.pending == 0
            assert fetch.call_count == count.call_count == complete.call_count == 1
            assert complete.call_args.kwargs["provider_override"].name == "groq"


@pytest.mark.asyncio
@pytest.mark.parametrize("evidence,held", [
    ("unknown", True), ("reserved", True), ("accepted_without_count", True), ("legacy_marker", True),
    ("definite_failed", False), ("exact_count", False), ("overflow_count", False),
    ("different_request", False), ("unsupported_routes", False),
    ("changed_occurrence", False), ("mismatched_binding", False), ("missing_state", False),
])
async def test_count_deferral_requires_current_request_route_and_binding_proof(
    tmp_path: Path, evidence: str, held: bool,
) -> None:
    model_execution = ModelExecution()
    config, progress, packet, report = await saved_selection(tmp_path,
                                                             execution=model_execution)
    config.llm.providers = []
    identity = report.reviews[0].selections[0].evidence_id
    with (patch("digest.reading_brief.fetch_article", AsyncMock(return_value=fetched("Complete source."))),
          patch("digest.llm.count_gemini_tokens", AsyncMock(side_effect=TimeoutError)),
          patch("digest.llm.complete", side_effect=AssertionError("No generation after uncertain count"))):
        await prepare_selected_sources(progress, packet, report, config, tmp_path, time.monotonic() + 1000,
                                       execution=model_execution)
    state = load_state(tmp_path, identity)
    page = state.pages[0]
    if evidence == "reserved":
        page.request_attempts[0].status = "reserved"
        page.request_attempts[0].finished_at = None
    elif evidence == "accepted_without_count":
        page.request_attempts[0].status = "accepted"
    elif evidence == "legacy_marker":
        page.legacy_count_request_sha256 = page.prompt_sha256
        page.request_attempts = []
    elif evidence == "definite_failed":
        page.request_attempts[0].status = "definite_failed"
    elif evidence in {"exact_count", "overflow_count"}:
        state.exact_counts[page.prompt_sha256] = 100 if evidence == "exact_count" else state.route.input_tokens + 1
    elif evidence == "different_request":
        config.reading_brief = replace(config.reading_brief, max_output_tokens=1024)
    elif evidence == "unsupported_routes":
        config.reading_brief = replace(config.reading_brief, model="unsupported")
    elif evidence == "changed_occurrence":
        config, progress, packet, report = await saved_selection(
            tmp_path, execution=model_execution, description="Changed occurrence",
        )
    elif evidence == "mismatched_binding":
        report.reviews[0].response_sha256 = "0" * 64
    save_state(tmp_path, state)
    if evidence == "missing_state":
        (tmp_path / "reading_briefs" / f"{identity}.json").unlink()
    before = {path: path.read_bytes() for path in tmp_path.rglob("*.json")}
    assert bool(deferred_source_reports(progress, tmp_path, config)) == held
    assert before == {path: path.read_bytes() for path in before}


@pytest.mark.asyncio
async def test_mixed_count_held_and_resumable_sources_keep_the_packet_eligible(tmp_path: Path) -> None:
    model_execution = ModelExecution()
    config, progress, packet, report = await saved_selection(tmp_path,
                                                             execution=model_execution, all_selected=True)
    config.llm.providers = []
    with (patch("digest.reading_brief.fetch_article", AsyncMock(return_value=fetched("Complete source."))),
          patch("digest.llm.count_gemini_tokens", AsyncMock(side_effect=[TimeoutError(), RuntimeError("HTTP 503")])),
          patch("digest.llm.complete", side_effect=AssertionError("No admitted source"))):
        result = await prepare_selected_sources(progress, packet, report, config, tmp_path, time.monotonic() + 1000,
                                                execution=model_execution)
    assert result.pending == 2
    assert not deferred_source_reports(progress, tmp_path, config)


@pytest.mark.asyncio
@pytest.mark.parametrize("profile_error", ["tokenizer_assets_missing", "tokenizer_asset_integrity_mismatch"])
async def test_unavailable_local_fallback_releases_planning_and_restored_profile_resumes(
    tmp_path: Path, profile_error: str,
) -> None:
    model_execution = ModelExecution()
    config, progress, packet, report = await saved_selection(tmp_path,
                                                             execution=model_execution)
    config.llm.providers = []
    identity = report.reviews[0].selections[0].evidence_id
    with (patch("digest.reading_brief.fetch_article", AsyncMock(return_value=fetched("Complete source."))) as fetch,
          patch("digest.llm.count_gemini_tokens", AsyncMock(side_effect=TimeoutError)) as count,
          patch("digest.llm.complete", side_effect=generate) as complete,
          patch("digest.source_admission.count_gpt_input",
                side_effect=TokenProfileUnavailable(profile_error)) as local_count):
        first = await prepare_selected_sources(progress, packet, report, config, tmp_path, time.monotonic() + 1000,
                                               execution=model_execution)
        assert first.pending == 1
        retained = {path: path.read_bytes() for root in ("reading_briefs", "reading_bindings")
                    for path in (tmp_path / root).rglob("*.json")}
        config.llm.providers = [ProviderConfig("groq", "openai/gpt-oss-120b")]
        _, articles = population(3)
        inventory = CollectionInventory(sources=[SourceCollectionOutcome("A", "https://a.example/feed", "tech", 3)])
        _capture_candidates(inventory, config.enabled_sources, [articles["tech"]], {}, NOW, [], {})
        new_identity = article_hash(articles["tech"][2].title, articles["tech"][2].link)
        for _ in range(2):
            assert deferred_source_reports(progress, tmp_path, config)
            with patch("digest.application.candidate_review._instant", return_value=NOW):
                next_packet, next_report, _ = _candidate_inputs(
                    progress, inventory, config, config, {}, {}, str(tmp_path), False, {})
            assert next_packet is not None and next_report is None
            assert new_identity in {item.evidence_id for item in next_packet.evidence.items}
            assert identity not in {item.evidence_id for item in next_packet.evidence.items}
        assert retained == {path: path.read_bytes() for path in retained}
        assert fetch.call_count == count.call_count == 1
        assert complete.call_count == 0 and local_count.call_count == 4
        local_count.side_effect = None
        local_count.return_value = 1000
        assert not deferred_source_reports(progress, tmp_path, config)
        with patch("digest.application.candidate_review._instant", return_value=NOW):
            recovered_packet, recovered_report, _ = _candidate_inputs(
                progress, inventory, config, config, {}, {}, str(tmp_path), False, {})
        assert recovered_packet == packet and recovered_report == report
        resumed = await prepare_selected_sources(
            progress, recovered_packet, recovered_report, config, tmp_path, time.monotonic() + 1000,
            execution=model_execution)
        assert resumed.technical_complete == 1 and resumed.pending == 0
        assert fetch.call_count == count.call_count == complete.call_count == 1
        assert complete.call_args.kwargs["provider_override"].name == "groq"


@pytest.mark.asyncio
@pytest.mark.parametrize("local_admission", ["oversized", "other_error", "unavailable_without_count_hold"])
async def test_local_profile_deferral_requires_count_hold_and_explicit_profile_failure(
    tmp_path: Path, local_admission: str,
) -> None:
    model_execution = ModelExecution()
    config, progress, packet, report = await saved_selection(tmp_path,
                                                             execution=model_execution)
    config.llm.providers = []
    identity = report.reviews[0].selections[0].evidence_id
    with (patch("digest.reading_brief.fetch_article", AsyncMock(return_value=fetched("Complete source."))),
          patch("digest.llm.count_gemini_tokens", AsyncMock(side_effect=TimeoutError)),
          patch("digest.llm.complete", side_effect=AssertionError("No admitted route"))):
        await prepare_selected_sources(progress, packet, report, config, tmp_path, time.monotonic() + 1000,
                                       execution=model_execution)
    config.llm.providers = [ProviderConfig("groq", "openai/gpt-oss-120b")]
    if local_admission == "unavailable_without_count_hold":
        state = load_state(tmp_path, identity)
        state.pages[0].request_attempts[0].status = "definite_failed"
        save_state(tmp_path, state)
    error = (TokenProfileUnavailable("Fixture assets unavailable")
             if local_admission == "unavailable_without_count_hold"
             else ValueError("Unclassified local failure") if local_admission == "other_error" else None)
    with patch("digest.source_admission.count_gpt_input", return_value=1_000_000, side_effect=error) as local_count:
        assert not deferred_source_reports(progress, tmp_path, config)
    assert local_count.call_count == (0 if local_admission == "unavailable_without_count_hold" else 1)
