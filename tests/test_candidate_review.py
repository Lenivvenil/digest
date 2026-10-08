"""Offline candidate continuation contracts; no model or network calls."""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import pytest

from digest.adapters.models.execution import ModelExecution
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
from digest.domain.editorial.attempts import restore_review
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
        packet.evidence, config.review, config.radar.language, sources=config.sources),
        sort_keys=True).encode()).hexdigest()
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
    reconcile_packet(progress, first, restore_review(report), config, tmp_path)
    restored = load_candidate_progress(tmp_path)
    assert pending_completed_report(restored) == report
    mark_prepared(restored, report.evidence.bundle_id, tmp_path)
    assert pending_completed_report(restored) == report
    second = plan_packet(restored, config, NOW)
    assert len(second.evidence.items) == 20
    assert not ({item.evidence_id for item in first.evidence.items}
                & {item.evidence_id for item in second.evidence.items})
    begin_packet(restored, second, tmp_path)
    reconcile_packet(restored, second, restore_review(report_for(second, config)), config, tmp_path)
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
    reconcile_packet(progress, packet, restore_review(report_for(packet, config, "invalid")), config, tmp_path)
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
    result = restore_review(report)
    report.evidence = replace(report.evidence, bundle_id="changed")
    with pytest.raises(ValueError, match="planned evidence"):
        reconcile_packet(progress, packet, result, config, tmp_path)
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


@pytest.mark.parametrize("bank_category", ["Banking", "000 Banking"])
def test_coeval_source_priority_precedes_category_names(bank_category: str) -> None:
    config = fixture_config()
    config.sources = [
        SourceConfig(f"Art {index:02}", f"https://a{index}.example/feed", f"A{index:02}", True, priority=3)
        for index in range(20)
    ] + [SourceConfig("Bank", "https://bank.example/feed", bank_category, True, priority=1)]
    articles = {source.category: [Article(source.name, source.url + "/article", "Short excerpt",
                                         source.name, source.category, NOW)] for source in config.sources}
    progress = merge_candidates(CandidateProgress(), articles, config, {}, {"Bank": 5}, NOW)
    packet = plan_packet(progress, config, NOW)
    assert packet is not None and len(packet.articles) == 20
    assert [article.source for article in packet.articles] == ["Bank", *[f"Art {index:02}" for index in range(19)]]
    assert packet.priorities["Bank"] == 5
    assert build_evidence_bundle(packet_articles(packet), config.review) == packet.evidence


def test_older_source_head_precedes_fresh_higher_priority_after_resume(tmp_path: Path) -> None:
    config, articles = population(1)
    config.sources.append(SourceConfig("B", "https://b.example/feed", "z-science", True, priority=1))
    older = Article("Older science", "https://b.example/1", "Evidence", "B", "z-science", NOW)
    progress = merge_candidates(CandidateProgress(), {"z-science": [older]}, config, {}, now=NOW)
    save_candidate_progress(progress, tmp_path)
    restored = load_candidate_progress(tmp_path)
    later = NOW + timedelta(hours=1)
    merge_candidates(restored, {**articles, "z-science": [older]}, config, {}, {"A": 5}, later)
    config.review.max_evidence_articles = 1
    packet = plan_packet(restored, config, later)
    assert packet is not None and packet.articles[0].source == "B"
    candidate = next(item for item in restored.candidates.values() if item.article.source == "B")
    assert candidate.first_observed_at == NOW.isoformat()


def test_source_round_rechecks_head_age_before_priority() -> None:
    config, articles = population(2)
    config.sources.append(SourceConfig("B", "https://b.example/feed", "science", True, priority=1))
    science = [Article(f"Science {index}", f"https://b.example/{index}", "Evidence", "B", "science", NOW)
               for index in range(2)]
    progress = merge_candidates(CandidateProgress(), {"tech": articles["tech"][:1]}, config, {}, now=NOW)
    merge_candidates(progress, {"science": science}, config, {}, now=NOW + timedelta(hours=1))
    merge_candidates(progress, articles, config, {}, {"A": 5}, NOW + timedelta(hours=2))
    config.review.max_evidence_articles = 3
    packet = plan_packet(progress, config, NOW + timedelta(hours=2))
    assert packet is not None and [article.source for article in packet.articles] == ["A", "B", "B"]


