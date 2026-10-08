"""Candidate request assembly and ordered retained-work/storage coordination."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from digest._serialization import canonical_json_bytes as _canonical
from digest._util import utc_instant as _instant
from digest.adapters.storage.candidate_progress import MAX_BYTES, progress_size
from digest.application.candidate_lifecycle import (
    checkpoint_candidates,
    ensure_report_accounting,
    index_candidate,
    persist_candidates,
)
from digest.application.review_request import build_evidence_bundle, build_review_messages
from digest.config import Config
from digest.domain.catalog.articles import Article, article_hash
from digest.domain.editorial.candidate_policy import (
    apply_packet_report,
    clear_disposition,
    eligibility_policy,
    excluded_policy_relevant,
    packet_articles,
    plan_articles,
    reconcile_eligibility,
    register_occurrence,
    validate_packet_report,
)
from digest.domain.editorial.candidates import CandidateArticle, CandidatePacket, CandidateProgress
from digest.domain.editorial.dispositions import CandidateDispositionCapture
from digest.domain.editorial.reviews import BlindReviewReport, review_prompt_hash, validate_request_evidence_bundle
from digest.radar.collector import CollectionInventory

# Candidate-only current-work capacity: source bodies and resolved history live
# in independently verified objects. Accepted preparation keeps its 4 MB bound.
# No identity truncation, TTL or automatic archive deletion is implied.
# Two 32K-character responses, up to 12 JSON bytes per astral Unicode character,
# capture/report copies and duplicated 16K evidence plus metadata fit within 2MiB.
# This is a storage reserve, not an increase to provider tokens or request count.
RESPONSE_STORAGE_RESERVE = 2_097_152


def _persist_observed_source(article: CandidateArticle, cache_dir: str | Path | None) -> None:
    if cache_dir is not None:
        from digest.adapters.storage.candidate_objects import put_article

        put_article(article, cache_dir)


def merge_candidates(
    progress: CandidateProgress, articles_by_category: dict[str, list[Article]], config: Config,
    delivered_cache: dict[str, str], effective_priorities: dict[str, int] | None = None,
    now: datetime | None = None, *, inventory: CollectionInventory | None = None,
    delivery_history: dict[str, str] | None = None, cache_dir: str | Path | None = None,
) -> CandidateProgress:
    """Merge observations; recheck old work without changing original timestamps.

    ``delivered_cache`` must be the pre-collection cache, never collect's mutated
    allocation cache. ``delivery_history`` may contain unpruned cache records;
    their timestamp is cache evidence, not independent Telegram confirmation.
    Undated absent entries retain unknown publication age.
    """
    instant = _instant(now)
    if cache_dir is not None:
        _restore_relevant_history(progress, articles_by_category, config, cache_dir, instant)
    progress.policy_sha256 = eligibility_policy(config.sources, config.filters.blocklist_keywords)
    sources = {source.name: source for source in config.enabled_sources}
    observed = [article for group in articles_by_category.values() for article in group]
    if inventory is not None:
        observed.extend(item.article for item in inventory.observations if item.eligible)
    for article in observed:
        source = sources.get(article.source)
        if source is None:
            continue
        identity = article_hash(article.title, article.link)
        saved = CandidateArticle(article.title, article.link, article.description, article.source,
                                 article.category, article.pub_date.isoformat() if article.pub_date else None,
                                 source.url)
        _persist_observed_source(saved, cache_dir)
        register_occurrence(progress, identity, saved, source.priority, instant)
    reconcile_eligibility(
        progress, sources, config.filters.blocklist_keywords, delivered_cache, effective_priorities,
        instant, delivery_history,
    )
    if inventory is not None:
        audit = asdict(inventory)
        source_urls = {source.name: source.url for source in config.sources}
        for raw, observation in zip(audit["observations"], inventory.observations, strict=True):
            raw["source_url"] = source_urls[observation.article.source]
            observed_candidate = progress.candidates.get(observation.identity)
            if observation.eligible and observed_candidate is not None:
                article = observation.article
                observed_saved = next((saved for saved in (observed_candidate.article,
                                                           *observed_candidate.occurrences)
                                       if saved.article() == article), None)
                if observed_saved is not None:
                    raw.pop("article")
                    raw["article_reference"] = {
                        "identity": observation.identity,
                        "occurrence_sha256": hashlib.sha256(_canonical(asdict(observed_saved))).hexdigest(),
                    }
        progress.latest_collection_json = json.dumps(
            audit, ensure_ascii=False, sort_keys=True,
            default=lambda value: value.isoformat() if isinstance(value, datetime) else str(value),
        )
    return progress


def plan_packet(progress: CandidateProgress, config: Config, now: datetime | None = None) -> CandidatePacket | None:
    """Bound fresh/age source turns and technical continuation in one request.

    A fitting unseen item gets the first opportunity. Retry reservations consume
    only actual admitted evidence, then unused count/character capacity backfills.
    With a one-item limit, unseen work retains preference. No extra request or
    editorial decision is implied by a turn, a byte skip or a saved plan.
    """
    instant = _instant(now)
    selected = plan_articles(
        progress, instant, max_evidence_articles=config.review.max_evidence_articles,
        max_excerpt_chars=config.review.max_excerpt_chars,
        max_technical_retry_articles=config.review.max_technical_retry_articles,
    )
    if not selected:
        return None
    packet = CandidatePacket(build_evidence_bundle({}, config.review), tuple(selected),
                             {item.source: progress.candidates[article_hash(item.title, item.link)].priority
                              for item in selected}, _instant(now).isoformat())
    packet.evidence = build_evidence_bundle(packet_articles(packet), config.review)
    packet.collection_json = progress.latest_collection_json
    validate_request_evidence_bundle(packet.evidence, max_evidence_articles=config.review.max_evidence_articles,
                                     max_excerpt_chars=config.review.max_excerpt_chars)
    packet.prompt_hash = review_prompt_hash(build_review_messages(
        packet.evidence, config.review, config.radar.language, sources=config.sources,
        closing=getattr(config, "closing", None)))
    packet.max_selections = config.review.max_selections
    return packet


def begin_packet(
    progress: CandidateProgress, packet: CandidatePacket, cache_dir: str | Path = ".cache", *,
    skipped_empty_reports: set[str] | None = None,
) -> Path:
    """Persist a planned attempt; this is not evidence that dispatch occurred."""
    if packet.report is not None:
        raise ValueError("Cannot begin an already completed candidate packet.")
    progress.packets.append(packet)
    for item in packet.evidence.items:
        candidate = progress.candidates[item.evidence_id]
        if candidate.disposition is not None:
            index_candidate(candidate, progress, cache_dir)
        clear_disposition(candidate)
        candidate.status = "technical_pending"
    path = checkpoint_candidates(progress, cache_dir, skipped_empty_reports=skipped_empty_reports)
    if progress_size(progress, cache_dir) + RESPONSE_STORAGE_RESERVE > MAX_BYTES:
        raise ValueError(
            "Insufficient candidate working-set capacity before model work; no input was truncated.")
    return path


def reconcile_packet(
    progress: CandidateProgress, packet: CandidatePacket, report: BlindReviewReport, config: Config,
    cache_dir: str | Path = ".cache", *, disposition_capture: CandidateDispositionCapture | None = None,
) -> Path:
    """Keep only validated primary/fallback results; technical failure is unfinished."""
    validate_packet_report(
        progress, packet, report, max_evidence_articles=config.review.max_evidence_articles,
        max_excerpt_chars=config.review.max_excerpt_chars,
    )
    prompt_hash = review_prompt_hash(build_review_messages(
        report.evidence, config.review, config.radar.language, sources=config.sources,
        closing=getattr(config, "closing", None)))
    apply_packet_report(progress, packet, report, prompt_hash, disposition_capture=disposition_capture)
    path = persist_candidates(progress, cache_dir)
    ensure_report_accounting(progress, report, cache_dir)
    return path


def mark_prepared(progress: CandidateProgress, bundle_id: str, cache_dir: str | Path = ".cache") -> Path:
    for packet in progress.packets:
        if packet.evidence.bundle_id == bundle_id and packet.report is not None:
            packet.handed_to_preparation = True
    return checkpoint_candidates(progress, cache_dir)


def _restore_relevant_history(
    progress: CandidateProgress, articles: dict[str, list[Article]], config: Config, cache_dir: str | Path,
    now: datetime,
) -> None:
    from digest.adapters.storage import candidate_objects as storage

    identities = {article_hash(item.title, item.link) for group in articles.values() for item in group}
    if storage.read_policy(cache_dir) != eligibility_policy(config.sources, config.filters.blocklist_keywords):
        for identity in storage.list_excluded(cache_dir):
            header = storage.read_candidate_header(identity, cache_dir)
            if header is not None and excluded_policy_relevant(
                header, {source.name: source for source in config.enabled_sources}, now):
                identities.add(identity)
    known_packets = {storage.packet_key(packet) for packet in progress.packets}
    for identity in sorted(identities - progress.candidates.keys()):
        candidate = storage.load_candidate(identity, cache_dir)
        if candidate is None:
            continue
        progress.candidates[identity] = candidate
        for packet in storage.load_candidate_packets(identity, cache_dir):
            key = storage.packet_key(packet)
            if key not in known_packets:
                progress.packets.append(packet)
                known_packets.add(key)
