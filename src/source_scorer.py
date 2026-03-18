"""Source quality scoring: data models, statistics tracking, and scoring engine."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from src.config import AdaptiveConfig, SourceConfig

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


def detect_trending_sources(
    stats: dict[str, SourceStats], window: int = 7
) -> list[str]:
    """Return source names where articles_found shows >50% increase.

    Compares the last `window` days against the previous `window` days.
    Sources with insufficient history (fewer than `window` snapshots in
    the recent window) are ignored.
    """
    trending: list[str] = []
    for name, s in stats.items():
        if len(s.history) < window:
            continue
        recent = s.history[-window:]
        previous = s.history[-2 * window : -window] if len(s.history) >= 2 * window else []
        recent_total = sum(snap.articles_found for snap in recent)
        previous_total = sum(snap.articles_found for snap in previous)
        if previous_total == 0:
            # No baseline to compare — cannot determine a trend
            continue
        increase = (recent_total - previous_total) / previous_total
        if increase > 0.5:
            trending.append(name)
    return trending


def calculate_effective_priorities(
    sources: list["SourceConfig"],
    stats: dict[str, SourceStats],
    feedback_scores: dict[str, float],
    adaptive_config: "AdaptiveConfig",
) -> dict[str, int]:
    """Compute effective priorities by combining base priority, quality score, and feedback.

    For each source:
      weighted = (base_priority/5)*base_weight + score*score_weight + feedback*feedback_weight
      scaled to min_priority..max_priority range, with +1 trend bonus (capped).
    """
    trending = detect_trending_sources(stats)
    min_p = adaptive_config.min_priority
    max_p = adaptive_config.max_priority
    p_range = max_p - min_p

    result: dict[str, int] = {}
    for source in sources:
        base_norm = source.priority / 5.0
        score = calculate_score(stats[source.name]) if source.name in stats else 0.5
        feedback = feedback_scores.get(source.name, 0.5)

        weighted = (
            base_norm * adaptive_config.base_weight
            + score * adaptive_config.score_weight
            + feedback * adaptive_config.feedback_weight
        )
        # Scale weighted (0.0-1.0) to priority range
        priority = round(min_p + weighted * p_range)
        # Apply trend bonus
        if source.name in trending:
            priority += 1
        priority = max(min_p, min(max_p, priority))
        result[source.name] = priority
    return result


def evaluate_trial_sources(
    sources: list["SourceConfig"],
    stats: dict[str, SourceStats],
    today: str,
) -> tuple[list[str], list[str]]:
    """Evaluate trial sources and decide which to promote or demote.

    Returns (promote_names, demote_names):
    - promote: trials with score > 0.6 after trial_days elapsed
    - demote: trials with score < 0.3 after trial_days elapsed
    - others remain in trial
    """
    promote: list[str] = []
    demote: list[str] = []

    try:
        today_dt = datetime.strptime(today, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        logger.warning("Invalid today date for trial evaluation: %s", today)
        return [], []

    for source in sources:
        if not source.trial:
            continue
        if source.trial_started is None:
            continue
        try:
            started_dt = datetime.strptime(source.trial_started, "%Y-%m-%d").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            logger.warning(
                "Invalid trial_started date for source '%s': %s",
                source.name,
                source.trial_started,
            )
            continue

        elapsed = (today_dt - started_dt).days
        if elapsed < source.trial_days:
            continue

        score = calculate_score(stats[source.name]) if source.name in stats else 0.5
        if score > 0.6:
            promote.append(source.name)
        elif score < 0.3:
            demote.append(source.name)

    return promote, demote


def apply_trial_decisions(
    config_path: str, promote: list[str], demote: list[str]
) -> None:
    """Update config.yaml: set trial=false for promoted, enabled=false for demoted."""
    if not promote and not demote:
        return

    path = Path(config_path)
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)

    if not isinstance(data, dict) or "sources" not in data:
        logger.warning("Cannot apply trial decisions: invalid config structure")
        return

    for source_entry in data["sources"]:
        name = source_entry.get("name")
        if name in promote:
            source_entry["trial"] = False
            logger.info("Promoted trial source '%s' to permanent", name)
        elif name in demote:
            source_entry["enabled"] = False
            logger.info("Demoted trial source '%s' (disabled)", name)

    tmp_path = path.with_suffix(".yaml.tmp")
    with tmp_path.open("w", encoding="utf-8") as fh:
        yaml.dump(data, fh, default_flow_style=False, allow_unicode=True)
    tmp_path.replace(path)
