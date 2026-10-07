"""Direct digest execution: collect an approved portfolio, present, then publish.

Collection and publication are explicit phase handoffs. The collection record
still carries mutable feedback/scoring state for the existing legacy outcome
policy; separating those stores is later work, not a new persisted run context.
Markdown consumption, cards transport, and compact confirmation remain distinct.
"""
from __future__ import annotations

import dataclasses
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from digest.application import analysis, investigation, presentation, run_state
from digest.application.results import DigestPreview, Preview, RadarPreview, RunStats

if TYPE_CHECKING:
    from digest.adapters.models.execution import ModelExecution
    from digest.config import Config
    from digest.delivery.issue_guard import IssueGuard
    from digest.domain.catalog.articles import Article
    from digest.domain.catalog.sources import SourceStateStore, SourceStats
    from digest.domain.delivery.outcomes import IssueDeliveryResult
    from digest.domain.editorial.reviews import BlindReviewReport
    from digest.domain.feedback.values import FeedbackStore
    from digest.irritator import IrritatorStatus
    from digest.irritator.ranker import RankedSignal
    from digest.radar.collector import SourceFetchMetrics
    from digest.radar.summarizer import ArticleSummary


@dataclass(frozen=True)
class LegacyCollection:
    """Approved collection portfolio, observed inputs and pre-delivery state."""

    config: Config
    execution: ModelExecution
    compact: bool
    review_led_only: bool
    cache_dir: str
    source_state: SourceStateStore
    source_stats: dict[str, SourceStats]
    feedback_store: FeedbackStore
    feedback_usable: bool
    feedback_collected: int
    previous_article_sources: dict[str, str]
    effective_priorities: dict[str, int]
    feeds_count: int
    articles_by_category: dict[str, list[Article]]
    collected_cache: dict[str, str]
    fetch_metrics: dict[str, SourceFetchMetrics]
    total_articles: int
    contributing_sources: list[str]


@dataclass(frozen=True)
class LegacyPublication:
    """Final presentation and canonical category coverage for direct publication."""

    combined: str
    cards: list[ArticleSummary]
    ranked: list[RankedSignal]
    irritator_status: IrritatorStatus
    review_report: BlindReviewReport | None
    summarized_categories: set[str]


