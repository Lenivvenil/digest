"""Deterministic candidate occurrence, eligibility, scheduling and result rules.

Explicit source values, limits and instants keep these rules independent of
configuration, model execution and candidate storage coordination.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Any

from digest._serialization import canonical_json_bytes as _canonical
from digest.domain.catalog.articles import Article, article_hash
from digest.domain.catalog.sources import SourceConfig
from digest.domain.editorial.attempts import HistoricalDispositions, ResolvedReview, restore_review
from digest.domain.editorial.candidates import Candidate, CandidateArticle, CandidatePacket, CandidateProgress
from digest.domain.editorial.candidates import latest_occurrence_packet as _latest_occurrence_packet
from digest.domain.editorial.candidates import validate_progress as _validate
from digest.domain.editorial.dispositions import CandidateDispositionAttempt
from digest.domain.editorial.evidence import build_evidence_bundle
from digest.domain.editorial.reviews import (
    BlindReviewReport,
    validate_request_evidence_bundle,
)
from digest.domain.editorial.reviews import validate_canonical_report as _validate_report
from digest.filters import is_blocked


def packet_articles(packet: CandidatePacket) -> dict[str, list[Article]]:
    grouped: dict[str, list[Article]] = {}
    for saved in packet.articles:
        grouped.setdefault(saved.category, []).append(saved.article())
    return grouped


def register_occurrence(
    progress: CandidateProgress, identity: str, saved: CandidateArticle, priority: int, instant: datetime,
) -> None:
    """Register an observed source after its application persistence boundary."""
    if identity not in progress.candidates:
        progress.candidates[identity] = Candidate(identity, saved, instant.isoformat(), priority)
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
            clear_disposition(candidate)
    _current_occurrences(candidate)


def reconcile_eligibility(
    progress: CandidateProgress, sources: dict[str, SourceConfig], blocklist: list[str],
    delivered_cache: dict[str, str], effective_priorities: dict[str, int] | None,
    instant: datetime, delivery_history: dict[str, str] | None,
) -> None:
    """Recheck exact occurrences without changing original observation timestamps."""
    def exclusion(saved: CandidateArticle, identity: str) -> str:
        article = saved.article()
        source = sources.get(article.source)
        if source is None or source.url != saved.source_url or source.category != article.category:
            return "source_disabled_or_changed"
        if identity in delivered_cache:
            return "existing_delivery_cache"
        if article.pub_date is not None and article.pub_date < instant - timedelta(hours=source.recency_hours):
            return "outside_source_recency"
        if (is_blocked(article.title, blocklist)
                or is_blocked(article.description, blocklist)):
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
                clear_disposition(candidate)
            candidate.article = chosen
        reason = exclusion(candidate.article, identity)
        candidate.eligible = not reason
        candidate.eligibility_reason = reason
        source = sources.get(candidate.article.source)
        if source is not None:
            candidate.priority = (effective_priorities or {}).get(source.name, source.priority)


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


def _admit_candidate(
    selected: list[CandidateArticle], candidate: Candidate, max_evidence_articles: int, max_excerpt_chars: int,
) -> bool:
    articles: dict[str, list[Article]] = {}
    for saved in [*selected, candidate.article]:
        articles.setdefault(saved.category, []).append(saved.article())
    if len(build_evidence_bundle(
        articles, max_evidence_articles=max_evidence_articles, max_excerpt_chars=max_excerpt_chars,
    ).items) == len(selected) + 1:
        selected.append(candidate.article)
        return True
    if not build_evidence_bundle(
        {candidate.article.category: [candidate.article.article()]},
        max_evidence_articles=max_evidence_articles, max_excerpt_chars=max_excerpt_chars,
    ).items:
        candidate.status = "technical_pending"
    return False


def _closing_opportunity(
    selected: list[CandidateArticle], candidates: list[Candidate], protected_count: int, *,
    max_evidence_articles: int, max_excerpt_chars: int,
) -> list[CandidateArticle]:
    """Offer one approved occurrence before freezing, without changing candidate history."""
    if not candidates or any(candidate.article in selected for candidate in candidates):
        return selected
    retained = selected
    if len(selected) == max_evidence_articles:
        if len(selected) <= protected_count:
            return selected
        # Only the final ordinary backfill admission can be deferred. Its
        # original pending status and prior attempts remain untouched.
        retained = selected[:-1]
    for candidate in candidates:
        proposed = [*retained, candidate.article]
        articles: dict[str, list[Article]] = {}
        for saved in proposed:
            articles.setdefault(saved.category, []).append(saved.article())
        bundle = build_evidence_bundle(
            articles, max_evidence_articles=max_evidence_articles, max_excerpt_chars=max_excerpt_chars,
        )
        if (len(bundle.items) == len(proposed)
                and {item.evidence_id for item in bundle.items}
                == {article_hash(saved.title, saved.link) for saved in proposed}):
            return proposed
    return selected


def plan_articles(
    progress: CandidateProgress,
    instant: datetime,
    *,
    max_evidence_articles: int,
    max_excerpt_chars: int,
    max_technical_retry_articles: int,
    closing_sources: frozenset[tuple[str, str, str]] = frozenset(),
) -> list[CandidateArticle]:
    """Bound fresh/age source turns and technical continuation in one request.

    The oldest fitting unseen identity gets the protected first opportunity.
    Remaining unseen work keeps its established fresh/age source turns.
    Retry reservations consume only actual admitted evidence, then unused
    count/character capacity backfills. With a one-item limit, unseen age takes
    precedence over freshness. No extra request or editorial decision is implied
    by a turn, a byte skip or a saved plan.
    An absent approved closing source gets one bounded final opportunity, using
    spare capacity or deferring only the last ordinary backfill admission.
    """
    unseen = [item for item in progress.candidates.values() if item.eligible and item.status == "not_presented"]
    retries = [item for item in progress.candidates.values() if item.eligible and item.status == "technical_pending"]
    retry_times = {}
    for item in retries:
        packet = _latest_occurrence_packet(item, progress.packets)
        retry_times[item.identity] = datetime.fromisoformat(packet.planned_at if packet else item.first_observed_at)
    unseen = _source_turns(unseen, instant)
    retries = _source_turns(retries, instant, retry_times)
    # Filter the established queues so retry attempt-age ordering survives.
    closing_candidates = [
        item
        for item in [*unseen, *retries]
        if (item.article.source, item.article.source_url, item.article.category) in closing_sources
    ]
    limit = max_evidence_articles
    reserved = min(max_technical_retry_articles, max(0, limit - bool(unseen)))
    selected: list[CandidateArticle] = []
    # Identity age survives occurrence changes. Preserve the existing backfill
    # queue; rebuilding source turns here would change its fresh/age parity.
    oldest_unseen = sorted(
        unseen,
        key=lambda item: (
            datetime.fromisoformat(item.first_observed_at),
            -item.priority,
            item.article.source,
            item.identity,
        ),
    )
    for candidate in oldest_unseen:
        unseen.remove(candidate)
        if _admit_candidate(selected, candidate, max_evidence_articles, max_excerpt_chars):
            break
    used_retries = 0
    while retries and used_retries < reserved and len(selected) < limit:
        used_retries += _admit_candidate(selected, retries.pop(0), max_evidence_articles, max_excerpt_chars)
    protected_count = len(selected)
    for candidate in [*unseen, *retries]:
        if len(selected) >= limit:
            break
        _admit_candidate(selected, candidate, max_evidence_articles, max_excerpt_chars)
    return _closing_opportunity(
        selected,
        closing_candidates,
        protected_count,
        max_evidence_articles=max_evidence_articles,
        max_excerpt_chars=max_excerpt_chars,
    )


def _current_occurrences(candidate: Candidate) -> None:
    """Keep only the latest live alternative per source binding; history stays in objects."""
    latest = {(item.source, item.source_url, item.category): item for item in candidate.occurrences}
    if candidate.status != "selected":
        latest.pop((candidate.article.source, candidate.article.source_url, candidate.article.category), None)
    candidate.occurrences = tuple(item for item in latest.values() if item != candidate.article)


def clear_disposition(candidate: Candidate) -> None:
    candidate.disposition = None
    candidate.decision_response_sha256 = None
    candidate.decision_prompt_hash = None
    candidate.decision_occurrence_sha256 = None


def validate_packet_report(
    progress: CandidateProgress, packet: CandidatePacket, report: BlindReviewReport, *,
    max_evidence_articles: int, max_excerpt_chars: int,
) -> None:
    if packet not in progress.packets or report.evidence != packet.evidence:
        raise ValueError("Candidate result does not match its planned evidence.")
    validate_request_evidence_bundle(report.evidence, max_evidence_articles=max_evidence_articles,
                                     max_excerpt_chars=max_excerpt_chars)
    _validate_report(report)


def apply_packet_report(
    progress: CandidateProgress, packet: CandidatePacket, result: ResolvedReview, prompt_hash: str,
) -> None:
    """Apply the resolved delivery response before persisting candidate accounting."""
    review = result.chosen.review
    accountable = review.slot in {"primary", "secondary"}
    if accountable and review.status in {"ok", "partial", "abstained"} and (
        review.prompt_hash != prompt_hash or review.prompt_hash != packet.prompt_hash
    ):
        raise ValueError("Candidate result prompt differs from the planned review contract.")
    selected = {item.evidence_id for item in result.selections} if accountable else set()
    rejected = {item.evidence_id for item in review.rejected_items if item.evidence_id is not None}
    captured = result.chosen.dispositions
    dispositions = ({item.evidence_id: item for item in captured.dispositions}
                    if isinstance(captured, CandidateDispositionAttempt) else {})
    packet.disposition_attempts = result.disposition_attempts
    for item in packet.evidence.items:
        candidate = progress.candidates[item.evidence_id]
        clear_disposition(candidate)
        decision = dispositions.get(item.evidence_id)
        if item.evidence_id in selected:
            candidate.status = "selected"
        elif decision is not None and decision.status in {"not_selected", "duplicate"}:
            candidate.status = "duplicate" if decision.status == "duplicate" else "not_selected"
        elif captured is not HistoricalDispositions.NOT_RECORDED:
            candidate.status = "technical_pending"
        elif accountable and review.status in {"ok", "partial", "abstained"} and item.evidence_id not in rejected:
            candidate.status = "not_selected_without_editorial_reason"
        else:
            candidate.status = "technical_pending"
        if decision is not None and isinstance(captured, CandidateDispositionAttempt):
            candidate.disposition = decision
            candidate.decision_response_sha256 = captured.response_sha256
            candidate.decision_prompt_hash = captured.prompt_hash
            candidate.decision_occurrence_sha256 = _digest(asdict(candidate.article))
    packet.report = result.report


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
        if not packet.report.reviews:
            continue
        try:
            result = restore_review(packet.report, packet.disposition_attempts)
        except ValueError:
            continue  # Invalid historical selections cannot be promoted to recoverable work.
        selections = result.selections
        if not selections:
            same_eligible_occurrences = all(
                article_hash(saved.title, saved.link) in progress.candidates
                and progress.candidates[article_hash(saved.title, saved.link)].eligible
                and progress.candidates[article_hash(saved.title, saved.link)].article == saved
                for saved in packet.articles
            )
            if (result.abstention_complete and not packet.handed_to_preparation
                    and same_eligible_occurrences):
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


def candidate_accounting(progress: CandidateProgress) -> dict[str, Any]:
    """Additive archive data, never part of the strict review report dataclass."""
    _validate(progress)
    return asdict(progress)


def eligibility_policy(sources: list[SourceConfig], blocklist: list[str]) -> str:
    return _digest({"sources": [(source.name, source.url, source.category, source.enabled, source.recency_hours)
                                for source in sources], "blocklist": blocklist})


def excluded_policy_relevant(header: dict[str, Any], sources: dict[str, SourceConfig], now: datetime) -> bool:
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
