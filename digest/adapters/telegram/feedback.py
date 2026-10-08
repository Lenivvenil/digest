"""Private-owner feedback protocol; no feedback or proposal file access."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, cast

import httpx

from digest.domain.catalog.proposals import PendingSource
from digest.domain.feedback.rules import (
    is_replayed_feedback,
    record_article_vote,
    record_callback_reply,
    record_command_reply,
    record_message_reply,
    record_source_decision,
)
from digest.domain.feedback.values import COMMANDS, VOTE_REPLIES, FeedbackStore

logger = logging.getLogger(__name__)
TELEGRAM_TEXT_LIMIT = 4096


class FeedbackOwnerError(ValueError):
    """The configured feedback recipient is not a supported private owner."""


class FeedbackWebhookActive(ValueError):
    """Polling would conflict with an existing Telegram webhook."""


def owner_chat_id() -> str:
    owner = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not owner or not owner.isdecimal() or int(owner) <= 0:
        raise FeedbackOwnerError("TELEGRAM_CHAT_ID must identify the owner's private chat")
    return owner


def _owned_message(message: Any, sender: Any, owner: str) -> bool:
    if not isinstance(message, dict) or not isinstance(sender, dict):
        return False
    chat = message.get("chat")
    return (
        isinstance(chat, dict)
        and chat.get("type") == "private"
        and type(chat.get("id")) is int
        and str(chat["id"]) == owner
        and type(sender.get("id")) is int
        and str(sender["id"]) == owner
    )


def _record_article_vote(store: FeedbackStore, article_hash: str, rating: str) -> str:
    # Sample time only when the original vote rule has known attribution.
    recorded_at = datetime.now(tz=timezone.utc) if store.article_source_map.get(article_hash, "") else None
    outcome = record_article_vote(store, article_hash, rating, recorded_at=recorded_at)
    if outcome == "unknown_article":
        logger.warning("Feedback article attribution unavailable")
    return outcome


def _record_source_decision(
    store: FeedbackStore,
    pending: list[PendingSource],
    hash8: str,
    action: str,
) -> str:
    return record_source_decision(store, pending, hash8, action, now=datetime.now(tz=timezone.utc))


def owned_source_update(update: dict[str, Any], owner: str) -> bool:
    """Source inputs need readable proposal state before any batch is consumed."""
    callback = update.get("callback_query")
    if callback is not None:
        return (
            isinstance(callback, dict)
            and _owned_message(callback.get("message"), callback.get("from"), owner)
            and isinstance(callback.get("data"), str)
            and callback["data"].startswith("src:")
        )
    message = update.get("message")
    return (
        isinstance(message, dict)
        and _owned_message(message, message.get("from"), owner)
        and isinstance(message.get("text"), str)
        and message["text"].strip().startswith(("/start source_", "/source"))
    )


def _collect_callback(
    callback: dict[str, Any],
    store: FeedbackStore,
    owner: str,
    pending: list[PendingSource],
) -> str:
    if not _owned_message(callback.get("message"), callback.get("from"), owner):
        return "rejected_owner"
    identifier, payload = callback.get("id"), callback.get("data")
    if not isinstance(identifier, str) or not identifier or not isinstance(payload, str):
        return "malformed"
    if is_replayed_feedback(store, identifier, callback=True):
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
    record_callback_reply(store, identifier, reply_text)
    return outcome


def collect_update(
    update: dict[str, Any],
    store: FeedbackStore,
    owner: str,
    pending: list[PendingSource],
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
        record_command_reply(store, command)
        return "commands"
    vote = re.fullmatch(r"/start vote_([gb])_([0-9a-f]{8})", command) or re.fullmatch(
        r"/vote ([gb]) ([0-9a-f]{8})",
        command,
    )
    source = re.fullmatch(r"/start source_(ok|no)_([0-9a-f]{8})", command) or re.fullmatch(
        r"/source (ok|no) ([0-9a-f]{8})",
        command,
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
    if is_replayed_feedback(store, identifier, callback=False):
        return "duplicates"
    if source is not None:
        outcome = _record_source_decision(store, pending, source[2], source[1])
        reply_kind: Literal["vote", "source"] = "source"
    else:
        assert vote is not None
        outcome = _record_article_vote(store, vote[2], vote[1])
        reply_kind = "vote"
    record_message_reply(store, identifier, reply_kind, outcome)
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


def fresh_cursor(store: FeedbackStore, *, now: datetime) -> bool:
    """Telegram may choose a new ID generation after a week without updates."""
    try:
        observed = datetime.fromisoformat(store.cursor_observed_at)
        if observed.tzinfo is None:
            return False
        age = now - observed
        # A returned update may already be up to 24 hours old when observed.
        return store.last_update_id > 0 and timedelta(0) <= age < timedelta(days=6)
    except ValueError:
        return False


async def poll_updates(bot_token: str, *, offset: int | None) -> list[dict[str, Any]]:
    """Check the webhook, poll once and validate the complete update envelope."""
    api_url = f"https://api.telegram.org/bot{bot_token}"
    async with _telegram_client() as client:
        response = await client.get(f"{api_url}/getWebhookInfo", timeout=10.0)
        response.raise_for_status()
        info = response.json()
        if (
            not isinstance(info, dict)
            or info.get("ok") is not True
            or not isinstance(info.get("result"), dict)
            or not isinstance(info["result"].get("url"), str)
        ):
            raise ValueError("Invalid Telegram webhook response")
        if info["result"]["url"]:
            raise FeedbackWebhookActive("Active Telegram webhook; polling disabled")
        body: dict[str, object] = {
            "allowed_updates": ["callback_query", "message"],
            "timeout": 10,
            "limit": 100,
        }
        if offset is not None:
            body["offset"] = offset
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
    return cast(list[dict[str, Any]], updates)


async def send_replies(
    bot_token: str,
    store: FeedbackStore,
    owner: str,
    command_reply: Callable[[str], str],
) -> dict[str, int]:
    """Bound terminal UI dispatch to one vote/source summary and 30 seconds total."""
    counts = {"attempted": 0, "ack_ok": 0, "ack_failed": 0}
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
                            json={"callback_query_id": reply.identifier, "text": reply.text},
                            timeout=5.0,
                        )
                    else:
                        text = (
                            vote_text
                            if reply.kind == "vote"
                            else source_text
                            if reply.kind == "source"
                            else command_reply(reply.identifier)
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
    return counts