async def _collect_legacy(
    config: Config, config_path: str, dry_run: bool, radar_only: bool, feedback_precollected: bool,
    *, execution: ModelExecution,
) -> LegacyCollection:
    """Apply feedback and approved sources before observing the current portfolio."""
    from digest._util import cleanup_stale_tmp
    from digest.adapters.storage.sources import load_source_state, load_stats
    from digest.application.source_scoring import calculate_effective_priorities
    from digest.domain.catalog.source_rules import calculate_feedback_priorities
    from digest.domain.feedback.rules import get_source_feedback_score
    from digest.radar import AllFeedsFailedError, collect

    compact = getattr(config.telegram, "delivery_mode", "cards") == "compact"
    review_led_only = _review_led(config)
    cache_dir = ".cache"
    source_state = load_source_state(cache_dir)
    cleanup_stale_tmp(Path(cache_dir))
    source_stats = load_stats(cache_dir)
    feedback_store, feedback_usable, feedback_collected = await run_state.collect_run_feedback(
        config, cache_dir, dry_run, feedback_precollected,
    )
    run_state.require_attribution_store(compact and not dry_run and not radar_only, feedback_usable)
    config, execution = run_state.apply_pending_approvals(
        config, config_path, cache_dir, feedback_store, enabled=feedback_usable and not dry_run,
        execution=execution,
    )
    feeds_count = len(config.enabled_sources)
    saved_article_source_map = dict(feedback_store.article_source_map)
    feedback_scores: dict[str, float] = {}
    for source in config.enabled_sources:
        score = get_source_feedback_score(feedback_store, source.name, now=datetime.now(tz=timezone.utc))
        if score is not None:
            feedback_scores[source.name] = score
    if config.adaptive.enabled:
        effective_priorities = calculate_effective_priorities(
            config.effective_sources(source_state), source_stats, feedback_scores, config.adaptive,
        )
    else:
        effective_priorities = calculate_feedback_priorities(
            config.effective_sources(source_state), feedback_scores, config.adaptive,
        )

    run_config = dataclasses.replace(
        config,
        sources=[s for s in config.sources if s.enabled and not source_state.is_demoted(s.name)],
    )
    fetch_metrics: dict[str, SourceFetchMetrics] = {}
    try:
        articles_by_category, cache = await collect(
            run_config, effective_priorities=effective_priorities, fetch_metrics=fetch_metrics,
        )
    except AllFeedsFailedError:
        run_state.save_failed_run_stats(
            source_stats, fetch_metrics, cache_dir,
            {s.name for s in config.enabled_sources}, dry_run=dry_run,
        )
        raise

    total_articles = sum(len(arts) for arts in articles_by_category.values())
    contributing_sources = sorted({a.source for articles in articles_by_category.values() for a in articles})

    return LegacyCollection(
        config=config, execution=execution, compact=compact, review_led_only=review_led_only,
        cache_dir=cache_dir, source_state=source_state, source_stats=source_stats, feedback_store=feedback_store,
        feedback_usable=feedback_usable, feedback_collected=feedback_collected,
        previous_article_sources=saved_article_source_map, effective_priorities=effective_priorities,
        feeds_count=feeds_count, articles_by_category=articles_by_category, collected_cache=cache,
        fetch_metrics=fetch_metrics, total_articles=total_articles, contributing_sources=contributing_sources,
    )


async def run_legacy(
    config: Config, config_path: str, dry_run: bool, radar_only: bool, verbose: bool, *, execution: ModelExecution,
    started_at: float, emit_preview: Callable[[Preview], None],
    feedback_precollected: bool = False, issue_guard: IssueGuard | None = None,
) -> RunStats:
    """Choose preview or publication after collection and canonical analysis."""
    from digest.adapters.storage.feedback import save_feedback
    from digest.adapters.storage.sources import save_stats
    from digest.application.delivery import save_delivery_cache

    collected = await _collect_legacy(
        config, config_path, dry_run, radar_only, feedback_precollected, execution=execution,
    )
    config, execution, cache_dir = collected.config, collected.execution, collected.cache_dir
    articles_by_category, fetch_metrics = collected.articles_by_category, collected.fetch_metrics
    source_stats, feedback_store = collected.source_stats, collected.feedback_store
    feeds_count, total_articles = collected.feeds_count, collected.total_articles
    feedback_collected = collected.feedback_collected
    compact, review_led_only = collected.compact, collected.review_led_only
    logger = logging.getLogger(__name__)
    if not articles_by_category:
        logger.info("No eligible articles in this processing packet. Nothing to summarize.")
        if not dry_run:
            save_delivery_cache(collected.collected_cache, compact, cache_dir)
            run_state.record_source_stats(source_stats, fetch_metrics, articles_by_category, set())
            save_stats(source_stats, cache_dir, active_sources={s.name for s in config.enabled_sources})
            if collected.feedback_usable:
                save_feedback(feedback_store, cache_dir)
        return _empty_run_stats(feeds_count, feedback_collected)

    summaries, trends, top_articles, review_report = await analysis.analyze_articles(
        articles_by_category, config, execution=execution,
    )
    if _analysis_missing(summaries, top_articles, review_report):
        logger.error("All category summarizations failed.")
        run_state.save_failed_run_stats(
            source_stats, fetch_metrics, cache_dir,
            {s.name for s in config.enabled_sources}, dry_run=dry_run,
        )
        await _notify_summaries_failed(
            dry_run=dry_run, telegram_enabled=config.telegram.enabled and not compact,
        )
        return _empty_run_stats(feeds_count, feedback_collected, total_articles)

    combined = presentation.combined_summary(summaries, trends, review_led_only, config.radar.language)
    combined = presentation.publication_intro(combined, review_report, config)

    if radar_only:
        combined, top_articles = await presentation.primary_presentation(
            combined, top_articles, config, Path(cache_dir) / "translations", dry_run, execution=execution,
        )
        emit_preview(RadarPreview(
            combined, top_articles, bool(getattr(getattr(config, "translation", None), "enabled", False)),
        ))
        return RunStats(
            feeds_fetched=feeds_count, new_articles=total_articles,
            digest_length=len(combined), telegram_sent=False,
            telegram_partial=False, markdown_saved=False, markdown_path="",
            feedback_collected=feedback_collected,
        )

    # Irritator pipeline
    if review_led_only:
        from digest.irritator import IrritatorStatus

        all_ranked: list[RankedSignal] = []
        irritator_status = IrritatorStatus(presentation.deferred_review_status(config.radar.language), "deferred")
    else:
        _, all_ranked, irritator_status = await investigation.run_irritator(
            summaries, config, verbose, execution=execution,
        )

    combined, top_articles, all_ranked = await presentation.publication_presentation(
        combined, top_articles, all_ranked, config, Path(cache_dir) / "translations", dry_run,
        execution=execution,
    )

    # Dry-run output
    if dry_run:
        emit_preview(DigestPreview(combined, top_articles, all_ranked, irritator_status, review_report))
        return RunStats(
            feeds_fetched=feeds_count, new_articles=total_articles,
            digest_length=len(combined), telegram_sent=False,
            telegram_partial=False, markdown_saved=False, markdown_path="",
            feedback_collected=feedback_collected,
        )

    publication = LegacyPublication(
        combined, top_articles, all_ranked, irritator_status, review_report,
        {summary.category for summary in summaries},
    )
    return await _publish_legacy(collected, publication, issue_guard, started_at)


