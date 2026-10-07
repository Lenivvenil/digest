"""Pure per-item disposition values and stored-attempt validation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, cast

if TYPE_CHECKING:
    from digest.domain.editorial.reviews import EvidenceBundle, ModelReview

DispositionStatus = Literal["selected", "not_selected", "duplicate", "deferred"]


@dataclass(frozen=True)
class CandidateDisposition:
    evidence_id: str
    status: DispositionStatus
    reason: str = ""
    retained_id: str | None = None


@dataclass(frozen=True)
class CandidateDispositionAttempt:
    slot: str
    provider: str
    model: str
    bundle_id: str
    prompt_hash: str
    response_sha256: str | None
    status: Literal["complete", "incomplete"]
    dispositions: tuple[CandidateDisposition, ...]
    unresolved_ids: tuple[str, ...]
    errors: tuple[str, ...]
    finish_reason: str | None = None


@dataclass
class CandidateDispositionCapture:
    attempts: list[CandidateDispositionAttempt] = field(default_factory=list)


def _entry(value: object, known: set[str], selected: set[str]) -> CandidateDisposition:
    if not isinstance(value, dict):
        raise ValueError("invalid disposition schema")
    identity, status = value.get("evidence_id"), value.get("status")
    if not isinstance(identity, str) or identity not in known:
        raise ValueError("unknown disposition evidence id")
    if not isinstance(status, str) or status not in {"selected", "not_selected", "duplicate", "deferred"}:
        raise ValueError("invalid disposition status")
    keys = {"evidence_id", "status"}
    if status != "selected":
        keys.add("reason")
    if status == "duplicate":
        keys.add("retained_id")
    if set(value) != keys:
        raise ValueError("invalid disposition schema")
    reason, retained = value.get("reason", ""), value.get("retained_id")
    if status != "selected" and (not isinstance(reason, str) or not reason.strip() or len(reason) > 240):
        raise ValueError("invalid disposition reason")
    if (status == "selected") != (identity in selected):
        raise ValueError("disposition contradicts accepted selection")
    if status == "duplicate" and (not isinstance(retained, str) or retained not in known or retained == identity):
        raise ValueError("invalid duplicate retained id")
    return CandidateDisposition(identity, cast(DispositionStatus, status), reason.strip(), retained)


def validate_disposition_attempt(
    attempt: CandidateDispositionAttempt, bundle: EvidenceBundle, review: ModelReview,
) -> None:
    """Validate a stored capture's structure and exact report binding, without raw text.

    The original capture checked the raw response hash. This checks its persisted
    provenance and decisions; it cannot reconstruct or certify absent raw output.
    """
    binding = (attempt.slot, attempt.provider, attempt.model, attempt.bundle_id,
               attempt.prompt_hash, attempt.response_sha256)
    expected = (review.slot, review.provider, review.model, bundle.bundle_id,
                review.prompt_hash, review.response_sha256)
    if binding != expected or review.bundle_id != bundle.bundle_id:
        raise ValueError("Disposition attempt report binding mismatch.")
    known = {item.evidence_id for item in bundle.items}
    selected = {item.evidence_id for item in review.selections}
    rejected = {item.evidence_id for item in review.rejected_items if item.evidence_id in known}
    if (not isinstance(attempt.dispositions, tuple) or len(attempt.dispositions) > len(known)
            or not isinstance(attempt.unresolved_ids, tuple)
            or attempt.finish_reason is not None and not isinstance(attempt.finish_reason, str)
            or not isinstance(attempt.errors, tuple) or len(attempt.errors) > 2 * len(known) + 2
            or any(not isinstance(error, str) or not error.strip() or len(error) > 600
                   for error in attempt.errors)):
        raise ValueError("Invalid disposition attempt fields.")
    parsed: dict[str, CandidateDisposition] = {}
    for item in attempt.dispositions:
        if (not isinstance(item, CandidateDisposition) or not isinstance(item.reason, str)
                or item.status == "selected" and item.reason != ""
                or item.status != "duplicate" and item.retained_id is not None):
            raise ValueError("Invalid stored disposition fields.")
        raw = {"evidence_id": item.evidence_id, "status": item.status}
        if item.status != "selected":
            raw["reason"] = item.reason
        if item.status == "duplicate":
            if not isinstance(item.retained_id, str):
                raise ValueError("Invalid stored duplicate retained id.")
            raw["retained_id"] = item.retained_id
        validated = _entry(raw, known, selected)
        if item.evidence_id in rejected:
            raise ValueError("Rejected selection output cannot supply a stored disposition.")
        if validated != item or item.evidence_id in parsed:
            raise ValueError("Invalid or duplicated stored disposition.")
        parsed[item.evidence_id] = item
    for item in parsed.values():
        if item.status == "duplicate":
            retained = parsed.get(item.retained_id or "")
            if retained is None or retained.status != "selected":
                raise ValueError("Stored duplicate target is not a validated selection.")
    unresolved = tuple(item.evidence_id for item in bundle.items
                       if item.evidence_id not in parsed or parsed[item.evidence_id].status == "deferred")
    expected_status = "incomplete" if unresolved or attempt.errors else "complete"
    if attempt.unresolved_ids != unresolved or attempt.status != expected_status:
        raise ValueError("Stored disposition completeness is inconsistent.")
    if review.status in {"invalid", "unavailable"} and (parsed or not attempt.errors):
        raise ValueError("Failed review cannot supply dispositions.")
    if review.status == "partial" and not attempt.errors:
        raise ValueError("Partial review cannot claim complete disposition validation.")

    if (attempt.finish_reason is not None and attempt.finish_reason not in {"stop", "STOP", "end_turn"}
            and (parsed or not attempt.errors)):
        raise ValueError("Unfinished provider response cannot supply dispositions.")
