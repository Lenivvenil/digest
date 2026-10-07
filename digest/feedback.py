"""Compatibility exports for feedback values, storage and application operations.

New production callers import the responsible domain, adapter or application owner.
"""

from datetime import datetime, timezone

from digest.adapters.storage.feedback import FEEDBACK_FILE, load_feedback, save_feedback
from digest.adapters.telegram.feedback import TELEGRAM_TEXT_LIMIT, FeedbackOwnerError, FeedbackWebhookActive
from digest.application.feedback import acknowledge_feedback, collect_feedback
from digest.domain.feedback.rules import apply_delivery_attribution
from digest.domain.feedback.rules import get_source_feedback_score as _get_source_feedback_score
from digest.domain.feedback.values import (
    COMMANDS,
    POLL_COUNT_KEYS,
    SEEN_CALLBACK_LIMIT,
    SEEN_MESSAGE_LIMIT,
    VOTE_REPLIES,
    ArticleFeedback,
    FeedbackStore,
    PendingReply,
)


def get_source_feedback_score(store: FeedbackStore, source_name: str, days: int = 14) -> float | None:
    """Compatibility entrypoint retaining an independently sampled scoring time."""
    return _get_source_feedback_score(store, source_name, days, now=datetime.now(tz=timezone.utc))


__all__ = [
    "ArticleFeedback", "FeedbackStore", "PendingReply", "FeedbackOwnerError", "FeedbackWebhookActive",
    "FEEDBACK_FILE", "TELEGRAM_TEXT_LIMIT", "SEEN_CALLBACK_LIMIT", "SEEN_MESSAGE_LIMIT",
    "POLL_COUNT_KEYS", "COMMANDS", "VOTE_REPLIES", "apply_delivery_attribution",
    "load_feedback", "save_feedback", "collect_feedback", "acknowledge_feedback", "get_source_feedback_score",
]