async def _publish_legacy(
    collected: LegacyCollection, publication: LegacyPublication,
    issue_guard: IssueGuard | None, started_at: float,
) -> RunStats:
    """Archive first, send with scenario policy, apply known coverage, then finish."""
    from digest.application.delivery import LegacyOutcomePolicy, apply_confirmed_outcome
    from digest.delivery import send_article_cards, write_digest
    from digest.delivery.telegram import send_compact_issue
    from digest.domain.delivery.outcomes import ArticleDeliveryResult

    config, compact, cache_dir = collected.config, collected.compact, collected.cache_dir
    articles_by_category = collected.articles_by_category
    feeds_count, total_articles = collected.feeds_count, collected.total_articles
    combined, top_articles, all_ranked = publication.combined, publication.cards, publication.ranked
    irritator_status, review_report = publication.irritator_status, publication.review_report
    logger = logging.getLogger(__name__)
    md_path = write_digest(
        combined, config,
        top_articles=top_articles or None,
        ranked_signals=all_ranked or None,
        review_report=review_report,
        irritator_status=irritator_status,
        sources_count=len(articles_by_category), articles_count=total_articles,
    )
    markdown_saved = md_path is not None
    markdown_path = str(md_path) if md_path else ""

    telegram_sent = False
    telegram_partial = False
    card_delivery = ArticleDeliveryResult()
    issue_delivery: IssueDeliveryResult | None = None
    delivered_at = datetime.now(tz=timezone.utc)
    if config.telegram.enabled:
        try:
            if compact:
                assert issue_guard is not None
                issue_delivery = await send_compact_issue(
                    top_articles, config, notice=combined, before_send=issue_guard.mark_sending,
                )
                card_delivery = issue_delivery
                telegram_sent = issue_delivery.complete
                telegram_partial = bool(issue_delivery.confirmed_chunks) and not issue_delivery.complete
            else:
                card_delivery = await send_article_cards(
                    articles_by_category, config, top_articles=top_articles,
                )
                telegram_sent = card_delivery.sent > 0 and card_delivery.failed == 0
                telegram_partial = card_delivery.sent > 0 and card_delivery.failed > 0
            delivered_at = datetime.now(tz=timezone.utc)
            if not compact:
                nano_status = _build_nano_status(
                    feeds_count, total_articles,
                    sum(m.fetch_ok for m in collected.fetch_metrics.values()),
                    sum(not m.fetch_ok for m in collected.fetch_metrics.values()),
                    collected.source_stats, config, collected.effective_priorities,
                )
                await _legacy_delivery_extras(
                    top_articles, all_ranked, irritator_status, review_report, config,
                    collected.review_led_only, nano_status,
                )

        except Exception as exc:
            logger.warning("Telegram delivery failed (non-critical): %s", exc)

    telegram_required = getattr(config.telegram, "required", False)
    applied = apply_confirmed_outcome(LegacyOutcomePolicy(
        outcome=card_delivery,
        config=config,
        cache_dir=cache_dir,
        compact=compact,
        telegram_complete=telegram_sent,
        markdown_saved=markdown_saved,
        delivered_at=delivered_at,
        contributing_sources=collected.contributing_sources,
        feedback=collected.feedback_store,
        feedback_usable=collected.feedback_usable,
        previous_article_sources=collected.previous_article_sources,
        collected_cache=collected.collected_cache,
        articles_by_category=articles_by_category,
        summarized_categories=publication.summarized_categories,
        top_articles=top_articles,
        fetch_metrics=collected.fetch_metrics,
        source_stats=collected.source_stats,
        source_state=collected.source_state,
    ))
    _finish_compact(issue_guard, issue_delivery)

    return RunStats(
        feeds_fetched=feeds_count, new_articles=total_articles,
        digest_length=len(combined), telegram_sent=telegram_sent,
        telegram_partial=telegram_partial, markdown_saved=markdown_saved,
        markdown_path=markdown_path, sources_promoted=applied.sources_promoted,
        sources_demoted=applied.sources_demoted, feedback_collected=collected.feedback_collected,
        duration_seconds=time.monotonic() - started_at,
        required_delivery_failed=telegram_required and not telegram_sent,
        review_status=review_report.status if review_report is not None else "not_requested",
        review_checkpoint=str(md_path.with_suffix(".review.json")) if md_path and review_report is not None else "",
    )


