"""User feedback collection and storage for adaptive source management."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from digest._util import atomic_json_write

logger = logging.getLogger(__name__)

FEEDBACK_FILE = "feedback.json"


@dataclass
class ArticleFeedback:
    article_hash: str
    source_name: str
    rating: int
    timestamp: str


@dataclass
class FeedbackStore:
    ratings: list[ArticleFeedback] = field(default_factory=list)
    last_update_id: int = 0
    last_digest_sources: list[str] = field(default_factory=list)
    # ISO timestamp of the last successfully delivered digest (set in main.py).
    last_digest_time: str = ""
    # Pending source approval decisions: source_hash -> "approved" | "rejected"
    source_decisions: dict[str, str] = field(default_factory=dict)
    # Per-article feedback attribution: article_hash (8-char) -> source_name.
    # Populated after each digest delivery; capped at 1000 entries.
    article_source_map: dict[str, str] = field(default_factory=dict)


def load_feedback(cache_dir: str) -> FeedbackStore:
    """Load feedback store from JSON file. Return empty store if missing."""
    path = Path(cache_dir) / FEEDBACK_FILE
    if not path.exists():
        return FeedbackStore()
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            logger.warning("Invalid feedback format, expected dict")
            return FeedbackStore()
        ratings: list[ArticleFeedback] = []
        for i, r in enumerate(data.get("ratings", [])):
            try:
                ratings.append(
                    ArticleFeedback(
                        article_hash=r["article_hash"],
                        source_name=r["source_name"],
                        rating=r["rating"],
                        timestamp=r["timestamp"],
                    )
                )
            except (KeyError, TypeError) as exc:
                logger.warning("Skipping malformed feedback rating at index %d: %s", i, exc)
        return FeedbackStore(
            ratings=ratings,
            last_update_id=data.get("last_update_id", 0),
            last_digest_sources=data.get("last_digest_sources", []),
            last_digest_time=data.get("last_digest_time", ""),
            source_decisions=data.get("source_decisions", {}),
            article_source_map=data.get("article_source_map", {}),
        )
    except json.JSONDecodeError as exc:
        logger.warning("Corrupted feedback JSON in %s, starting fresh: %s", path, exc)
        return FeedbackStore()
    except Exception as exc:
        logger.warning("Failed to load feedback: %s", exc)
        return FeedbackStore()


def save_feedback(store: FeedbackStore, cache_dir: str) -> None:
    """Serialize feedback store to JSON. Prunes ratings older than 30 days."""
    path = Path(cache_dir) / FEEDBACK_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=30)
    pruned_ratings: list[ArticleFeedback] = []
    for r in store.ratings:
        try:
            ts = datetime.fromisoformat(r.timestamp)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts >= cutoff:
                pruned_ratings.append(r)
        except ValueError:
            pruned_ratings.append(r)
    store.ratings = pruned_ratings
    # Prune article_source_map to the last 1000 entries (FIFO)
    if len(store.article_source_map) > 1000:
        keys = list(store.article_source_map.keys())
        for k in keys[: len(keys) - 1000]:
            del store.article_source_map[k]
    data = {
        "ratings": [asdict(r) for r in store.ratings],
        "last_update_id": store.last_update_id,
        "last_digest_sources": store.last_digest_sources,
        "last_digest_time": store.last_digest_time,
        "source_decisions": store.source_decisions,
        "article_source_map": store.article_source_map,
    }
    try:
        atomic_json_write(path, data)
    except Exception as exc:
        logger.warning("Failed to save feedback: %s", exc)


async def collect_feedback(
    bot_token: str, store: FeedbackStore, *, cache_dir: str = ".cache"
) -> FeedbackStore:
    """Poll Telegram getUpdates for new feedback callback queries.

    Parses callback_data matching 'fb:a:g:N' or 'fb:a:b:N', answers each
    callback query, and appends ratings to the store.
    """
    offset = store.last_update_id + 1 if store.last_update_id > 0 else None
    api_url = f"https://api.telegram.org/bot{bot_token}/getUpdates"

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            # Diagnostic: check current webhook state before doing anything.
            info_url = f"https://api.telegram.org/bot{bot_token}/getWebhookInfo"
            try:
                info_resp = await client.get(info_url, timeout=10.0)
                info_data = info_resp.json()
                wh_url = info_data.get("result", {}).get("url", "")
                pending = info_data.get("result", {}).get("pending_update_count", 0)
                if wh_url:
                    logger.warning(
                        "Active webhook detected: url=%s, pending_updates=%d — "
                        "deleting to enable polling",
                        wh_url,
                        pending,
                    )
                else:
                    logger.info("No webhook configured (pending_updates=%d)", pending)
            except Exception as exc:
                logger.warning("getWebhookInfo failed: %s", exc)

            # Ensure polling mode: delete any active webhook so getUpdates receives updates.
            delete_url = f"https://api.telegram.org/bot{bot_token}/deleteWebhook"
            try:
                wh_resp = await client.post(delete_url, timeout=10.0)
                wh_data = wh_resp.json()
                if not wh_data.get("ok"):
                    logger.warning("deleteWebhook returned not ok: %s", wh_data)
                else:
                    logger.debug("deleteWebhook ok (polling mode ensured)")
            except Exception as exc:
                logger.warning("deleteWebhook failed (continuing anyway): %s", exc)

            body: dict[str, object] = {
                "allowed_updates": ["callback_query", "message"],
                "timeout": 10,
            }
            if offset is not None:
                body["offset"] = offset
            response = await client.post(api_url, json=body)
            response.raise_for_status()
            data = response.json()

            if not data.get("ok"):
                logger.warning("Telegram getUpdates returned not ok: %s", data)
                return store

            results = data.get("result", [])
            if not results:
                logger.info(
                    "getUpdates returned 0 results (offset=%s, last_update_id=%d)",
                    offset,
                    store.last_update_id,
                )

            for update in results:
                update_id = update.get("update_id", 0)

                try:
                    # Handle text commands (e.g. /status)
                    message = update.get("message")
                    if message:
                        text = message.get("text", "").strip()
                        if text == "/status":
                            chat_id = str(message.get("chat", {}).get("id", ""))
                            if chat_id:
                                last_time = store.last_digest_time or "unknown"
                                source_count = len(store.last_digest_sources)
                                status_text = (
                                    f"Last digest: {last_time}\n"
                                    f"Sources: {source_count}"
                                )
                                try:
                                    send_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
                                    await client.post(
                                        send_url,
                                        json={"chat_id": chat_id, "text": status_text},
                                    )
                                except Exception as exc:
                                    logger.warning("Failed to send /status reply: %s", exc)
                        elif text == "/bubble":
                            chat_id = str(message.get("chat", {}).get("id", ""))
                            if chat_id:
                                from digest.source_scorer import (
                                    compute_bubble_report,
                                    load_source_category_map,
                                    load_source_state,
                                    load_stats,
                                )
                                stats = load_stats(cache_dir)
                                state = load_source_state(cache_dir)
                                category_map = load_source_category_map(cache_dir)
                                report = compute_bubble_report(
                                    store, stats, state, category_map=category_map or None
                                )
                                try:
                                    send_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
                                    await client.post(
                                        send_url,
                                        json={"chat_id": chat_id, "text": report},
                                    )
                                except Exception as exc:
                                    logger.warning("Failed to send /bubble reply: %s", exc)
                        continue

                    callback_query = update.get("callback_query")
                    if not callback_query:
                        continue

                    callback_data = callback_query.get("data", "")
                    parts = callback_data.split(":")
                    callback_id = callback_query.get("id", "")
                    now = datetime.now(tz=timezone.utc).isoformat()

                    if not parts:
                        continue

                    if parts[0] == "src" and len(parts) == 3 and parts[1] in ("ok", "no"):
                        # Source approval: src:{ok|no}:{source_hash}
                        decision = "approved" if parts[1] == "ok" else "rejected"
                        store.source_decisions[parts[2]] = decision
                        logger.info(
                            "Source approval decision: %s for hash %s", decision, parts[2]
                        )
                    elif (
                        parts[0] == "fb"
                        and len(parts) == 4
                        and parts[1] == "a"
                        and parts[2] in ("g", "b")
                    ):
                        # Per-article feedback: fb:a:{g|b}:{article_hash}
                        rating = 1 if parts[2] == "g" else -1
                        art_hash = parts[3]
                        source = store.article_source_map.get(art_hash, "")
                        if not source:
                            logger.warning(
                                "article_source_map miss for hash %s - rating recorded with empty source_name",
                                art_hash,
                            )
                        store.ratings.append(
                            ArticleFeedback(
                                article_hash=art_hash,
                                source_name=source,
                                rating=rating,
                                timestamp=now,
                            )
                        )
                    elif parts[0] == "fb":
                        logger.debug("Ignoring legacy feedback callback: %s", callback_data)
                    else:
                        continue

                    # Answer the callback query to dismiss the loading indicator
                    answer_url = f"https://api.telegram.org/bot{bot_token}/answerCallbackQuery"
                    await client.post(
                        answer_url,
                        json={"callback_query_id": callback_id},
                        timeout=5.0,
                    )
                except Exception as exc:
                    logger.warning(
                        "Failed to process feedback update %d: %s", update_id, exc
                    )
                finally:
                    if update_id > store.last_update_id:
                        store.last_update_id = update_id

    except Exception as exc:
        logger.warning("Failed to collect feedback from Telegram: %s", exc)

    return store


def get_source_feedback_score(
    store: FeedbackStore, source_name: str, days: int = 14
) -> float | None:
    """Aggregate feedback ratings for a source over the last N days.

    Returns a score between 0.0 and 1.0, or None if no feedback exists.
    """
    now = datetime.now(tz=timezone.utc)
    relevant: list[int] = []

    for fb in store.ratings:
        if fb.source_name != source_name:
            continue
        try:
            ts = datetime.fromisoformat(fb.timestamp)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            age_days = (now - ts).days
            if age_days <= days:
                relevant.append(fb.rating)
        except ValueError:
            continue

    if not relevant:
        return None

    avg = sum(relevant) / len(relevant)
    return (avg + 1.0) / 2.0
