"""Carry one accepted investigation into a later ordinary edition, without new work."""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict, replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from digest.adapters.storage import edition as editions
from digest.adapters.storage import post_delivery as storage
from digest.domain.delivery.edition import Receipts, SupplementEdition
from digest.domain.delivery.supplement import PendingSupplement, SupplementFragment, fragment_identity, fragment_window
from digest.domain.investigation.delivered import DeliveredInvestigationInput
from digest.presentation.supplement import fragment_text

if TYPE_CHECKING:
    from digest.domain.editorial.reviews import EvidenceBundle
    from digest.irritator.evidence_stage import EvidenceIrritatorResult
    from digest.review_checkpoint import FullSourceEvidence

logger = logging.getLogger(__name__)


def prepare_fragment(
    record: dict[str, Any],
    result_path: Path,
    result: EvidenceIrritatorResult,
    presented: EvidenceIrritatorResult,
    origin: DeliveredInvestigationInput,
    *,
    language: str,
    notice: str,
    bundle: EvidenceBundle,
    source: FullSourceEvidence | None,
) -> None:
    """Persist a lossless immutable projection; per-attempt state is the only pending slot."""
    from digest._serialization import canonical_json_bytes, unique_object

    result_bytes = result_path.read_bytes()
    if canonical_json_bytes(json.loads(result_bytes, object_pairs_hook=unique_object)) != canonical_json_bytes(
        asdict(result)
    ):
        raise ValueError("Stored canonical investigation differs from its validated result.")
    from digest.irritator.evidence_stage import eligible_delivered_result

    if not eligible_delivered_result(result, origin, bundle, source):
        record["supplement_status"] = "archive_only"
        record["supplement_reason"] = "no_accepted_delivered_counter_signal"
        return
    # Translation may alter only the already requested presentation fields.
    if (
        len(presented.narratives) != len(result.narratives)
        or len(presented.ranked_signals) != len(result.ranked_signals)
        or any(
            replace(shown, claim=canonical.claim) != canonical
            for shown, canonical in zip(presented.narratives, result.narratives, strict=True)
        )
        or any(
            replace(shown, narrative_claim=canonical.narrative_claim, reasoning=canonical.reasoning) != canonical
            for shown, canonical in zip(presented.ranked_signals, result.ranked_signals, strict=True)
        )
    ):
        raise ValueError("Supplement presentation changed canonical evidence or ranking membership.")
    investigated_at = datetime.now(UTC).isoformat()
    text = fragment_text(result, presented, origin, language=language, notice=notice, investigated_at=investigated_at)
    fragment = SupplementFragment(
        "",
        origin,
        record["checkpoint"],
        storage.repository_relative_path(result_path),
        hashlib.sha256(result_bytes).hexdigest(),
        investigated_at,
        text,
    )
    fragment = replace(fragment, fragment_id=fragment_identity(fragment))
    projection = result_path.with_suffix(".fragment.json")
    if not storage.create_attempt(projection, asdict(fragment)):
        raise ValueError("Supplement projection already exists; refusing replacement.")
    projection_sha = hashlib.sha256(projection.read_bytes()).hexdigest()
    reference = storage.repository_relative_path(projection)
    storage.read_projection(reference, projection_sha)
    record["fragment"] = {
        "projection": reference,
        "projection_sha256": projection_sha,
        "fragment_id": fragment.fragment_id,
        "included_ready": None,
        "released_binding": None,
    }
    record["supplement_status"] = "pending"


def _pending(marker: Path, record: dict[str, Any]) -> PendingSupplement:
    saved = record["fragment"]
    if record["supplement_status"] == "pending" and saved["included_ready"] is not None:
        raise ValueError("Pending supplement retains an unresolved ready reservation.")
    fragment = storage.read_projection(saved["projection"], saved["projection_sha256"])
    from digest._serialization import canonical_json_bytes

    if (
        fragment.fragment_id != saved["fragment_id"]
        or fragment.origin.checkpoint_sha256 != record["checkpoint_sha256"]
        or canonical_json_bytes(asdict(fragment.origin)) != canonical_json_bytes(record["delivered"])
        or hashlib.sha256(canonical_json_bytes(record["delivered"])).hexdigest() != record["delivered_sha256"]
    ):
        raise ValueError("Pending supplement binding changed.")
    return PendingSupplement(
        fragment, saved["projection"], saved["projection_sha256"], storage.repository_relative_path(marker.resolve())
    )


def _release_unclaimed(
    pending: PendingSupplement,
    record: dict[str, Any],
    now: datetime,
    cache: Path = Path(".cache"),
) -> None:
    """Persist proof-bound release before a later ready can replace the old manifest."""
    saved = record["fragment"]
    previous = saved["included_ready"]
    path = cache / editions.READY_FILE
    data, digest = editions.read_record(path, previous, allowed_schemas=(3,))
    editions.validate_manifest_record(data, data["payloads"][0]["chat_id"], now, fresh=False)
    if (
        digest != previous
        or data["supplement"]["fragment"]["fragment_id"] != pending.fragment.fragment_id
        or now < datetime.fromisoformat(data["expires_at"])
        or any((cache / name).exists() for name in (editions.CLAIM_FILE, editions.RECEIPTS_FILE))
    ):
        raise ValueError("Reserved supplement has no proven expired, unclaimed origin ready.")
    saved["released_binding"] = {"ready_sha256": previous, "reason": "expired_unclaimed_ready"}
    saved["included_ready"] = None
    record["supplement_status"] = "pending"
    storage.save_attempt(Path(pending.attempt), record)


