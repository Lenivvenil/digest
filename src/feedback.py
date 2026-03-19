"""User feedback collection and storage for adaptive source management."""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

FEEDBACK_FILE = "feedback.json"


def _category_hash(category: str) -> str:
    """Return an 8-character hex hash for a category name (for callback_data)."""
    return hashlib.md5(category.encode()).hexdigest()[:8]


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
    # Mapping of digest_id (YYYYMMDD_HHMMSS) -> contributing source names.
    # Allows correct attribution when feedback arrives for older digests.
    digest_sources_map: dict[str, list[str]] = field(default_factory=dict)
    # Mapping of digest_id -> {category: [source_names]} for per-category feedback.
    digest_category_sources_map: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    # ISO timestamp of the last successfully delivered digest (set in main.py).
    last_digest_time: str = ""


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
        ratings = [
            ArticleFeedback(
                article_hash=r["article_hash"],
                source_name=r["source_name"],
                rating=r["rating"],
                timestamp=r["timestamp"],
            )
            for r in data.get("ratings", [])
        ]
        return FeedbackStore(
            ratings=ratings,
            last_update_id=data.get("last_update_id", 0),
            last_digest_sources=data.get("last_digest_sources", []),
            digest_sources_map=data.get("digest_sources_map", {}),
            digest_category_sources_map=data.get("digest_category_sources_map", {}),
            last_digest_time=data.get("last_digest_time", ""),
        )
    except Exception as exc:
        logger.warning("Failed to load feedback: %s", exc)
        return FeedbackStore()


def save_feedback(store: FeedbackStore, cache_dir: str) -> None:
    """Serialize feedback store to JSON."""
    path = Path(cache_dir) / FEEDBACK_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "ratings": [asdict(r) for r in store.ratings],
        "last_update_id": store.last_update_id,
        "last_digest_sources": store.last_digest_sources,
        "digest_sources_map": store.digest_sources_map,
        "digest_category_sources_map": store.digest_category_sources_map,
        "last_digest_time": store.last_digest_time,
    }
    try:
        with path.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
    except Exception as exc:
        logger.warning("Failed to save feedback: %s", exc)


async def collect_feedback(bot_token: str, store: FeedbackStore) -> FeedbackStore:
    """Poll Telegram getUpdates for new feedback callback queries.

    Parses callback_data matching 'fb:good:N' or 'fb:bad:N', answers each
    callback query, and appends ratings to the store.
    """
    offset = store.last_update_id + 1 if store.last_update_id > 0 else None
    api_url = f"https://api.telegram.org/bot{bot_token}/getUpdates"
    params: dict[str, str | int] = {"allowed_updates": '["callback_query", "message"]'}
    if offset is not None:
        params["offset"] = offset

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(api_url, params=params)
            response.raise_for_status()
            data = response.json()

            if not data.get("ok"):
                logger.warning("Telegram getUpdates returned not ok: %s", data)
                return store

            for update in data.get("result", []):
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
                        continue

                    callback_query = update.get("callback_query")
                    if not callback_query:
                        continue

                    callback_data = callback_query.get("data", "")
                    parts = callback_data.split(":")
                    callback_id = callback_query.get("id", "")
                    now = datetime.now(tz=timezone.utc).isoformat()

                    if not parts or parts[0] != "fb":
                        continue

                    if len(parts) == 5 and parts[1] == "cat" and parts[2] in ("good", "bad"):
                        # Per-category feedback: fb:cat:{good|bad}:{cat_hash}:{digest_id}
                        rating = 1 if parts[2] == "good" else -1
                        cat_hash = parts[3]
                        digest_id_cat = parts[4]
                        cat_map = store.digest_category_sources_map.get(digest_id_cat, {})
                        sources: list[str] = []
                        for cat_name, cat_sources_list in cat_map.items():
                            if _category_hash(cat_name) == cat_hash:
                                sources = cat_sources_list
                                break
                        if sources:
                            for src_name in sources:
                                store.ratings.append(
                                    ArticleFeedback(
                                        article_hash=callback_data,
                                        source_name=src_name,
                                        rating=rating,
                                        timestamp=now,
                                    )
                                )
                        else:
                            store.ratings.append(
                                ArticleFeedback(
                                    article_hash=callback_data,
                                    source_name="",
                                    rating=rating,
                                    timestamp=now,
                                )
                            )
                    elif len(parts) in (3, 4) and parts[1] in ("good", "bad"):
                        # Digest-level feedback: fb:{good|bad}:{chunk_index}[:{digest_id}]
                        rating = 1 if parts[1] == "good" else -1
                        digest_id = parts[3] if len(parts) == 4 else None

                        # Look up sources for the specific digest.
                        # - New-format callback with known digest_id → exact match
                        # - New-format callback with unknown digest_id (pruned/lost) → skip
                        #   attribution rather than misattributing to the wrong digest
                        # - Legacy callback (no digest_id) → fall back to last_digest_sources
                        if digest_id and digest_id in store.digest_sources_map:
                            digest_sources = store.digest_sources_map[digest_id]
                        elif digest_id:
                            # digest_id present but not found — pruned or unknown;
                            # record unscoped feedback rather than misattribute
                            logger.debug(
                                "Digest %s not in sources map; recording unscoped feedback",
                                digest_id,
                            )
                            digest_sources = []
                        else:
                            digest_sources = store.last_digest_sources
                        if digest_sources:
                            for src_name in digest_sources:
                                store.ratings.append(
                                    ArticleFeedback(
                                        article_hash=callback_data,
                                        source_name=src_name,
                                        rating=rating,
                                        timestamp=now,
                                    )
                                )
                        else:
                            # Fallback: no source mapping available (legacy data)
                            store.ratings.append(
                                ArticleFeedback(
                                    article_hash=callback_data,
                                    source_name="",
                                    rating=rating,
                                    timestamp=now,
                                )
                            )
                    else:
                        continue

                    # Answer the callback query to dismiss the loading indicator
                    answer_url = f"https://api.telegram.org/bot{bot_token}/answerCallbackQuery"
                    await client.post(
                        answer_url,
                        json={"callback_query_id": callback_id},
                    )
                except Exception as exc:
                    logger.warning(
                        "Failed to process feedback update %d: %s", update_id, exc
                    )
                finally:
                    # Advance offset after each update is fully processed (or failed)
                    # so we don't re-process it, but only after the try block completes
                    if update_id > store.last_update_id:
                        store.last_update_id = update_id

    except Exception as exc:
        logger.warning("Failed to collect feedback from Telegram: %s", exc)

    return store


def get_source_feedback_score(
    store: FeedbackStore, source_name: str, days: int = 14
) -> float | None:
    """Aggregate feedback ratings for a source over the last N days.

    Returns a score between 0.0 and 1.0, or None if no feedback exists
    for the given source in the time window.
    """
    now = datetime.now(tz=timezone.utc)
    relevant: list[int] = []

    for fb in store.ratings:
        # Only include ratings that are explicitly tagged with this source.
        # Ratings with empty source_name (chunk-level feedback) are excluded
        # from per-source scores to avoid penalizing/rewarding all sources
        # equally — they provide no signal for source discrimination.
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

    # Convert ratings (-1, 1) to 0.0-1.0 scale
    # Average of ratings mapped: -1 -> 0.0, 1 -> 1.0
    avg = sum(relevant) / len(relevant)
    return (avg + 1.0) / 2.0
