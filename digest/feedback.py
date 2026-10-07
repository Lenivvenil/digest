"""User feedback collection and storage for adaptive source management."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

import httpx

from digest._util import atomic_json_write
from digest.discovery import PendingSource, load_pending, proposal_binding, resolve_pending_proposal

logger = logging.getLogger(__name__)

FEEDBACK_FILE = "feedback.json"
TELEGRAM_TEXT_LIMIT = 4096
# Ten full poll batches of replay protection, not an infinite event ledger.
# Telegram retains ordinary message updates for at most 24 hours; offsets handle normal replay.
# Legacy callbacks expire after about 150 seconds and remain best effort.
SEEN_CALLBACK_LIMIT = 1000
SEEN_MESSAGE_LIMIT = 1000
POLL_COUNT_KEYS = (
    "received", "recorded_votes", "source_decisions", "commands",
    "unknown_source", "rejected_owner", "ignored", "malformed", "duplicates", "unknown_article", "superseded_replies",
)
COMMANDS = ("/status", "/bubble")
VOTE_REPLIES = {
    "recorded_votes": "Vote saved",
    "unknown_article": "Article can no longer be matched",
}


class FeedbackOwnerError(ValueError):
    """The configured feedback recipient is not a supported private owner."""


class FeedbackWebhookActive(ValueError):
    """Polling would conflict with an existing Telegram webhook."""


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


def _parse_feedback(data: Any, *, strict: bool) -> FeedbackStore:
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
        parsed_ratings.append(ArticleFeedback(
            rating["article_hash"], rating["source_name"], rating["rating"], rating["timestamp"],
        ))
    offset = data.get("last_update_id", 0)
    digest_time = data.get("last_digest_time", "")
    cursor_observed = data.get("cursor_observed_at", "")
    previous = data.get("previous_update_id", 0)
    if (
        type(offset) is not int or offset < 0 or not isinstance(digest_time, str)
        or not isinstance(cursor_observed, str) or type(previous) is not int or previous < 0
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
            or (reply["kind"] == "source" and (
                reply["identifier"] not in ("source_decisions", "unknown_source") or reply.get("text", "")
            ))
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
    )


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
        return _parse_feedback(json.loads(content), strict=strict)
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


def _owner_chat_id() -> str:
    owner = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not owner or not owner.isdecimal() or int(owner) <= 0:
        raise FeedbackOwnerError("TELEGRAM_CHAT_ID must identify the owner's private chat")
    return owner


def _owned_message(message: Any, sender: Any, owner: str) -> bool:
    if not isinstance(message, dict) or not isinstance(sender, dict):
        return False
    chat = message.get("chat")
    return (
        isinstance(chat, dict) and chat.get("type") == "private"
        and type(chat.get("id")) is int and str(chat["id"]) == owner
        and type(sender.get("id")) is int and str(sender["id"]) == owner
    )


def _record_article_vote(store: FeedbackStore, article_hash: str, rating: str) -> str:
    source = store.article_source_map.get(article_hash, "")
    if not source:
        logger.warning("Feedback article attribution unavailable")
        return "unknown_article"
    store.ratings.append(ArticleFeedback(
        article_hash, source, 1 if rating == "g" else -1,
        datetime.now(tz=timezone.utc).isoformat(),
    ))
    return "recorded_votes"


def _record_source_decision(
    store: FeedbackStore, pending: list[PendingSource], hash8: str, action: str,
) -> str:
    proposal = resolve_pending_proposal(pending, hash8)
    if proposal is None:
        return "unknown_source"
    store.source_decisions[hash8] = "approved" if action == "ok" else "rejected"
    store.source_decision_bindings[hash8] = proposal_binding(proposal)
    return "source_decisions"


def _owned_source_update(update: dict[str, Any], owner: str) -> bool:
    """Source inputs need readable proposal state before any batch is consumed."""
    callback = update.get("callback_query")
    if callback is not None:
        return (
            isinstance(callback, dict) and _owned_message(callback.get("message"), callback.get("from"), owner)
            and isinstance(callback.get("data"), str) and callback["data"].startswith("src:")
        )
    message = update.get("message")
    return (
        isinstance(message, dict) and _owned_message(message, message.get("from"), owner)
        and isinstance(message.get("text"), str)
        and message["text"].strip().startswith(("/start source_", "/source"))
    )


def _collect_callback(
    callback: dict[str, Any], store: FeedbackStore, owner: str, pending: list[PendingSource],
) -> str:
    if not _owned_message(callback.get("message"), callback.get("from"), owner):
        return "rejected_owner"
    identifier, payload = callback.get("id"), callback.get("data")
    if not isinstance(identifier, str) or not identifier or not isinstance(payload, str):
        return "malformed"
    if identifier in store.seen_callback_ids:
        return "duplicates"
    parts = payload.split(":")
    outcome = "ignored"
    reply_text = ""
    if len(parts) == 4 and parts[:2] == ["fb", "a"] and parts[2] in ("g", "b"):
        if not re.fullmatch(r"[0-9a-f]{8}", parts[3]):
            return "malformed"
        outcome = _record_article_vote(store, parts[3], parts[2])
        if outcome == "unknown_article":
            reply_text = VOTE_REPLIES[outcome]
    elif len(parts) == 3 and parts[0] == "src" and parts[1] in ("ok", "no"):
        if not re.fullmatch(r"[0-9a-f]{8}", parts[2]):
            return "malformed"
        outcome = _record_source_decision(store, pending, parts[2], parts[1])
        reply_text = "Decision saved" if outcome == "source_decisions" else "Proposal unavailable or expired"
    elif len(parts) != 3 or parts[0] != "fb" or parts[1] not in ("good", "bad") or not parts[2].isdigit():
        return "ignored"
    store.seen_callback_ids.append(identifier)
    store.pending_replies.append(PendingReply("callback", identifier, reply_text))
    return outcome


def _collect_update(
    update: dict[str, Any], store: FeedbackStore, owner: str, pending: list[PendingSource],
) -> str:
    callback = update.get("callback_query")
    if callback is not None:
        if not isinstance(callback, dict):
            return "malformed"
        return _collect_callback(callback, store, owner, pending)
    message = update.get("message")
    if message is None:
        return "ignored"
    if not isinstance(message, dict):
        return "malformed"
    if not _owned_message(message, message.get("from"), owner):
        return "rejected_owner"
    text = message.get("text", "")
    if not isinstance(text, str):
        return "malformed"
    command = text.strip()
    if command in COMMANDS:
        store.pending_replies.append(PendingReply("command", command))
        return "commands"
    vote = re.fullmatch(r"/start vote_([gb])_([0-9a-f]{8})", command) or re.fullmatch(
        r"/vote ([gb]) ([0-9a-f]{8})", command,
    )
    source = re.fullmatch(r"/start source_(ok|no)_([0-9a-f]{8})", command) or re.fullmatch(
        r"/source (ok|no) ([0-9a-f]{8})", command,
    )
    if vote is None and source is None:
        if command.startswith(("/vote", "/start vote_", "/source", "/start source_")):
            return "malformed"
        return "ignored"
    message_id = message.get("message_id")
    if type(message_id) is not int or message_id <= 0:
        return "malformed"
    # Message IDs are per chat; keep their namespace separate from arbitrary callback IDs.
    identifier = f"{hashlib.sha256(owner.encode()).hexdigest()}:{message_id}"
    if identifier in store.seen_message_ids:
        return "duplicates"
    if source is not None:
        outcome = _record_source_decision(store, pending, source[2], source[1])
        reply_kind: Literal["vote", "source"] = "source"
    else:
        assert vote is not None
        outcome = _record_article_vote(store, vote[2], vote[1])
        reply_kind = "vote"
    store.seen_message_ids.append(identifier)
    store.pending_replies.append(PendingReply(reply_kind, outcome))
    return outcome


@asynccontextmanager
async def _telegram_client() -> AsyncIterator[httpx.AsyncClient]:
    """Keep token-bearing request URLs out of HTTP client diagnostics."""
    loggers = [logging.getLogger(name) for name in ("httpx", "httpcore")]
    levels = [item.level for item in loggers]
    try:
        for item in loggers:
            item.setLevel(max(item.getEffectiveLevel(), logging.WARNING))
        async with httpx.AsyncClient(timeout=30.0) as client:
            yield client
    finally:
        for item, level in zip(loggers, levels, strict=True):
            item.setLevel(level)


def _fresh_cursor(store: FeedbackStore) -> bool:
    """Telegram may choose a new ID generation after a week without updates."""
    try:
        observed = datetime.fromisoformat(store.cursor_observed_at)
        if observed.tzinfo is None:
            return False
        age = datetime.now(tz=timezone.utc) - observed
        # A returned update may already be up to 24 hours old when observed.
        return store.last_update_id > 0 and timedelta(0) <= age < timedelta(days=6)
    except ValueError:
        return False


async def collect_feedback(
    bot_token: str, store: FeedbackStore, *, cache_dir: str = ".cache", acknowledge: bool = True,
) -> FeedbackStore:
    """Poll once, persist the candidate batch, then optionally acknowledge its UI."""
    owner = _owner_chat_id()
    owner_hash = hashlib.sha256(owner.encode()).hexdigest()
    # Never overwrite durable state or acknowledge from a stale in-memory cursor.
    persisted = load_feedback(cache_dir, strict=True)
    if (Path(cache_dir) / FEEDBACK_FILE).exists() and persisted != store:
        raise ValueError("Feedback store changed; reload it before polling")
    trusted_cursor = _fresh_cursor(store)
    api_url = f"https://api.telegram.org/bot{bot_token}"
    async with _telegram_client() as client:
        response = await client.get(f"{api_url}/getWebhookInfo", timeout=10.0)
        response.raise_for_status()
        info = response.json()
        if (
            not isinstance(info, dict) or info.get("ok") is not True
            or not isinstance(info.get("result"), dict)
            or not isinstance(info["result"].get("url"), str)
        ):
            raise ValueError("Invalid Telegram webhook response")
        if info["result"]["url"]:
            raise FeedbackWebhookActive("Active Telegram webhook; polling disabled")
        body: dict[str, object] = {
            "allowed_updates": ["callback_query", "message"], "timeout": 10, "limit": 100,
        }
        if trusted_cursor:
            body["offset"] = store.last_update_id + 1
        response = await client.post(f"{api_url}/getUpdates", json=body)
        response.raise_for_status()
        data = response.json()
    if not isinstance(data, dict) or data.get("ok") is not True or not isinstance(data.get("result"), list):
        raise ValueError("Invalid Telegram updates response")
    updates = data["result"]
    if len(updates) > 100 or any(
        not isinstance(update, dict) or type(update.get("update_id")) is not int or update["update_id"] <= 0
        for update in updates
    ):
        raise ValueError("Invalid Telegram update envelope")
    pending = load_pending(cache_dir, strict=True) if any(_owned_source_update(item, owner) for item in updates) else []
    candidate = deepcopy(store)
    if not trusted_cursor and updates:
        candidate.previous_update_id = store.last_update_id
        candidate.last_update_id = 0
    counts = dict.fromkeys(POLL_COUNT_KEYS, 0)
    counts["received"] = len(updates)
    counts["superseded_replies"] = len(candidate.pending_replies)
    candidate.pending_replies = []
    seen_updates: set[int] = set()
    for update in updates:
        update_id = update["update_id"]
        if (trusted_cursor and update_id <= store.last_update_id) or update_id in seen_updates:
            counts["duplicates"] += 1
            continue
        seen_updates.add(update_id)
        counts[_collect_update(update, candidate, owner, pending)] += 1
        candidate.last_update_id = max(candidate.last_update_id, update_id)
    if seen_updates:
        candidate.cursor_observed_at = datetime.now(tz=timezone.utc).isoformat()
    candidate.last_poll_counts = counts
    candidate.pending_owner_sha256 = owner_hash if candidate.pending_replies else ""
    save_feedback(candidate, cache_dir, strict=True)
    if acknowledge:
        expected = hashlib.sha256((Path(cache_dir) / FEEDBACK_FILE).read_bytes()).hexdigest()
        await acknowledge_feedback(bot_token, cache_dir, expected)
        return load_feedback(cache_dir, strict=True)
    return candidate


def _command_reply(command: str, store: FeedbackStore, cache_dir: str) -> str:
    if command == "/status":
        return (
            f"Last digest: {store.last_digest_time or 'unknown'}\n"
            f"Sources: {len(store.last_digest_sources)}"
        )
    from digest.source_scorer import (
        compute_bubble_report,
        load_source_category_map,
        load_source_state,
        load_stats,
    )
    return compute_bubble_report(
        store, load_stats(cache_dir), load_source_state(cache_dir),
        category_map=load_source_category_map(cache_dir) or None,
    )[:TELEGRAM_TEXT_LIMIT]


async def acknowledge_feedback(bot_token: str, cache_dir: str, expected_sha256: str) -> dict[str, int]:
    """Best-effort terminal UI work for exactly the externally committed file bytes."""
    content = (Path(cache_dir) / FEEDBACK_FILE).read_bytes()
    if hashlib.sha256(content).hexdigest() != expected_sha256:
        raise ValueError("Feedback batch SHA-256 mismatch; no acknowledgments sent")
    store = _parse_feedback(json.loads(content), strict=True)
    owner = _owner_chat_id()
    if store.pending_replies and store.pending_owner_sha256 != hashlib.sha256(owner.encode()).hexdigest():
        raise ValueError("Pending feedback owner binding mismatch; no acknowledgments sent")
    counts = {"attempted": 0, "ack_ok": 0, "ack_failed": 0}
    if not store.pending_replies:
        return counts
    vote_replies = [reply for reply in store.pending_replies if reply.kind == "vote"]
    source_replies = [reply for reply in store.pending_replies if reply.kind == "source"]
    replies = [reply for reply in store.pending_replies if reply.kind not in ("vote", "source")]
    source_text = ""
    if source_replies:
        saved = sum(reply.identifier == "source_decisions" for reply in source_replies)
        unavailable = len(source_replies) - saved
        source_text = f"Source decisions saved: {saved}. Unavailable or expired proposals: {unavailable}."
        replies.append(source_replies[0])
    vote_text = ""
    if vote_replies:
        saved = sum(reply.identifier == "recorded_votes" for reply in vote_replies)
        unknown = len(vote_replies) - saved
        vote_text = f"Votes saved: {saved}. Unknown articles: {unknown}."
        # One ephemeral dispatch entry; durable per-vote receipts stay unchanged.
        replies.append(vote_replies[0])
    api_url = f"https://api.telegram.org/bot{bot_token}"
    try:
        async with asyncio.timeout(30.0), _telegram_client() as client:
            for reply in replies:
                counts["attempted"] += 1
                try:
                    if reply.kind == "callback":
                        response = await client.post(
                            f"{api_url}/answerCallbackQuery",
                            json={"callback_query_id": reply.identifier, "text": reply.text}, timeout=5.0,
                        )
                    else:
                        text = (
                            vote_text if reply.kind == "vote" else source_text if reply.kind == "source"
                            else _command_reply(reply.identifier, store, cache_dir)
                        )
                        response = await client.post(
                            f"{api_url}/sendMessage",
                            json={"chat_id": owner, "text": text},
                            timeout=5.0,
                        )
                    response.raise_for_status()
                    result = response.json()
                    if not isinstance(result, dict) or result.get("ok") is not True:
                        raise ValueError("Telegram reply rejected")
                except Exception as exc:
                    counts["ack_failed"] += 1
                    logger.warning("Feedback reply failed (%s)", type(exc).__name__)
                else:
                    counts["ack_ok"] += 1
    except TimeoutError:
        # Unfinished UI work is terminal too; it must never hold up later polling.
        counts["attempted"] = len(replies)
        counts["ack_failed"] = len(replies) - counts["ack_ok"]
        logger.warning("Feedback reply budget exhausted (%d failed)", counts["ack_failed"])
    store.pending_replies = []
    store.pending_owner_sha256 = ""
    save_feedback(store, cache_dir, strict=True)
    return counts


def get_source_feedback_score(
    store: FeedbackStore, source_name: str, days: int = 14
) -> float | None:
    """Aggregate the latest vote per article for a source over the last N days.

    Returns a score between 0.0 and 1.0, or None if no feedback exists.
    """
    now = datetime.now(tz=timezone.utc)
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