def _build_nano_status(
    feeds_count: int,
    total_articles: int,
    ok_count: int,
    err_count: int,
    source_stats: dict[str, SourceStats],
    config: Any,
    effective_priorities: dict[str, int] | None,
) -> str:
    """Build a two-line status footer for the digest message."""
    from digest.application.source_scoring import calculate_score

    provider_names: list[str] = []
    seen: set[str] = set()
    for pc in config.llm.providers:
        if pc.name not in seen:
            provider_names.append(pc.name)
            seen.add(pc.name)
    for route in getattr(config.llm, "routing", []):
        if route.provider not in seen:
            provider_names.append(route.provider)
            seen.add(route.provider)
    models_str = ", ".join(provider_names) if provider_names else config.llm.model

    line1 = (
        f"\U0001f4ca {feeds_count} src | {total_articles} art | "
        f"{ok_count} ok / {err_count} err | {models_str}"
    )

    promoted_count = 0
    demoted_count = 0
    if effective_priorities:
        for source in config.enabled_sources:
            ep = effective_priorities.get(source.name)
            if ep is not None:
                if ep > source.priority:
                    promoted_count += 1
                elif ep < source.priority:
                    demoted_count += 1

    scores = [calculate_score(s) for s in source_stats.values() if s.total_fetches > 0]
    avg_score = sum(scores) / len(scores) if scores else 0.0

    line2 = (
        f"\U0001f4c8 {promoted_count} \u2191 | {demoted_count} \u2193 | "
        f"avg score: {avg_score:.2f}"
    )
    return f"{line1}\n{line2}"


