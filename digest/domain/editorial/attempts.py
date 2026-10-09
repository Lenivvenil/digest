"""Response-owned review evidence and one delivery selection policy.

Reports and packets keep their existing wire formats. Only their historical
adapter reconstructs response ownership from separately persisted records.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from typing import Literal

from digest._serialization import extract_json as _extract_json
from digest.domain.editorial.dispositions import CandidateDispositionAttempt, validate_disposition_attempt
from digest.domain.editorial.reviews import (
    BlindReviewReport,
    EvidenceBundle,
    EvidenceSelection,
    ModelReview,
    _parse_live_selection,
    validated_cached_selections,
)


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
    contract_version: int = 1
    rejected_evidence_ids: tuple[str, ...] = ()


def _unique_designation(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Conflicting JSON fields cannot designate a closing item.")
        result[key] = value
    return result


def capture_closing(review: ModelReview, text: str | None, finish_reason: str | None) -> ClosingAttempt:
    """Historical v1 designation; fresh enabled requests explicitly use v2 below."""
    status: Literal["selected", "unavailable", "incomplete"] = "incomplete"
    reason, identity = "missing_or_invalid_closing_designation", None
    if (
        text is not None
        and review.status in {"ok", "partial", "abstained"}
        and hashlib.sha256(text.encode()).hexdigest() == review.response_sha256
        and finish_reason in {None, "stop", "STOP", "end_turn"}
    ):
        try:
            raw = _extract_json(text)
            unique, _ = json.JSONDecoder(object_pairs_hook=_unique_designation).raw_decode(text[text.index("{") :])
            if raw != unique:
                raise ValueError("Closing designation differs from the accepted review envelope.")
            closing = raw.get("closing") if isinstance(raw, dict) else None
            if (
                isinstance(closing, dict)
                and set(closing) == {"schema_version", "evidence_id"}
                and type(closing["schema_version"]) is int
                and closing["schema_version"] == 1
            ):
                proposed = closing["evidence_id"]
                if proposed is None:
                    status, reason = "unavailable", "no_suitable_item_in_packet"
                elif (
                    isinstance(proposed, str)
                    and proposed in {item.evidence_id for item in review.selections}
                    and proposed not in {item.evidence_id for item in review.rejected_items}
                ):
                    status, reason, identity = "selected", "same_response_designation", proposed
        except (ValueError, TypeError, KeyError):
            pass
    return ClosingAttempt(
        review.slot,
        review.provider,
        review.model,
        review.bundle_id,
        review.prompt_hash,
        review.response_sha256,
        status,
        reason,
        identity,
    )


def capture_closing_selection(
    review: ModelReview,
    text: str | None,
    finish_reason: str | None,
    bundle: EvidenceBundle,
    *,
    eligible: set[str],
    max_detailed_selections: int,
) -> tuple[ClosingAttempt, EvidenceSelection | None]:
    """Validate the fresh v2 optional card without changing the main review outcome.

    Rejected optional identities are transient accounting evidence, never main
    rejections. Main raw entries consume detail capacity even when rejected.
    """
    status: Literal["selected", "unavailable", "incomplete"] = "incomplete"
    reason = "missing_or_invalid_closing_selection"
    selection = None
    rejected_ids: tuple[str, ...] = ()
    if (
        text is not None
        and review.status in {"ok", "partial", "abstained"}
        and hashlib.sha256(text.encode()).hexdigest() == review.response_sha256
        and finish_reason in {None, "stop", "STOP", "end_turn"}
    ):
        try:
            raw = _extract_json(text)
            closing = raw.get("closing") if isinstance(raw, dict) else None
            proposed = closing.get("selection") if isinstance(closing, dict) else None
            identity = proposed.get("evidence_id") if isinstance(proposed, dict) else None
            wrapper_identity = closing.get("evidence_id") if isinstance(closing, dict) else None
            known = {item.evidence_id: item for item in bundle.items}
            main_ids = {item.evidence_id for item in review.selections}
            rejected_main = {item.evidence_id for item in review.rejected_items}
            # Invalid old/mixed wrappers can still propose identities. These
            # block terminal accounting only; they never supply a v2 card.
            proposed_ids = {
                value for value in (identity, wrapper_identity) if isinstance(value, str) and value in known
            }
            explicit_null = (
                isinstance(closing, dict)
                and set(closing) == {"schema_version", "selection"}
                and type(closing["schema_version"]) is int
                and closing["schema_version"] == 2
                and proposed is None
            )
            untrusted_identity = (
                proposed is not None
                and (not isinstance(identity, str) or identity not in known)
                or wrapper_identity is not None
                and (not isinstance(wrapper_identity, str) or wrapper_identity not in known)
            )
            # An unreadable proposal cannot safely identify which residual
            # candidate it concerns. Only the exact null contract asserts that
            # no closing item was proposed.
            rejected_ids = tuple(
                item.evidence_id
                for item in bundle.items
                if (item.evidence_id in proposed_ids or untrusted_identity or not proposed_ids and not explicit_null)
                and item.evidence_id not in main_ids - rejected_main
            )
            try:
                unique, _ = json.JSONDecoder(object_pairs_hook=_unique_designation).raw_decode(text[text.index("{") :])
            except ValueError:
                # A last-wins decode cannot identify every conflicting optional
                # proposal. Preserve main cards, but do not terminally dismiss
                # any remaining candidate using this ambiguous envelope.
                rejected_ids = tuple(
                    item.evidence_id for item in bundle.items if item.evidence_id not in main_ids - rejected_main
                )
                raise
            if raw != unique:
                raise ValueError("Closing selection differs from the accepted review envelope.")
            if (
                not isinstance(closing, dict)
                or set(closing) != {"schema_version", "selection"}
                or type(closing["schema_version"]) is not int
                or closing["schema_version"] != 2
            ):
                raise ValueError("Expected closing selection contract v2.")
            if proposed is None:
                status, reason = "unavailable", "no_suitable_item_in_packet"
            elif len(raw["selections"]) >= min(len(bundle.items), max_detailed_selections):
                raise ValueError("Closing selection exceeds the shared detail allowance.")
            elif not isinstance(identity, str) or identity in main_ids | rejected_main:
                raise ValueError("Closing selection must have its own unconflicted identity.")
            elif identity not in eligible:
                reason = "source_not_eligible_for_closing"
            else:
                selection = _parse_live_selection(proposed, known, review.limitations)
                status, reason, rejected_ids = "selected", "same_response_selection", ()
        except (ValueError, TypeError, KeyError):
            pass
    return ClosingAttempt(
        review.slot,
        review.provider,
        review.model,
        review.bundle_id,
        review.prompt_hash,
        review.response_sha256,
        status,
        reason,
        selection.evidence_id if selection is not None else None,
        2,
        rejected_ids,
    ), selection


class HistoricalDispositions(Enum):
    """Absent old records differ from a missing attempt in a captured packet."""

    NOT_RECORDED = "not_recorded"
    MISSING_ATTEMPT = "missing_attempt"


@dataclass(frozen=True)
class ReviewAttempt:
    review: ModelReview
    dispositions: CandidateDispositionAttempt | HistoricalDispositions
    closing: ClosingAttempt | None = None


@dataclass(frozen=True)
class ResolvedReview:
    """Shallow-frozen delivery authority constructed by the private resolver."""

    report: BlindReviewReport
    attempts: tuple[ReviewAttempt, ...]
    chosen: ReviewAttempt
    selections: tuple[EvidenceSelection, ...]
    outcome: Literal["selected", "primary_abstained", "incomplete"]
    abstention_complete: bool

    @property
    def disposition_attempts(self) -> tuple[CandidateDispositionAttempt, ...]:
        return tuple(
            attempt.dispositions
            for attempt in self.attempts
            if isinstance(attempt.dispositions, CandidateDispositionAttempt)
        )


def _resolve_review(
    report: BlindReviewReport,
    attempts: tuple[ReviewAttempt, ...],
) -> ResolvedReview:
    reviews = {review.slot: review for review in report.reviews}
    if len(reviews) != len(report.reviews) or not attempts:
        raise ValueError("Review result requires unique report slots and an actual attempt.")
    slots = [attempt.review.slot for attempt in attempts]
    if len(set(slots)) != len(slots):
        raise ValueError("Review result contains duplicate attempts.")
    for attempt in attempts:
        review = attempt.review
        if reviews.get(review.slot) != review or review.bundle_id != report.evidence.bundle_id:
            raise ValueError("Review attempt does not bind its exact report.")
        if isinstance(attempt.dispositions, CandidateDispositionAttempt):
            validate_disposition_attempt(attempt.dispositions, report.evidence, review)
        elif not isinstance(attempt.dispositions, HistoricalDispositions):
            raise ValueError("Review attempt requires disposition evidence or historical provenance.")
        closing = attempt.closing
        if closing is not None and (
            closing.slot,
            closing.provider,
            closing.model,
            closing.bundle_id,
            closing.prompt_hash,
            closing.response_sha256,
        ) != (
            review.slot,
            review.provider,
            review.model,
            report.evidence.bundle_id,
            review.prompt_hash,
            review.response_sha256,
        ):
            raise ValueError("Closing attempt does not bind its exact report.")
    chosen = attempts[0]
    if chosen.review.status in {"invalid", "unavailable"}:
        chosen = next(
            (
                attempt
                for attempt in attempts
                if attempt.review.slot == "secondary" and attempt.review.status in {"ok", "partial"}
            ),
            chosen,
        )
    selections: tuple[EvidenceSelection, ...] = ()
    if chosen.review.status in {"ok", "partial", "abstained"}:
        selections = tuple(validated_cached_selections(chosen.review, report.evidence)[0])
    abstention_complete = chosen.review.status == "abstained" and (
        chosen.dispositions is HistoricalDispositions.NOT_RECORDED
        or isinstance(chosen.dispositions, CandidateDispositionAttempt)
        and chosen.dispositions.status == "complete"
    )
    outcome: Literal["selected", "primary_abstained", "incomplete"] = "incomplete"
    if selections:
        outcome = "selected"
    elif abstention_complete and any(
        review.slot == "primary" and review.status == "abstained" for review in report.reviews
    ):
        outcome = "primary_abstained"
    return ResolvedReview(
        report=report,
        attempts=attempts,
        chosen=chosen,
        selections=selections,
        outcome=outcome,
        abstention_complete=abstention_complete,
    )


def resolve_review(report: BlindReviewReport, attempts: tuple[ReviewAttempt, ...]) -> ResolvedReview:
    """Resolve fresh primary/fallback attempts, with no unrecorded disposition path.

    The synthetic pending secondary is report provenance, never a response.
    """
    actual = tuple(
        review
        for review in report.reviews
        if not (review.error == "pending_independent_review" and review.attempted_at is None)
    )
    expected = (
        ("primary", "secondary")
        if attempts and attempts[0].review.status in {"invalid", "unavailable"}
        else ("primary",)
    )
    if (
        tuple(attempt.review.slot for attempt in attempts) != expected
        or tuple(attempt.review for attempt in attempts) != actual
        or any(not isinstance(attempt.dispositions, CandidateDispositionAttempt) for attempt in attempts)
    ):
        raise ValueError("Fresh review requires every actual primary-first response and its dispositions.")
    return _resolve_review(report, attempts)


def restore_review(
    report: BlindReviewReport,
    dispositions: tuple[CandidateDispositionAttempt, ...] = (),
) -> ResolvedReview:
    """Join historical wire records once, preserving their recorded report order.

    A wholly absent capture is old provenance. A partial captured packet cannot
    turn its missing delivery disposition into historical abstention permission.
    Report-first category/replay semantics survive here without relaxing the live resolver.
    """
    captures = {attempt.slot: attempt for attempt in dispositions}
    if len(captures) != len(dispositions) or any(slot not in {r.slot for r in report.reviews} for slot in captures):
        raise ValueError("Saved dispositions contain duplicate or unmatched review slots.")
    missing = HistoricalDispositions.MISSING_ATTEMPT if dispositions else HistoricalDispositions.NOT_RECORDED
    return _resolve_review(
        report, tuple(ReviewAttempt(review, captures.get(review.slot, missing)) for review in report.reviews)
    )
