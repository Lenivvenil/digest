"""Optional same-response closing designation, without changing legacy review objects.

Feed approval and source-specific attribution review happen before activation.
This module validates provenance, not the editorial quality of a model judgment.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from digest._sanitize import sanitize_article
from digest.application.review_request import eligible_ids as eligible_ids
from digest.config import ClosingConfig
from digest.domain.catalog.articles import article_hash
from digest.domain.catalog.occurrences import SourceOccurrence, occurrence_sha256
from digest.domain.catalog.sources import SourceConfig
from digest.domain.editorial.attempts import ClosingAttempt as ClosingAttempt
from digest.domain.editorial.attempts import ResolvedReview, restore_review
from digest.domain.editorial.attempts import capture_closing as capture_closing
from digest.presentation.source_attribution import attribute_source_card as attribute_source_card
from digest.radar.summarizer import ArticleSummary

if TYPE_CHECKING:
    from collections.abc import Sequence

    from digest.domain.editorial.candidates import CandidatePacket
    from digest.domain.editorial.reviews import BlindReviewReport


@dataclass(frozen=True)
class ClosingOccurrence(SourceOccurrence):
    """Transitional named compatibility value while #147/#148 retire old boundaries."""


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


def main_attribution_occurrences(
    report: BlindReviewReport | None,
    cards: Sequence[ArticleSummary],
    sources: Sequence[SourceConfig],
    *,
    closing_snapshot: bool,
    cache_dir: str | Path = ".cache",
) -> dict[str, ClosingOccurrence]:
    """Compatibility boundary; general main attribution belongs to application source attribution."""
    from digest.application.source_attribution import main_attribution_occurrences as resolve_occurrences

    return {
        identity: ClosingOccurrence(**asdict(occurrence))
        for identity, occurrence in resolve_occurrences(
            report,
            cards,
            sources,
            closing_snapshot=closing_snapshot,
            cache_dir=cache_dir,
        ).items()
    }


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
        canonical.title,
        canonical.link,
        canonical.source,
        canonical.category,
    ):
        return None
    try:
        attributed, credited = attribute_source_card(card, provenance.occurrence, provenance.occurrence_sha256)
    except ValueError:
        return None
    return attributed if credited else None


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def decide_closing(
    result: ResolvedReview,
    packet: CandidatePacket,
    settings: ClosingConfig,
    sources: Sequence[SourceConfig],
) -> ClosingDecision:
    report, review = result.report, result.chosen.review
    attempt = result.chosen.closing
    if attempt is None:
        return ClosingDecision("incomplete", "missing_delivery_closing_capture")
    if attempt.status != "selected":
        return ClosingDecision(attempt.status, attempt.reason)
    if attempt.evidence_id not in eligible_ids(report.evidence, settings, sources):
        return ClosingDecision("unavailable", "source_not_eligible_for_closing")
    occurrences = [item for item in packet.articles if article_hash(item.title, item.link) == attempt.evidence_id]
    if len(occurrences) != 1 or packet.evidence != report.evidence:
        return ClosingDecision("incomplete", "closing_occurrence_not_bound")
    occurrence = ClosingOccurrence(**asdict(occurrences[0]))
    if not any(
        (binding.name, binding.url, binding.category) == (occurrence.source, occurrence.source_url, occurrence.category)
        for binding in settings.approved_sources
    ):
        return ClosingDecision("unavailable", "source_not_eligible_for_closing")
    selection = next(item for item in review.selections if item.evidence_id == attempt.evidence_id)
    evidence = next(item for item in report.evidence.items if item.evidence_id == attempt.evidence_id)
    decision = ClosingDecision(
        "selected",
        attempt.reason,
        ArticleSummary(occurrence.title, occurrence.link, occurrence.source, occurrence.category, selection.reason),
        ClosingProvenance(
            attempt.contract_version,
            _digest(asdict(report)),
            review.slot,
            review.provider,
            review.model,
            report.evidence.bundle_id,
            review.prompt_hash,
            review.response_sha256 or "",
            evidence.evidence_id,
            _digest(asdict(evidence)),
            occurrence_sha256(occurrence),
            occurrence,
        ),
    )
    validate_closing(decision, report)
    return decision


def validate_closing(decision: ClosingDecision, report: BlindReviewReport | None) -> None:
    """Revalidate persisted binding, never consult current source or provider settings."""
    if (
        decision.status not in {"selected", "unavailable", "incomplete"}
        or not decision.reason
        or len(decision.reason) > 200
    ):
        raise ValueError("Invalid terminal closing decision.")
    if decision.status != "selected":
        if decision.card is not None or decision.provenance is not None:
            raise ValueError("Omitted closing decision cannot contain a card.")
        return
    card, provenance = decision.card, decision.provenance
    if report is None or card is None or provenance is None:
        raise ValueError("Selected closing decision requires report, card and provenance.")
    review = restore_review(report).chosen.review
    if (
        type(provenance.contract_version) is not int
        or provenance.contract_version not in {1, 2}
        or provenance.report_sha256 != _digest(asdict(report))
        or (
            provenance.slot,
            provenance.provider,
            provenance.model,
            provenance.bundle_id,
            provenance.prompt_hash,
            provenance.response_sha256,
        )
        != (
            review.slot,
            review.provider,
            review.model,
            report.evidence.bundle_id,
            review.prompt_hash,
            review.response_sha256,
        )
        or review.status not in {"ok", "partial"}
        or not provenance.response_sha256
    ):
        raise ValueError("Closing delivery review binding mismatch.")
    if provenance.evidence_id in {item.evidence_id for item in review.rejected_items}:
        raise ValueError("Conflicting selected identity cannot supply a closing story.")
    evidence = next((item for item in report.evidence.items if item.evidence_id == provenance.evidence_id), None)
    selection = next((item for item in review.selections if item.evidence_id == provenance.evidence_id), None)
    occurrence = provenance.occurrence
    title, excerpt, source = sanitize_article(occurrence.title, occurrence.description, occurrence.source)
    if (
        evidence is None
        or selection is None
        or provenance.evidence_sha256 != _digest(asdict(evidence))
        or provenance.occurrence_sha256 != occurrence_sha256(occurrence)
        or article_hash(occurrence.title, occurrence.link) != provenance.evidence_id
        or (evidence.title, evidence.url, evidence.source, evidence.category, evidence.published)
        != (title, occurrence.link, source, occurrence.category[:200], occurrence.published)
        or not excerpt.startswith(evidence.excerpt)
        or not occurrence.source_url
        or card
        != ArticleSummary(occurrence.title, occurrence.link, occurrence.source, occurrence.category, selection.reason)
    ):
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
        if (
            not isinstance(record, dict)
            or set(record) != {"schema_version", "report_sha256", "decision", "sha256"}
            or type(record["schema_version"]) is not int
            or record["schema_version"] != 1
            or record["report_sha256"] != identity
            or record["sha256"] != _digest({key: value for key, value in record.items() if key != "sha256"})
        ):
            raise ValueError("Invalid closing capture envelope.")
        decision: ClosingDecision = _restore(record["decision"], ClosingDecision)
        validate_closing(decision, report)
        return decision
    except (OSError, ValueError, TypeError, KeyError, UnicodeError):
        return ClosingDecision("incomplete", "invalid_delivery_closing_capture")
