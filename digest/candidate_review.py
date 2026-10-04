"""Recoverable RSS candidate progress, separate from accepted preparation.

Registration, planned requests and validated selections are distinct facts. This
module makes no model calls and imposes no lifetime beyond current eligibility.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from digest._util import atomic_json_write
from digest.candidate_dispositions import (
    CandidateDisposition,
    CandidateDispositionAttempt,
    CandidateDispositionCapture,
    validate_disposition_attempt,
)
from digest.config import Config
from digest.filters import is_blocked
from digest.preparation import _canonical, _instant, _restore, _safe, _unique_object, _validate_report
from digest.radar.collector import Article, CollectionInventory, article_hash
from digest.review import (
    BlindReviewReport,
    EvidenceBundle,
    _delivery_review,
    _validated_cached_selections,
    build_evidence_bundle,
    build_review_messages,
)
from digest.review_checkpoint import validate_evidence_bundle

CANDIDATE_FILE = "candidate_progress.json"
# Candidate-only current-work capacity: source bodies and resolved history live
# in independently verified objects. Accepted preparation keeps its 4 MB bound.
# No identity truncation, TTL or automatic archive deletion is implied.
MAX_BYTES = 32_000_000
# Two 32K-character responses, up to 12 JSON bytes per astral Unicode character,
# capture/report copies and duplicated 16K evidence plus metadata fit within 2MiB.
# This is a storage reserve, not an increase to provider tokens or request count.
RESPONSE_STORAGE_RESERVE = 2_097_152
CandidateStatus = Literal[
    "selected", "not_selected", "duplicate", "not_selected_without_editorial_reason",
    "not_presented", "technical_pending",
]


@dataclass(frozen=True)
class CandidateArticle:
    title: str
    link: str
    description: str
    source: str
    category: str
    published: str | None
    source_url: str

    def article(self) -> Article:
        return Article(self.title, self.link, self.description, self.source, self.category,
                       datetime.fromisoformat(self.published) if self.published else None)


@dataclass
class Candidate:
    identity: str
    article: CandidateArticle
    first_observed_at: str
    priority: int
    status: CandidateStatus = "not_presented"
    eligible: bool = True
    eligibility_reason: str = ""
    occurrences: tuple[CandidateArticle, ...] = ()
    delivery_cache_observed_at: str | None = None
    disposition: CandidateDisposition | None = None
    decision_response_sha256: str | None = None
    decision_prompt_hash: str | None = None
    decision_occurrence_sha256: str | None = None


@dataclass
class CandidatePacket:
    evidence: EvidenceBundle
    articles: tuple[CandidateArticle, ...]
    priorities: dict[str, int]
    planned_at: str
    prompt_hash: str = ""
    max_selections: int = 5
    report: BlindReviewReport | None = None
    handed_to_preparation: bool = False
    disposition_attempts: tuple[CandidateDispositionAttempt, ...] = ()
    collection_json: str = "{}"


@dataclass
class CandidateProgress:
    schema_version: int = 1
    candidates: dict[str, Candidate] = field(default_factory=dict)
    packets: list[CandidatePacket] = field(default_factory=list)
    latest_collection_json: str = "{}"
    policy_sha256: str = ""


def packet_articles(packet: CandidatePacket) -> dict[str, list[Article]]:
    grouped: dict[str, list[Article]] = {}
    for saved in packet.articles:
        grouped.setdefault(saved.category, []).append(saved.article())
    return grouped


def _persist_observed_source(article: CandidateArticle, cache_dir: str | Path | None) -> None:
    if cache_dir is not None:
        from digest.candidate_storage import put_article

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


def plan_packet(progress: CandidateProgress, config: Config, now: datetime | None = None) -> CandidatePacket | None:
    """Fix membership before the evidence builder's URL ordering can reorder it.

    Unseen identities precede technical retries. A too-large item does not stop
    later fitting candidates. Bounds constrain requests, never editorial status.
    """
    candidates: list[Candidate] = []
    for status in ("not_presented", "technical_pending"):
        groups: dict[tuple[str, str], list[Candidate]] = {}
        for candidate in progress.candidates.values():
            if candidate.eligible and candidate.status == status:
                groups.setdefault((candidate.article.category, candidate.article.source), []).append(candidate)
        for group in groups.values():
            group.sort(key=lambda item: (item.first_observed_at, item.identity))
        # Interleave sources within categories, then categories. Priorities only
        # choose the order of each turn, never a source's editorial allowance.
        categories: dict[str, list[Candidate]] = {}
        for category in sorted({key[0] for key in groups}):
            sources = sorted((key for key in groups if key[0] == category),
                             key=lambda key: (-groups[key][0].priority, key[1]))
            ordered: list[Candidate] = []
            while any(groups[key] for key in sources):
                for key in sources:
                    if groups[key]:
                        ordered.append(groups[key].pop(0))
            categories[category] = ordered
        while any(categories.values()):
            for group in categories.values():
                if group:
                    candidates.append(group.pop(0))
    selected: list[CandidateArticle] = []
    for candidate in candidates:
        if len(selected) >= config.review.max_evidence_articles:
            break
        provisional = CandidatePacket(build_evidence_bundle({}, config.review),
                                      tuple([*selected, candidate.article]), {}, _instant(now).isoformat())
        bundle = build_evidence_bundle(packet_articles(provisional), config.review)
        # Every fixed member must survive the existing JSON budget.
        if len(bundle.items) == len(selected) + 1:
            selected.append(candidate.article)
        elif not build_evidence_bundle({candidate.article.category: [candidate.article.article()]},
                                       config.review).items:
            candidate.status = "technical_pending"
    if not selected:
        return None
    packet = CandidatePacket(build_evidence_bundle({}, config.review), tuple(selected),
                             {item.source: progress.candidates[article_hash(item.title, item.link)].priority
                              for item in selected}, _instant(now).isoformat())
    packet.evidence = build_evidence_bundle(packet_articles(packet), config.review)
    packet.collection_json = progress.latest_collection_json
    validate_evidence_bundle(packet.evidence, config)
    packet.prompt_hash = hashlib.sha256(json.dumps(build_review_messages(
        packet.evidence, config.review, config.radar.language), sort_keys=True).encode()).hexdigest()
    packet.max_selections = config.review.max_selections
    return packet


def _current_occurrences(candidate: Candidate) -> None:
    """Keep only the latest live alternative per source binding; history stays in objects."""
    latest = {(item.source, item.source_url, item.category): item for item in candidate.occurrences}
    if candidate.status != "selected":
        latest.pop((candidate.article.source, candidate.article.source_url, candidate.article.category), None)
    candidate.occurrences = tuple(item for item in latest.values() if item != candidate.article)


def begin_packet(progress: CandidateProgress, packet: CandidatePacket, cache_dir: str | Path = ".cache") -> Path:
    """Persist a planned attempt; this is not evidence that dispatch occurred."""
    if packet.report is not None:
        raise ValueError("Cannot begin an already completed candidate packet.")
    progress.packets.append(packet)
    for item in packet.evidence.items:
        candidate = progress.candidates[item.evidence_id]
        if candidate.disposition is not None:
            _index_candidate(candidate, progress, cache_dir)
        _clear_disposition(candidate)
        candidate.status = "technical_pending"
    path = save_candidate_progress(progress, cache_dir)
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
        report.evidence, config.review, config.radar.language), sort_keys=True).encode()).hexdigest()
    selected: set[str] = set()
    rejected: set[str] = set()
    valid = False
    for review in [_delivery_review(report)]:
        if review.slot not in {"primary", "secondary"} or review.status not in {"ok", "partial", "abstained"}:
            continue
        if review.prompt_hash != prompt_hash or review.prompt_hash != packet.prompt_hash:
            raise ValueError("Candidate result prompt differs from the planned review contract.")
        selections, _ = _validated_cached_selections(review, report.evidence, config.review.max_selections)
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
    path = save_candidate_progress(progress, cache_dir, retire=False)
    ensure_report_accounting(progress, report, cache_dir)
    return path


def pending_completed_report(progress: CandidateProgress) -> BlindReviewReport | None:
    """Recover undelivered selection after callers honor ready/preparation precedence.

    Handoff means accepted preparation, never confirmed delivery. Once that
    preparation expires, an eligible selected report remains recoverable. Empty
    abstentions stay consumed; mixed eligibility returns useful work to planning.
    """
    for packet in reversed(progress.packets):
        if packet.report is None:
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
    return save_candidate_progress(progress, cache_dir)


def _validate_decision(candidate: Candidate, progress: CandidateProgress) -> None:
    if candidate.disposition is None:
        if (candidate.status in {"not_selected", "duplicate"}
                or any(value is not None for value in (candidate.decision_response_sha256,
                       candidate.decision_prompt_hash, candidate.decision_occurrence_sha256))):
            raise ValueError("Resolved candidate lacks exact disposition evidence.")
        return
    if (candidate.disposition.evidence_id != candidate.identity
            or candidate.status in {"selected", "not_selected", "duplicate"}
            and candidate.disposition.status != candidate.status
            or candidate.decision_occurrence_sha256 != _digest(asdict(candidate.article))):
        raise ValueError("Candidate decision source occurrence mismatch.")
    for packet in progress.packets:
        if not any(article_hash(article.title, article.link) == candidate.identity
                   and _digest(asdict(article)) == candidate.decision_occurrence_sha256 for article in packet.articles):
            continue
        for attempt in packet.disposition_attempts:
            if (attempt.response_sha256 == candidate.decision_response_sha256
                    and attempt.prompt_hash == candidate.decision_prompt_hash
                    and candidate.disposition in attempt.dispositions):
                return
    raise ValueError("Candidate decision has no exact saved response evidence.")


def _validate(progress: CandidateProgress) -> None:
    if not isinstance(json.loads(progress.latest_collection_json), dict):
        raise ValueError("Invalid collection inventory snapshot.")
    if progress.schema_version != 1:
        raise ValueError("Unsupported candidate progress schema.")
    for identity, candidate in progress.candidates.items():
        if identity != candidate.identity or identity != article_hash(candidate.article.title, candidate.article.link):
            raise ValueError("Candidate identity mismatch.")
        if datetime.fromisoformat(candidate.first_observed_at).tzinfo is None:
            raise ValueError("Candidate observation requires a timezone.")
        if (candidate.delivery_cache_observed_at is not None
                and datetime.fromisoformat(candidate.delivery_cache_observed_at).tzinfo is None):
            raise ValueError("Candidate delivery-cache evidence requires a timezone.")
        if candidate.article in candidate.occurrences:
            raise ValueError("Candidate repeats its canonical occurrence.")
        if any(article_hash(saved.title, saved.link) != identity for saved in candidate.occurrences):
            raise ValueError("Candidate occurrence identity mismatch.")
        article = candidate.article.article()
        if article.pub_date is not None and article.pub_date.tzinfo is None:
            raise ValueError("Candidate publication time requires a timezone.")
        _validate_decision(candidate, progress)
    for packet in progress.packets:
        if not isinstance(json.loads(packet.collection_json), dict):
            raise ValueError("Invalid packet collection accounting.")
        if len(packet.prompt_hash) != 64 or packet.max_selections < 1:
            raise ValueError("Invalid frozen candidate prompt contract.")
        if datetime.fromisoformat(packet.planned_at).tzinfo is None:
            raise ValueError("Candidate attempt requires a timezone.")
        known = {article_hash(article.title, article.link): article for article in packet.articles}
        if len(known) != len(packet.articles) or set(known) != {item.evidence_id for item in packet.evidence.items}:
            raise ValueError("Candidate packet membership mismatch.")
        _validate_report(packet.report or BlindReviewReport(1, packet.evidence, [], "incomplete", None, [], ""))
        if packet.disposition_attempts and packet.report is None:
            raise ValueError("Candidate dispositions lack their saved report.")
        if packet.report is not None:
            for attempt in packet.disposition_attempts:
                matching = next((review for review in packet.report.reviews if review.slot == attempt.slot), None)
                if matching is None:
                    raise ValueError("Saved disposition has no matching model review.")
                validate_disposition_attempt(attempt, packet.evidence, matching)
            if packet.report.evidence != packet.evidence:
                raise ValueError("Candidate report evidence mismatch.")
            for review in packet.report.reviews:
                if review.status in {"ok", "partial", "abstained"}:
                    _validated_cached_selections(review, packet.evidence, packet.max_selections)
                    if review.prompt_hash != packet.prompt_hash:
                        raise ValueError("Candidate report prompt mismatch.")


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
    from digest import candidate_storage as storage

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
                packet.handed_to_preparation = True
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


def _proof_packets(candidate: Candidate, packets: list[CandidatePacket]) -> list[CandidatePacket]:
    matches = [packet for packet in packets if any(item.evidence_id == candidate.identity
                                                  for item in packet.evidence.items)]
    if candidate.disposition is not None:
        matches = [packet for packet in matches if any(
            attempt.response_sha256 == candidate.decision_response_sha256
            and attempt.prompt_hash == candidate.decision_prompt_hash
            and candidate.disposition in attempt.dispositions for attempt in packet.disposition_attempts)]
    return [max(reversed(matches), key=lambda packet: packet.planned_at)] if matches else []


def _index_candidate(candidate: Candidate, progress: CandidateProgress, cache_dir: str | Path) -> None:
    from digest import candidate_storage as storage

    keys = []
    for packet in _proof_packets(candidate, progress.packets):
        storage.freeze_packet(packet, {}, cache_dir)
        keys.append(storage.packet_key(packet))
    storage.save_candidate(candidate, tuple(keys), cache_dir)


def _retire_indexed_work(progress: CandidateProgress, cache_dir: str | Path) -> None:
    """Verified index first, active removal last; excluded work is not completed work."""
    from digest import candidate_storage as storage

    retiring = []
    for identity, candidate in progress.candidates.items():
        proof = _proof_packets(candidate, progress.packets)
        unconsumed = any(_accepted_empty_packet(packet) and not packet.handed_to_preparation for packet in proof)
        if candidate.eligible and (candidate.status not in {"not_selected", "duplicate"} or unconsumed):
            continue
        _index_candidate(candidate, progress, cache_dir)
        retiring.append(identity)
    for identity in retiring:
        del progress.candidates[identity]
    required = {storage.packet_key(packet) for candidate in progress.candidates.values()
                for packet in _proof_packets(candidate, progress.packets)}
    retained = []
    for packet in progress.packets:
        if storage.packet_key(packet) in required:
            retained.append(packet)
        else:
            storage.freeze_packet(packet, {}, cache_dir)
    progress.packets = retained


def _accepted_empty_packet(packet: CandidatePacket) -> bool:
    if packet.report is None or not packet.report.reviews:
        return False
    review = _delivery_review(packet.report)
    capture = next((item for item in packet.disposition_attempts if item.slot == review.slot), None)
    return review.status == "abstained" and (
        not packet.disposition_attempts or capture is not None and capture.status == "complete")


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _active_candidate_body(candidate: Candidate) -> dict[str, Any]:
    raw = asdict(candidate)
    del raw["article"], raw["occurrences"]
    return {"candidate": raw, "article_ref": _digest(asdict(candidate.article)),
            "occurrence_refs": [_digest(asdict(article)) for article in candidate.occurrences]}


def _active_packet_body(packet: CandidatePacket, cache_dir: str | Path | None) -> dict[str, Any]:
    from digest.candidate_storage import packet_key, read_packet

    key = packet_key(packet)
    path = _safe(Path(cache_dir) / "candidate_reports" / f"{key}.json") if cache_dir is not None else None
    if path is not None and cache_dir is not None and path.exists():
        if read_packet(key, cache_dir) != replace(packet, handed_to_preparation=False):
            raise ValueError("Active candidate packet differs from its frozen evidence.")
        return {"packet_ref": key, "handed_to_preparation": packet.handed_to_preparation}
    raw = asdict(packet)
    del raw["articles"]
    raw["article_refs"] = [_digest(asdict(article)) for article in packet.articles]
    return {"packet": raw}


def _working_set(progress: CandidateProgress, cache_dir: str | Path | None) -> dict[str, Any]:
    _validate(progress)
    return {"kind": "candidate_working_set", "schema_version": 1,
            "candidates": {identity: _active_candidate_body(candidate)
                           for identity, candidate in progress.candidates.items()},
            "packets": [_active_packet_body(packet, cache_dir) for packet in progress.packets],
            "latest_collection_json": progress.latest_collection_json, "policy_sha256": progress.policy_sha256}


def _working_record(progress: CandidateProgress, cache_dir: str | Path | None) -> dict[str, Any]:
    body = _working_set(progress, cache_dir)
    return {"candidate_accounting": body, "sha256": _digest(body)}


def progress_size(progress: CandidateProgress, cache_dir: str | Path | None = None) -> int:
    """Measure only the current working checkpoint, never indexed historical bodies."""
    return len(json.dumps(_working_record(progress, cache_dir), indent=2).encode("utf-8"))


def save_candidate_progress(
    progress: CandidateProgress, cache_dir: str | Path = ".cache", *, retire: bool = True,
) -> Path:
    from digest.candidate_storage import encode_active_candidate, put_article, write_policy

    for candidate in progress.candidates.values():
        encode_active_candidate(candidate, cache_dir)
    for packet in progress.packets:
        for article in packet.articles:
            put_article(article, cache_dir)
    progress.latest_collection_json = _materialize_collection(progress.latest_collection_json, cache_dir)
    for packet in progress.packets:
        packet.collection_json = _materialize_collection(packet.collection_json, cache_dir)
    if retire:
        _retire_indexed_work(progress, cache_dir)
    record = _working_record(progress, cache_dir)
    if len(json.dumps(record, indent=2).encode("utf-8")) > MAX_BYTES:
        raise ValueError(f"Candidate progress exceeds {MAX_BYTES}-byte budget; no manifest was truncated.")
    path = _safe(Path(cache_dir) / CANDIDATE_FILE)
    _safe(path.with_suffix(path.suffix + ".tmp"))
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json_write(path, record)
    # The header cannot acknowledge a policy until reactivated active work is durable.
    if progress.policy_sha256:
        write_policy(progress.policy_sha256, cache_dir)
    return path


def _materialize_collection(value: str, cache_dir: str | Path) -> str:
    from digest.candidate_storage import put_article

    raw = json.loads(value)
    sources = {item["source"]: item["url"] for item in raw.get("sources", [])}
    for observation in raw.get("observations", []):
        article = observation.pop("article", None)
        if article is not None:
            saved = CandidateArticle(article["title"], article["link"], article["description"], article["source"],
                                     article["category"], article["pub_date"],
                                     observation.get("source_url") or sources[article["source"]])
            observation["article_reference"] = {"identity": observation["identity"],
                                                "occurrence_sha256": put_article(saved, cache_dir)}
        reference = observation["article_reference"]
        if (reference["identity"] != observation["identity"]
                or not re.fullmatch(r"[0-9a-f]{64}", reference["occurrence_sha256"])):
            raise ValueError("Collection accounting source identity mismatch.")
    return json.dumps(raw, ensure_ascii=False, sort_keys=True)


def _restore_active_packet(raw: Any, cache_dir: str | Path) -> CandidatePacket:
    from digest.candidate_storage import read_article, read_packet

    if not isinstance(raw, dict):
        raise ValueError("Invalid active candidate packet.")
    if set(raw) == {"packet_ref", "handed_to_preparation"}:
        if type(raw["handed_to_preparation"]) is not bool:
            raise ValueError("Invalid candidate preparation handoff flag.")
        return replace(read_packet(raw["packet_ref"], cache_dir),
                       handed_to_preparation=raw["handed_to_preparation"])
    if set(raw) != {"packet"} or not isinstance(raw["packet"], dict):
        raise ValueError("Invalid active candidate packet fields.")
    body = dict(raw["packet"])
    refs = body.pop("article_refs", None)
    if not isinstance(refs, list) or "articles" in body:
        raise ValueError("Invalid active candidate source references.")
    body["articles"] = [asdict(read_article(sha, cache_dir)) for sha in refs]
    packet: CandidatePacket = _restore(body, CandidatePacket)
    return packet


def load_candidate_progress(cache_dir: str | Path = ".cache") -> CandidateProgress:
    from digest.candidate_storage import decode_active_candidate

    path = _safe(Path(cache_dir) / CANDIDATE_FILE)
    if not path.exists():
        return CandidateProgress()
    if path.stat().st_size > MAX_BYTES:
        raise ValueError(f"Candidate progress exceeds {MAX_BYTES}-byte budget.")
    record = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
    if (not isinstance(record, dict) or set(record) != {"candidate_accounting", "sha256"}
            or record["sha256"] != _digest(record["candidate_accounting"])):
        raise ValueError("Candidate progress hash or envelope mismatch.")
    body = record["candidate_accounting"]
    if (not isinstance(body, dict) or set(body) != {"kind", "schema_version", "candidates", "packets",
                                                  "latest_collection_json", "policy_sha256"}
            or body["kind"] != "candidate_working_set" or type(body["schema_version"]) is not int
            or body["schema_version"] != 1 or not isinstance(body["candidates"], dict)
            or not isinstance(body["packets"], list) or not isinstance(body["latest_collection_json"], str)
            or not isinstance(body["policy_sha256"], str)):
        raise ValueError("Unsupported candidate prototype checkpoint; regenerate draft candidate accounting.")
    policy_sha = body["policy_sha256"]
    if policy_sha and (len(policy_sha) != 64 or any(char not in "0123456789abcdef" for char in policy_sha)):
        raise ValueError("Invalid active candidate policy hash.")
    progress = CandidateProgress(
        candidates={identity: decode_active_candidate(raw, cache_dir) for identity, raw in body["candidates"].items()},
        packets=[_restore_active_packet(raw, cache_dir) for raw in body["packets"]],
        latest_collection_json=body["latest_collection_json"], policy_sha256=policy_sha)
    _validate(progress)
    return progress


def _report_accounting_path(report: BlindReviewReport, cache_dir: str | Path) -> Path:
    return _safe(Path(cache_dir) / "candidate_reports" / f"{_digest(asdict(report))}.json")


def ensure_report_accounting(
    progress: CandidateProgress, report: BlindReviewReport, cache_dir: str | Path = ".cache",
) -> Path:
    """Freeze only this saved report's packet; current counts are bounded as-of data."""
    from digest.candidate_storage import freeze_packet

    packet = next((item for item in reversed(progress.packets) if item.report == report), None)
    if packet is None:
        raise ValueError("Candidate accounting cannot freeze an unrelated report.")
    instant = _instant(None)
    counts = {status: sum(candidate.status == status for candidate in progress.candidates.values())
              for status in ("selected", "not_selected", "duplicate", "not_selected_without_editorial_reason",
                             "not_presented", "technical_pending")}
    unfinished = [datetime.fromisoformat(candidate.first_observed_at) for candidate in progress.candidates.values()
                  if candidate.eligible and candidate.status in {"not_presented", "technical_pending"}]
    inventory = json.loads(packet.collection_json)
    serialized_bytes = progress_size(progress, cache_dir)
    delivery = _delivery_review(report)
    attempt = next((item for item in packet.disposition_attempts if item.slot == delivery.slot), None)
    packet_ids = {item.evidence_id for item in packet.evidence.items}
    summary = {
        "accounted_at": instant.isoformat(), "serialized_bytes": serialized_bytes,
        "remaining_capacity_bytes": MAX_BYTES - serialized_bytes, "capacity_bytes": MAX_BYTES,
        "capacity_scope": "current active working set; immutable source and packet objects are stored separately",
        "packet_response_coverage_complete": bool(attempt and not attempt.errors
            and {item.evidence_id for item in attempt.dispositions} == packet_ids),
        "packet_metadata_decisions_complete": bool(attempt and attempt.status == "complete"),
        "eligible_metadata_decisions_complete": all(
            item.disposition is not None and item.status in {"selected", "not_selected", "duplicate"}
            for item in progress.candidates.values() if item.eligible),
        "registered_identities": len(progress.candidates),
        "eligible_identities": sum(candidate.eligible for candidate in progress.candidates.values()),
        "statuses": counts,
        "oldest_eligible_unfinished_observed_at": min(unfinished).isoformat() if unfinished else None,
        "oldest_eligible_unfinished_age_hours":
            max(0, (instant - min(unfinished)).total_seconds() / 3600) if unfinished else None,
        "latest_observed_occurrences": len(inventory.get("observations", [])),
        "current_collection": inventory,
        "limits": "Model judgments over RSS metadata; not semantic correctness or full-source reading",
    }
    return freeze_packet(packet, summary, cache_dir)


