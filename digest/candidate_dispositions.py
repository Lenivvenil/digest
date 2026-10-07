"""Same-response metadata decisions, outside the accepted preparation wire format.

These are model judgments over incomplete RSS evidence, never full-source review.
Missing and inconsistent output remains technical work; it is not editorial rejection.
"""
from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from digest._serialization import extract_json as _extract_json
from digest.domain.editorial.dispositions import CandidateDisposition as CandidateDisposition
from digest.domain.editorial.dispositions import CandidateDispositionAttempt as CandidateDispositionAttempt
from digest.domain.editorial.dispositions import CandidateDispositionCapture as CandidateDispositionCapture
from digest.domain.editorial.dispositions import DispositionStatus as DispositionStatus
from digest.domain.editorial.dispositions import _entry as _entry
from digest.domain.editorial.dispositions import validate_disposition_attempt as validate_disposition_attempt

if TYPE_CHECKING:
    from digest.domain.editorial.reviews import EvidenceBundle, ModelReview


def _parse_dispositions(
    text: str, known: set[str], selected: set[str], rejected: set[str],
) -> tuple[dict[str, CandidateDisposition], list[str]]:
    raw = _extract_json(text)
    if not isinstance(raw, dict) or "dispositions" not in raw:
        return {}, ["missing per-item dispositions (legacy response)"]
    values = raw["dispositions"]
    if not isinstance(values, list) or len(values) > len(known):
        return {}, ["invalid disposition count"]
    parsed: dict[str, CandidateDisposition] = {}
    seen: set[str] = set()
    invalid: set[str] = set()
    errors: list[str] = []
    for index, value in enumerate(values):
        identity = value.get("evidence_id") if isinstance(value, dict) else None
        try:
            if isinstance(identity, str):
                if identity in seen:
                    invalid.add(identity)
                    raise ValueError("duplicated disposition evidence id")
                seen.add(identity)
            if isinstance(identity, str) and identity in rejected:
                raise ValueError("disposition evidence id has rejected selection output")
            item = _entry(value, known, selected)
            parsed[item.evidence_id] = item
        except ValueError as exc:
            if isinstance(identity, str) and identity in known:
                invalid.add(identity)
            errors.append(f"disposition {index}: {exc}")
    for identity in invalid:
        parsed.pop(identity, None)
    # Only one-hop retention of a validated selected item is supported. This
    # rejects cycles/chains without inventing a semantic clustering policy.
    for identity, item in tuple(parsed.items()):
        if item.status == "duplicate":
            retained = parsed.get(item.retained_id or "")
            if retained is None or retained.status != "selected":
                parsed.pop(identity)
                errors.append(f"duplicate {identity}: retained item is not a validated selection")
    return parsed, errors


def capture_review_dispositions(
    bundle: EvidenceBundle, review: ModelReview, text: str | None,
    *, finish_reason: str | None = None,
) -> CandidateDispositionAttempt:
    """Capture only validated same-response decisions and exact provenance hashes."""
    known = {item.evidence_id for item in bundle.items}
    parsed: dict[str, CandidateDisposition] = {}
    errors: list[str] = []
    if (review.bundle_id != bundle.bundle_id
            or text is not None and hashlib.sha256(text.encode()).hexdigest() != review.response_sha256):
        errors.append("disposition evidence or response binding mismatch")
    elif finish_reason is not None and finish_reason not in {"stop", "STOP", "end_turn"}:
        errors.append("provider reported unfinished response")
    elif text is None or review.status in {"invalid", "unavailable"}:
        errors.append("review response unavailable or invalid")
    else:
        try:
            parsed, errors = _parse_dispositions(
                text, known, {item.evidence_id for item in review.selections},
                {item.evidence_id for item in review.rejected_items if item.evidence_id in known},
            )
        except (ValueError, TypeError, KeyError):
            errors.append("invalid disposition envelope")
    if review.status == "partial":
        errors.append("selection response partially invalid")
    unresolved = tuple(item.evidence_id for item in bundle.items
                       if item.evidence_id not in parsed or parsed[item.evidence_id].status == "deferred")
    return CandidateDispositionAttempt(
        review.slot, review.provider, review.model, bundle.bundle_id, review.prompt_hash, review.response_sha256,
        "incomplete" if unresolved or errors else "complete",
        tuple(parsed[item.evidence_id] for item in bundle.items if item.evidence_id in parsed),
        unresolved, tuple(errors), finish_reason,
    )
