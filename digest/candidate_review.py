"""Recoverable RSS candidate progress, separate from accepted preparation.

Registration, planned requests and validated selections are distinct facts. This
module makes no model calls and imposes no lifetime beyond current eligibility.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from digest._util import atomic_json_write
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
# Measured 54 feeds x 200 parsed entries x 500-character excerpts write about
# 16 MB after deduplicating metadata. This is candidate-only transport capacity;
# accepted preparation keeps its independent 4 MB bound. History can still fill
# this bounded file; no truncation, TTL or automatic pruning is implied.
MAX_BYTES = 32_000_000
CandidateStatus = Literal["selected", "not_selected_without_editorial_reason", "not_presented", "technical_pending"]


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


@dataclass
class CandidateProgress:
    schema_version: int = 1
    candidates: dict[str, Candidate] = field(default_factory=dict)
    packets: list[CandidatePacket] = field(default_factory=list)
    latest_collection_json: str = "{}"


def packet_articles(packet: CandidatePacket) -> dict[str, list[Article]]:
    grouped: dict[str, list[Article]] = {}
    for saved in packet.articles:
        grouped.setdefault(saved.category, []).append(saved.article())
    return grouped


def merge_candidates(
    progress: CandidateProgress, articles_by_category: dict[str, list[Article]], config: Config,
    delivered_cache: dict[str, str], effective_priorities: dict[str, int] | None = None,
    now: datetime | None = None, *, inventory: CollectionInventory | None = None,
    delivery_history: dict[str, str] | None = None,
) -> CandidateProgress:
    """Merge observations; recheck old work without changing original timestamps.

    ``delivered_cache`` must be the pre-collection cache, never collect's mutated
    allocation cache. ``delivery_history`` may contain unpruned cache records;
    their timestamp is cache evidence, not independent Telegram confirmation.
    Undated absent entries retain unknown publication age.
    """
    instant = _instant(now)
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
        if identity not in progress.candidates:
            progress.candidates[identity] = Candidate(identity, saved, instant.isoformat(), source.priority)
        candidate = progress.candidates[identity]
        if saved != candidate.article and saved not in candidate.occurrences:
            candidate.occurrences = (*candidate.occurrences, saved)

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
        if (candidate.status == "selected" and exclusion(candidate.article, identity)
                and any(not exclusion(saved, identity) for saved in candidate.occurrences)):
            candidate.status = "technical_pending"
        if candidate.status in {"not_presented", "technical_pending"}:
            occurrences = (candidate.article, *candidate.occurrences)
            chosen = next((saved for saved in occurrences if not exclusion(saved, identity)), candidate.article)
            candidate.occurrences = tuple(saved for saved in occurrences if saved != chosen)
            candidate.article = chosen
        reason = exclusion(candidate.article, identity)
        candidate.eligible = not reason
        candidate.eligibility_reason = reason
        source = sources.get(candidate.article.source)
        if source is not None:
            candidate.priority = (effective_priorities or {}).get(source.name, source.priority)
    if inventory is not None:
        audit = asdict(inventory)
        for raw, observation in zip(audit["observations"], inventory.observations, strict=True):
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
    validate_evidence_bundle(packet.evidence, config)
    packet.prompt_hash = hashlib.sha256(json.dumps(build_review_messages(
        packet.evidence, config.review, config.radar.language), sort_keys=True).encode()).hexdigest()
    packet.max_selections = config.review.max_selections
    return packet


def begin_packet(progress: CandidateProgress, packet: CandidatePacket, cache_dir: str | Path = ".cache") -> Path:
    """Persist a planned attempt; this is not evidence that dispatch occurred."""
    if packet.report is not None:
        raise ValueError("Cannot begin an already completed candidate packet.")
    progress.packets.append(packet)
    for item in packet.evidence.items:
        progress.candidates[item.evidence_id].status = "technical_pending"
    return save_candidate_progress(progress, cache_dir)


def reconcile_packet(
    progress: CandidateProgress, packet: CandidatePacket, report: BlindReviewReport, config: Config,
    cache_dir: str | Path = ".cache",
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
    for item in packet.evidence.items:
        candidate = progress.candidates[item.evidence_id]
        if item.evidence_id in selected:
            candidate.status = "selected"
        elif valid and item.evidence_id not in rejected:
            candidate.status = "not_selected_without_editorial_reason"
        else:
            candidate.status = "technical_pending"
    packet.report = report
    path = save_candidate_progress(progress, cache_dir)
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
            if successful.status == "abstained" and not packet.handed_to_preparation:
                return packet.report
            continue
        eligible = [item for item in selections if progress.candidates[item.evidence_id].eligible
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
    for packet in progress.packets:
        if len(packet.prompt_hash) != 64 or packet.max_selections < 1:
            raise ValueError("Invalid frozen candidate prompt contract.")
        if datetime.fromisoformat(packet.planned_at).tzinfo is None:
            raise ValueError("Candidate attempt requires a timezone.")
        known = {article_hash(article.title, article.link): article for article in packet.articles}
        if len(known) != len(packet.articles) or set(known) != {item.evidence_id for item in packet.evidence.items}:
            raise ValueError("Candidate packet membership mismatch.")
        if any(identity not in progress.candidates or article not in (
                progress.candidates[identity].article, *progress.candidates[identity].occurrences)
               for identity, article in known.items()):
            raise ValueError("Candidate packet source metadata changed.")
        _validate_report(packet.report or BlindReviewReport(1, packet.evidence, [], "incomplete", None, [], ""))
        if packet.report is not None:
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


def progress_size(progress: CandidateProgress) -> int:
    """Return exact mutable-checkpoint JSON bytes under the atomic writer format."""
    body = candidate_accounting(progress)
    record = {"candidate_accounting": body, "sha256": hashlib.sha256(_canonical(body)).hexdigest()}
    return len(json.dumps(record, indent=2).encode("utf-8"))


def save_candidate_progress(progress: CandidateProgress, cache_dir: str | Path = ".cache") -> Path:
    body = candidate_accounting(progress)
    record = {"candidate_accounting": body, "sha256": hashlib.sha256(_canonical(body)).hexdigest()}
    if len(json.dumps(record, indent=2).encode("utf-8")) > MAX_BYTES:
        raise ValueError(f"Candidate progress exceeds {MAX_BYTES}-byte budget; no manifest was truncated.")
    path = _safe(Path(cache_dir) / CANDIDATE_FILE)
    _safe(path.with_suffix(path.suffix + ".tmp"))
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json_write(path, record)
    return path


def load_candidate_progress(cache_dir: str | Path = ".cache") -> CandidateProgress:
    path = _safe(Path(cache_dir) / CANDIDATE_FILE)
    if not path.exists():
        return CandidateProgress()
    if path.stat().st_size > MAX_BYTES:
        raise ValueError(f"Candidate progress exceeds {MAX_BYTES}-byte budget.")
    record = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
    if (not isinstance(record, dict) or set(record) != {"candidate_accounting", "sha256"}
            or record["sha256"] != hashlib.sha256(_canonical(record["candidate_accounting"])).hexdigest()):
        raise ValueError("Candidate progress hash or envelope mismatch.")
    progress: CandidateProgress = _restore(record["candidate_accounting"], CandidateProgress)
    _validate(progress)
    return progress


def _report_accounting_path(report: BlindReviewReport, cache_dir: str | Path) -> Path:
    digest = hashlib.sha256(_canonical(asdict(report))).hexdigest()
    return _safe(Path(cache_dir) / "candidate_reports" / f"{digest}.json")


def _read_report_accounting(path: Path, report: BlindReviewReport) -> dict[str, Any]:
    if path.stat().st_size > MAX_BYTES:
        raise ValueError(f"Candidate accounting archive exceeds {MAX_BYTES}-byte budget.")
    record = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
    if (not isinstance(record, dict) or type(record.get("schema_version")) is not int
            or record["schema_version"] != 1):
        raise ValueError("Invalid frozen candidate accounting envelope or schema version.")
    body = {key: value for key, value in record.items() if key != "sha256"}
    if (record.get("sha256") != hashlib.sha256(_canonical(body)).hexdigest()
            or record.get("report_sha256") != hashlib.sha256(_canonical(asdict(report))).hexdigest()
            or record.get("report_bundle_id") != report.evidence.bundle_id):
        raise ValueError("Frozen candidate accounting hash or report binding mismatch.")
    progress: CandidateProgress = _restore(record.get("candidate_accounting"), CandidateProgress)
    _validate(progress)
    if not any(packet.report == report for packet in progress.packets):
        raise ValueError("Frozen candidate accounting has no matching report.")
    return record


def ensure_report_accounting(
    progress: CandidateProgress, report: BlindReviewReport, cache_dir: str | Path = ".cache",
) -> Path:
    """Idempotently freeze saved report accounting, including after interrupted writes."""
    if not any(packet.report == report for packet in progress.packets):
        raise ValueError("Candidate accounting cannot freeze an unrelated report.")
    path = _report_accounting_path(report, cache_dir)
    if path.exists():
        _read_report_accounting(path, report)
        return path
    instant = _instant(None)
    counts = {status: sum(candidate.status == status for candidate in progress.candidates.values())
              for status in ("selected", "not_selected_without_editorial_reason", "not_presented", "technical_pending")}
    unfinished = [datetime.fromisoformat(candidate.first_observed_at) for candidate in progress.candidates.values()
                  if candidate.eligible and candidate.status in {"not_presented", "technical_pending"}]
    inventory = json.loads(progress.latest_collection_json)
    serialized_bytes = progress_size(progress)
    payload = {
        "schema_version": 1,
        "report_bundle_id": report.evidence.bundle_id,
        "report_sha256": hashlib.sha256(_canonical(asdict(report))).hexdigest(),
        "accounted_at": instant.isoformat(),
        "summary": {
            "serialized_bytes": serialized_bytes,
            "remaining_capacity_bytes": MAX_BYTES - serialized_bytes,
            "capacity_bytes": MAX_BYTES,
            "capacity_scope": "mutable candidate progress checkpoint; retained history also consumes capacity",
            "registered_identities": len(progress.candidates),
            "eligible_identities": sum(candidate.eligible for candidate in progress.candidates.values()),
            "statuses": counts,
            "oldest_eligible_unfinished_observed_at": min(unfinished).isoformat() if unfinished else None,
            "oldest_eligible_unfinished_age_hours":
                max(0, (instant - min(unfinished)).total_seconds() / 3600) if unfinished else None,
            "latest_observed_occurrences": len(inventory.get("observations", [])),
            "limits": "RSS metadata exposure only; no editorial rejection or full-source reading established",
        },
        "candidate_accounting": candidate_accounting(progress),
    }
    payload["sha256"] = hashlib.sha256(_canonical(payload)).hexdigest()
    if len(json.dumps(payload, indent=2).encode("utf-8")) > MAX_BYTES:
        raise ValueError(f"Candidate accounting archive exceeds {MAX_BYTES}-byte budget.")
    _safe(path.with_suffix(path.suffix + ".tmp"))
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json_write(path, payload)
    return path


def archive_candidate_accounting(
    report: BlindReviewReport, archive: Path, cache_dir: str | Path = ".cache",
) -> Path | None:
    """Copy frozen exact-report accounting; unrelated mutable progress is irrelevant.

    An absent report-bound record is a legacy report. Corruption of this exact
    accepted report's record fails closed without consulting current settings.
    """
    frozen = _report_accounting_path(report, cache_dir)
    if not frozen.exists():
        return None
    payload = _read_report_accounting(frozen, report)
    path = _safe(Path(str(archive) + ".candidates.json"))
    _safe(path.with_suffix(path.suffix + ".tmp"))
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if _read_report_accounting(path, report) != payload:
            raise ValueError("Existing candidate accounting archive differs from frozen report accounting.")
        return path
    atomic_json_write(path, payload)
    return path