def candidate_accounting_sources(
    report: BlindReviewReport, cache_dir: str | Path = ".cache",
) -> list[Path]:
    """Verify and enumerate exact source objects for accepted checkpoint hash refs."""
    from digest.candidate_storage import read_report_record

    if not _report_accounting_path(report, cache_dir).exists():
        return []
    record = read_report_record(_digest(asdict(report)), cache_dir)
    collection = json.loads(record["packet"]["collection_json"])
    keys = [*record["packet"]["article_refs"], *(item["article_reference"]["occurrence_sha256"]
             for item in collection.get("observations", []))]
    from digest.candidate_storage import read_article

    for key in keys:
        read_article(key, cache_dir)
    return [_safe(Path(cache_dir) / "candidate_sources" / f"{sha}.json")
            for sha in dict.fromkeys(keys)]


def archive_candidate_accounting(
    report: BlindReviewReport, archive: Path, cache_dir: str | Path = ".cache",
) -> Path | None:
    """Copy exact immutable packet accounting; legacy accepted reports need none."""
    from digest.candidate_storage import read_report_record

    frozen = _report_accounting_path(report, cache_dir)
    if not frozen.exists():
        return None
    payload = read_report_record(_digest(asdict(report)), cache_dir)
    path = _safe(Path(str(archive) + ".candidates.json"))
    _safe(path.with_suffix(path.suffix + ".tmp"))
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.stat().st_size > MAX_BYTES:
            raise ValueError("Existing candidate accounting archive exceeds its byte budget.")
        saved = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
        if saved != payload:
            raise ValueError("Existing candidate accounting archive differs from frozen report accounting.")
        return path
    atomic_json_write(path, payload)
    return path
