"""Offline candidate continuation contracts; no model or network calls."""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import pytest

from digest.candidate_review import (
    CandidatePacket,
    CandidateProgress,
    begin_packet,
    load_candidate_progress,
    mark_prepared,
    merge_candidates,
    packet_articles,
    pending_completed_report,
    plan_packet,
    reconcile_packet,
    save_candidate_progress,
)
from digest.config import Config, SourceConfig
from digest.radar.collector import Article
from digest.review import (
    BlindReviewReport,
    EvidenceSelection,
    ModelReview,
    build_evidence_bundle,
    build_review_messages,
)
from scripts.review_fixture import fixture_config

NOW = datetime(2026, 10, 4, 12, tzinfo=UTC)


def population(count: int = 47) -> tuple[Config, dict[str, list[Article]]]:
    config = fixture_config()
    config.sources = [SourceConfig("A", "https://a.example/feed", "tech", True)]
    articles = {"tech": [Article(f"Item {index}", f"https://a.example/{index}", "Evidence excerpt",
                                  "A", "tech", NOW) for index in range(count)]}
    return config, articles


def report_for(
    packet: CandidatePacket, config: Config,
    status: Literal["ok", "partial", "abstained", "invalid", "unavailable"] = "ok", slot: str = "primary",
) -> BlindReviewReport:
    item = packet.evidence.items[0]
    selections = [EvidenceSelection(item.evidence_id, "Useful", item.title, "high")] if status == "ok" else []
    prompt_hash = hashlib.sha256(json.dumps(build_review_messages(
        packet.evidence, config.review, config.radar.language), sort_keys=True).encode()).hexdigest()
    review = ModelReview(slot, "test", "test", packet.evidence.bundle_id, prompt_hash, status,
                         selections=selections, limitations=["RSS only"])
    return BlindReviewReport(1, packet.evidence, [review], "incomplete", None, [], "pending_independent_review")


def test_47_candidates_continue_beyond_first_twenty_and_preserve_report(tmp_path: Path) -> None:
    config, articles = population()
    progress = merge_candidates(CandidateProgress(), articles, config, {}, now=NOW)
    first = plan_packet(progress, config, NOW)
    assert len(first.evidence.items) == 20
    assert build_evidence_bundle(packet_articles(first), config.review) == first.evidence
    begin_packet(progress, first, tmp_path)
    report = report_for(first, config)
    reconcile_packet(progress, first, report, config, tmp_path)
    restored = load_candidate_progress(tmp_path)
    assert pending_completed_report(restored) == report
    mark_prepared(restored, report.evidence.bundle_id, tmp_path)
    assert pending_completed_report(restored) == report
    second = plan_packet(restored, config, NOW)
    assert len(second.evidence.items) == 20
    assert not ({item.evidence_id for item in first.evidence.items}
                & {item.evidence_id for item in second.evidence.items})
    begin_packet(restored, second, tmp_path)
    reconcile_packet(restored, second, report_for(second, config), config, tmp_path)
    assert sum(candidate.status == "selected" for candidate in restored.candidates.values()) == 2
    assert sum(candidate.status == "not_selected_without_editorial_reason"
               for candidate in restored.candidates.values()) == 38
    assert sum(candidate.status == "not_presented" for candidate in restored.candidates.values()) == 7


def test_planned_interruption_is_technical_and_unseen_precedes_retry(tmp_path: Path) -> None:
    config, articles = population(21)
    progress = merge_candidates(CandidateProgress(), articles, config, {}, now=NOW)
    first = plan_packet(progress, config, NOW)
    begin_packet(progress, first, tmp_path)
    restored = load_candidate_progress(tmp_path)
    assert restored.packets[0].report is None
    assert sum(item.status == "technical_pending" for item in restored.candidates.values()) == 20
    config.review.max_evidence_articles = 1
    next_packet = plan_packet(restored, config, NOW)
    assert next_packet.evidence.items[0].evidence_id not in {item.evidence_id for item in first.evidence.items}


def test_invalid_response_never_becomes_editorial_rejection(tmp_path: Path) -> None:
    config, articles = population(3)
    progress = merge_candidates(CandidateProgress(), articles, config, {}, now=NOW)
    packet = plan_packet(progress, config, NOW)
    begin_packet(progress, packet, tmp_path)
    reconcile_packet(progress, packet, report_for(packet, config, "invalid"), config, tmp_path)
    assert {candidate.status for candidate in progress.candidates.values()} == {"technical_pending"}
    assert pending_completed_report(load_candidate_progress(tmp_path)) is None


