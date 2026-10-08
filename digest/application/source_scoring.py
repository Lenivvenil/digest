"""Source-policy composition with the existing per-decision clock observations."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from digest.domain.catalog import source_rules
from digest.domain.catalog.sources import AdaptiveConfig, SourceConfig, SourceStateStore, SourceStats
from digest.domain.feedback.values import FeedbackStore
from digest.presentation import bubble

logger = logging.getLogger(__name__)


def update_stats(
    stats: dict[str, SourceStats],
    source_name: str,
    fetch_ok: bool,
    articles_found: int,
    articles_included: int,
    avg_desc_len: float,
) -> None:
    source_rules.update_stats(
        stats,
        source_name,
        fetch_ok,
        articles_found,
        articles_included,
        avg_desc_len,
        observed_at=datetime.now(tz=timezone.utc),
    )


def calculate_score(stats: SourceStats) -> float:
    """Sample only when recency previously reached the clock, separately per source."""
    factors = source_rules.source_score_factors(stats)
    now = datetime.now(tz=timezone.utc) if factors is not None and factors[3] is not None else None
    return source_rules.score_from_factors(factors, now=now)


def calculate_effective_priorities(
    sources: list[SourceConfig],
    stats: dict[str, SourceStats],
    feedback_scores: dict[str, float],
    adaptive_config: AdaptiveConfig,
) -> dict[str, int]:
    trending = source_rules.detect_trending_sources(stats)
    result: dict[str, int] = {}
    for source in sources:
        score = calculate_score(stats[source.name]) if source.name in stats else 0.5
        result[source.name] = source_rules.calculate_effective_priority(
            source,
            score,
            feedback_scores.get(source.name, 0.5),
            adaptive_config,
            trending=source.name in trending,
        )
    return result


def evaluate_trial_sources(
    sources: list[SourceConfig],
    stats: dict[str, SourceStats],
    today: str,
    source_state: SourceStateStore | None = None,
) -> tuple[list[str], list[str], list[str]]:
    """Keep trial-day eligibility separate from each eligible source's score time."""
    if source_state is None:
        source_state = SourceStateStore()
    try:
        today_dt = datetime.strptime(today, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        logger.warning("Invalid today date for trial evaluation: %s", today)
        return [], [], []
    promote: list[str] = []
    demote: list[str] = []
    needs_start: list[str] = []
    for source in sources:
        status = source_rules.trial_source_status(source, source_state, today=today, today_dt=today_dt)
        if status == "skip":
            continue
        if status == "start":
            needs_start.append(source.name)
            continue
        score = calculate_score(stats[source.name]) if source.name in stats else 0.5
        decision = source_rules.trial_score_decision(score)
        if decision == "promote":
            promote.append(source.name)
        elif decision == "demote":
            demote.append(source.name)
    return promote, demote, needs_start


def compute_bubble_report(
    feedback_store: FeedbackStore,
    source_stats: dict[str, SourceStats],
    source_state: SourceStateStore,
    category_map: dict[str, str] | None = None,
) -> str:
    return bubble.compute_bubble_report(
        feedback_store,
        source_stats,
        source_state,
        category_map,
        now=datetime.now(tz=timezone.utc),
    )