def test_old_unseen_source_advances_despite_continual_fresh_arrivals(tmp_path: Path) -> None:
    config, articles = population(3)
    config.sources.append(SourceConfig("B", "https://b.example/feed", "science", True, priority=1))
    articles["science"] = [Article("Science", "https://b.example/1", "Evidence", "B", "science", NOW)]
    config.review.max_evidence_articles = 1
    progress = merge_candidates(CandidateProgress(), articles, config, {}, {"A": 5}, NOW)
    observed_at = {identity: candidate.first_observed_at for identity, candidate in progress.candidates.items()}
    served = []
    for window in range(1, 5):
        later = NOW + timedelta(hours=window)
        fresh = {"tech": [Article(f"Fresh {window}-{index}", f"https://a.example/{window}-{index}", "Evidence",
                                  "A", "tech", later) for index in range(3)]}
        merge_candidates(progress, fresh, config, {}, {"A": 5}, later)
        packet = plan_packet(progress, config, later)
        assert packet is not None
        served.append(packet.articles[0].source)
        begin_packet(progress, packet, tmp_path)
        reconcile_packet(progress, packet, restore_review(report_for(packet, config)), config, tmp_path)
        mark_prepared(progress, packet.evidence.bundle_id, tmp_path)
        progress = load_candidate_progress(tmp_path)
    # A's freshness turn exposes its newer head; B's older observation now
    # precedes that head. Its opportunity no longer waits for A's old backlog.
    assert served == ["B", "A", "A", "A"]
    assert all(candidate.first_observed_at == observed_at[identity]
               for identity, candidate in progress.candidates.items() if identity in observed_at)


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
    reconcile_packet(progress, packet, restore_review(report), config, tmp_path)
    assert progress.candidates[rejected_id].status == "technical_pending"
    assert pending_completed_report(load_candidate_progress(tmp_path)) == report

    progress = merge_candidates(CandidateProgress(), articles, config, {}, now=NOW)
    packet = plan_packet(progress, config, NOW)
    begin_packet(progress, packet, tmp_path)
    report = report_for(packet, config, "invalid")
    report.reviews.append(report_for(packet, config, "ok", "secondary").reviews[0])
    reconcile_packet(progress, packet, restore_review(report), config, tmp_path)
    assert progress.candidates[packet.evidence.items[0].evidence_id].status == "selected"


def test_abstained_fallback_does_not_invent_primary_selection(tmp_path: Path) -> None:
    config, articles = population(3)
    progress = merge_candidates(CandidateProgress(), articles, config, {}, now=NOW)
    packet = plan_packet(progress, config, NOW)
    begin_packet(progress, packet, tmp_path)
    report = report_for(packet, config, "invalid")
    report.reviews.append(report_for(packet, config, "abstained", "secondary").reviews[0])
    reconcile_packet(progress, packet, restore_review(report), config, tmp_path)
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
    from digest.candidate_storage import read_article

    source = read_article(audit["observations"][0]["article_reference"]["occurrence_sha256"], tmp_path)
    assert source.published == NOW.isoformat()
    assert audit["observations"][0]["exclusion_reasons"] == ["seen_cache"]
    assert audit["observations"][0]["source_names"] == ["A", "B"]


