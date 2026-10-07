"""Apply confirmed delivery through attribution, deduplication and accounting owners.

Prepared and direct delivery retain different persistence policies. These ordered
writes are not a transaction: an interrupted prepared application leaves its
receipts unapplied and held for inspection, never automatically reconciled.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from digest.adapters.storage import delivery_state

if TYPE_CHECKING:
    from digest.config import Config, SourceConfig
    from digest.domain.delivery.outcomes import ArticleDeliveryResult, IssueDeliveryResult
    from digest.domain.feedback.values import FeedbackStore
    from digest.radar.collector import Article, SourceFetchMetrics
    from digest.radar.summarizer import ArticleSummary
    from digest.source_scorer import SourceStateStore, SourceStats


@dataclass(frozen=True)
class PreparedOutcomePolicy:
    """Frozen delivery facts; mutable state is reloaded at application time."""

    outcome: IssueDeliveryResult
    cache_dir: str
    publication_day: date
    contributing_sources: list[str]
    adaptive_enabled: bool
    enabled_sources: list[SourceConfig]


@dataclass(frozen=True)
class LegacyOutcomePolicy:
    """Transitional direct-run handoff, including its Markdown consumption rule.

    These required inputs are application context, not a persisted domain policy.
    The legacy application supplies its collected state; decoupling mutable
    feedback/scoring ownership remains staged work after #147-A.
    """

    outcome: ArticleDeliveryResult
    config: Config
    cache_dir: str
    compact: bool
    telegram_complete: bool
    markdown_saved: bool
    delivered_at: datetime
    contributing_sources: list[str]
    feedback: FeedbackStore
    feedback_usable: bool
    previous_article_sources: dict[str, str]
    collected_cache: dict[str, str]
    articles_by_category: dict[str, list[Article]]
    summarized_categories: set[str]
    top_articles: list[ArticleSummary]
    fetch_metrics: dict[str, SourceFetchMetrics]
    source_stats: dict[str, SourceStats]
    source_state: SourceStateStore


@dataclass(frozen=True)
class AppliedOutcome:
    sources_promoted: int = 0
    sources_demoted: int = 0


def save_delivery_cache(cache: dict[str, str], compact: bool, cache_dir: str) -> None:
    """Retain strict compact writes and best-effort legacy card persistence."""
    if compact:
        delivery_state.save_delivery_cache(cache, cache_dir)
    else:
        from digest.radar import save_dedup_cache

        # The legacy owner uses its existing default cache location.
        save_dedup_cache(cache)


def apply_confirmed_outcome(policy: PreparedOutcomePolicy | LegacyOutcomePolicy) -> AppliedOutcome:
    """Apply confirmed Telegram coverage and legacy eligible Markdown output.

    Markdown consumption never creates Telegram attribution. Callers finalize
    transport receipts only after this application operation returns.
    """
    if isinstance(policy, PreparedOutcomePolicy):
        return _apply_prepared(policy)
    return _apply_legacy(policy)


def _apply_prepared(policy: PreparedOutcomePolicy) -> AppliedOutcome:
    from digest.adapters.storage.feedback import load_feedback, save_feedback
    from digest.domain.feedback.rules import apply_delivery_attribution
    from digest.source_scorer import (
        apply_trial_decisions_to_cache,
        evaluate_trial_sources,
        load_source_state,
        load_stats,
        record_delivered_articles,
    )

    outcome = policy.outcome
    if not outcome.delivered_hashes:
        return AppliedOutcome()
    store = load_feedback(policy.cache_dir, strict=True)
    cache = delivery_state.load_delivery_cache(Path(policy.cache_dir) / "seen_articles.json")
    now = datetime.now(timezone.utc)
    new_hashes = outcome.delivered_hashes - cache.keys()
    for identity in outcome.delivered_hashes:
        cache.setdefault(identity, now.isoformat())
    apply_delivery_attribution(
        store, outcome.article_source_map, complete=outcome.complete,
        contributing_sources=policy.contributing_sources, delivered_at=now,
    )

    # Preserve current votes/cursors/decisions; every write failure propagates.
    # Order is feedback -> stats -> optional adaptive state -> seen articles.
    save_feedback(store, policy.cache_dir, strict=True)
    stats = load_stats(policy.cache_dir)
    record_delivered_articles(stats, new_hashes, outcome.article_source_map, policy.publication_day)
    delivery_state.save_delivery_source_stats(stats, policy.cache_dir)
    if policy.adaptive_enabled:
        state = load_source_state(policy.cache_dir)
        today = now.date().isoformat()
        promote, demote, start = evaluate_trial_sources(policy.enabled_sources, stats, today, state)
        apply_trial_decisions_to_cache(state, promote, demote, today, start)
        delivery_state.save_delivery_source_state(state, policy.cache_dir)
    save_delivery_cache(cache, True, policy.cache_dir)
    return AppliedOutcome()


def _apply_legacy(policy: LegacyOutcomePolicy) -> AppliedOutcome:
    from digest.adapters.storage.feedback import save_feedback
    from digest.application.run_state import record_source_stats
    from digest.domain.feedback.rules import apply_delivery_attribution
    from digest.radar.collector import article_hash
    from digest.source_scorer import (
        apply_trial_decisions_to_cache,
        evaluate_trial_sources,
        save_source_category_map,
        save_source_state,
        save_stats,
    )

    outcome = policy.outcome
    delivered_hashes = set(outcome.delivered_hashes)
    if (not policy.compact and policy.markdown_saved
            and (not getattr(policy.config.telegram, "required", False) or policy.telegram_complete)):
        delivered_hashes.update(
            article_hash(article.title, article.link)
            for category, articles in policy.articles_by_category.items()
            if category in policy.summarized_categories
            for article in articles
        )
        delivered_hashes.update(article_hash(article.title, article.link) for article in policy.top_articles)
    collected_hashes = {
        article_hash(article.title, article.link)
        for articles in policy.articles_by_category.values() for article in articles
    }
    # Keep collection timestamps and old entries; suppress no unconfirmed new work.
    delivered_cache = {
        key: timestamp for key, timestamp in policy.collected_cache.items()
        if key not in collected_hashes or key in delivered_hashes
    }
    record_source_stats(policy.source_stats, policy.fetch_metrics, policy.articles_by_category, delivered_hashes)
    promoted = demoted = 0
    if outcome.sent > 0 or policy.markdown_saved:
        apply_delivery_attribution(
            policy.feedback, outcome.article_source_map, complete=policy.telegram_complete,
            contributing_sources=policy.contributing_sources, delivered_at=policy.delivered_at,
        )
        # Legacy order is seen -> feedback -> source state -> stats -> category map.
        # The existing owner functions keep their caught-versus-propagated failures.
        save_delivery_cache(delivered_cache, policy.compact, policy.cache_dir)
        if policy.config.adaptive.enabled:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            promote, demote, start = evaluate_trial_sources(
                policy.config.enabled_sources, policy.source_stats, today, policy.source_state,
            )
            if promote or demote or start:
                apply_trial_decisions_to_cache(policy.source_state, promote, demote, today, start)
                promoted, demoted = len(promote), len(demote)
    else:
        # Votes and the polling cursor survive an unrelated delivery failure.
        policy.feedback.article_source_map = policy.previous_article_sources

    if policy.feedback_usable:
        save_feedback(policy.feedback, policy.cache_dir, strict=policy.compact)
    save_source_state(policy.source_state, policy.cache_dir)
    save_stats(policy.source_stats, policy.cache_dir, active_sources={s.name for s in policy.config.enabled_sources})
    save_source_category_map(policy.config.enabled_sources, policy.cache_dir)
    return AppliedOutcome(promoted, demoted)