async def _notify_skipped_cards(top_articles: list[Any]) -> None:
    """Surface a Telegram notice when the LLM picker returned no cards.

    Prevents the perception of a 'skipped digest' when summaries were still
    written to markdown but no cards landed in Telegram.
    """
    if top_articles:
        return
    await _send_status_message(
        "⚠️ Radar: LLM picker returned no top articles "
        "— cards skipped; summary saved to markdown."
    )


async def _notify_summaries_failed(*, dry_run: bool, telegram_enabled: bool) -> None:
    """Surface a Telegram notice when all summarization providers failed.

    Prevents a 'silently skipped' digest when the whole pipeline aborted
    before any markdown or card landed.
    """
    if dry_run or not telegram_enabled:
        return
    await _send_status_message(
        "❌ Radar: all LLM providers for role=summarize failed "
        "— digest not assembled (see workflow logs)."
    )


def _review_status_line(report: BlindReviewReport | None, language: str = "en") -> str:
    if report is None:
        return ""
    russian = language == "ru"
    statuses = ({"ok": "ответ принят", "partial": "часть карточек принята", "abstained": "нет выбора",
                 "invalid": "ответ не прошёл проверку", "unavailable": "ответ не получен"} if russian else
                {"ok": "accepted", "partial": "partially accepted", "abstained": "no selection",
                 "invalid": "response failed validation", "unavailable": "no response"})
    details = []
    for review in report.reviews:
        pending = review.error == "pending_independent_review"
        state = (("ожидает отдельного этапа" if russian else "waiting for separate stage")
                 if pending else statuses[review.status])
        details.append(f"{review.model}: {state}")
    complete = report.status == "complete"
    heading = (("Сравнение моделей завершено" if complete else "Сравнение моделей ещё не завершено") if russian else
               ("Model comparison complete" if complete else "Model comparison incomplete"))
    return "\n" + heading + ". " + "; ".join(details)


async def _legacy_delivery_extras(
    cards: list[ArticleSummary], ranked: list[Any], irritator_status: IrritatorStatus,
    review_report: BlindReviewReport | None, config: Any, review_led_only: bool, nano_status: str,
) -> None:
    from digest.delivery import send_counter_signals
    from digest.delivery.telegram import send_status_message

    if not review_led_only:
        await _notify_skipped_cards(cards)
        await send_counter_signals(ranked, config, irritator_status=irritator_status)
    nano_status += _review_status_line(review_report, config.radar.language)
    if review_led_only:
        nano_status += "\n" + irritator_status.text
    await send_status_message(nano_status, disable_notification=True)


def _finish_compact(guard: IssueGuard | None, result: IssueDeliveryResult | None) -> None:
    if guard is not None and result is not None and guard.state == "sending":
        outcome = ("confirmed" if result.complete else "unknown" if result.outcome == "unknown" else
                   "partial" if result.confirmed_chunks else "failed_no_delivery")
        guard.finish(outcome, accepted_count=result.confirmed_chunks, attempted_count=result.attempted_chunks)


def _empty_run_stats(feeds: int, feedback: int, articles: int = 0) -> RunStats:
    return RunStats(feeds, articles, 0, False, False, False, "", feedback_collected=feedback)


def _analysis_missing(summaries: list[Any], cards: list[Any], report: Any) -> bool:
    return not summaries and not cards and report is None


def _review_led(config: Any) -> bool:
    return bool(getattr(getattr(config, "review", None), "enabled", False) and config.review.review_led_only)


async def _send_status_message(text: str) -> bool:
    """Best-effort notice policy; the Telegram adapter owns HTTP and retries."""
    from digest.delivery.telegram import send_status_message

    try:
        return await send_status_message(text)
    except Exception as exc:
        logging.getLogger(__name__).warning("Failed to send status message: %s", exc)
        return False
