"""Feedback durability, source decisions, and fetch accounting for digest runs."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from digest.adapters.models.execution import ModelExecution

if TYPE_CHECKING:
    from digest.config import Config
    from digest.domain.catalog.sources import SourceStats
    from digest.domain.feedback.values import FeedbackStore
    from digest.radar.collector import Article, SourceFetchMetrics


def process_pending_approvals(
    config_path: str, cache_dir: str, feedback_store: FeedbackStore,
) -> None:
    """Apply only decisions bound to a still-current proposal, independently of delivery."""
    from copy import deepcopy

    from digest.adapters.storage.feedback import save_feedback
    from digest.adapters.storage.pending_sources import load_pending, save_pending
    from digest.adapters.storage.source_config import add_source_to_config
    from digest.application.discovery import record_source_history
    from digest.domain.feedback.rules import applicable_source_decision

    logger = logging.getLogger(__name__)
    pending = load_pending(cache_dir, strict=True)
    candidate = deepcopy(feedback_store)
    remaining = list(pending)
    for ps in pending:
        decision = applicable_source_decision(candidate, pending, ps.source_hash, now=datetime.now(tz=timezone.utc))
        if decision is None:
            continue
        if decision == "approved":
            try:
                add_source_to_config(config_path, ps)
            except Exception as exc:
                logger.error("Source application incomplete (%s); decision retained", type(exc).__name__)
                continue
        record_source_history(ps, decision, cache_dir)
        remaining.remove(ps)
        candidate.source_decisions.pop(ps.source_hash, None)
        candidate.source_decision_bindings.pop(ps.source_hash, None)
    if remaining == pending:
        return
    # Config additions are idempotent if a later persistence step fails.
    from datetime import timedelta
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=30)
    for source in remaining:
        stamp = datetime.fromisoformat(source.discovered_at)
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        if stamp < cutoff:
            record_source_history(source, "expired", cache_dir)
    save_pending(remaining, cache_dir, strict=True)
    save_feedback(candidate, cache_dir, strict=True)
    feedback_store.source_decisions = candidate.source_decisions
    feedback_store.source_decision_bindings = candidate.source_decision_bindings


def apply_pending_approvals(
    config: Config, config_path: str, cache_dir: str, feedback_store: FeedbackStore, *,
    execution: ModelExecution, enabled: bool,
) -> tuple[Config, ModelExecution]:
    """Apply durable decisions before collection, then use the current runtime config."""
    from digest.config import load_config

    if not enabled or not feedback_store.source_decisions:
        return config, execution
    try:
        process_pending_approvals(config_path, cache_dir, feedback_store)
    except Exception as exc:
        logging.getLogger(__name__).warning("Source decision persistence incomplete (%s)", type(exc).__name__)
    # A state-write failure can follow a successful idempotent config addition.
    return load_config(config_path), ModelExecution()


def record_source_stats(
    source_stats: dict[str, SourceStats],
    fetch_metrics: dict[str, SourceFetchMetrics],
    articles_by_category: dict[str, list[Article]],
    delivered_hashes: set[str],
) -> None:
    """Combine fetch observations with confirmed output, including failed feeds."""
    from digest.application.source_scoring import update_stats
    from digest.radar.collector import article_hash

    included: dict[str, int] = {}
    for articles in articles_by_category.values():
        for article in articles:
            if article_hash(article.title, article.link) in delivered_hashes:
                included[article.source] = included.get(article.source, 0) + 1
    for name, metrics in fetch_metrics.items():
        update_stats(
            source_stats, name, metrics.fetch_ok, metrics.articles_found,
            included.get(name, 0), metrics.avg_description_length,
        )


def save_failed_run_stats(
    source_stats: dict[str, SourceStats],
    fetch_metrics: dict[str, SourceFetchMetrics],
    cache_dir: str,
    active_sources: set[str],
    *,
    dry_run: bool,
) -> None:
    """Retain fetch health on failed runs without consuming article/feedback state."""
    from digest.adapters.storage.sources import save_stats

    if not dry_run:
        record_source_stats(source_stats, fetch_metrics, {}, set())
        save_stats(source_stats, cache_dir, active_sources=active_sources)


async def collect_run_feedback(
    config: Config, cache_dir: str, dry_run: bool, precollected: bool,
) -> tuple[FeedbackStore, bool, int]:
    """Feedback durability is independent of today's analysis/delivery outcome."""
    from digest.adapters.storage.feedback import load_feedback
    from digest.application.feedback import collect_feedback
    from digest.domain.feedback.values import FeedbackStore

    logger = logging.getLogger(__name__)
    try:
        store = load_feedback(cache_dir, strict=True)
    except Exception as exc:
        logger.warning("Feedback state unavailable (%s); preserve it and continue without polling", type(exc).__name__)
        return FeedbackStore(), False, 0
    if dry_run or precollected or not config.telegram.enabled:
        return store, True, 0
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        return store, True, 0
    before = len(store.ratings)
    try:
        store = await collect_feedback(token, store, cache_dir=cache_dir)
    except Exception as exc:
        logger.warning("Feedback collection incomplete (%s); primary processing continues", type(exc).__name__)
        # Strict collector writes a candidate atomically. Reload any committed prefix
        # (including votes whose UI acknowledgement failed), never replace it blindly.
        try:
            store = load_feedback(cache_dir, strict=True)
        except Exception:
            return store, False, 0
    return store, True, max(0, len(store.ratings) - before)


def require_attribution_store(publishing_compact: bool, usable: bool) -> None:
    if publishing_compact and not usable:
        raise ValueError("Compact publication requires a valid feedback store for durable article attribution.")
