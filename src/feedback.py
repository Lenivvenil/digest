"""User feedback collection and storage for adaptive source management."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import httpx

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
    params: dict[str, str | int] = {"allowed_updates": '["callback_query"]'}
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
                if update_id > store.last_update_id:
                    store.last_update_id = update_id

                callback_query = update.get("callback_query")
                if not callback_query:
                    continue

                callback_data = callback_query.get("data", "")
                parts = callback_data.split(":")
                if len(parts) != 3 or parts[0] != "fb":
                    continue
                if parts[1] not in ("good", "bad"):
                    continue

                rating = 1 if parts[1] == "good" else -1
                # Use callback query id as article_hash since we don't have
                # article-level granularity in the current button scheme
                callback_id = callback_query.get("id", "")
                source_name = ""  # Will be enriched when article-level buttons are added
                now = datetime.now(tz=timezone.utc).isoformat()

                store.ratings.append(
                    ArticleFeedback(
                        article_hash=callback_data,
                        source_name=source_name,
                        rating=rating,
                        timestamp=now,
                    )
                )

                # Answer the callback query to dismiss the loading indicator
                answer_url = f"https://api.telegram.org/bot{bot_token}/answerCallbackQuery"
                await client.post(
                    answer_url,
                    json={"callback_query_id": callback_id},
                )

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
