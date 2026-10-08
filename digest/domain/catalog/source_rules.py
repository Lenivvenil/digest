"""Pure source scoring, fetch accounting and trial lifecycle rules."""

from __future__ import annotations

import logging
import math
from datetime import date, datetime, timezone
from typing import Literal

from digest.domain.catalog.sources import (
    HISTORY_MAX_DAYS,
    AdaptiveConfig,
    DailySnapshot,
    SourceConfig,
    SourceStateStore,
    SourceStats,
)

logger = logging.getLogger(__name__)


def update_stats(
    stats: dict[str, SourceStats],
    source_name: str,
    fetch_ok: bool,
    articles_found: int,
    articles_included: int,
    avg_desc_len: float,
    *,
    observed_at: datetime,
) -> None:
    """Update statistics for a source after a fetch. Caps history at 30 days."""
    today = observed_at.strftime("%Y-%m-%d")

    if source_name not in stats:
        stats[source_name] = SourceStats(name=source_name)

    s = stats[source_name]
    s.total_fetches += 1
    if fetch_ok:
        s.successful_fetches += 1
        s.total_articles_found += articles_found
        s.articles_included_in_digest += articles_included
        s.last_seen = today
        if avg_desc_len > 0:
            if s.avg_description_length == 0.0:
                s.avg_description_length = avg_desc_len
            else:
                s.avg_description_length = s.avg_description_length * 0.7 + avg_desc_len * 0.3

    if s.history and s.history[-1].date == today:
        snap = s.history[-1]
        snap.articles_found = max(snap.articles_found, articles_found)
        snap.articles_included = max(snap.articles_included, articles_included)
        snap.fetch_ok = snap.fetch_ok or fetch_ok
    else:
        s.history.append(
            DailySnapshot(
                date=today,
                articles_found=articles_found,
                articles_included=articles_included,
                fetch_ok=fetch_ok,
            )
        )

    if len(s.history) > HISTORY_MAX_DAYS:
        s.history = s.history[-HISTORY_MAX_DAYS:]


def record_delivered_articles(
    stats: dict[str, SourceStats],
    new_hashes: set[str],
    article_source_map: dict[str, str],
    publication_day: date,
) -> None:
    """Count new confirmed coverage on its publication day without another fetch.

    The application owns deduplication and passes only hashes absent from its
    current delivered cache. This operation alone is not safe to replay after a
    partial multi-file write.
    """
    for identity in new_hashes:
        source = article_source_map.get(identity[:8])
        if source in stats:
            stats[source].articles_included_in_digest += 1
            day = publication_day.isoformat()
            history = stats[source].history
            entry = next((item for item in history if item.date == day), None)
            if entry is None:
                entry = DailySnapshot(day, 0, 0, False)
                history.append(entry)
                history.sort(key=lambda item: item.date)
            entry.articles_included += 1
            stats[source].history = history[-HISTORY_MAX_DAYS:]


