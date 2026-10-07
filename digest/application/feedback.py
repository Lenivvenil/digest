"""Feedback collection, local persistence and exact-byte acknowledgement effects."""

from __future__ import annotations

import hashlib
from copy import deepcopy
from datetime import datetime, timezone

from digest.adapters.storage.feedback import (
    feedback_exists,
    feedback_sha256,
    load_feedback,
    read_exact_batch,
    save_feedback,
)
from digest.adapters.storage.pending_sources import load_pending
from digest.adapters.telegram.feedback import (
    TELEGRAM_TEXT_LIMIT,
    collect_update,
    fresh_cursor,
    owned_source_update,
    owner_chat_id,
    poll_updates,
    send_replies,
)
from digest.domain.feedback.rules import clear_pending_replies
from digest.domain.feedback.values import POLL_COUNT_KEYS, FeedbackStore


async def collect_feedback(
    bot_token: str, store: FeedbackStore, *, cache_dir: str = ".cache", acknowledge: bool = True,
) -> FeedbackStore:
    """Poll once, persist the candidate batch, then optionally acknowledge its UI."""
    owner = owner_chat_id()
    owner_hash = hashlib.sha256(owner.encode()).hexdigest()
    # Never overwrite durable state or acknowledge from a stale in-memory cursor.
    persisted = load_feedback(cache_dir, strict=True)
    if feedback_exists(cache_dir) and persisted != store:
        raise ValueError("Feedback store changed; reload it before polling")
    trusted_cursor = fresh_cursor(store, now=datetime.now(tz=timezone.utc))
    updates = await poll_updates(bot_token, offset=store.last_update_id + 1 if trusted_cursor else None)
    pending = load_pending(cache_dir, strict=True) if any(owned_source_update(item, owner) for item in updates) else []
    candidate = deepcopy(store)
    if not trusted_cursor and updates:
        candidate.previous_update_id = store.last_update_id
        candidate.last_update_id = 0
    counts = dict.fromkeys(POLL_COUNT_KEYS, 0)
    counts["received"] = len(updates)
    counts["superseded_replies"] = len(candidate.pending_replies)
    clear_pending_replies(candidate)
    seen_updates: set[int] = set()
    for update in updates:
        update_id = update["update_id"]
        if (trusted_cursor and update_id <= store.last_update_id) or update_id in seen_updates:
            counts["duplicates"] += 1
            continue
        seen_updates.add(update_id)
        counts[collect_update(update, candidate, owner, pending)] += 1
        candidate.last_update_id = max(candidate.last_update_id, update_id)
    if seen_updates:
        candidate.cursor_observed_at = datetime.now(tz=timezone.utc).isoformat()
    candidate.last_poll_counts = counts
    candidate.pending_owner_sha256 = owner_hash if candidate.pending_replies else ""
    save_feedback(candidate, cache_dir, strict=True)
    if acknowledge:
        expected = feedback_sha256(cache_dir)
        await acknowledge_feedback(bot_token, cache_dir, expected)
        return load_feedback(cache_dir, strict=True)
    return candidate


def _command_reply(command: str, store: FeedbackStore, cache_dir: str) -> str:
    if command == "/status":
        return (
            f"Last digest: {store.last_digest_time or 'unknown'}\n"
            f"Sources: {len(store.last_digest_sources)}"
        )
    from digest.adapters.storage.sources import load_source_category_map, load_source_state, load_stats
    from digest.application.source_scoring import compute_bubble_report
    return compute_bubble_report(
        store, load_stats(cache_dir), load_source_state(cache_dir),
        category_map=load_source_category_map(cache_dir) or None,
    )[:TELEGRAM_TEXT_LIMIT]


async def acknowledge_feedback(bot_token: str, cache_dir: str, expected_sha256: str) -> dict[str, int]:
    """Best-effort terminal UI work for exactly the externally committed file bytes."""
    store = read_exact_batch(cache_dir, expected_sha256)
    owner = owner_chat_id()
    if store.pending_replies and store.pending_owner_sha256 != hashlib.sha256(owner.encode()).hexdigest():
        raise ValueError("Pending feedback owner binding mismatch; no acknowledgments sent")
    counts = {"attempted": 0, "ack_ok": 0, "ack_failed": 0}
    if not store.pending_replies:
        return counts
    counts = await send_replies(bot_token, store, owner, lambda command: _command_reply(command, store, cache_dir))
    clear_pending_replies(store)
    save_feedback(store, cache_dir, strict=True)
    return counts
