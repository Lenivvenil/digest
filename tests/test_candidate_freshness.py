"""Offline scheduling contracts for fresh evidence and bounded technical continuation."""
from __future__ import annotations

import json
import textwrap
from dataclasses import asdict, replace
from datetime import timedelta
from pathlib import Path

import pytest

from digest.adapters.storage.candidate_progress import load_candidate_progress
from digest.application.candidate_lifecycle import checkpoint_candidates
from digest.application.candidate_review import begin_packet, merge_candidates, plan_packet
from digest.application.review_request import build_evidence_bundle
from digest.config import Config, ReviewConfig, SourceConfig, load_config
from digest.domain.editorial.candidate_policy import packet_articles
from digest.domain.editorial.candidates import CandidatePacket, CandidateProgress
from digest.domain.editorial.reviews import MAX_EVIDENCE_JSON_CHARS
from digest.radar.collector import Article, article_hash
from tests.test_candidate_review import NOW, population
from tests.test_config import MINIMAL_CONFIG


def _packet(progress: CandidateProgress, config: Config, hours: int = 0) -> CandidatePacket:
    packet = plan_packet(progress, config, NOW + timedelta(hours=hours))
    assert packet is not None
    return packet


def _new_articles(count: int, hours: int = 1) -> dict[str, list[Article]]:
    return {"tech": [Article(f"Fresh {hours}-{index}", f"https://a.example/fresh-{hours}-{index}",
                             "Synthetic evidence", "A", "tech", NOW + timedelta(hours=hours))
                     for index in range(count)]}


