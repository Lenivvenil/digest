"""Native composition admission of an existing fragment; no new investigation."""

from __future__ import annotations

import logging
from datetime import date, datetime
from pathlib import Path

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
    return pending


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