def test_saved_absent_items_keep_dates_and_current_boundaries(tmp_path: Path) -> None:
    config, articles = population(4)
    progress = merge_candidates(CandidateProgress(), articles, config, {}, now=NOW)
    original = {identity: item.article.published for identity, item in progress.candidates.items()}
    save_candidate_progress(progress, tmp_path)
    restored = load_candidate_progress(tmp_path)
    merge_candidates(restored, {}, config, {}, now=NOW + timedelta(hours=25))
    assert all(not candidate.eligible for candidate in restored.candidates.values())
    assert {identity: item.article.published for identity, item in restored.candidates.items()} == original
    merge_candidates(restored, {}, config, {}, now=NOW)
    identity = next(iter(restored.candidates))
    merge_candidates(restored, {}, config, {identity: NOW.isoformat()}, now=NOW)
    assert restored.candidates[identity].eligibility_reason == "existing_delivery_cache"
    config.sources[0].enabled = False
    merge_candidates(restored, {}, config, {}, now=NOW)
    assert all(not candidate.eligible for candidate in restored.candidates.values())


def test_huge_candidate_does_not_block_later_fitting_item(tmp_path: Path) -> None:
    config, articles = population(2)
    articles["tech"][0].link += "X" * 20000
    progress = merge_candidates(CandidateProgress(), articles, config, {}, now=NOW)
    packet = plan_packet(progress, config, NOW)
    assert len(packet.evidence.items) == 1
    assert packet.evidence.items[0].title == "Item 1"
    huge = next(candidate for candidate in progress.candidates.values() if len(candidate.article.link) > 1000)
    assert huge.status == "technical_pending"


def test_bundle_mismatch_and_tampering_fail_closed(tmp_path: Path) -> None:
    config, articles = population(3)
    progress = merge_candidates(CandidateProgress(), articles, config, {}, now=NOW)
    packet = plan_packet(progress, config, NOW)
    path = begin_packet(progress, packet, tmp_path)
    report = report_for(packet, config)
    report.evidence = replace(report.evidence, bundle_id="changed")
    with pytest.raises(ValueError, match="planned evidence"):
        reconcile_packet(progress, packet, report, config, tmp_path)
    record = json.loads(path.read_text())
    record["candidate_accounting"]["candidates"][next(iter(progress.candidates))]["priority"] = 999
    path.write_text(json.dumps(record))
    with pytest.raises(ValueError, match="hash"):
        load_candidate_progress(tmp_path)


def test_packet_diversity_and_priority_are_scheduling_preferences() -> None:
    config, articles = population(21)
    config.sources.append(SourceConfig("B", "https://b.example/feed", "science", True, priority=1))
    articles["science"] = [Article("Science", "https://b.example/1", "Evidence", "B", "science", NOW)]
    config.review.max_evidence_articles = 2
    progress = merge_candidates(CandidateProgress(), articles, config, {}, {"A": 5}, NOW)
    packet = plan_packet(progress, config, NOW)
    assert {item.source for item in packet.evidence.items} == {"A", "B"}
    assert packet.priorities == {"A": 5, "B": 1}


def test_partial_keeps_rejected_identity_technical_and_fallback_matches_delivery(tmp_path: Path) -> None:
    from digest.review import RejectedSelection

    config, articles = population(3)
    progress = merge_candidates(CandidateProgress(), articles, config, {}, now=NOW)
    packet = plan_packet(progress, config, NOW)
    begin_packet(progress, packet, tmp_path)
    report = report_for(packet, config)
    report.reviews[0].status = "partial"
    rejected_id = packet.evidence.items[1].evidence_id
    report.reviews[0].rejected_items = [RejectedSelection(1, "quote is not in supplied evidence", rejected_id)]
    reconcile_packet(progress, packet, report, config, tmp_path)
    assert progress.candidates[rejected_id].status == "technical_pending"
    assert pending_completed_report(load_candidate_progress(tmp_path)) == report

    progress = merge_candidates(CandidateProgress(), articles, config, {}, now=NOW)
    packet = plan_packet(progress, config, NOW)
    begin_packet(progress, packet, tmp_path)
    report = report_for(packet, config, "invalid")
    report.reviews.append(report_for(packet, config, "ok", "secondary").reviews[0])
    reconcile_packet(progress, packet, report, config, tmp_path)
    assert progress.candidates[packet.evidence.items[0].evidence_id].status == "selected"


