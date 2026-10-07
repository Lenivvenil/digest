"""Recoverable RSS candidate progress, separate from accepted preparation.

Registration, planned requests and validated selections are distinct facts. This
module makes no model calls and imposes no lifetime beyond current eligibility.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from digest._serialization import canonical_json_bytes as _canonical
from digest._util import utc_instant as _instant
from digest.adapters.storage.candidate_progress import CANDIDATE_FILE as CANDIDATE_FILE
from digest.adapters.storage.candidate_progress import MAX_BYTES as MAX_BYTES
from digest.adapters.storage.candidate_progress import archive_candidate_accounting as archive_candidate_accounting
from digest.adapters.storage.candidate_progress import candidate_accounting_sources as candidate_accounting_sources
from digest.adapters.storage.candidate_progress import load_candidate_progress as load_candidate_progress
from digest.adapters.storage.candidate_progress import progress_size as progress_size
from digest.application.candidate_lifecycle import (
    checkpoint_candidates,
    index_candidate,
    persist_candidates,
)
from digest.application.candidate_lifecycle import ensure_report_accounting as ensure_report_accounting
from digest.candidate_dispositions import (
    CandidateDispositionCapture,
    validate_disposition_attempt,
)
from digest.config import Config
from digest.domain.editorial.candidates import Candidate as Candidate
from digest.domain.editorial.candidates import CandidateArticle as CandidateArticle
from digest.domain.editorial.candidates import CandidatePacket as CandidatePacket
from digest.domain.editorial.candidates import CandidateProgress as CandidateProgress
from digest.domain.editorial.candidates import CandidateStatus as CandidateStatus
from digest.domain.editorial.candidates import latest_occurrence_packet as _latest_occurrence_packet
from digest.domain.editorial.candidates import validate_progress as _validate
from digest.domain.editorial.reviews import validate_canonical_report as _validate_report
from digest.filters import is_blocked
from digest.radar.collector import Article, CollectionInventory, article_hash
from digest.review import (
    BlindReviewReport,
    _delivery_review,
    _validated_cached_selections,
    build_evidence_bundle,
    build_review_messages,
)
from digest.review_checkpoint import validate_evidence_bundle

# Candidate-only current-work capacity: source bodies and resolved history live
# in independently verified objects. Accepted preparation keeps its 4 MB bound.
# No identity truncation, TTL or automatic archive deletion is implied.
# Two 32K-character responses, up to 12 JSON bytes per astral Unicode character,
# capture/report copies and duplicated 16K evidence plus metadata fit within 2MiB.
# This is a storage reserve, not an increase to provider tokens or request count.
RESPONSE_STORAGE_RESERVE = 2_097_152
def packet_articles(packet: CandidatePacket) -> dict[str, list[Article]]:
    grouped: dict[str, list[Article]] = {}
    for saved in packet.articles:
        grouped.setdefault(saved.category, []).append(saved.article())
    return grouped


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
    progress.policy_sha256 = _eligibility_policy(config)
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
        if identity not in progress.candidates:
            progress.candidates[identity] = Candidate(identity, saved, instant.isoformat(), source.priority)
        candidate = progress.candidates[identity]
        if saved != candidate.article and saved not in candidate.occurrences:
            candidate.occurrences = (*candidate.occurrences, saved)
            same_binding = (saved.source, saved.source_url, saved.category) == (
                candidate.article.source, candidate.article.source_url, candidate.article.category)
            if (candidate.status in {"not_selected", "duplicate"}
                    or same_binding and candidate.status in {"not_presented", "technical_pending"}):
                old = candidate.article
                candidate.article = saved
                candidate.occurrences = tuple(item for item in (*candidate.occurrences, old) if item != saved)
                candidate.status = "not_presented"
                _clear_disposition(candidate)
        _current_occurrences(candidate)

    def exclusion(saved: CandidateArticle, identity: str) -> str:
        article = saved.article()
        source = sources.get(article.source)
        if source is None or source.url != saved.source_url or source.category != article.category:
            return "source_disabled_or_changed"
        if identity in delivered_cache:
            return "existing_delivery_cache"
        if article.pub_date is not None and article.pub_date < instant - timedelta(hours=source.recency_hours):
            return "outside_source_recency"
        if (is_blocked(article.title, config.filters.blocklist_keywords)
                or is_blocked(article.description, config.filters.blocklist_keywords)):
            return "current_blocklist"
        return ""

    for identity, candidate in progress.candidates.items():
        cached_at = (delivery_history if delivery_history is not None else delivered_cache).get(identity)
        if candidate.delivery_cache_observed_at is None and isinstance(cached_at, str):
            try:
                cached_time = datetime.fromisoformat(cached_at)
                if cached_time.tzinfo is not None and cached_time.utcoffset() is not None:
                    candidate.delivery_cache_observed_at = cached_at
            except ValueError:
                pass  # Malformed raw-cache values are not delivery evidence.
        # Original and prior packet occurrences stay immutable; only unfinished
        # work may choose another eligible exact-identity occurrence.
        if (candidate.status in {"selected", "not_selected", "duplicate"} and exclusion(candidate.article, identity)
                and any(not exclusion(saved, identity) for saved in candidate.occurrences)):
            candidate.status = "technical_pending"
        if candidate.status in {"not_presented", "technical_pending"}:
            occurrences = (candidate.article, *candidate.occurrences)
            chosen = next((saved for saved in occurrences if not exclusion(saved, identity)), candidate.article)
            candidate.occurrences = tuple(saved for saved in occurrences if saved != chosen)
            if chosen != candidate.article:
                _clear_disposition(candidate)
            candidate.article = chosen
        reason = exclusion(candidate.article, identity)
        candidate.eligible = not reason
        candidate.eligibility_reason = reason
        source = sources.get(candidate.article.source)
        if source is not None:
            candidate.priority = (effective_priorities or {}).get(source.name, source.priority)
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


def _freshness(candidate: Candidate, instant: datetime) -> datetime | None:
    published = candidate.article.published
    if published is None:
        return None
    value = datetime.fromisoformat(published)
    # A timestamp future at first observation is not evidence of publication
    # freshness, even after the advertised date passes. It remains eligible.
    return value if value <= min(instant, datetime.fromisoformat(candidate.first_observed_at)) else None


def _source_turns(
    candidates: list[Candidate], instant: datetime, retry_times: dict[str, datetime] | None = None,
) -> list[Candidate]:
    groups: dict[tuple[str, str], list[Candidate]] = {}
    for candidate in candidates:
        groups.setdefault((candidate.article.category, candidate.article.source), []).append(candidate)
    ordered = []
    round_number = 0
    while groups:
        heads = {}
        for key, group in groups.items():
            pool = group
            if retry_times is None and round_number % 2 == 0:
                dated = [(stamp, item) for item in group if (stamp := _freshness(item, instant)) is not None]
                if dated:
                    newest = max(stamp for stamp, _ in dated)
                    pool = [item for stamp, item in dated if stamp == newest]
            heads[key] = min(pool, key=lambda item: (
                (retry_times or {}).get(item.identity, datetime.fromisoformat(item.first_observed_at)),
                datetime.fromisoformat(item.first_observed_at), item.identity,
            ))
        for key in sorted(heads, key=lambda key: (
            (retry_times or {}).get(heads[key].identity, datetime.fromisoformat(heads[key].first_observed_at)),
            datetime.fromisoformat(heads[key].first_observed_at), -heads[key].priority,
            key[1], heads[key].identity,
        )):
            head = heads[key]
            ordered.append(head)
            groups[key].remove(head)
            if not groups[key]:
                del groups[key]
        round_number += 1
    return ordered


def _admit_candidate(selected: list[CandidateArticle], candidate: Candidate, config: Config) -> bool:
    articles: dict[str, list[Article]] = {}
    for saved in [*selected, candidate.article]:
        articles.setdefault(saved.category, []).append(saved.article())
    if len(build_evidence_bundle(articles, config.review).items) == len(selected) + 1:
        selected.append(candidate.article)
        return True
    if not build_evidence_bundle({candidate.article.category: [candidate.article.article()]}, config.review).items:
        candidate.status = "technical_pending"
    return False


def plan_packet(progress: CandidateProgress, config: Config, now: datetime | None = None) -> CandidatePacket | None:
    """Bound fresh/age source turns and technical continuation in one request.

    A fitting unseen item gets the first opportunity. Retry reservations consume
    only actual admitted evidence, then unused count/character capacity backfills.
    With a one-item limit, unseen work retains preference. No extra request or
    editorial decision is implied by a turn, a byte skip or a saved plan.
    """
    instant = _instant(now)
    unseen = [item for item in progress.candidates.values() if item.eligible and item.status == "not_presented"]
    retries = [item for item in progress.candidates.values() if item.eligible and item.status == "technical_pending"]
    retry_times = {}
    for item in retries:
        packet = _latest_occurrence_packet(item, progress.packets)
        retry_times[item.identity] = datetime.fromisoformat(packet.planned_at if packet else item.first_observed_at)
    unseen = _source_turns(unseen, instant)
    retries = _source_turns(retries, instant, retry_times)
    limit = config.review.max_evidence_articles
    reserved = min(config.review.max_technical_retry_articles, max(0, limit - bool(unseen)))
    selected: list[CandidateArticle] = []
    # Count and byte protection for the first fitting unseen opportunity. Skips
    # stay pending; do not reserve fictitious capacity for an unadmitted item.
    while unseen and not selected:
        _admit_candidate(selected, unseen.pop(0), config)
    used_retries = 0
    while retries and used_retries < reserved and len(selected) < limit:
        used_retries += _admit_candidate(selected, retries.pop(0), config)
    for candidate in [*unseen, *retries]:
        if len(selected) >= limit:
            break
        _admit_candidate(selected, candidate, config)
    if not selected:
        return None
    packet = CandidatePacket(build_evidence_bundle({}, config.review), tuple(selected),
                             {item.source: progress.candidates[article_hash(item.title, item.link)].priority
                              for item in selected}, _instant(now).isoformat())
    packet.evidence = build_evidence_bundle(packet_articles(packet), config.review)
    packet.collection_json = progress.latest_collection_json
    validate_evidence_bundle(packet.evidence, config)
    packet.prompt_hash = hashlib.sha256(json.dumps(build_review_messages(
        packet.evidence, config.review, config.radar.language, sources=config.sources,
        closing=getattr(config, "closing", None)),
        sort_keys=True).encode()).hexdigest()
    packet.max_selections = config.review.max_selections
    return packet


def _current_occurrences(candidate: Candidate) -> None:
    """Keep only the latest live alternative per source binding; history stays in objects."""
    latest = {(item.source, item.source_url, item.category): item for item in candidate.occurrences}
    if candidate.status != "selected":
        latest.pop((candidate.article.source, candidate.article.source_url, candidate.article.category), None)
    candidate.occurrences = tuple(item for item in latest.values() if item != candidate.article)


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
        _clear_disposition(candidate)
        candidate.status = "technical_pending"
    path = checkpoint_candidates(progress, cache_dir, skipped_empty_reports=skipped_empty_reports)
    if progress_size(progress, cache_dir) + RESPONSE_STORAGE_RESERVE > MAX_BYTES:
        raise ValueError(
            "Insufficient candidate working-set capacity before model work; no input was truncated.")
    return path


def _clear_disposition(candidate: Candidate) -> None:
    candidate.disposition = None
    candidate.decision_response_sha256 = None
    candidate.decision_prompt_hash = None
    candidate.decision_occurrence_sha256 = None


def reconcile_packet(
    progress: CandidateProgress, packet: CandidatePacket, report: BlindReviewReport, config: Config,
    cache_dir: str | Path = ".cache", *, disposition_capture: CandidateDispositionCapture | None = None,
) -> Path:
    """Keep only validated primary/fallback results; technical failure is unfinished."""
    if packet not in progress.packets or report.evidence != packet.evidence:
        raise ValueError("Candidate result does not match its planned evidence.")
    validate_evidence_bundle(report.evidence, config)
    _validate_report(report)
    prompt_hash = hashlib.sha256(json.dumps(build_review_messages(
        report.evidence, config.review, config.radar.language, sources=config.sources,
        closing=getattr(config, "closing", None)),
        sort_keys=True).encode()).hexdigest()
    selected: set[str] = set()
    rejected: set[str] = set()
    valid = False
    for review in [_delivery_review(report)]:
        if review.slot not in {"primary", "secondary"} or review.status not in {"ok", "partial", "abstained"}:
            continue
        if review.prompt_hash != prompt_hash or review.prompt_hash != packet.prompt_hash:
            raise ValueError("Candidate result prompt differs from the planned review contract.")
        selections, _ = _validated_cached_selections(review, report.evidence)
        selected.update(item.evidence_id for item in selections)
        rejected.update(item.evidence_id for item in review.rejected_items if item.evidence_id is not None)
        valid = True
        break  # Same primary-first fallback semantics as delivery.
    delivery_review = _delivery_review(report)
    captured = None
    if disposition_capture is not None:
        for attempt in disposition_capture.attempts:
            matching = next((item for item in report.reviews if item.slot == attempt.slot), None)
            if matching is None:
                raise ValueError("Disposition capture has no matching review slot.")
            validate_disposition_attempt(attempt, packet.evidence, matching)
            if attempt.slot == delivery_review.slot:
                if captured is not None:
                    raise ValueError("Duplicate delivery disposition capture.")
                captured = attempt
        packet.disposition_attempts = tuple(disposition_capture.attempts)
    dispositions = {item.evidence_id: item for item in captured.dispositions} if captured else {}
    for item in packet.evidence.items:
        candidate = progress.candidates[item.evidence_id]
        _clear_disposition(candidate)
        decision = dispositions.get(item.evidence_id)
        if item.evidence_id in selected:
            candidate.status = "selected"
        elif decision is not None and decision.status in {"not_selected", "duplicate"}:
            candidate.status = "duplicate" if decision.status == "duplicate" else "not_selected"
        elif disposition_capture is not None:
            candidate.status = "technical_pending"
        elif valid and item.evidence_id not in rejected:
            candidate.status = "not_selected_without_editorial_reason"
        else:
            candidate.status = "technical_pending"
        if decision is not None and captured is not None:
            candidate.disposition = decision
            candidate.decision_response_sha256 = captured.response_sha256
            candidate.decision_prompt_hash = captured.prompt_hash
            candidate.decision_occurrence_sha256 = _digest(asdict(candidate.article))
    packet.report = report
    path = persist_candidates(progress, cache_dir)
    ensure_report_accounting(progress, report, cache_dir)
    return path


def pending_completed_report(
    progress: CandidateProgress, *, skip_reports: set[str] | None = None,
) -> BlindReviewReport | None:
    """Recover undelivered selection after callers honor ready/preparation precedence.

    Handoff means accepted preparation, never confirmed delivery. Once that
    preparation expires, an eligible selected report remains recoverable. Empty
    abstentions stay consumed; mixed eligibility returns useful work to planning.
    """
    for packet in reversed(progress.packets):
        if packet.report is None or _digest(asdict(packet.report)) in (skip_reports or set()):
            continue
        successful = _delivery_review(packet.report) if packet.report.reviews else None
        if successful is None or successful.status not in {"ok", "partial", "abstained"}:
            continue
        selections = successful.selections
        if not selections:
            capture = next((item for item in packet.disposition_attempts if item.slot == successful.slot), None)
            complete = not packet.disposition_attempts or capture is not None and capture.status == "complete"
            same_eligible_occurrences = all(
                article_hash(saved.title, saved.link) in progress.candidates
                and progress.candidates[article_hash(saved.title, saved.link)].eligible
                and progress.candidates[article_hash(saved.title, saved.link)].article == saved
                for saved in packet.articles
            )
            if (successful.status == "abstained" and not packet.handed_to_preparation
                    and complete and same_eligible_occurrences):
                return packet.report
            continue
        eligible = [item for item in selections if item.evidence_id in progress.candidates
                    and progress.candidates[item.evidence_id].eligible
                    and progress.candidates[item.evidence_id].delivery_cache_observed_at is None
                    and progress.candidates[item.evidence_id].status in {"selected", "technical_pending"}]
        original = {article_hash(saved.title, saved.link): saved for saved in packet.articles}
        if len(eligible) == len(selections) and all(
                progress.candidates[item.evidence_id].article == original[item.evidence_id] for item in eligible):
            return packet.report
        # A report containing now-ineligible selections cannot be replayed as an
        # accepted whole. Keep its eligible selections available to a new packet.
        for item in eligible:
            progress.candidates[item.evidence_id].status = "technical_pending"
    return None


def mark_prepared(progress: CandidateProgress, bundle_id: str, cache_dir: str | Path = ".cache") -> Path:
    for packet in progress.packets:
        if packet.evidence.bundle_id == bundle_id and packet.report is not None:
            packet.handed_to_preparation = True
    return checkpoint_candidates(progress, cache_dir)


def candidate_accounting(progress: CandidateProgress) -> dict[str, Any]:
    """Additive archive data, never part of the strict review report dataclass."""
    _validate(progress)
    return asdict(progress)


def _eligibility_policy(config: Config) -> str:
    return _digest({"sources": [(source.name, source.url, source.category, source.enabled, source.recency_hours)
                                for source in config.sources], "blocklist": config.filters.blocklist_keywords})


def _restore_relevant_history(
    progress: CandidateProgress, articles: dict[str, list[Article]], config: Config, cache_dir: str | Path,
    now: datetime,
) -> None:
    from digest.adapters.storage import candidate_objects as storage

    identities = {article_hash(item.title, item.link) for group in articles.values() for item in group}
    if storage.read_policy(cache_dir) != _eligibility_policy(config):
        for identity in storage.list_excluded(cache_dir):
            header = storage.read_candidate_header(identity, cache_dir)
            if header is not None and _excluded_policy_relevant(header, config, now):
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


def _excluded_policy_relevant(header: dict[str, Any], config: Config, now: datetime) -> bool:
    sources = {source.name: source for source in config.enabled_sources}
    for fields in header["policy_fields"]:
        source = sources.get(fields["source"])
        if source is None or source.url != fields["source_url"] or source.category != fields["category"]:
            continue
        published = datetime.fromisoformat(fields["published"]) if fields["published"] is not None else None
        if published is None or published >= now - timedelta(hours=source.recency_hours):
            return True  # Current blocklist needs the exact source text, loaded only for relevant identities.
    return False


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def save_candidate_progress(
    progress: CandidateProgress, cache_dir: str | Path = ".cache", *, retire: bool = True,
    skipped_empty_reports: set[str] | None = None,
) -> Path:
    """Legacy API: retain its retirement semantics while new callers name the effect."""
    if retire:
        return checkpoint_candidates(progress, cache_dir, skipped_empty_reports=skipped_empty_reports)
    return persist_candidates(progress, cache_dir)
