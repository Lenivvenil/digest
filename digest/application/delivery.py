"""Apply confirmed delivery through attribution, deduplication and accounting owners.

Direct delivery retains its legacy persistence policy. Prepared publication owns
its receipt-bound accounting in application.prepared_delivery.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from digest.adapters.storage import delivery_state

if TYPE_CHECKING:
    from digest.config import Config
    from digest.domain.catalog.sources import SourceStateStore, SourceStats
    from digest.domain.delivery.outcomes import ArticleDeliveryResult
    from digest.domain.feedback.values import FeedbackStore
    from digest.radar.collector import Article, SourceFetchMetrics
    from digest.radar.summarizer import ArticleSummary


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


@dataclass(frozen=True)
class _DirectAccounting:
    """Separate transport confirmation, archive consumption and the output gate."""

    confirmed_hashes: set[str]
    archive_hashes: set[str]
    has_output: bool

    @property
    def consumed_hashes(self) -> set[str]:
        """Identities eligible for direct-run deduplication and source inclusion."""
        return self.confirmed_hashes | self.archive_hashes


def save_delivery_cache(cache: dict[str, str], compact: bool, cache_dir: str) -> None:
    """Retain strict compact writes and best-effort legacy card persistence."""
    if compact:
        delivery_state.save_delivery_cache(cache, cache_dir)
    else:
        from digest.radar import save_dedup_cache

        # The legacy owner uses its existing default cache location.
        save_dedup_cache(cache)


def apply_confirmed_outcome(policy: LegacyOutcomePolicy) -> AppliedOutcome:
    """Apply confirmed Telegram coverage and legacy eligible Markdown output.

    Markdown consumption never creates Telegram attribution. Prepared publication
    owns its receipt-bound accounting separately.
    """
    return _apply_legacy(policy)



def _direct_accounting(policy: LegacyOutcomePolicy) -> _DirectAccounting:
    """Decide direct-run consumption without changing state or observing time."""
    from digest.radar.collector import article_hash

    confirmed_hashes = set(policy.outcome.delivered_hashes)
    archive_hashes: set[str] = set()
    if (
        not policy.compact
        and policy.markdown_saved
        and (not getattr(policy.config.telegram, "required", False) or policy.telegram_complete)
    ):
        archive_hashes.update(
            article_hash(article.title, article.link)
            for category, articles in policy.articles_by_category.items()
            if category in policy.summarized_categories
            for article in articles
        )
        archive_hashes.update(article_hash(article.title, article.link) for article in policy.top_articles)
    return _DirectAccounting(confirmed_hashes, archive_hashes, policy.outcome.sent > 0 or policy.markdown_saved)


def _apply_legacy(policy: LegacyOutcomePolicy) -> AppliedOutcome:
    from digest.adapters.storage.feedback import save_feedback
    from digest.adapters.storage.sources import save_source_category_map, save_source_state, save_stats
    from digest.application.run_state import record_source_stats
    from digest.application.source_scoring import evaluate_trial_sources
    from digest.domain.catalog.source_rules import apply_trial_decisions_to_cache
    from digest.domain.feedback.rules import apply_delivery_attribution
    from digest.radar.collector import article_hash

    outcome = policy.outcome
    accounting = _direct_accounting(policy)
    consumed_hashes = accounting.consumed_hashes
    collected_hashes = {
        article_hash(article.title, article.link)
        for articles in policy.articles_by_category.values()
        for article in articles
    }
    # Keep collection timestamps and old entries; consume only qualifying output.
    consumed_cache = {
        key: timestamp
        for key, timestamp in policy.collected_cache.items()
        if key not in collected_hashes or key in consumed_hashes
    }
    record_source_stats(policy.source_stats, policy.fetch_metrics, policy.articles_by_category, consumed_hashes)
    promoted = demoted = 0
    if accounting.has_output:
        apply_delivery_attribution(
            policy.feedback,
            outcome.article_source_map,
            complete=policy.telegram_complete,
            contributing_sources=policy.contributing_sources,
            delivered_at=policy.delivered_at,
        )
        # Legacy order is seen -> feedback -> source state -> stats -> category map.
        # The existing owner functions keep their caught-versus-propagated failures.
        save_delivery_cache(consumed_cache, policy.compact, policy.cache_dir)
        if policy.config.adaptive.enabled:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            promote, demote, start = evaluate_trial_sources(
                policy.config.enabled_sources,
                policy.source_stats,
                today,
                policy.source_state,
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