def test_abstained_fallback_does_not_invent_primary_selection(tmp_path: Path) -> None:
    config, articles = population(3)
    progress = merge_candidates(CandidateProgress(), articles, config, {}, now=NOW)
    packet = plan_packet(progress, config, NOW)
    begin_packet(progress, packet, tmp_path)
    report = report_for(packet, config, "invalid")
    report.reviews.append(report_for(packet, config, "abstained", "secondary").reviews[0])
    reconcile_packet(progress, packet, report, config, tmp_path)
    assert {candidate.status for candidate in progress.candidates.values()} == {"technical_pending"}
    assert pending_completed_report(progress) is None


def test_full_collection_audit_and_original_timestamps_survive(tmp_path: Path) -> None:
    from digest.radar.collector import CandidateObservation, CollectionInventory, article_hash

    config, articles = population(2)
    article = articles["tech"][0]
    inventory = CollectionInventory(observations=[
        CandidateObservation(article, article_hash(article.title, article.link), 4, ("seen_cache",),
                             source_names=("A", "B")),
    ])
    progress = merge_candidates(CandidateProgress(), articles, config, {}, now=NOW, inventory=inventory)
    save_candidate_progress(progress, tmp_path)
    audit = json.loads(load_candidate_progress(tmp_path).latest_collection_json)
    assert audit["observations"][0]["article"]["pub_date"] == NOW.isoformat()
    assert audit["observations"][0]["exclusion_reasons"] == ["seen_cache"]
    assert audit["observations"][0]["source_names"] == ["A", "B"]


def test_capacity_failure_preserves_previous_checkpoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config, articles = population(2)
    progress = merge_candidates(CandidateProgress(), articles, config, {}, now=NOW)
    path = save_candidate_progress(progress, tmp_path)
    original = path.read_bytes()
    monkeypatch.setattr("digest.candidate_review.MAX_BYTES", 5)
    with pytest.raises(ValueError, match="no manifest was truncated"):
        save_candidate_progress(progress, tmp_path)
    assert path.read_bytes() == original


def test_private_archive_exact_match_summary_and_legacy_compatibility(tmp_path: Path) -> None:
    from digest.candidate_review import archive_candidate_accounting

    config, articles = population(21)
    progress = merge_candidates(CandidateProgress(), articles, config, {}, now=NOW)
    packet = plan_packet(progress, config, NOW)
    report = report_for(packet, config)
    archive = tmp_path / "review.json"
    assert archive_candidate_accounting(report, archive, tmp_path) is None
    begin_packet(progress, packet, tmp_path)
    reconcile_packet(progress, packet, report, config, tmp_path)
    sidecar = archive_candidate_accounting(report, archive, tmp_path)
    saved = json.loads(sidecar.read_text())
    assert saved["summary"]["statuses"] == {
        "selected": 1, "not_selected_without_editorial_reason": 19, "not_presented": 1, "technical_pending": 0,
    }
    assert saved["summary"]["registered_identities"] == 21
    original = sidecar.read_bytes()
    mark_prepared(progress, packet.evidence.bundle_id, tmp_path)
    assert archive_candidate_accounting(report, archive, tmp_path) == sidecar
    assert sidecar.read_bytes() == original
    changed_report = replace(report, third_model_reason="changed")
    assert archive_candidate_accounting(changed_report, archive, tmp_path) is None


def test_current_blocklist_and_undated_absent_resume_keep_honest_age() -> None:
    config, articles = population(2)
    articles["tech"][0].pub_date = None
    progress = merge_candidates(CandidateProgress(), articles, config, {}, now=NOW)
    undated = next(item for item in progress.candidates.values() if item.article.published is None)
    observed = undated.first_observed_at
    merge_candidates(progress, {}, config, {}, now=NOW + timedelta(days=3))
    assert undated.eligible
    assert undated.article.published is None
    assert undated.first_observed_at == observed
    config.filters.blocklist_keywords = ["Item"]
    merge_candidates(progress, {}, config, {}, now=NOW + timedelta(days=3))
    assert not undated.eligible
    assert undated.eligibility_reason == "current_blocklist"