def test_capacity_failure_preserves_previous_checkpoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config, articles = population(2)
    progress = merge_candidates(CandidateProgress(), articles, config, {}, now=NOW)
    path = save_candidate_progress(progress, tmp_path)
    original = path.read_bytes()
    monkeypatch.setattr("digest.adapters.storage.candidate_progress.MAX_BYTES", 5)
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
    reconcile_packet(progress, packet, restore_review(report), config, tmp_path)
    sidecar = archive_candidate_accounting(report, archive, tmp_path)
    saved = json.loads(sidecar.read_text())
    assert saved["summary"]["statuses"] == {
        "selected": 1, "not_selected": 0, "duplicate": 0,
        "not_selected_without_editorial_reason": 19, "not_presented": 1, "technical_pending": 0,
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
    reconcile_packet(progress, first, restore_review(report_for(first, config)), config, tmp_path)
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
    from digest.candidate_storage import packet_key, read_packet

    assert read_packet(packet_key(first), tmp_path).articles[0].source == "A"
    assert len(restored.packets) == 1 and restored.packets[0].articles[0].source == "B"


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
    reconcile_packet(progress, packet, restore_review(report), config, tmp_path)
    frozen = next((tmp_path / "candidate_reports").glob("*.json"))
    original = frozen.read_bytes()
    (tmp_path / CANDIDATE_FILE).write_text("corrupt unrelated subsequent work")
    sidecar = archive_candidate_accounting(report, tmp_path / "accepted-review.json", tmp_path)
    assert sidecar is not None
    assert sidecar.read_bytes() == original
    frozen.write_text(original.decode().replace('"selected": 1', '"selected": 999'))
    with pytest.raises(ValueError, match="hash"):
        archive_candidate_accounting(report, tmp_path / "another-review.json", tmp_path)


def test_handed_unsent_selection_recovers_after_expiry_but_delivery_cache_excludes(tmp_path: Path) -> None:
    config, articles = population(2)
    config.sources[0].recency_hours = 72
    progress = merge_candidates(CandidateProgress(), articles, config, {}, now=NOW)
    packet = plan_packet(progress, config, NOW)
    assert packet is not None
    begin_packet(progress, packet, tmp_path)
    report = report_for(packet, config)
    reconcile_packet(progress, packet, restore_review(report), config, tmp_path)
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
    reconcile_packet(progress, packet, restore_review(report), config, tmp_path)
    mark_prepared(progress, report.evidence.bundle_id, tmp_path)
    first_id = packet.evidence.items[0].evidence_id
    merge_candidates(progress, {}, config, {first_id: NOW.isoformat()}, now=NOW)
    assert pending_completed_report(progress) is None
    retry = plan_packet(progress, config, NOW)
    assert retry is not None
    assert [item.evidence_id for item in retry.evidence.items] == [second.evidence_id]

    begin_packet(progress, retry, tmp_path)
    abstention = report_for(retry, config, "abstained")
    reconcile_packet(progress, retry, restore_review(abstention), config, tmp_path)
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


def test_parser_population_fits_candidate_bound_without_duplicate_metadata(tmp_path: Path) -> None:
    from digest.candidate_review import MAX_BYTES, progress_size
    from digest.radar.collector import CandidateObservation, CollectionInventory, SourceCollectionOutcome, article_hash

    config = fixture_config()
    source_count = 54  # Retain the largest parser-population bound; smaller duplicate removed.
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
    reconcile_packet(restored, first, restore_review(report_for(first, config)), config, tmp_path)
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
    reconcile_packet(progress, packet, restore_review(report), config, tmp_path)
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
    import digest.adapters.storage.candidate_progress as progress_storage
    import digest.candidate_review as candidate_review

    config, articles = population(2)
    progress = merge_candidates(CandidateProgress(), articles, config, {}, now=NOW)
    packet = plan_packet(progress, config, NOW)
    assert packet is not None
    begin_packet(progress, packet, tmp_path)
    report = report_for(packet, config)
    original_write = progress_storage.atomic_json_write

    def fail_one_write(path: Path, data: object) -> None:
        is_progress = path.name == candidate_review.CANDIDATE_FILE
        if ((failed_boundary == "progress" and is_progress)
                or (failed_boundary == "frozen" and path.parent.name == "candidate_reports")):
            raise OSError("synthetic interrupted persistence")
        original_write(path, data)

    monkeypatch.setattr(progress_storage, "atomic_json_write", fail_one_write)
    monkeypatch.setattr("digest.adapters.storage.candidate_objects.atomic_json_write", fail_one_write)
    with pytest.raises(OSError, match="interrupted persistence"):
        reconcile_packet(progress, packet, restore_review(report), config, tmp_path)
    restored = load_candidate_progress(tmp_path)
    if failed_boundary == "progress":
        assert restored.packets[0].report is None
        assert pending_completed_report(restored) is None
        assert {item.status for item in restored.candidates.values()} == {"technical_pending"}
        assert not list((tmp_path / "candidate_reports").glob("*.json"))
    else:
        assert pending_completed_report(restored) == report
        monkeypatch.setattr(progress_storage, "atomic_json_write", original_write)
        monkeypatch.setattr("digest.adapters.storage.candidate_objects.atomic_json_write", original_write)
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
    reconcile_packet(progress, packet, restore_review(report), config, tmp_path)
    frozen = next((tmp_path / "candidate_reports").glob("*.json"))
    record = json.loads(frozen.read_text())
    record["schema_version"] = True
    record["sha256"] = hashlib.sha256(_canonical({key: value for key, value in record.items()
                                                 if key != "sha256"})).hexdigest()
    frozen.write_text(json.dumps(record))
    with pytest.raises(ValueError, match="schema"):
        archive_candidate_accounting(report, tmp_path / "review.json", tmp_path)


def compactable_fixture(tmp_path: Path) -> tuple[CandidateProgress, Config, str]:
    config, articles = population(3)
    config.review.max_evidence_articles = 1
    for article in articles["tech"]:
        article.description = "Original source evidence " * 100
    progress = merge_candidates(CandidateProgress(), articles, config, {}, now=NOW)
    packet = plan_packet(progress, config, NOW)
    assert packet is not None
    begin_packet(progress, packet, tmp_path)
    reconcile_packet(progress, packet, restore_review(report_for(packet, config)), config, tmp_path)
    excluded = next(candidate for candidate in progress.candidates.values() if candidate.status == "not_presented")
    config.filters.blocklist_keywords = [excluded.article.title]
    merge_candidates(progress, {}, config, {}, now=NOW)
    return progress, config, excluded.identity


def test_indexed_exclusion_rehydrates_exact_sources_after_policy_change(tmp_path: Path) -> None:
    from dataclasses import replace

    from digest.candidate_storage import load_candidate

    progress, config, identity = compactable_fixture(tmp_path)
    original = replace(progress.candidates[identity])
    path = save_candidate_progress(progress, tmp_path)
    assert identity not in json.loads(path.read_text())["candidate_accounting"]["candidates"]
    assert identity not in load_candidate_progress(tmp_path).candidates
    assert load_candidate(identity, tmp_path) == original
    config.filters.blocklist_keywords = []
    restored = load_candidate_progress(tmp_path)
    merge_candidates(restored, {}, config, {}, now=NOW, cache_dir=tmp_path)
    recovered = restored.candidates[identity]
    assert recovered.eligible and recovered.article == original.article
    assert recovered.first_observed_at == original.first_observed_at
    packet = plan_packet(restored, config, NOW)
    assert packet is not None and identity in {item.evidence_id for item in packet.evidence.items}


def test_excluded_selected_unknown_and_technical_work_retains_original_status_and_proof(tmp_path: Path) -> None:
    from dataclasses import replace

    from digest.candidate_storage import load_candidate, load_candidate_packets

    config, articles = population(4)
    config.review.max_evidence_articles = 3
    progress = merge_candidates(CandidateProgress(), articles, config, {}, now=NOW)
    packet = plan_packet(progress, config, NOW)
    assert packet is not None
    begin_packet(progress, packet, tmp_path)
    report = report_for(packet, config)
    reconcile_packet(progress, packet, restore_review(report), config, tmp_path)
    selected = report.reviews[0].selections[0].evidence_id
    progress.candidates[selected].delivery_cache_observed_at = NOW.isoformat()
    next(candidate for candidate in progress.candidates.values()
         if candidate.status == "not_presented").status = "technical_pending"
    config.filters.blocklist_keywords = ["Item"]
    merge_candidates(progress, {}, config, {}, now=NOW)
    expected = {identity: replace(candidate) for identity, candidate in progress.candidates.items()}
    mark_prepared(progress, report.evidence.bundle_id, tmp_path)
    assert load_candidate_progress(tmp_path).candidates == {}
    for identity, candidate in expected.items():
        assert load_candidate(identity, tmp_path) == candidate
    assert load_candidate(selected, tmp_path).status == "selected"
    assert load_candidate_packets(selected, tmp_path)[0].report == report
    assert {item.status for item in expected.values()} == {
        "selected", "not_selected_without_editorial_reason", "technical_pending"}


def test_active_schema_requires_exact_integer(tmp_path: Path) -> None:
    from digest.preparation import _canonical

    config, articles = population(1)
    progress = merge_candidates(CandidateProgress(), articles, config, {}, now=NOW)
    path = save_candidate_progress(progress, tmp_path)
    record = json.loads(path.read_text())
    record["candidate_accounting"]["schema_version"] = 1.0
    record["sha256"] = hashlib.sha256(_canonical(record["candidate_accounting"])).hexdigest()
    path.write_text(json.dumps(record))
    with pytest.raises(ValueError, match="Unsupported candidate prototype"):
        load_candidate_progress(tmp_path)


def test_interrupted_retirement_keeps_previous_active_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import digest.adapters.storage.candidate_progress as progress_storage
    import digest.candidate_review as candidate_review

    progress, _, _ = compactable_fixture(tmp_path)
    path = tmp_path / candidate_review.CANDIDATE_FILE
    original = path.read_bytes()
    archives = {archive.name: archive.read_bytes() for archive in (tmp_path / "candidate_reports").glob("*.json")}

    def interrupted_write(path: Path, data: object) -> None:
        raise OSError("synthetic interrupted compaction")

    monkeypatch.setattr(progress_storage, "atomic_json_write", interrupted_write)
    with pytest.raises(OSError, match="interrupted compaction"):
        save_candidate_progress(progress, tmp_path)
    assert path.read_bytes() == original
    retained = {archive.name: archive.read_bytes() for archive in (tmp_path / "candidate_reports").glob("*.json")}
    assert retained == archives
    assert len(load_candidate_progress(tmp_path).candidates) == 3


def resolved_fixture(tmp_path: Path) -> tuple[CandidateProgress, Config, BlindReviewReport]:
    from digest.candidate_dispositions import capture_review_dispositions

    config, articles = population(3)
    for article in articles["tech"]:
        article.description = "Specific original evidence " * 100
    progress = merge_candidates(CandidateProgress(), articles, config, {}, now=NOW)
    packet = plan_packet(progress, config, NOW)
    assert packet is not None
    begin_packet(progress, packet, tmp_path)
    report = report_for(packet, config, "abstained")
    raw = json.dumps({"selections": [], "limitations": ["RSS only"], "dispositions": [
        {"evidence_id": item.evidence_id, "status": "not_selected",
         "reason": "The supplied excerpt only repeats the vendor announcement without technical detail."}
        for item in packet.evidence.items
    ]})
    report.reviews[0] = replace(report.reviews[0], response_sha256=hashlib.sha256(raw.encode()).hexdigest())
    result = restore_review(report, (capture_review_dispositions(packet.evidence, report.reviews[0], raw),))
    assert result.disposition_attempts[0].status == "complete"
    reconcile_packet(progress, packet, result, config, tmp_path)
    return progress, config, report


def test_consumed_resolved_packet_retires_active_work_and_preserves_indexed_decisions(tmp_path: Path) -> None:
    from dataclasses import replace

    from digest.candidate_review import progress_size
    from digest.candidate_storage import load_candidate, load_candidate_packets

    progress, config, report = resolved_fixture(tmp_path)
    expected = {identity: replace(candidate) for identity, candidate in progress.candidates.items()}
    packet = progress.packets[0]
    active_bytes = progress_size(progress, tmp_path)
    assert pending_completed_report(progress) == report
    path = mark_prepared(progress, report.evidence.bundle_id, tmp_path)
    wire = json.loads(path.read_text())["candidate_accounting"]
    assert wire["candidates"] == {} and wire["packets"] == []
    assert path.stat().st_size == progress_size(progress, tmp_path) < active_bytes / 2
    restored = load_candidate_progress(tmp_path)
    assert restored == progress
    assert pending_completed_report(restored) is None and plan_packet(restored, config, NOW) is None
    for identity, candidate in expected.items():
        assert load_candidate(identity, tmp_path) == candidate
        assert load_candidate_packets(identity, tmp_path) == (packet,)
        assert candidate.status == "not_selected" and candidate.disposition.reason


def test_changed_excerpt_reopens_indexed_identity_and_preserves_original_decision(tmp_path: Path) -> None:
    from digest.candidate_storage import load_candidate, load_candidate_packets

    progress, config, report = resolved_fixture(tmp_path)
    candidate = next(iter(progress.candidates.values()))
    identity, original, decision = candidate.identity, candidate.article, candidate.disposition
    observed_at = candidate.first_observed_at
    mark_prepared(progress, report.evidence.bundle_id, tmp_path)
    progress = load_candidate_progress(tmp_path)
    assert identity not in progress.candidates
    changed = replace(original.article(), description="New contrary technical result with material details.")
    merge_candidates(progress, {changed.category: [changed]}, config, {},
                     now=NOW + timedelta(hours=1), cache_dir=tmp_path)
    candidate = progress.candidates[identity]
    assert candidate.status == "not_presented"
    assert candidate.disposition is None and candidate.decision_response_sha256 is None
    assert candidate.first_observed_at == observed_at and candidate.article.description == changed.description
    assert original not in candidate.occurrences
    packet = plan_packet(progress, config, NOW + timedelta(hours=1))
    assert packet is not None and [item.evidence_id for item in packet.evidence.items] == [identity]
    assert packet.articles[0].description == changed.description
    saved = load_candidate(identity, tmp_path)
    assert saved is not None and saved.disposition == decision
    proof = load_candidate_packets(identity, tmp_path)[0]
    assert original in proof.articles and decision in proof.disposition_attempts[0].dispositions
    save_candidate_progress(progress, tmp_path)
    assert load_candidate_progress(tmp_path).candidates[identity].article.description == changed.description


def test_duplicate_metadata_compacts_without_retiring_selected_retained_target(tmp_path: Path) -> None:
    from dataclasses import asdict

    from digest.candidate_dispositions import capture_review_dispositions

    config, articles = population(2)
    progress = merge_candidates(CandidateProgress(), articles, config, {}, now=NOW)
    packet = plan_packet(progress, config, NOW)
    assert packet is not None
    begin_packet(progress, packet, tmp_path)
    report = report_for(packet, config)
    retained, duplicate = packet.evidence.items
    raw = json.dumps({"selections": [asdict(report.reviews[0].selections[0])], "dispositions": [
        {"evidence_id": retained.evidence_id, "status": "selected"},
        {"evidence_id": duplicate.evidence_id, "status": "duplicate", "retained_id": retained.evidence_id,
         "reason": "RSS descriptions identify the same reproduced announcement without a distinct result."},
    ]})
    report.reviews[0] = replace(report.reviews[0], response_sha256=hashlib.sha256(raw.encode()).hexdigest())
    result = restore_review(report, (capture_review_dispositions(packet.evidence, report.reviews[0], raw),))
    reconcile_packet(progress, packet, result, config, tmp_path)
    path = mark_prepared(progress, report.evidence.bundle_id, tmp_path)
    wire = json.loads(path.read_text())["candidate_accounting"]
    assert duplicate.evidence_id not in wire["candidates"]
    assert retained.evidence_id in wire["candidates"]
    assert "packet_ref" in wire["packets"][0]
    restored = load_candidate_progress(tmp_path)
    from digest.candidate_storage import load_candidate, load_candidate_packets

    assert load_candidate(duplicate.evidence_id, tmp_path).status == "duplicate"
    assert load_candidate_packets(duplicate.evidence_id, tmp_path)[0].report == report
    assert restored.candidates[retained.evidence_id].status == "selected"
    assert pending_completed_report(restored) == report


@pytest.mark.parametrize("selected", [True, False])
def test_occurrence_switch_clears_only_active_typed_decision(tmp_path: Path, selected: bool) -> None:
    from digest.candidate_dispositions import capture_review_dispositions

    config, articles = population(1)
    config.sources.append(SourceConfig("B", "https://b.example/feed", "tech", True))
    other = replace(articles["tech"][0], source="B")
    articles["tech"].append(other)
    progress = merge_candidates(CandidateProgress(), articles, config, {}, now=NOW)
    packet = plan_packet(progress, config, NOW)
    assert packet is not None
    begin_packet(progress, packet, tmp_path)
    report = report_for(packet, config, "ok" if selected else "abstained")
    identity = packet.evidence.items[0].evidence_id
    decision = {"evidence_id": identity, "status": "selected" if selected else "deferred"}
    if not selected:
        decision["reason"] = "Useful work remains outside this response allowance."
    raw = json.dumps({"dispositions": [decision]})
    report.reviews[0].response_sha256 = hashlib.sha256(raw.encode()).hexdigest()
    result = restore_review(report, (capture_review_dispositions(packet.evidence, report.reviews[0], raw),))
    reconcile_packet(progress, packet, result, config, tmp_path)
    original_capture = packet.disposition_attempts
    assert progress.candidates[identity].disposition is not None
    config.sources[0].enabled = False
    merge_candidates(progress, {}, config, {}, now=NOW)
    assert progress.candidates[identity].article.source == "B"
    assert progress.candidates[identity].disposition is None
    assert progress.candidates[identity].decision_occurrence_sha256 is None
    save_candidate_progress(progress, tmp_path)
    restored = load_candidate_progress(tmp_path)
    assert restored.packets[0].disposition_attempts == original_capture
    assert restored.packets[0].articles[0].source == "A"


@pytest.mark.parametrize("kind", ["deferred", "missing", "complete", "legacy"])
def test_candidate_empty_handoff_requires_resolved_metadata_not_technical_deferral(tmp_path: Path, kind: str) -> None:
    from digest.candidate_dispositions import capture_review_dispositions
    from digest.edition_runtime import IncompleteSelection, accept_preparation
    from digest.preparation import AcceptedPreparation, PreparationSnapshot, load_preparation

    config, articles = population(2)
    progress = merge_candidates(CandidateProgress(), articles, config, {}, now=NOW)
    packet = plan_packet(progress, config, NOW)
    assert packet is not None
    begin_packet(progress, packet, tmp_path)
    report = report_for(packet, config, "abstained")
    raw_data: dict[str, object] = {"selections": [], "limitations": ["No cards in this response."]}
    if kind in {"deferred", "complete"}:
        raw_data["dispositions"] = [
            {"evidence_id": item.evidence_id, "status": "deferred" if kind == "deferred" else "not_selected",
             "reason": "Useful but waiting for capacity." if kind == "deferred" else "Only generic marketing detail."}
            for item in packet.evidence.items
        ]
    raw = json.dumps(raw_data)
    report.reviews[0].response_sha256 = hashlib.sha256(raw.encode()).hexdigest()
    result = restore_review(report, (() if kind == "legacy" else (
        capture_review_dispositions(packet.evidence, report.reviews[0], raw),)))
    reconcile_packet(progress, packet, result, config, tmp_path)
    snapshot = PreparationSnapshot([], [], "", report, 1, 2, ["A"])
    outcome = accept_preparation(snapshot, result, cache_dir=str(tmp_path))
    assert isinstance(outcome, AcceptedPreparation if kind in {"complete", "legacy"} else IncompleteSelection)
    assert (load_preparation(tmp_path) is not None) == (kind in {"complete", "legacy"})
    assert (pending_completed_report(progress) is not None) == (kind in {"complete", "legacy"})
    if kind == "complete":
        changed = replace(articles["tech"][0], description="New material qualification changes this evidence.")
        merge_candidates(progress, {"tech": [changed]}, config, {}, now=NOW)
        assert pending_completed_report(progress) is None
        assert plan_packet(progress, config, NOW) is not None


def test_accepted_readback_must_match_the_saved_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from typing import Any

    from digest.edition_runtime import accept_preparation
    from digest.preparation import PreparationSnapshot, load_accepted_preparation

    config, articles = population(2)
    progress = merge_candidates(CandidateProgress(), articles, config, {}, now=NOW)
    packet = plan_packet(progress, config, NOW)
    assert packet is not None
    report = report_for(packet, config, "abstained")
    snapshot = PreparationSnapshot([], [], "Accepted canonical notice", report, 1, 2, ["A"])

    def mismatched_readback(*args: Any, **kwargs: Any) -> Any:
        accepted = load_accepted_preparation(*args, **kwargs)
        if accepted is None:
            return None
        return replace(accepted, snapshot=replace(accepted.snapshot, combined="Unrelated canonical notice"))

    monkeypatch.setattr("digest.preparation.load_accepted_preparation", mismatched_readback)
    with pytest.raises(ValueError, match="readback differs"):
        accept_preparation(snapshot, restore_review(report, packet.disposition_attempts), cache_dir=str(tmp_path))
    accepted = load_accepted_preparation(tmp_path)
    assert accepted is not None and accepted.snapshot == snapshot
    assert not packet.handed_to_preparation


@pytest.mark.asyncio
async def test_response_storage_reserve_covers_supported_escaped_unicode_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_execution = ModelExecution()
    from unittest.mock import AsyncMock

    from digest.candidate_review import RESPONSE_STORAGE_RESERVE, progress_size
    from digest.review import run_primary_review

    config = fixture_config()
    config.sources = [SourceConfig("A", "https://x/rss", "c", True)]
    config.review.max_evidence_articles = 100
    config.review.max_selections = 10
    config.review.max_detailed_selections = 10  # Nine valid entries plus one rejected quote test storage capacity.
    config.review.max_excerpt_chars = 50
    articles = {"c": [Article(f"T{i}", f"https://x/{i}", "D", "A", "c", None) for i in range(100)]}
    progress = merge_candidates(CandidateProgress(), articles, config, {}, now=NOW)
    packet = plan_packet(progress, config, NOW)
    assert packet is not None and len(packet.evidence.items) == 80
    begin_packet(progress, packet, tmp_path)
    before = progress_size(progress)
    items = packet.evidence.items
    selections = [{"evidence_id": item.evidence_id, "reason": "😀" * 180,
                   "quote": item.title, "confidence": "high"} for item in items[:9]]
    selections.append({"evidence_id": items[9].evidence_id, "reason": "bad",
                       "quote": "fabricated quote", "confidence": "high"})
    dispositions = [{"evidence_id": item.evidence_id, "status": "selected"}
                    if index < 9 else {"evidence_id": item.evidence_id, "status": "deferred", "reason": "😀" * 240}
                    for index, item in enumerate(items)]
    raw = json.dumps({"selections": selections, "limitations": ["x"], "dispositions": dispositions}, ensure_ascii=False)
    provider = AsyncMock(side_effect=[("😀" * 32000, {}), (raw, {})])
    monkeypatch.setattr("digest.application.review.complete", provider)
    result = await run_primary_review(packet_articles(packet), config,
        execution=model_execution)
    report = result.report
    reconcile_packet(progress, packet, result, config, tmp_path)
    growth = progress_size(progress) - before
    assert 1_048_576 < growth <= RESPONSE_STORAGE_RESERVE
    assert provider.await_count == 2 and [review.status for review in report.reviews] == ["invalid", "partial"]


def test_old_deferred_work_keeps_eligibility_without_becoming_re_reviewed(tmp_path: Path) -> None:
    from digest.candidate_dispositions import capture_review_dispositions

    config, articles = population(1)
    progress = merge_candidates(CandidateProgress(), articles, config, {}, now=NOW)
    packet = plan_packet(progress, config, NOW)
    assert packet is not None
    begin_packet(progress, packet, tmp_path)
    report = report_for(packet, config, "abstained")
    raw = json.dumps({"selections": [], "limitations": ["RSS only"], "dispositions": [
        {"evidence_id": item.evidence_id, "status": "deferred", "reason": "Useful but output capacity exhausted."}
        for item in packet.evidence.items]})
    report.reviews[0].response_sha256 = hashlib.sha256(raw.encode()).hexdigest()
    result = restore_review(report, (capture_review_dispositions(packet.evidence, report.reviews[0], raw),))
    reconcile_packet(progress, packet, result, config, tmp_path)
    # Emulate a valid archived report from a different prompt contract, preserving
    # every matching provenance reference before the ordinary save/load boundary.
    old_hash = hashlib.sha256(b"older selection contract").hexdigest()
    packet.prompt_hash = report.reviews[0].prompt_hash = old_hash
    packet.disposition_attempts = tuple(replace(attempt, prompt_hash=old_hash)
                                        for attempt in packet.disposition_attempts)
    candidate = next(iter(progress.candidates.values()))
    candidate.decision_prompt_hash = old_hash
    save_candidate_progress(progress, tmp_path)
    restored = load_candidate_progress(tmp_path)
    merge_candidates(restored, {}, config, {}, now=NOW + timedelta(hours=1))
    candidate = next(iter(restored.candidates.values()))
    assert candidate.eligible and candidate.status == "technical_pending"
    assert candidate.disposition.status == "deferred"
    assert candidate.decision_prompt_hash == old_hash
    assert pending_completed_report(restored) is None
    next_packet = plan_packet(restored, config, NOW + timedelta(hours=1))
    assert next_packet is not None and next_packet.evidence == packet.evidence
    assert next_packet.prompt_hash != old_hash and next_packet.report is None
    assert restored.packets[0].report.reviews[0].prompt_hash == old_hash
    assert candidate.status == "technical_pending" and candidate.decision_prompt_hash == old_hash