def test_retry_allowance_defaults_without_changing_evidence_bounds(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(textwrap.dedent(MINIMAL_CONFIG))
    assert ReviewConfig().max_technical_retry_articles == 4
    config = load_config(str(path))
    assert config.review.max_technical_retry_articles == 4
    assert config.review.max_evidence_articles == 20
    assert MAX_EVIDENCE_JSON_CHARS == 16000


@pytest.mark.parametrize("value", [0, 4, 100])
def test_retry_allowance_accepts_integer_boundaries(tmp_path: Path, value: int) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(textwrap.dedent(MINIMAL_CONFIG) + f"\nreview:\n  max_technical_retry_articles: {value}\n")
    assert load_config(str(path)).review.max_technical_retry_articles == value


@pytest.mark.parametrize("value", ["-1", "101", "true", "false", "4.0", "'4'", "null"])
def test_retry_allowance_rejects_non_integer_or_out_of_range_values(tmp_path: Path, value: str) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(textwrap.dedent(MINIMAL_CONFIG) + f"\nreview:\n  max_technical_retry_articles: {value}\n")
    with pytest.raises(ValueError, match="review.max_technical_retry_articles"):
        load_config(str(path))


def test_fresh_same_source_article_follows_oldest_ahead_of_remaining_backlog() -> None:
    config, articles = population(33)
    progress = merge_candidates(CandidateProgress(), articles, config, {}, now=NOW)
    oldest = min(progress.candidates.values(), key=lambda item: (item.first_observed_at, item.identity))
    fresh = _new_articles(1)
    merge_candidates(progress, fresh, config, {}, now=NOW + timedelta(hours=1))
    packet = _packet(progress, config, 1)
    assert len(packet.articles) == 20
    assert packet.articles[0] == oldest.article
    assert packet.articles[1].title == fresh["tech"][0].title
    assert all(candidate.status == "not_presented" for candidate in progress.candidates.values())
    assert build_evidence_bundle(packet_articles(packet), config.review) == packet.evidence


def test_fresh_and_oldest_turns_preserve_missing_and_future_date_candidates() -> None:
    config, _ = population(0)
    config.review.max_evidence_articles = 5
    progress = CandidateProgress()
    examples = [
        ("Undated oldest", None),
        ("Future at observation", NOW + timedelta(hours=1)),
        ("Old valid date", NOW - timedelta(hours=1)),
        ("Newest valid date", NOW + timedelta(minutes=3)),
        ("Second newest date", NOW + timedelta(minutes=2)),
    ]
    for index, (title, published) in enumerate(examples):
        article = Article(title, f"https://a.example/{index}", "Synthetic evidence", "A", "tech", published)
        merge_candidates(progress, {"tech": [article]}, config, {}, now=NOW + timedelta(minutes=index))
    packet = _packet(progress, config, 2)
    assert [item.title for item in packet.articles] == [
        "Undated oldest", "Newest valid date", "Second newest date", "Future at observation", "Old valid date",
    ]
    assert packet.articles[0].published is None
    assert packet.articles[3].published == (NOW + timedelta(hours=1)).isoformat()


@pytest.mark.parametrize("hours", [0, 2])
def test_date_future_at_observation_never_acquires_a_freshness_boost(hours: int) -> None:
    config, articles = population(2)
    articles["tech"][0].pub_date = NOW + timedelta(hours=1)
    articles["tech"][1].pub_date = NOW - timedelta(minutes=1)
    config.review.max_evidence_articles = 2
    oldest = Article("Oldest unseen", "https://a.example/oldest", "Synthetic evidence", "A", "tech", None)
    progress = merge_candidates(CandidateProgress(), {"tech": [oldest]}, config, {}, now=NOW - timedelta(minutes=1))
    merge_candidates(progress, articles, config, {}, now=NOW)
    assert [item.title for item in _packet(progress, config, hours).articles] == ["Oldest unseen", "Item 1"]
    assert all(candidate.eligible for candidate in progress.candidates.values())


def test_all_undated_candidates_keep_observation_order_across_reload(tmp_path: Path) -> None:
    config, articles = population(4)
    progress = CandidateProgress()
    for index, article in enumerate(articles["tech"]):
        article.pub_date = None
        merge_candidates(progress, {"tech": [article]}, config, {}, now=NOW + timedelta(minutes=index))
    checkpoint_candidates(progress, tmp_path)
    packet = _packet(load_candidate_progress(tmp_path), config, 1)
    assert [item.title for item in packet.articles] == [f"Item {index}" for index in range(4)]
    assert all(item.published is None for item in packet.articles)


@pytest.mark.parametrize(
    "limit,allowance,unseen_count,retry_count,expected_retries",
    [(20, 4, 30, 8, 4), (5, 100, 30, 8, 4), (1, 4, 30, 8, 0),
     (5, 0, 30, 8, 0), (5, 0, 1, 8, 4), (5, 4, 30, 1, 1),
     (5, 4, 0, 8, 5), (5, 4, 2, 1, 1)],
)
def test_retry_reservation_and_backfill_keep_the_same_packet_bounds(
    tmp_path: Path, limit: int, allowance: int, unseen_count: int, retry_count: int, expected_retries: int,
) -> None:
    config, articles = population(retry_count)
    config.review.max_evidence_articles = retry_count
    progress = merge_candidates(CandidateProgress(), articles, config, {}, now=NOW)
    first = _packet(progress, config)
    retry_ids = {item.evidence_id for item in first.evidence.items}
    begin_packet(progress, first, tmp_path)
    progress = load_candidate_progress(tmp_path)
    config.review.max_evidence_articles = limit
    config.review.max_technical_retry_articles = allowance
    fresh = _new_articles(unseen_count)
    merge_candidates(progress, fresh, config, {}, now=NOW + timedelta(hours=1))
    before = {identity: candidate.status for identity, candidate in progress.candidates.items()}
    packet = _packet(progress, config, 1)
    identities = {item.evidence_id for item in packet.evidence.items}
    assert len(identities & retry_ids) == expected_retries
    assert len(packet.articles) == min(limit, unseen_count + retry_count)
    if unseen_count:
        assert article_hash(packet.articles[0].title, packet.articles[0].link) not in retry_ids
    assert sum(len(json.dumps(asdict(item), ensure_ascii=False)) for item in packet.evidence.items) <= 16000
    assert build_evidence_bundle(packet_articles(packet), config.review) == packet.evidence
    assert {identity: candidate.status for identity, candidate in progress.candidates.items()} == before
    assert packet.report is None and all(saved.report is None for saved in progress.packets)


def test_retry_rotation_survives_save_load_and_indexed_retirement(tmp_path: Path) -> None:
    config, articles = population(6)
    config.review.max_evidence_articles = 6
    progress = merge_candidates(CandidateProgress(), articles, config, {}, now=NOW)
    initial = _packet(progress, config)
    original_ids = {item.evidence_id for item in initial.evidence.items}
    observations = {identity: candidate.first_observed_at for identity, candidate in progress.candidates.items()}
    begin_packet(progress, initial, tmp_path)
    config.review.max_evidence_articles = 3
    config.review.max_technical_retry_articles = 1
    served = []
    for hours in range(1, 7):
        progress = load_candidate_progress(tmp_path)
        merge_candidates(progress, _new_articles(3, hours), config, {}, now=NOW + timedelta(hours=hours))
        packet = _packet(progress, config, hours)
        retried = {item.evidence_id for item in packet.evidence.items} & original_ids
        assert len(retried) == 1
        served.extend(retried)
        begin_packet(progress, packet, tmp_path)
        checkpoint_candidates(progress, tmp_path)
        if hours == 3:
            config.sources[0].enabled = False
            merge_candidates(progress, {}, config, {}, now=NOW + timedelta(hours=hours))
            checkpoint_candidates(progress, tmp_path)
            progress = load_candidate_progress(tmp_path)
            assert progress.candidates == {} and progress.packets == []
            config.sources[0].enabled = True
            merge_candidates(progress, {}, config, {}, now=NOW + timedelta(hours=hours), cache_dir=tmp_path)
            assert original_ids <= progress.candidates.keys()
            checkpoint_candidates(progress, tmp_path)
    restored = load_candidate_progress(tmp_path)
    assert len(served) == len(set(served)) == 6
    assert set(served) == original_ids
    assert all(restored.candidates[identity].first_observed_at == observed
               for identity, observed in observations.items())
    assert all(packet.planned_at != initial.planned_at for packet in restored.packets)
    assert all(packet.report is None for packet in restored.packets)
    assert all(candidate.disposition is None for candidate in restored.candidates.values())


def test_changed_occurrence_does_not_borrow_an_unrelated_attempt_timestamp(tmp_path: Path) -> None:
    config, articles = population(1)
    config.sources += [SourceConfig("B", "https://b.example/feed", "tech", True),
                       SourceConfig("C", "https://c.example/feed", "tech", True)]
    original = articles["tech"][0]
    alternate = replace(original, source="B", description="Alternative source occurrence")
    config.review.max_evidence_articles = 1
    progress = merge_candidates(CandidateProgress(), {"tech": [original, alternate]}, config, {}, now=NOW)
    begin_packet(progress, _packet(progress, config), tmp_path)
    competitor = Article("Other retry", "https://c.example/item", "Synthetic evidence", "C", "tech", NOW)
    merge_candidates(progress, {"tech": [competitor]}, config, {}, now=NOW + timedelta(hours=1))
    other_packet = _packet(progress, config, 1)
    assert other_packet.articles[0].source == "C"
    begin_packet(progress, other_packet, tmp_path)
    latest_original = _packet(progress, config, 2)
    assert latest_original.articles[0].source == "A"
    begin_packet(progress, latest_original, tmp_path)
    config.sources[0].enabled = False
    merge_candidates(progress, {}, config, {}, now=NOW + timedelta(hours=3))
    checkpoint_candidates(progress, tmp_path)
    restored = load_candidate_progress(tmp_path)
    packet = _packet(restored, config, 3)
    assert packet.articles[0].source == "B"
    assert packet.articles[0].description == alternate.description
    assert restored.candidates[article_hash(original.title, original.link)].first_observed_at == NOW.isoformat()


def test_rebound_occurrence_keeps_exact_and_newer_audit_proofs_after_retirement(tmp_path: Path) -> None:
    config, articles = population(2)
    config.sources += [SourceConfig(name, f"https://{name.lower()}.example/feed", "tech", True)
                       for name in ("B", "C", "D")]
    original = articles["tech"][0]
    alternate = replace(original, source="B", description="Alternative source occurrence")
    keeper = replace(articles["tech"][1], source="C")
    config.review.max_evidence_articles = 2
    progress = merge_candidates(CandidateProgress(), {"tech": [original, alternate, keeper]}, config, {}, now=NOW)
    exact_packet = _packet(progress, config)
    assert {item.source for item in exact_packet.articles} == {"A", "C"}
    begin_packet(progress, exact_packet, tmp_path)

    config.review.max_evidence_articles = 1
    competitor = Article("Other retry", "https://d.example/item", "Synthetic evidence", "D", "tech", NOW)
    merge_candidates(progress, {"tech": [competitor]}, config, {}, now=NOW + timedelta(hours=1))
    competitor_packet = _packet(progress, config, 1)
    assert competitor_packet.articles[0].source == "D"
    begin_packet(progress, competitor_packet, tmp_path)
    config.sources[0].enabled = False
    merge_candidates(progress, {}, config, {}, now=NOW + timedelta(hours=2))
    audit_packet = _packet(progress, config, 2)
    assert audit_packet.articles[0].source == "B"
    begin_packet(progress, audit_packet, tmp_path)

    config.sources[0].enabled = True
    config.sources[1].enabled = False
    config.sources[2].enabled = False
    merge_candidates(progress, {}, config, {}, now=NOW + timedelta(hours=3))
    checkpoint_candidates(progress, tmp_path)
    restored = load_candidate_progress(tmp_path)
    identity = article_hash(original.title, original.link)
    assert article_hash(keeper.title, keeper.link) not in restored.candidates
    assert restored.candidates[identity].article.article() == original
    proofs = [packet for packet in restored.packets
              if any(item.evidence_id == identity for item in packet.evidence.items)]
    assert proofs == [exact_packet, audit_packet]
    assert restored.candidates[identity].status == "technical_pending"
    next_packet = _packet(restored, config, 3)
    assert next_packet.articles[0].source == "A"
    begin_packet(restored, next_packet, tmp_path)
    assert _packet(load_candidate_progress(tmp_path), config, 4).articles[0].source == "D"


def test_oversized_unseen_and_retry_candidates_do_not_consume_admitted_slots(tmp_path: Path) -> None:
    config, articles = population(3)
    articles["tech"][0].link += "x" * 20000
    progress = merge_candidates(CandidateProgress(), {"tech": articles["tech"][:1]}, config, {}, now=NOW)
    merge_candidates(progress, {"tech": articles["tech"][1:]}, config, {}, now=NOW + timedelta(minutes=1))
    first = _packet(progress, config, 1)
    assert len(first.articles) == 2
    retry_ids = {item.evidence_id for item in first.evidence.items}
    begin_packet(progress, first, tmp_path)
    fresh = _new_articles(4, 2)
    fresh["tech"][0].link += "x" * 20000
    fresh["tech"][0].pub_date = NOW + timedelta(hours=2, minutes=1)
    merge_candidates(progress, fresh, config, {}, now=NOW + timedelta(hours=2, minutes=1))
    config.review.max_evidence_articles = 5
    config.review.max_technical_retry_articles = 2
    packet = _packet(progress, config, 3)
    assert len(packet.articles) == 5
    assert len({item.evidence_id for item in packet.evidence.items} & retry_ids) == 2
    assert packet.articles[0].title.startswith("Fresh")
    oversized = [candidate for candidate in progress.candidates.values() if len(candidate.article.link) > 20000]
    assert len(oversized) == 2
    assert all(candidate.status == "technical_pending" and candidate.disposition is None for candidate in oversized)
    assert all(len(article.link) < 20000 for article in packet.articles)
    assert packet.report is None


def test_joint_byte_pressure_skips_large_retry_and_backfills_small_items(tmp_path: Path) -> None:
    config, articles = population(3)
    articles["tech"][0].link += "x" * 14500
    progress = merge_candidates(CandidateProgress(), {"tech": articles["tech"][:1]}, config, {}, now=NOW)
    merge_candidates(progress, {"tech": articles["tech"][1:]}, config, {}, now=NOW + timedelta(minutes=1))
    first = _packet(progress, config, 1)
    assert len(first.articles) == 3
    begin_packet(progress, first, tmp_path)
    large = next(candidate for candidate in progress.candidates.values() if len(candidate.article.link) > 10000)
    fresh = _new_articles(2, 2)
    for article in fresh["tech"]:
        article.link += "y" * 1500
    merge_candidates(progress, fresh, config, {}, now=NOW + timedelta(hours=2))
    config.review.max_evidence_articles = 4
    config.review.max_technical_retry_articles = 2
    packet = _packet(progress, config, 2)
    assert len(packet.articles) == 4
    assert large.identity not in {item.evidence_id for item in packet.evidence.items}
    assert large.status == "technical_pending" and large.disposition is None
    assert len(build_evidence_bundle({"tech": [large.article.article()]}, config.review).items) == 1
    assert sum(len(json.dumps(asdict(item), ensure_ascii=False)) for item in packet.evidence.items) <= 16000