def test_disabled_original_source_preserves_packet_and_uses_eligible_duplicate(tmp_path: Path) -> None:
    config, articles = population(1)
    progress = merge_candidates(CandidateProgress(), articles, config, {}, now=NOW)
    first = plan_packet(progress, config, NOW)
    begin_packet(progress, first, tmp_path)
    reconcile_packet(progress, first, report_for(first, config), config, tmp_path)
    config.sources[0].enabled = False
    config.sources.append(SourceConfig("B", "https://b.example/feed", "science", True))
    duplicate = replace(articles["tech"][0], source="B", category="science")
    merge_candidates(progress, {"science": [duplicate]}, config, {}, now=NOW)
    candidate = next(iter(progress.candidates.values()))
    assert candidate.eligible
    assert candidate.article.source == "B"
    assert {saved.source for saved in (candidate.article, *candidate.occurrences)} == {"A", "B"}
    assert candidate.status == "technical_pending"
    assert pending_completed_report(progress) is None
    second = plan_packet(progress, config, NOW)
    assert second.evidence.items[0].source == "B"
    assert first.evidence.items[0].source == "A"
    begin_packet(progress, second, tmp_path)
    restored = load_candidate_progress(tmp_path)
    assert restored.packets[0].articles[0].source == "A"
    assert restored.packets[1].articles[0].source == "B"


def test_legacy_report_ignores_corrupt_unrelated_mutable_progress(tmp_path: Path) -> None:
    from digest.candidate_review import CANDIDATE_FILE, archive_candidate_accounting

    config, articles = population(2)
    progress = merge_candidates(CandidateProgress(), articles, config, {}, now=NOW)
    packet = plan_packet(progress, config, NOW)
    assert packet is not None
    report = report_for(packet, config)
    (tmp_path / CANDIDATE_FILE).write_text("corrupt unrelated candidate work")
    assert archive_candidate_accounting(report, tmp_path / "accepted-review.json", tmp_path) is None


def test_frozen_report_accounting_is_independent_of_mutable_work(tmp_path: Path) -> None:
    from digest.candidate_review import CANDIDATE_FILE, archive_candidate_accounting

    config, articles = population(2)
    progress = merge_candidates(CandidateProgress(), articles, config, {}, now=NOW)
    packet = plan_packet(progress, config, NOW)
    assert packet is not None
    begin_packet(progress, packet, tmp_path)
    report = report_for(packet, config)
    reconcile_packet(progress, packet, report, config, tmp_path)
    frozen = next((tmp_path / "candidate_reports").glob("*.json"))
    original = frozen.read_bytes()
    (tmp_path / CANDIDATE_FILE).write_text("corrupt unrelated subsequent work")
    sidecar = archive_candidate_accounting(report, tmp_path / "accepted-review.json", tmp_path)
    assert sidecar is not None
    assert sidecar.read_bytes() == original
    frozen.write_text(original.decode().replace('"selected": 1', '"selected": 999'))
    with pytest.raises(ValueError, match="hash or report binding"):
        archive_candidate_accounting(report, tmp_path / "another-review.json", tmp_path)


def test_handed_unsent_selection_recovers_after_expiry_but_delivery_cache_excludes(tmp_path: Path) -> None:
    config, articles = population(2)
    config.sources[0].recency_hours = 72
    progress = merge_candidates(CandidateProgress(), articles, config, {}, now=NOW)
    packet = plan_packet(progress, config, NOW)
    assert packet is not None
    begin_packet(progress, packet, tmp_path)
    report = report_for(packet, config)
    reconcile_packet(progress, packet, report, config, tmp_path)
    mark_prepared(progress, report.evidence.bundle_id, tmp_path)
    restored = load_candidate_progress(tmp_path)
    merge_candidates(restored, {}, config, {}, now=NOW + timedelta(days=1))
    assert pending_completed_report(restored) == report
    selected = report.reviews[0].selections[0].evidence_id
    merge_candidates(restored, {}, config, {selected: NOW.isoformat()}, now=NOW + timedelta(days=1))
    assert pending_completed_report(restored) is None
    assert not restored.candidates[selected].eligible
    assert plan_packet(restored, config, NOW + timedelta(days=1)) is None


