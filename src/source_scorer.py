"""Source quality scoring: data models, statistics tracking, and scoring engine."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

STATS_FILE = "source_stats.json"
HISTORY_MAX_DAYS = 30


@dataclass
class DailySnapshot:
    date: str
    articles_found: int
    articles_included: int
    fetch_ok: bool


@dataclass
class SourceStats:
    name: str
    total_fetches: int = 0
    successful_fetches: int = 0
    total_articles_found: int = 0
    articles_included_in_digest: int = 0
    avg_description_length: float = 0.0
    last_seen: str | None = None
    history: list[DailySnapshot] = field(default_factory=list)


def load_stats(cache_dir: str) -> dict[str, SourceStats]:
    """Load source statistics from JSON file. Return empty dict if missing."""
    path = Path(cache_dir) / STATS_FILE
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            logger.warning("Invalid source stats format, expected dict")
            return {}
        result: dict[str, SourceStats] = {}
        for name, raw in data.items():
            history = [
                DailySnapshot(
                    date=snap["date"],
                    articles_found=snap["articles_found"],
                    articles_included=snap["articles_included"],
                    fetch_ok=snap["fetch_ok"],
                )
                for snap in raw.get("history", [])
            ]
            result[name] = SourceStats(
                name=raw.get("name", name),
                total_fetches=raw.get("total_fetches", 0),
                successful_fetches=raw.get("successful_fetches", 0),
                total_articles_found=raw.get("total_articles_found", 0),
                articles_included_in_digest=raw.get("articles_included_in_digest", 0),
                avg_description_length=raw.get("avg_description_length", 0.0),
                last_seen=raw.get("last_seen"),
                history=history,
            )
        return result
    except Exception as exc:
        logger.warning("Failed to load source stats: %s", exc)
        return {}


def save_stats(stats: dict[str, SourceStats], cache_dir: str) -> None:
    """Serialize source statistics to JSON."""
    path = Path(cache_dir) / STATS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {name: asdict(s) for name, s in stats.items()}
    try:
        with path.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
    except Exception as exc:
        logger.warning("Failed to save source stats: %s", exc)


def update_stats(
    stats: dict[str, SourceStats],
    source_name: str,
    fetch_ok: bool,
    articles_found: int,
    articles_included: int,
    avg_desc_len: float,
) -> None:
    """Update statistics for a source after a fetch. Caps history at 30 days."""
    today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")

    if source_name not in stats:
        stats[source_name] = SourceStats(name=source_name)

    s = stats[source_name]
    s.total_fetches += 1
    if fetch_ok:
        s.successful_fetches += 1
        s.total_articles_found += articles_found
        s.articles_included_in_digest += articles_included
        s.last_seen = today
        # Rolling average for description length
        if avg_desc_len > 0:
            if s.avg_description_length == 0.0:
                s.avg_description_length = avg_desc_len
            else:
                s.avg_description_length = (
                    s.avg_description_length * 0.7 + avg_desc_len * 0.3
                )

    s.history.append(
        DailySnapshot(
            date=today,
            articles_found=articles_found,
            articles_included=articles_included,
            fetch_ok=fetch_ok,
        )
    )

    # Cap history at HISTORY_MAX_DAYS
    if len(s.history) > HISTORY_MAX_DAYS:
        s.history = s.history[-HISTORY_MAX_DAYS:]


def calculate_score(stats: SourceStats) -> float:
    """Calculate composite quality score (0.0-1.0) for a source.

    Components:
    - reliability: successful_fetches / total_fetches
    - productivity: articles_included / articles_found
    - description quality: avg_description_length > 100
    - recency: last_seen within 3 days
    """
    if stats.total_fetches == 0:
        return 0.5  # New source with no history gets neutral score

    # Reliability (weight: 0.3)
    reliability = stats.successful_fetches / stats.total_fetches

    # Productivity (weight: 0.3)
    if stats.total_articles_found == 0:
        productivity = 0.0
    else:
        productivity = stats.articles_included_in_digest / stats.total_articles_found

    # Description quality (weight: 0.2)
    if stats.avg_description_length >= 100:
        desc_quality = 1.0
    else:
        desc_quality = stats.avg_description_length / 100.0

    # Recency (weight: 0.2)
    if stats.last_seen is None:
        recency = 0.0
    else:
        try:
            last = datetime.strptime(stats.last_seen, "%Y-%m-%d").replace(
                tzinfo=timezone.utc
            )
            days_ago = (datetime.now(tz=timezone.utc) - last).days
            if days_ago <= 3:
                recency = 1.0
            else:
                recency = max(0.0, 1.0 - (days_ago - 3) / 7.0)
        except ValueError:
            recency = 0.0

    score = (
        reliability * 0.3
        + productivity * 0.3
        + desc_quality * 0.2
        + recency * 0.2
    )
    return min(1.0, max(0.0, score))