def source_score_factors(stats: SourceStats) -> tuple[float, float, float, datetime | None] | None:
    """Prepare quality factors before the original conditional recency observation."""
    if stats.total_fetches == 0:
        return None

    reliability = stats.successful_fetches / stats.total_fetches

    recent_snaps = stats.history[-7:] if stats.history else []
    recent_found = sum(s.articles_found for s in recent_snaps)
    recent_included = sum(s.articles_included for s in recent_snaps)
    if recent_found == 0:
        if stats.total_articles_found > 0:
            productivity = min(1.0, stats.articles_included_in_digest / stats.total_articles_found)
        else:
            productivity = 0.0
    else:
        productivity = min(1.0, recent_included / recent_found)

    if stats.avg_description_length >= 100:
        desc_quality = 1.0
    else:
        desc_quality = stats.avg_description_length / 100.0

    last = None
    if stats.last_seen is not None:
        try:
            last = datetime.strptime(stats.last_seen, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return reliability, productivity, desc_quality, last


def score_from_factors(
    factors: tuple[float, float, float, datetime | None] | None,
    *,
    now: datetime | None,
) -> float:
    """Apply an explicit recency observation and the unchanged quality weights."""
    if factors is None:
        return 0.5
    reliability, productivity, desc_quality, last = factors
    recency = 0.0
    if last is not None:
        if now is None:
            raise ValueError("A recency observation is required for a dated source.")
        try:
            days_ago = (now - last).days
            if days_ago <= 3:
                recency = 1.0
            else:
                recency = max(0.0, 1.0 - (days_ago - 3) / 7.0)
        except ValueError:
            recency = 0.0
    score = reliability * 0.3 + productivity * 0.3 + desc_quality * 0.2 + recency * 0.2
    return min(1.0, max(0.0, score))


def detect_trending_sources(stats: dict[str, SourceStats], window: int = 7) -> list[str]:
    """Return source names where articles_found shows >50% increase."""
    trending: list[str] = []
    for name, s in stats.items():
        if len(s.history) < window:
            continue
        recent = s.history[-window:]
        previous = s.history[-2 * window : -window] if len(s.history) >= 2 * window else []
        recent_total = sum(snap.articles_found for snap in recent)
        previous_total = sum(snap.articles_found for snap in previous)
        if previous_total == 0:
            continue
        increase = (recent_total - previous_total) / previous_total
        if increase > 0.5:
            trending.append(name)
    return trending


def calculate_effective_priority(
    source: SourceConfig,
    quality_score: float,
    feedback_score: float,
    adaptive_config: AdaptiveConfig,
    *,
    trending: bool,
) -> int:
    """Combine one source's observed quality, feedback and configured priority."""
    min_p = adaptive_config.min_priority
    max_p = adaptive_config.max_priority
    p_range = max_p - min_p
    base_norm = source.priority / 5.0
    weighted = (
        base_norm * adaptive_config.base_weight
        + quality_score * adaptive_config.score_weight
        + feedback_score * adaptive_config.feedback_weight
    )
    priority = round(min_p + weighted * p_range)
    if trending:
        priority += 1
    return max(min_p, min(max_p, priority))


def calculate_feedback_priorities(
    sources: list[SourceConfig],
    feedback_scores: dict[str, float],
    adaptive_config: AdaptiveConfig,
) -> dict[str, int]:
    """Bounded vote-only adjustment; unrated sources keep their configured priority.

    No objective quality/trending signal or source lifecycle mutation is introduced
    when automatic adaptive management is disabled.
    """
    span = adaptive_config.max_priority - adaptive_config.min_priority
    result: dict[str, int] = {}
    for source in sources:
        score = feedback_scores.get(source.name)
        if score is None or adaptive_config.feedback_weight == 0:
            result[source.name] = source.priority
            continue
        delta = round((2 * score - 1) * adaptive_config.feedback_weight * span)
        result[source.name] = max(
            adaptive_config.min_priority, min(adaptive_config.max_priority, source.priority + delta)
        )
    return result


def trial_source_status(
    source: SourceConfig,
    source_state: SourceStateStore,
    *,
    today: str,
    today_dt: datetime,
) -> Literal["skip", "start", "score"]:
    """Select the existing trial path without reading time or source statistics."""
    if not source.trial:
        return "skip"
    if source_state.is_graduated(source.name) or source_state.is_demoted(source.name):
        return "skip"
    trial_started = source_state.get_trial_started(source.name)
    if trial_started is None:
        logger.info(
            "Trial source '%s' has no trial_started date; will initialize to %s",
            source.name,
            today,
        )
        return "start"
    try:
        started_dt = datetime.strptime(trial_started, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        logger.warning("Invalid trial_started date for source '%s': %s", source.name, trial_started)
        return "skip"
    return "skip" if (today_dt - started_dt).days < source.trial_days else "score"


def trial_score_decision(score: float) -> Literal["promote", "demote", "retain"]:
    if score > 0.6:
        return "promote"
    if score < 0.3:
        return "demote"
    return "retain"


def apply_trial_decisions_to_cache(
    store: SourceStateStore,
    promote: list[str],
    demote: list[str],
    today: str,
    needs_start: list[str] | None = None,
) -> SourceStateStore:
    """Apply trial promotion/demotion decisions to the source state cache.

    Graduated sources: mark_graduated (trial_started cleared).
    Demoted sources: mark_demoted.
    needs_start sources: initialize trial_started to today.
    """
    if needs_start is None:
        needs_start = []
    for name in promote:
        store.mark_graduated(name)
        logger.info("Graduated trial source '%s' to permanent", name)
    for name in demote:
        store.mark_demoted(name)
        logger.info("Demoted trial source '%s'", name)
    for name in needs_start:
        store.set_trial_started(name, today)
        logger.info("Initialized trial_started for '%s' to %s", name, today)
    return store


def _diversity_score(source_stats: dict[str, SourceStats]) -> tuple[float, str]:
    """Shannon entropy over 7-day article inclusion per source → (score 0–100, label)."""
    recent: dict[str, int] = {}
    for name, s in source_stats.items():
        count = sum(snap.articles_included for snap in s.history[-7:])
        if count > 0:
            recent[name] = count
    total = sum(recent.values())
    n = len(recent)
    if total == 0:
        return 0.0, "No data"
    if n == 1:
        return 0.0, "Single source"
    entropy = -sum((c / total) * math.log2(c / total) for c in recent.values())
    score = entropy / math.log2(n) * 100
    if score >= 70:
        label = "Diverse"
    elif score >= 40:
        label = "Moderate"
    else:
        label = "Concentrated"
    return score, label