def test_partly_ineligible_selection_requeues_eligible_subset_and_abstention_stays_consumed(tmp_path: Path) -> None:
    config, articles = population(2)
    progress = merge_candidates(CandidateProgress(), articles, config, {}, now=NOW)
    packet = plan_packet(progress, config, NOW)
    assert packet is not None
    begin_packet(progress, packet, tmp_path)
    report = report_for(packet, config)
    second = packet.evidence.items[1]
    report.reviews[0].selections.append(EvidenceSelection(second.evidence_id, "Useful too", second.title, "high"))
    reconcile_packet(progress, packet, report, config, tmp_path)
    mark_prepared(progress, report.evidence.bundle_id, tmp_path)
    first_id = packet.evidence.items[0].evidence_id
    merge_candidates(progress, {}, config, {first_id: NOW.isoformat()}, now=NOW)
    assert pending_completed_report(progress) is None
    retry = plan_packet(progress, config, NOW)
    assert retry is not None
    assert [item.evidence_id for item in retry.evidence.items] == [second.evidence_id]

    begin_packet(progress, retry, tmp_path)
    abstention = report_for(retry, config, "abstained")
    reconcile_packet(progress, retry, abstention, config, tmp_path)
    mark_prepared(progress, abstention.evidence.bundle_id, tmp_path)
    assert pending_completed_report(progress) is None
    assert plan_packet(progress, config, NOW) is None


def test_empty_partial_is_not_reused_as_accepted_work(tmp_path: Path) -> None:
    config, articles = population(1)
    progress = merge_candidates(CandidateProgress(), articles, config, {}, now=NOW)
    packet = plan_packet(progress, config, NOW)
    assert packet is not None
    begin_packet(progress, packet, tmp_path)
    # Defensive recovery guard: live validators normally classify this invalid.
    packet.report = report_for(packet, config, "partial")
    assert pending_completed_report(progress) is None
    assert next(iter(progress.candidates.values())).status == "technical_pending"
    assert plan_packet(progress, config, NOW) is not None


@pytest.mark.parametrize("source_count", [10, 54])
def test_parser_population_fits_candidate_bound_without_duplicate_metadata(tmp_path: Path, source_count: int) -> None:
    from digest.candidate_review import MAX_BYTES, progress_size
    from digest.radar.collector import CandidateObservation, CollectionInventory, SourceCollectionOutcome, article_hash

    config = fixture_config()
    config.sources = [SourceConfig(f"Source {index}", f"https://s{index}.example/feed", "tech", True)
                      for index in range(source_count)]
    inventory = CollectionInventory()
    for source in config.sources:
        inventory.sources.append(SourceCollectionOutcome(source.name, source.url, source.category, 3, True, 200))
        for index in range(200):
            article = Article(f"Ordinary feed title {source.name} {index}", f"{source.url}/{index}", "E" * 500,
                              source.name, source.category, NOW)
            inventory.observations.append(CandidateObservation(article, article_hash(article.title, article.link),
                                                                3, (), source_names=(source.name,)))
    progress = merge_candidates(CandidateProgress(), inventory.eligible_articles(), config, {}, now=NOW,
                                inventory=inventory)
    assert len(progress.candidates) == source_count * 200
    assert all(not candidate.occurrences for candidate in progress.candidates.values())
    path = save_candidate_progress(progress, tmp_path)
    assert path.stat().st_size == progress_size(progress) < MAX_BYTES
    restored = load_candidate_progress(tmp_path)
    audit = json.loads(restored.latest_collection_json)
    assert len(audit["observations"]) == source_count * 200
    assert all("article_reference" in observation and "article" not in observation
               for observation in audit["observations"])
    first = plan_packet(restored, config, NOW)
    assert first is not None
    begin_packet(restored, first, tmp_path)
    reconcile_packet(restored, first, report_for(first, config), config, tmp_path)
    second = plan_packet(restored, config, NOW)
    assert second is not None
    assert not ({item.evidence_id for item in first.evidence.items}
                & {item.evidence_id for item in second.evidence.items})
    assert len(load_candidate_progress(tmp_path).candidates) == source_count * 200
    assert path.stat().st_size < MAX_BYTES


