"""Feedback records and the retained reply/replay vocabulary."""

from dataclasses import dataclass, field
from typing import Literal

# Ten full poll batches of replay protection, not an infinite event ledger.
# Telegram retains ordinary message updates for at most 24 hours; offsets handle normal replay.
# Legacy callbacks expire after about 150 seconds and remain best effort.
SEEN_CALLBACK_LIMIT = 1000
SEEN_MESSAGE_LIMIT = 1000
POLL_COUNT_KEYS = (
    "received",
    "recorded_votes",
    "source_decisions",
    "commands",
    "unknown_source",
    "rejected_owner",
    "ignored",
    "malformed",
    "duplicates",
    "unknown_article",
    "superseded_replies",
)
COMMANDS = ("/status", "/bubble")
VOTE_REPLIES = {
    "recorded_votes": "Vote saved",
    "unknown_article": "Article can no longer be matched",
}


@dataclass
class ArticleFeedback:
    article_hash: str
    source_name: str
    rating: int
    timestamp: str


@dataclass
class PendingReply:
    """Minimal UI receipt; commands and votes retain only recognized outcome tags."""

    kind: Literal["callback", "command", "vote", "source"]
    identifier: str
    text: str = ""


@dataclass
class FeedbackStore:
    ratings: list[ArticleFeedback] = field(default_factory=list)
    last_update_id: int = 0
    cursor_observed_at: str = ""
    previous_update_id: int = 0
    last_digest_sources: list[str] = field(default_factory=list)
    last_digest_time: str = ""
    source_decisions: dict[str, str] = field(default_factory=dict)
    source_decision_bindings: dict[str, str] = field(default_factory=dict)
    article_source_map: dict[str, str] = field(default_factory=dict)
    pending_replies: list[PendingReply] = field(default_factory=list)
    pending_owner_sha256: str = ""
    seen_callback_ids: list[str] = field(default_factory=list)
    last_poll_counts: dict[str, int] = field(default_factory=dict)
    seen_message_ids: list[str] = field(default_factory=list)
    last_successful_poll_at: str = ""
