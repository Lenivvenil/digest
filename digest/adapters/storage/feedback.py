"""Feedback codec and local atomic persistence with post-success pruning."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from digest._util import atomic_json_write
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

logger = logging.getLogger(__name__)
FEEDBACK_FILE = "feedback.json"


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError("Invalid feedback string list")
    return value


def _string_map(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or any(
        not isinstance(key, str) or not isinstance(item, str) for key, item in value.items()
    ):
        raise ValueError("Invalid feedback mapping")
    return value


def parse_feedback(data: Any, *, strict: bool) -> FeedbackStore:
    if not isinstance(data, dict):
        raise ValueError("Invalid feedback object")
    ratings = data.get("ratings", [])
    if not isinstance(ratings, list):
        raise ValueError("Invalid feedback ratings")
    parsed_ratings: list[ArticleFeedback] = []
    for rating in ratings:
        if (
            not isinstance(rating, dict)
            or any(not isinstance(rating.get(key), str) for key in ("article_hash", "source_name", "timestamp"))
            or type(rating.get("rating")) is not int
            or rating["rating"] not in (-1, 1)
        ):
            if strict:
                raise ValueError("Invalid feedback rating")
            logger.warning("Skipping malformed feedback rating")
            continue
        parsed_ratings.append(
            ArticleFeedback(
                rating["article_hash"],
                rating["source_name"],
                rating["rating"],
                rating["timestamp"],
            )
        )
    offset = data.get("last_update_id", 0)
    digest_time = data.get("last_digest_time", "")
    cursor_observed = data.get("cursor_observed_at", "")
    successful_poll = data.get("last_successful_poll_at", "")
    previous = data.get("previous_update_id", 0)
    if (
        type(offset) is not int
        or offset < 0
        or not isinstance(digest_time, str)
        or not isinstance(cursor_observed, str)
        or not isinstance(successful_poll, str)
        or type(previous) is not int
        or previous < 0
    ):
        raise ValueError("Invalid feedback metadata")
    replies = data.get("pending_replies", [])
    if not isinstance(replies, list):
        raise ValueError("Invalid pending feedback replies")
    parsed_replies: list[PendingReply] = []
    for reply in replies:
        if (
            not isinstance(reply, dict)
            or reply.get("kind") not in ("callback", "command", "vote", "source")
            or not isinstance(reply.get("identifier"), str)
            or not reply["identifier"]
            or not isinstance(reply.get("text", ""), str)
            or (reply["kind"] == "command" and (reply["identifier"] not in COMMANDS or reply.get("text", "")))
            or (
                reply["kind"] == "source"
                and (reply["identifier"] not in ("source_decisions", "unknown_source") or reply.get("text", ""))
            )
            or (reply["kind"] == "vote" and (reply["identifier"] not in VOTE_REPLIES or reply.get("text", "")))
        ):
            raise ValueError("Invalid pending feedback reply")
        parsed_replies.append(PendingReply(reply["kind"], reply["identifier"], reply.get("text", "")))
    owner_hash = data.get("pending_owner_sha256", "")
    if not isinstance(owner_hash, str) or (owner_hash and not re.fullmatch(r"[0-9a-f]{64}", owner_hash)):
        raise ValueError("Invalid feedback owner binding")
    counts = data.get("last_poll_counts", {})
    if not isinstance(counts, dict) or any(
        key not in POLL_COUNT_KEYS or type(value) is not int or value < 0 for key, value in counts.items()
    ):
        raise ValueError("Invalid feedback poll counts")
    return FeedbackStore(
        ratings=parsed_ratings,
        last_update_id=offset,
        cursor_observed_at=cursor_observed,
        previous_update_id=previous,
        last_digest_sources=_string_list(data.get("last_digest_sources", [])),
        last_digest_time=digest_time,
        source_decisions=_string_map(data.get("source_decisions", {})),
        source_decision_bindings=_string_map(data.get("source_decision_bindings", {})),
        article_source_map=_string_map(data.get("article_source_map", {})),
        pending_replies=parsed_replies,
        pending_owner_sha256=owner_hash,
        seen_callback_ids=_string_list(data.get("seen_callback_ids", []))[-SEEN_CALLBACK_LIMIT:],
        seen_message_ids=_string_list(data.get("seen_message_ids", []))[-SEEN_MESSAGE_LIMIT:],
        last_poll_counts=counts,
        last_successful_poll_at=successful_poll,
    )


def decode_feedback(content: bytes, *, strict: bool) -> FeedbackStore:
    """Decode the exact retained bytes without reading or replacing a file."""
    return parse_feedback(json.loads(content), strict=strict)


def feedback_exists(cache_dir: str) -> bool:
    """Check whether the feedback path exists after the caller's strict reload."""
    return (Path(cache_dir) / FEEDBACK_FILE).exists()


def feedback_sha256(cache_dir: str) -> str:
    """Hash the exact current file bytes after successful local persistence."""
    return hashlib.sha256((Path(cache_dir) / FEEDBACK_FILE).read_bytes()).hexdigest()


def read_exact_batch(cache_dir: str, expected_sha256: str) -> FeedbackStore:
    """Read once, verify the exact byte hash, then strictly decode that batch."""
    content = (Path(cache_dir) / FEEDBACK_FILE).read_bytes()
    if hashlib.sha256(content).hexdigest() != expected_sha256:
        raise ValueError("Feedback batch SHA-256 mismatch; no acknowledgments sent")
    return decode_feedback(content, strict=True)


def load_feedback(cache_dir: str, *, strict: bool = False) -> FeedbackStore:
    """Load legacy or current feedback; strict mode never resets a corrupt store."""
    try:
        content = (Path(cache_dir) / FEEDBACK_FILE).read_bytes()
    except FileNotFoundError:
        return FeedbackStore()
    except OSError as exc:
        if strict:
            raise
        logger.warning("Failed to load feedback (%s)", type(exc).__name__)
        return FeedbackStore()
    try:
        return decode_feedback(content, strict=strict)
    except (ValueError, TypeError, UnicodeError) as exc:
        if strict:
            raise
        logger.warning("Failed to load feedback (%s)", type(exc).__name__)
        return FeedbackStore()


def save_feedback(store: FeedbackStore, cache_dir: str, *, strict: bool = False) -> None:
    """Atomically persist feedback, pruning only after a successful write."""
    candidate = deepcopy(store)
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=30)
    ratings: list[ArticleFeedback] = []
    for rating in candidate.ratings:
        try:
            timestamp = datetime.fromisoformat(rating.timestamp)
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
            if timestamp >= cutoff:
                ratings.append(rating)
        except ValueError:
            ratings.append(rating)
    candidate.ratings = ratings
    candidate.article_source_map = dict(list(candidate.article_source_map.items())[-1000:])
    candidate.seen_callback_ids = candidate.seen_callback_ids[-SEEN_CALLBACK_LIMIT:]
    candidate.seen_message_ids = candidate.seen_message_ids[-SEEN_MESSAGE_LIMIT:]
    try:
        path = Path(cache_dir) / FEEDBACK_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json_write(path, asdict(candidate))
    except Exception as exc:
        if strict:
            raise
        logger.warning("Failed to save feedback (%s)", type(exc).__name__)
        return
    store.ratings = candidate.ratings
    store.article_source_map = candidate.article_source_map
    store.seen_callback_ids = candidate.seen_callback_ids
    store.seen_message_ids = candidate.seen_message_ids
