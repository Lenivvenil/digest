"""Optional same-response closing designation, without changing legacy review objects.

Feed approval and source-specific attribution review happen before activation.
This module validates provenance, not the editorial quality of a model judgment.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from digest._sanitize import sanitize_article
from digest._serialization import extract_json as _extract_json
from digest.config import ClosingConfig, SourceConfig
from digest.radar.collector import article_hash
from digest.radar.summarizer import ArticleSummary

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from digest.candidate_review import CandidatePacket
    from digest.review import BlindReviewReport, EvidenceBundle, ModelReview


@dataclass(frozen=True)
class ClosingOccurrence:
    title: str
    link: str
    description: str
    source: str
    category: str
    published: str | None
    source_url: str


@dataclass(frozen=True)
class ClosingProvenance:
    contract_version: int
    report_sha256: str
    slot: str
    provider: str
    model: str
    bundle_id: str
    prompt_hash: str
    response_sha256: str
    evidence_id: str
    evidence_sha256: str
    occurrence_sha256: str
    occurrence: ClosingOccurrence


@dataclass(frozen=True)
class ClosingDecision:
    status: Literal["selected", "unavailable", "incomplete"]
    reason: str
    card: ArticleSummary | None = None
    provenance: ClosingProvenance | None = None


# Reviewed presentation support, not source activation or a licence classifier.
# Unknown feed bindings need explicit attribution review before closing can render.
_CLOSING_SOURCE_CREDITS = {
    "https://www.england.nhs.uk/feed/": (
        "NHS England RSS feeds. Open Government Licence v3.0: "
        "https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/"
    ),
    "https://www.gov.uk/search/news-and-communications.atom?organisations%5B%5D=environment-agency": (
        "Contains public sector information licensed under the Open Government Licence v3.0. "
        "https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/"
    ),
}


def attribute_source_card(
    card: ArticleSummary, occurrence: ClosingOccurrence, occurrence_sha256: str,
) -> tuple[ArticleSummary, bool]:
    """Credit a supported exact feed on presentation only; validate frozen identity."""
    if ((card.title, card.link, card.source, card.category) != (
            occurrence.title, occurrence.link, occurrence.source, occurrence.category)
            or occurrence_sha256 != _digest(asdict(occurrence))):
        raise ValueError("Source attribution differs from the frozen article occurrence.")
    credit = _CLOSING_SOURCE_CREDITS.get(occurrence.source_url)
    if credit is None:
        return card, False  # Other ordinary source presentation is unchanged.
    return replace(card, summary=f"{card.summary} {credit}"), True


def main_attribution_occurrences(
    report: BlindReviewReport | None, cards: Sequence[ArticleSummary], sources: Sequence[SourceConfig],
    *, closing_snapshot: bool, cache_dir: str | Path = ".cache",
) -> dict[str, ClosingOccurrence]:
    """Resolve main credits before calls; legacy recovery remains explicit."""
    from digest.adapters.storage.candidate_objects import read_packet
    from digest.adapters.storage.checkpoints import safe_checkpoint_path as _safe

    required = closing_snapshot or any(source.enabled and source.url in _CLOSING_SOURCE_CREDITS
                                      for source in sources)
    try:
        report_sha = _digest(asdict(report)) if report is not None else None
        path = _safe(Path(cache_dir) / "candidate_reports" / f"{report_sha}.json") if report_sha else None
        if path is None or not path.exists():
            if required:
                raise ValueError(
                    "Source attribution needs the accepted report's immutable candidate packet. Restore "
                    f"{path or 'the bound review report'} and its referenced candidate_sources objects before "
                    "resuming preparation; do not rerun selection. Reconcile unfrozen legacy preparation "
                    "before enabling these feeds."
                )
            return {}  # Accepted legacy reports without supported feeds retain their old behavior.
        assert report_sha is not None
        packet = read_packet(report_sha, cache_dir)
        if packet.report != report:
            raise ValueError("Source attribution packet differs from the accepted review report.")
        occurrences = {}
        for card in cards:
            identity = article_hash(card.title, card.link)
            matches = [item for item in packet.articles if article_hash(item.title, item.link) == identity]
            if len(matches) != 1:
                raise ValueError("Main article has no unique immutable source occurrence for attribution.")
            source = matches[0]
            if source.source_url in _CLOSING_SOURCE_CREDITS:
                occurrence = ClosingOccurrence(**asdict(source))
                attribute_source_card(card, occurrence, _digest(asdict(occurrence)))  # Validate before any model call.
                occurrences[identity] = occurrence
        return occurrences
    except (OSError, ValueError) as exc:
        if required:
            raise
        logger.warning("Legacy attribution audit unavailable; retaining accepted presentation without "
                       "inferring source credit: %s", exc)
        return {}


def attribute_closing_card(decision: ClosingDecision, card: ArticleSummary) -> ArticleSummary | None:
    """Use reviewed credit for closing; unknown bindings omit the optional card.

    Source approval and item-specific rights remain separate activation gates.
    This does not alter canonical evidence, translate legal credit or infer a
    licence from an article hostname.
    """
    canonical, provenance = decision.card, decision.provenance
    if decision.status != "selected" or canonical is None or provenance is None:
        return None
    if (card.title, card.link, card.source, card.category) != (
        canonical.title, canonical.link, canonical.source, canonical.category,
    ):
        return None
    try:
        attributed, credited = attribute_source_card(card, provenance.occurrence, provenance.occurrence_sha256)
    except ValueError:
        return None
    return attributed if credited else None


@dataclass(frozen=True)
class ClosingAttempt:
    slot: str
    provider: str
    model: str
    bundle_id: str
    prompt_hash: str
    response_sha256: str | None
    status: Literal["selected", "unavailable", "incomplete"]
    reason: str
    evidence_id: str | None = None


@dataclass
class ClosingCapture:
    attempts: list[ClosingAttempt] = field(default_factory=list)


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def eligible_ids(
    bundle: EvidenceBundle, settings: ClosingConfig, sources: Sequence[SourceConfig],
) -> list[str]:
    """A sanitized name collision grants no eligible binding."""
    eligible = []
    for item in bundle.items:
        matches = [source for source in sources
                   if sanitize_article("", "", source.name)[2] == item.source
                   and source.category[:200] == item.category]
        if len(matches) != 1 or not matches[0].enabled:
            continue
        source = matches[0]
        if any((binding.name, binding.url, binding.category) == (source.name, source.url, source.category)
               for binding in settings.approved_sources):
            eligible.append(item.evidence_id)
    return eligible


def _unique_designation(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Conflicting JSON fields cannot designate a closing item.")
        result[key] = value
    return result


def capture_closing(review: ModelReview, text: str | None, finish_reason: str | None) -> ClosingAttempt:
    status: Literal["selected", "unavailable", "incomplete"] = "incomplete"
    reason, identity = "missing_or_invalid_closing_designation", None
    if (text is not None and review.status in {"ok", "partial", "abstained"}
            and hashlib.sha256(text.encode()).hexdigest() == review.response_sha256
            and finish_reason in {None, "stop", "STOP", "end_turn"}):
        try:
            raw = _extract_json(text)
            unique, _ = json.JSONDecoder(object_pairs_hook=_unique_designation).raw_decode(text[text.index("{"):])
            if raw != unique:
                raise ValueError("Closing designation differs from the accepted review envelope.")
            closing = raw.get("closing") if isinstance(raw, dict) else None
            if (isinstance(closing, dict) and set(closing) == {"schema_version", "evidence_id"}
                    and type(closing["schema_version"]) is int and closing["schema_version"] == 1):
                proposed = closing["evidence_id"]
                if proposed is None:
                    status, reason = "unavailable", "no_suitable_item_in_packet"
                elif (isinstance(proposed, str) and proposed in {item.evidence_id for item in review.selections}
                      and proposed not in {item.evidence_id for item in review.rejected_items}):
                    status, reason, identity = "selected", "same_response_designation", proposed
        except (ValueError, TypeError, KeyError):
            pass
    return ClosingAttempt(review.slot, review.provider, review.model, review.bundle_id, review.prompt_hash,
                          review.response_sha256, status, reason, identity)


def decide_closing(
    report: BlindReviewReport, packet: CandidatePacket, capture: ClosingCapture,
    settings: ClosingConfig, sources: Sequence[SourceConfig],
) -> ClosingDecision:
    from digest.review import _delivery_review

    review = _delivery_review(report)
    matches = [attempt for attempt in capture.attempts if attempt.slot == review.slot]
    if len(matches) != 1:
        return ClosingDecision("incomplete", "missing_delivery_closing_capture")
    attempt = matches[0]
    if (attempt.provider, attempt.model, attempt.bundle_id, attempt.prompt_hash, attempt.response_sha256) != (
            review.provider, review.model, report.evidence.bundle_id, review.prompt_hash, review.response_sha256):
        return ClosingDecision("incomplete", "closing_capture_binding_mismatch")
    if attempt.status != "selected":
        return ClosingDecision(attempt.status, attempt.reason)
    if attempt.evidence_id not in eligible_ids(report.evidence, settings, sources):
        return ClosingDecision("unavailable", "source_not_eligible_for_closing")
    occurrences = [item for item in packet.articles if article_hash(item.title, item.link) == attempt.evidence_id]
    if len(occurrences) != 1 or packet.evidence != report.evidence:
        return ClosingDecision("incomplete", "closing_occurrence_not_bound")
    occurrence = ClosingOccurrence(**asdict(occurrences[0]))
    if not any((binding.name, binding.url, binding.category) == (
            occurrence.source, occurrence.source_url, occurrence.category) for binding in settings.approved_sources):
        return ClosingDecision("unavailable", "source_not_eligible_for_closing")
    selection = next(item for item in review.selections if item.evidence_id == attempt.evidence_id)
    evidence = next(item for item in report.evidence.items if item.evidence_id == attempt.evidence_id)
    decision = ClosingDecision(
        "selected", "same_response_designation",
        ArticleSummary(occurrence.title, occurrence.link, occurrence.source, occurrence.category, selection.reason),
        ClosingProvenance(1, _digest(asdict(report)), review.slot, review.provider, review.model,
                          report.evidence.bundle_id, review.prompt_hash, review.response_sha256 or "",
                          evidence.evidence_id, _digest(asdict(evidence)), _digest(asdict(occurrence)), occurrence),
    )
    validate_closing(decision, report)
    return decision


def validate_closing(decision: ClosingDecision, report: BlindReviewReport | None) -> None:
    """Revalidate persisted binding, never consult current source or provider settings."""
    from digest.review import _delivery_review, _validated_cached_selections

    if (decision.status not in {"selected", "unavailable", "incomplete"}
            or not decision.reason or len(decision.reason) > 200):
        raise ValueError("Invalid terminal closing decision.")
    if decision.status != "selected":
        if decision.card is not None or decision.provenance is not None:
            raise ValueError("Omitted closing decision cannot contain a card.")
        return
    card, provenance = decision.card, decision.provenance
    if report is None or card is None or provenance is None:
        raise ValueError("Selected closing decision requires report, card and provenance.")
    review = _delivery_review(report)
    if (provenance.contract_version != 1 or provenance.report_sha256 != _digest(asdict(report))
            or (provenance.slot, provenance.provider, provenance.model, provenance.bundle_id,
                provenance.prompt_hash, provenance.response_sha256) != (
                    review.slot, review.provider, review.model, report.evidence.bundle_id,
                    review.prompt_hash, review.response_sha256)
            or review.status not in {"ok", "partial"} or not provenance.response_sha256):
        raise ValueError("Closing delivery review binding mismatch.")
    _validated_cached_selections(review, report.evidence)
    if provenance.evidence_id in {item.evidence_id for item in review.rejected_items}:
        raise ValueError("Conflicting selected identity cannot supply a closing story.")
    evidence = next((item for item in report.evidence.items if item.evidence_id == provenance.evidence_id), None)
    selection = next((item for item in review.selections if item.evidence_id == provenance.evidence_id), None)
    occurrence = provenance.occurrence
    title, excerpt, source = sanitize_article(occurrence.title, occurrence.description, occurrence.source)
    if (evidence is None or selection is None or provenance.evidence_sha256 != _digest(asdict(evidence))
            or provenance.occurrence_sha256 != _digest(asdict(occurrence))
            or article_hash(occurrence.title, occurrence.link) != provenance.evidence_id
            or (evidence.title, evidence.url, evidence.source, evidence.category, evidence.published) != (
                title, occurrence.link, source, occurrence.category[:200], occurrence.published)
            or not excerpt.startswith(evidence.excerpt) or not occurrence.source_url
            or card != ArticleSummary(occurrence.title, occurrence.link, occurrence.source,
                                      occurrence.category, selection.reason)):
        raise ValueError("Closing evidence, occurrence or card binding mismatch.")


def save_closing(decision: ClosingDecision, report: BlindReviewReport, cache_dir: str | Path) -> None:
    """Called after completed report persistence; no overwrite of a terminal sidecar."""
    from digest._util import atomic_json_write
    from digest.adapters.storage.checkpoints import safe_checkpoint_path as _safe

    validate_closing(decision, report)
    path = _safe(Path(cache_dir) / "closing_decisions" / f"{_digest(asdict(report))}.json")
    if path.exists():
        return
    body = {"schema_version": 1, "report_sha256": _digest(asdict(report)), "decision": asdict(decision)}
    record = {**body, "sha256": _digest(body)}
    if len(json.dumps(record, indent=2).encode()) > 100_000:
        raise ValueError("Closing capture exceeds its byte budget.")
    path.parent.mkdir(parents=True, exist_ok=True)
    _safe(path.with_suffix(path.suffix + ".tmp"))
    atomic_json_write(path, record)


def load_closing(report: BlindReviewReport, cache_dir: str | Path) -> ClosingDecision:
    """Absent/corrupt optional capture omits only closing; no selection retry."""
    from digest._serialization import restore_dataclass as _restore
    from digest._serialization import unique_object as _unique_object
    from digest.adapters.storage.checkpoints import safe_checkpoint_path as _safe

    try:
        identity = _digest(asdict(report))
        path = _safe(Path(cache_dir) / "closing_decisions" / f"{identity}.json")
        if not path.exists():
            return ClosingDecision("incomplete", "missing_delivery_closing_capture")
        if path.stat().st_size > 100_000:
            raise ValueError("Closing capture exceeds its byte budget.")
        record = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
        if (not isinstance(record, dict) or set(record) != {"schema_version", "report_sha256", "decision", "sha256"}
                or type(record["schema_version"]) is not int or record["schema_version"] != 1
                or record["report_sha256"] != identity
                or record["sha256"] != _digest({key: value for key, value in record.items() if key != "sha256"})):
            raise ValueError("Invalid closing capture envelope.")
        decision: ClosingDecision = _restore(record["decision"], ClosingDecision)
        validate_closing(decision, report)
        return decision
    except (OSError, ValueError, TypeError, KeyError, UnicodeError):
        return ClosingDecision("incomplete", "invalid_delivery_closing_capture")
