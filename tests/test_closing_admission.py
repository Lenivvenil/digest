"""Offline planning opportunities; eligibility never asserts closing suitability."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, replace
from datetime import timedelta
from pathlib import Path

import pytest

from digest.adapters.storage.candidate_objects import packet_key, read_packet
from digest.application.review_request import eligible_ids
from digest.candidate_dispositions import capture_review_dispositions
from digest.candidate_review import (
    CandidateProgress,
    begin_packet,
    load_candidate_progress,
    merge_candidates,
    packet_articles,
    plan_packet,
    reconcile_packet,
)
from digest.config import ClosingConfig, ClosingSourceBinding, Config, SourceConfig
from digest.domain.editorial.attempts import restore_review
from digest.domain.editorial.candidate_policy import _closing_opportunity
from digest.domain.editorial.candidates import Candidate
from digest.radar.collector import Article, article_hash
from digest.review import MAX_EVIDENCE_JSON_CHARS, build_evidence_bundle
from tests.test_candidate_review import NOW, population, report_for


def _population() -> tuple[Config, CandidateProgress]:
    config, _ = population(0)
    config.sources = [
        SourceConfig(f"Regular {index}", f"https://regular{index}.example/feed", "tech", True) for index in range(6)
    ]
    articles = [
        Article(
            f"Regular {index}",
            f"https://regular{index}.example/item",
            "Evidence excerpt",
            source.name,
            source.category,
            NOW,
        )
        for index, source in enumerate(config.sources)
    ]
    progress = merge_candidates(CandidateProgress(), {"tech": articles}, config, {}, now=NOW)
    source = SourceConfig("Community News", "https://community.example/feed", "society", True)
    config.sources.append(source)
    config.closing = ClosingConfig(False, (ClosingSourceBinding(source.name, source.url, source.category),))
    closing = [
        Article(
            f"Community {index}",
            f"https://community.example/{index}",
            "Synthetic community evidence",
            source.name,
            source.category,
            NOW + timedelta(seconds=index),
        )
        for index in range(2)
    ]
    merge_candidates(progress, {"society": closing}, config, {}, now=NOW + timedelta(minutes=1))
    return config, progress


@pytest.mark.parametrize("limit,reserved", [(1, 0), (3, 2), (6, 2)])
def test_opportunity_preserves_first_unseen_reserved_retries_and_exact_pending_history(
    tmp_path: Path,
    limit: int,
    reserved: int,
) -> None:
    config, progress = _population()
    # Establish two genuine prior attempts without involving the closing source.
    config.review.max_evidence_articles = 2
    first = plan_packet(progress, config, NOW + timedelta(minutes=1))
    assert first is not None
    begin_packet(progress, first, tmp_path)
    saved_first = asdict(first)
    config.review.max_evidence_articles = limit
    config.review.max_technical_retry_articles = reserved
    baseline = plan_packet(progress, config, NOW + timedelta(minutes=2))
    assert baseline is not None and not eligible_ids(baseline.evidence, config.closing, config.sources)
    before = asdict(progress)
    config.closing = replace(config.closing, enabled=True)
    packet = plan_packet(progress, config, NOW + timedelta(minutes=2))
    assert packet is not None and asdict(progress) == before
    assert asdict(first) == saved_first
    if limit <= 1 + reserved:
        assert packet.articles == baseline.articles and packet.evidence == baseline.evidence
        return
    assert packet.articles[:-1] == baseline.articles[:-1]
    assert packet.articles[-1].title == "Community 1"
    assert len(eligible_ids(packet.evidence, config.closing, config.sources)) == 1
    assert len(packet.articles) == limit
    assert build_evidence_bundle(packet_articles(packet), config.review) == packet.evidence
    prior_evidence = {item.evidence_id: item for item in baseline.evidence.items}
    assert all(
        item == prior_evidence[item.evidence_id] for item in packet.evidence.items if item.evidence_id in prior_evidence
    )
    donor = article_hash(baseline.articles[-1].title, baseline.articles[-1].link)
    begin_packet(progress, packet, tmp_path)
    restored = load_candidate_progress(tmp_path)
    assert asdict(restored.candidates[donor]) == before["candidates"][donor]
    assert asdict(read_packet(packet_key(first), tmp_path)) == saved_first


def test_deferred_backfill_keeps_its_original_model_disposition_and_attempt(tmp_path: Path) -> None:
    config, progress = _population()
    first = plan_packet(progress, config, NOW + timedelta(minutes=1))
    assert first is not None
    begin_packet(progress, first, tmp_path)
    report = report_for(first, config, "abstained")
    raw = json.dumps(
        {
            "dispositions": [
                {
                    "evidence_id": item.evidence_id,
                    "status": "deferred",
                    "reason": "Useful work awaits response capacity.",
                }
                for item in first.evidence.items
            ]
        }
    )
    report.reviews[0].response_sha256 = hashlib.sha256(raw.encode()).hexdigest()
    result = restore_review(report, (capture_review_dispositions(first.evidence, report.reviews[0], raw),))
    reconcile_packet(progress, first, result, config, tmp_path)
    config.review.max_evidence_articles = 4
    config.review.max_technical_retry_articles = 2
    baseline = plan_packet(progress, config, NOW + timedelta(minutes=2))
    assert baseline is not None and all(item.source != "Community News" for item in baseline.articles)
    donor = article_hash(baseline.articles[-1].title, baseline.articles[-1].link)
    saved_donor, saved_first = asdict(progress.candidates[donor]), asdict(first)
    config.closing = replace(config.closing, enabled=True)
    packet = plan_packet(progress, config, NOW + timedelta(minutes=2))
    assert packet is not None and packet.articles[:-1] == baseline.articles[:-1]
    assert packet.articles[-1].source == "Community News"
    begin_packet(progress, packet, tmp_path)
    restored = load_candidate_progress(tmp_path)
    assert asdict(restored.candidates[donor]) == saved_donor
    assert asdict(read_packet(packet_key(first), tmp_path)) == saved_first


@pytest.mark.parametrize("guard", ["disabled", "stale_approval", "changed_occurrence", "collision", "ineligible"])
def test_unavailable_exact_approval_leaves_the_baseline_packet(guard: str) -> None:
    config, progress = _population()
    config.review.max_evidence_articles = 4
    baseline = plan_packet(progress, config, NOW + timedelta(minutes=1))
    assert baseline is not None
    config.closing = replace(config.closing, enabled=guard != "disabled")
    if guard == "stale_approval":
        config.sources[-1].url += "changed"
    elif guard == "collision":
        config.sources.append(replace(config.sources[-1], name="Community  News", url="https://other.example/feed"))
    elif guard in {"changed_occurrence", "ineligible"}:
        for candidate in progress.candidates.values():
            if candidate.article.source == "Community News":
                if guard == "changed_occurrence":
                    candidate.article = replace(candidate.article, source_url="https://old.example/feed")
                else:
                    candidate.eligible = False
    packet = plan_packet(progress, config, NOW + timedelta(minutes=1))
    assert packet is not None and packet.articles == baseline.articles and packet.evidence == baseline.evidence


def test_existing_eligible_closing_admission_adds_no_second_opportunity() -> None:
    config, progress = _population()
    config.review.max_evidence_articles = 7
    baseline = plan_packet(progress, config, NOW + timedelta(minutes=1))
    assert baseline is not None and len(eligible_ids(baseline.evidence, config.closing, config.sources)) == 1
    config.closing = replace(config.closing, enabled=True)
    packet = plan_packet(progress, config, NOW + timedelta(minutes=1))
    assert packet is not None and packet.articles == baseline.articles and packet.evidence == baseline.evidence


def test_closing_retries_keep_attempt_age_rotation_instead_of_publication_freshness(tmp_path: Path) -> None:
    config, progress = _population()
    closing = [item for item in progress.candidates.values() if item.article.source == "Community News"]
    for index, candidate in enumerate(closing):
        first = plan_packet(
            CandidateProgress(candidates={candidate.identity: candidate}), config, NOW + timedelta(minutes=2 + index)
        )
        assert first is not None
        begin_packet(progress, first, tmp_path)
    config.review.max_evidence_articles = 2
    config.review.max_technical_retry_articles = 0
    config.closing = replace(config.closing, enabled=True)
    for index in range(2):
        packet = plan_packet(progress, config, NOW + timedelta(minutes=4 + index))
        assert packet is not None
        assert packet.articles[0].source.startswith("Regular")
        assert packet.articles[-1].title == f"Community {index}"
        begin_packet(progress, packet, tmp_path)
        progress = load_candidate_progress(tmp_path)


def test_complete_bundle_fit_skips_oversized_offer_and_never_displaces_for_spare_count() -> None:
    config, progress = _population()
    config.review.max_evidence_articles = 4
    config.review.max_excerpt_chars = 37
    packet = plan_packet(progress, config, NOW + timedelta(minutes=1))
    assert packet is not None
    selected = list(packet.articles)
    fitting = next(item for item in progress.candidates.values() if item.article.source == "Community News")
    fitting.article = replace(fitting.article, description="Synthetic evidence 😀 " * 10)
    # This item fits alone, but exceeds the whole bundle's character budget.
    large_article = replace(fitting.article, link="https://large.example/" + "x" * 15000)
    large = Candidate(
        article_hash(large_article.title, large_article.link),
        large_article,
        fitting.first_observed_at,
        fitting.priority,
    )
    assert len(build_evidence_bundle({large_article.category: [large_article.article()]}, config.review).items) == 1
    result = _closing_opportunity(selected, [large, fitting], 1, max_evidence_articles=4, max_excerpt_chars=37)
    assert result == [*selected[:-1], fitting.article]
    grouped: dict[str, list[Article]] = {}
    for item in result:
        grouped.setdefault(item.category, []).append(item.article())
    rebuilt = build_evidence_bundle(grouped, config.review)
    assert sum(len(json.dumps(asdict(item), ensure_ascii=False)) for item in rebuilt.items) <= MAX_EVIDENCE_JSON_CHARS
    assert all(len(item.excerpt) <= 37 for item in rebuilt.items)
    assert _closing_opportunity(selected, [large], 1, max_evidence_articles=4, max_excerpt_chars=37) is selected
    spare = selected[:-1]
    assert _closing_opportunity(spare, [fitting], 1, max_evidence_articles=4, max_excerpt_chars=37) == [
        *spare,
        fitting.article,
    ]
    assert _closing_opportunity(spare, [large], 1, max_evidence_articles=4, max_excerpt_chars=37) is spare
