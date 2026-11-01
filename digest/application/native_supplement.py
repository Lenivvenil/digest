"""Native composition admission of an existing fragment; no new investigation."""

from __future__ import annotations

import hashlib
import logging
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path

from digest._serialization import canonical_json_bytes, restore_dataclass
from digest.adapters.storage import native as storage
from digest.adapters.storage.issue_paths import safe_issue_path
from digest.adapters.storage.post_delivery import read_projection
from digest.application.supplement import pending_fragment, verify_pending_fragment
from digest.config import Config
from digest.domain.delivery.supplement import PendingSupplement, fragment_window


class SupplementLayoutError(ValueError):
    """Only optional layout admission failed; accepted main content stays intact."""


def verify_available(
    pending: PendingSupplement, *, day: date, now: datetime, owner_sha256: str,
) -> None:
    for path in (pending.attempt, pending.projection, pending.fragment.result, pending.fragment.checkpoint):
        safe_issue_path(Path(path))
    if (pending.fragment.origin.owner_sha256 != owner_sha256
            or fragment_window(pending.fragment, day) != "eligible"
            or fragment_window(pending.fragment, now.date()) == "expired"
            or read_projection(pending.projection, pending.projection_sha256) != pending.fragment):
        raise ValueError("Native supplement is changed, wrong-owner or outside its original window.")
    verify_pending_fragment(pending)


def select_existing(config: Config, *, day: date, now: datetime, owner_sha256: str) -> PendingSupplement | None:
    pending, reason = pending_fragment(config.obsidian.output_dir, day, now=now)
    if pending is None or reason != "eligible":
        if reason != "no_pending_fragment":
            logging.getLogger(__name__).info("Native optional supplement omitted: %s", reason)
        return None
    if pending.fragment.origin.owner_sha256 != owner_sha256:
        logging.getLogger(__name__).info("Native optional supplement omitted: wrong_owner")
        return None
    verify_available(pending, day=day, now=now, owner_sha256=owner_sha256)
    if not ranking_had_original_context(pending):
        logging.getLogger(__name__).info("Native optional supplement omitted: unverified_ranking_original_context")
        return None
    return pending


def ranking_had_original_context(pending: PendingSupplement) -> bool:
    """Require retained proof of context exposure, not today's reconstructed prompt.

    This is a necessary admission condition, not semantic acceptance. A complete
    extraction is not proof of publisher completeness. Unknown older provenance
    remains omitted even if a later acquisition policy would admit its snapshot.
    Only new selection uses this guard; frozen/consumed editions remain unchanged.
    """
    from digest.irritator.evidence_stage import NativeResearchProvenance

    try:
        result = storage.decode(storage.read_reference(
            storage.Reference(pending.fragment.result, pending.fragment.result_sha256)))
        if result.get("schema_version") != 3:
            return False
        provenance = restore_dataclass(result.get("native_provenance"), NativeResearchProvenance)
        session = storage.decode(storage.read_reference(
            storage.Reference(provenance.session.path, provenance.session.sha256)))
        phase_dir = Path(pending.attempt).with_suffix(".phases")
        if (set(session) != {"schema", "checkpoint", "delivered_sha256", "config_sha256", "attempt",
                             "attempt_sha256", "claim", "claim_sha256", "phases"}
                or session["schema"] != "native-irritator-session/v1"
                or session["attempt"] != pending.attempt
                or session["checkpoint"] != pending.fragment.checkpoint
                or session["delivered_sha256"] != hashlib.sha256(
                    canonical_json_bytes(asdict(pending.fragment.origin))).hexdigest()
                or session["phases"] != phase_dir.as_posix()
                or provenance.session.path != Path(pending.attempt).with_suffix(".phases.json").as_posix()):
            return False
        requests = []
        for index, reference in enumerate(provenance.phases):
            value = storage.decode(storage.read_reference(storage.Reference(reference.path, reference.sha256)))
            if value.get("phase") == "ranking" and value.get("schema") not in {
                "native-irritator-request/v1", "native-irritator-intent/v1", "native-irritator-response/v1",
            }:
                return False
            if value.get("phase") == "ranking" and value.get("schema") == "native-irritator-request/v1":
                if (set(value) != {"schema", "phase", "session_sha256", "prior", "input"}
                        or value["schema"] != "native-irritator-request/v1"
                        or value["session_sha256"] != provenance.session.sha256
                        or reference.path != (phase_dir / "ranking.request.json").as_posix()
                        or value["prior"] != [asdict(item) for item in provenance.phases[:index]]
                        or not isinstance(value["input"], dict) or set(value["input"]) != {"messages"}):
                    return False
                requests.append(value)
        if len(requests) != 1:
            return False
        messages = requests[0]["input"]["messages"]
        if (not isinstance(messages, list) or len(messages) != 2
                or any(not isinstance(item, dict) or set(item) != {"role", "content"}
                       or not isinstance(item["content"], str) for item in messages)
                or messages[0]["role"] != "system" or messages[1]["role"] != "user"):
            return False
        rankings = [item for item in result["diagnostics"] if item["stage"] == "ranking"]
        if (len(rankings) != 1 or rankings[0].get("prompt_sha256")
                != hashlib.sha256(canonical_json_bytes(messages)).hexdigest()):
            return False
        payload = storage.decode(messages[1]["content"].encode("utf-8"))
        canonical_json_bytes(payload)  # Reject non-finite values inside the retained JSON string.
        context = payload.get("original_document_context")
        original = result["documents"][0]
        snapshot = original["snapshot"]
        return bool(original["role"] == "origin" and snapshot["status"] == "complete"
                    and isinstance(context, str) and context.strip() and context == snapshot["text"])
    except (OSError, ValueError, KeyError, TypeError, IndexError, AttributeError, RecursionError):
        # Missing or unfamiliar proof omits optional content; never repair history.
        return False


def retained(pending: storage.PendingNative) -> PendingSupplement | None:
    if pending.projection is None:
        return None
    return storage.projected_supplement(storage.decode(storage.read_reference(pending.projection)))


def finish_reservation(ready_sha256: str, *, cache: Path, now: datetime, owner: str, confirmed: bool) -> None:
    from digest.adapters.storage import edition as editions
    from digest.application.supplement import finish_native_reservation, verify_consumed_fragment
    from digest.domain.delivery.edition import NativeSupplementEdition

    edition, _ = editions.load_edition(cache / editions.READY_FILE, owner, now, ready_sha256)
    if isinstance(edition, NativeSupplementEdition):
        finish_native_reservation(edition, ready_sha256, now, cache, allow_consumed=confirmed)
        if confirmed:
            _, claim_sha = editions.load_claim(cache / editions.CLAIM_FILE, ready_sha256, edition.owner_sha256)
            receipts = editions.load_receipts(
                cache, ready_sha256, claim_sha, len(edition.payloads), edition.owner_sha256,
            )
            if receipts is None or receipts.state != "confirmed" or not receipts.applied:
                raise ValueError("Confirmed native recovery lacks exact applied receipts.")
            verify_consumed_fragment(edition, ready_sha256, receipts)