def test_observed_cache_fact_outlives_pruning_without_inventing_delivery_on_handoff(tmp_path: Path) -> None:
    config, articles = population(1)
    config.sources[0].recency_hours = 400
    progress = merge_candidates(CandidateProgress(), articles, config, {}, now=NOW)
    packet = plan_packet(progress, config, NOW)
    assert packet is not None
    begin_packet(progress, packet, tmp_path)
    report = report_for(packet, config)
    reconcile_packet(progress, packet, report, config, tmp_path)
    mark_prepared(progress, report.evidence.bundle_id, tmp_path)
    identity = report.reviews[0].selections[0].evidence_id
    candidate = progress.candidates[identity]
    assert candidate.delivery_cache_observed_at is None
    merge_candidates(progress, {}, config, {}, now=NOW + timedelta(days=8), delivery_history={})
    assert pending_completed_report(progress) == report
    merge_candidates(progress, {}, config, {}, now=NOW + timedelta(days=8),
                     delivery_history={identity: NOW.isoformat()})
    assert candidate.eligible  # Ordinary cache eligibility has already expired.
    assert candidate.delivery_cache_observed_at == NOW.isoformat()
    save_candidate_progress(progress, tmp_path)
    restored = load_candidate_progress(tmp_path)
    merge_candidates(restored, {}, config, {}, now=NOW + timedelta(days=9), delivery_history={})
    assert restored.candidates[identity].eligible
    assert pending_completed_report(restored) is None
    assert restored.candidates[identity].delivery_cache_observed_at == NOW.isoformat()


@pytest.mark.parametrize("failed_boundary", ["progress", "frozen"])
def test_report_persistence_boundaries_recover_without_false_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failed_boundary: str,
) -> None:
    import digest.candidate_review as candidate_review

    config, articles = population(2)
    progress = merge_candidates(CandidateProgress(), articles, config, {}, now=NOW)
    packet = plan_packet(progress, config, NOW)
    assert packet is not None
    begin_packet(progress, packet, tmp_path)
    report = report_for(packet, config)
    original_write = candidate_review.atomic_json_write

    def fail_one_write(path: Path, data: object) -> None:
        is_progress = path.name == candidate_review.CANDIDATE_FILE
        if (failed_boundary == "progress" and is_progress) or (failed_boundary == "frozen" and not is_progress):
            raise OSError("synthetic interrupted persistence")
        original_write(path, data)

    monkeypatch.setattr(candidate_review, "atomic_json_write", fail_one_write)
    with pytest.raises(OSError, match="interrupted persistence"):
        reconcile_packet(progress, packet, report, config, tmp_path)
    restored = load_candidate_progress(tmp_path)
    if failed_boundary == "progress":
        assert restored.packets[0].report is None
        assert pending_completed_report(restored) is None
        assert {item.status for item in restored.candidates.values()} == {"technical_pending"}
        assert not list((tmp_path / "candidate_reports").glob("*.json"))
    else:
        assert pending_completed_report(restored) == report
        monkeypatch.setattr(candidate_review, "atomic_json_write", original_write)
        frozen = candidate_review.ensure_report_accounting(restored, report, tmp_path)
        assert frozen.exists()
        assert candidate_review.ensure_report_accounting(restored, report, tmp_path) == frozen
        assert candidate_review.archive_candidate_accounting(report, tmp_path / "review.json", tmp_path) is not None


def test_frozen_accounting_rejects_outer_schema_even_with_valid_hash(tmp_path: Path) -> None:
    from digest.candidate_review import archive_candidate_accounting
    from digest.preparation import _canonical

    config, articles = population(1)
    progress = merge_candidates(CandidateProgress(), articles, config, {}, now=NOW)
    packet = plan_packet(progress, config, NOW)
    assert packet is not None
    begin_packet(progress, packet, tmp_path)
    report = report_for(packet, config)
    reconcile_packet(progress, packet, report, config, tmp_path)
    frozen = next((tmp_path / "candidate_reports").glob("*.json"))
    record = json.loads(frozen.read_text())
    record["schema_version"] = True
    record["sha256"] = hashlib.sha256(_canonical({key: value for key, value in record.items()
                                                 if key != "sha256"})).hexdigest()
    frozen.write_text(json.dumps(record))
    with pytest.raises(ValueError, match="schema version"):
        archive_candidate_accounting(report, tmp_path / "review.json", tmp_path)