def pending_fragment(
    directory: str, publication_day: date, *, now: datetime | None = None
) -> tuple[
    PendingSupplement | None,
    str,
]:
    """Inspect existing per-investigation records; uncertainty omits only optional work."""
    available = []
    instant = now or datetime.now(UTC)
    for marker in storage.attempt_paths(directory):
        try:
            record = storage.read_attempt(marker)
            if record.get("schema_version") != 3 or "fragment" not in record:
                continue
            if record["supplement_status"] in {"consumed", "expired", "archive_only"}:
                continue
            if record["supplement_status"] not in {"pending", "included_ready"}:
                raise ValueError("Unknown pending supplement disposition.")
            pending = _pending(marker, record)
            if record["supplement_status"] == "included_ready":
                _release_unclaimed(pending, record, instant)
            window = fragment_window(pending.fragment, publication_day)
            if fragment_window(pending.fragment, instant.date()) == "expired":
                record.update(supplement_status="expired", supplement_reason="origin_day_plus_four_utc")
                storage.save_attempt(marker, record)
            else:
                available.append((pending, "ineligible_destination" if window == "expired" else window))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            logger.warning("Optional supplement held: %s", type(exc).__name__)
            return None, "unverified_pending_fragment"
    if len(available) > 1:
        return None, "multiple_pending_fragments"
    return available[0] if available else (None, "no_pending_fragment")


def archive_fragment(pending: PendingSupplement, reason: str) -> None:
    record = storage.read_attempt(Path(pending.attempt))
    if _pending(Path(pending.attempt), record) != pending or record["supplement_status"] != "pending":
        raise ValueError("Optional supplement changed before omission.")
    record.update(supplement_status="archive_only", supplement_reason=reason)
    storage.save_attempt(Path(pending.attempt), record)


def reserve_fragment(pending: PendingSupplement, ready_sha256: str) -> None:
    record = storage.read_attempt(Path(pending.attempt))
    if (
        _pending(Path(pending.attempt), record) != pending
        or record["supplement_status"] != "pending"
        or record["fragment"]["included_ready"] is not None
    ):
        raise ValueError("Supplement is no longer available for this ready edition.")
    record["fragment"]["included_ready"] = ready_sha256
    record["supplement_status"] = "included_ready"
    storage.save_attempt(Path(pending.attempt), record)


def verify_fragment_reservation(
    data: SupplementEdition,
    ready_sha256: str,
    *,
    allow_consumed: bool = False,
) -> dict[str, Any]:
    frozen = data.supplement
    record = storage.read_attempt(Path(frozen.attempt))
    pending = _pending(Path(frozen.attempt), record)
    saved = record["fragment"]
    permitted = {"included_ready", "consumed"} if allow_consumed else {"included_ready"}
    if (
        pending != PendingSupplement(frozen.fragment, frozen.projection, frozen.projection_sha256, frozen.attempt)
        or record["supplement_status"] not in permitted
        or saved["included_ready"] != ready_sha256
    ):
        raise ValueError("Frozen supplement reservation changed; publishing held.")
    return record


def release_replaced_fragment(data: SupplementEdition, ready_sha256: str, now: datetime, cache: Path) -> None:
    frozen = data.supplement
    record = storage.read_attempt(Path(frozen.attempt))
    pending = _pending(Path(frozen.attempt), record)
    saved = record["fragment"]
    if record["supplement_status"] == "included_ready":
        if saved["included_ready"] != ready_sha256:
            raise ValueError("Expired ready supplement has a different reservation.")
        _release_unclaimed(pending, record, now, cache)
    elif record["supplement_status"] not in {"pending", "expired", "archive_only"} or saved["released_binding"] != {
        "ready_sha256": ready_sha256,
        "reason": "expired_unclaimed_ready",
    }:
        raise ValueError("Expired ready supplement release is not proven.")


def consume_fragment(data: SupplementEdition, ready_sha256: str, receipts: Receipts) -> None:
    """Required application write, independent of uncertainty in later unrelated chunks."""
    record = verify_fragment_reservation(data, ready_sha256, allow_consumed=True)
    coverage = data.supplement.coverage.covering_chunks
    confirmed = {item.chunk: item for item in receipts.confirmed if item.owner_sha256 == data.owner_sha256}
    if not all(index in confirmed for index in coverage):
        raise ValueError("Supplement is not completely confirmed; edition remains unapplied and held.")
    proof = [asdict(confirmed[index]) for index in coverage]
    saved = record["fragment"]
    if record["supplement_status"] == "consumed":
        if saved.get("consumed_chunks") != proof:
            raise ValueError("Supplement consumption proof changed.")
        return
    saved.update(consumed_chunks=proof, consumed_at=datetime.now(UTC).isoformat())
    record["supplement_status"] = "consumed"
    storage.save_attempt(Path(data.supplement.attempt), record)
