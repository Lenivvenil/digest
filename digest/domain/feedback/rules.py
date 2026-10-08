"""Pure feedback decisions, replay transitions, attribution and vote aggregation."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from digest.domain.catalog.proposals import PendingSource, proposal_binding, resolve_pending_proposal
from digest.domain.feedback.values import ArticleFeedback, FeedbackStore, PendingReply


def apply_delivery_attribution(
    store: FeedbackStore,
    article_source_map: dict[str, str],
    *,
    complete: bool,
    contributing_sources: list[str],
    delivered_at: datetime,
) -> None:
    """Merge confirmed card attribution; update digest context only when complete."""
    store.article_source_map.update(article_source_map)
    if complete:
        store.last_digest_sources = contributing_sources
        store.last_digest_time = delivered_at.strftime("%Y-%m-%d %H:%M UTC")


def record_article_vote(
    store: FeedbackStore,
    article_hash: str,
    rating: str,
    *,
    recorded_at: datetime | None,
) -> str:
    source = store.article_source_map.get(article_hash, "")
    if not source:
        return "unknown_article"
    assert recorded_at is not None
    store.ratings.append(
        ArticleFeedback(
            article_hash,
            source,
            1 if rating == "g" else -1,
            recorded_at.isoformat(),
        )
    )
    return "recorded_votes"


def record_source_decision(
    store: FeedbackStore,
    pending: list[PendingSource],
    hash8: str,
    action: str,
    *,
    now: datetime,
) -> str:
    proposal = resolve_pending_proposal(pending, hash8, now=now)
    if proposal is None:
        return "unknown_source"
    store.source_decisions[hash8] = "approved" if action == "ok" else "rejected"
    store.source_decision_bindings[hash8] = proposal_binding(proposal)
    return "source_decisions"


def applicable_source_decision(
    store: FeedbackStore,
    pending: list[PendingSource],
    hash8: str,
    *,
    now: datetime,
) -> str | None:
    """Revalidate a saved decision at application, independently of collection."""
    decision = store.source_decisions.get(hash8)
    current = resolve_pending_proposal(pending, hash8, now=now)
    if (
        decision not in ("approved", "rejected")
        or current is None
        or store.source_decision_bindings.get(hash8) != proposal_binding(current)
    ):
        # Legacy unbound decisions cannot authorize a future proposal.
        return None
    return decision


def is_replayed_feedback(store: FeedbackStore, identifier: str, *, callback: bool) -> bool:
    """Callback IDs and per-owner message identities use separate ledgers."""
    return identifier in (store.seen_callback_ids if callback else store.seen_message_ids)


def get_source_feedback_score(store: FeedbackStore, source_name: str, days: int = 14, *, now: datetime) -> float | None:
    """Aggregate the latest vote per article for a source over the last N days.

    Returns a score between 0.0 and 1.0, or None if no feedback exists.
    """
    latest: dict[str, tuple[datetime, int]] = {}
    for feedback in store.ratings:
        if feedback.source_name != source_name:
            continue
        try:
            timestamp = datetime.fromisoformat(feedback.timestamp)
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
            if not 0 <= (now - timestamp).days <= days:
                continue
        except ValueError:
            continue
        previous = latest.get(feedback.article_hash)
        if previous is None or timestamp >= previous[0]:
            latest[feedback.article_hash] = (timestamp, feedback.rating)
    if not latest:
        return None
    average = sum(rating for _, rating in latest.values()) / len(latest)
    return (average + 1.0) / 2.0


def record_callback_reply(store: FeedbackStore, identifier: str, text: str) -> None:
    """Consume a recognized callback in its own replay namespace."""
    store.seen_callback_ids.append(identifier)
    store.pending_replies.append(PendingReply("callback", identifier, text))


def record_message_reply(
    store: FeedbackStore,
    identifier: str,
    kind: Literal["vote", "source"],
    outcome: str,
) -> None:
    """Consume a recognized owner message independently of callback IDs."""
    store.seen_message_ids.append(identifier)
    store.pending_replies.append(PendingReply(kind, outcome))


def record_command_reply(store: FeedbackStore, command: str) -> None:
    store.pending_replies.append(PendingReply("command", command))


def clear_pending_replies(store: FeedbackStore) -> None:
    """Terminal or superseded receipts must never block the next batch."""
    store.pending_replies = []
    store.pending_owner_sha256 = ""
