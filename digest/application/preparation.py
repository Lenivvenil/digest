"""Prepare canonical work and freeze editions without entering a sending workflow.

Recovery precedes new collection. Ordinary editions and experimental source work
share candidate acquisition, then have distinct terminal application operations.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from digest.application import analysis, run_state
from digest.application.results import RunStats

if TYPE_CHECKING:
    from digest.adapters.models.execution import ModelExecution
    from digest.closing import ClosingDecision
    from digest.config import Config
    from digest.domain.catalog.sources import SourceStateStore, SourceStats
    from digest.domain.editorial.attempts import ResolvedReview
    from digest.domain.editorial.candidates import CandidatePacket, CandidateProgress
    from digest.domain.editorial.reviews import BlindReviewReport
    from digest.domain.feedback.values import FeedbackStore
    from digest.preparation import AcceptedPreparation, PreparationSnapshot
    from digest.radar.collector import Article, CollectionInventory, SourceFetchMetrics
    from digest.radar.summarizer import ArticleSummary, CategorySummary


class AnalysisMode(Enum):
    REVIEW_LED = "review_led"
    CATEGORY = "category"


class EmptyWork(Enum):
    NO_CANDIDATES = "no_candidates"
    CATEGORY_ANALYSIS_FAILED = "category_analysis_failed"


@dataclass
class PreparationRun:
    """Feedback/approval effects completed before inspecting recoverable work."""

    config: Config
    execution: ModelExecution
    source_state: SourceStateStore
    source_stats: dict[str, SourceStats]
    feedback_store: FeedbackStore
    feedback_usable: bool
    feedback_collected: int


@dataclass
class CollectedArticles:
    source_config: Config
    articles: dict[str, list[Article]]
    fetch_metrics: dict[str, SourceFetchMetrics]
    collection_failed: bool
    collected_articles: int

    @property
    def article_count(self) -> int:
        return sum(len(items) for items in self.articles.values())


@dataclass
class CandidatePool:
    progress: CandidateProgress
    inventory: CollectionInventory
    delivered: dict[str, str]


@dataclass
class CandidateWork:
    progress: CandidateProgress
    packet: CandidatePacket


@dataclass
class ReviewedCandidates:
    work: CandidateWork
    result: ResolvedReview
    cards: list[ArticleSummary]

    @property
    def report(self) -> BlindReviewReport:
        return self.result.report


@dataclass
class CategoryAnalysis:
    """Supported legacy analysis; an optional review is content, not a mode flag."""

    summaries: list[CategorySummary]
    trends: str | None
    cards: list[ArticleSummary]
    report: BlindReviewReport | None


def _empty_stats(feeds: int, feedback: int, articles: int = 0) -> RunStats:
    return RunStats(feeds, articles, 0, False, False, False, "", feedback_collected=feedback)


def _candidate_setup(cache_dir: str) -> CandidatePool:
    from digest.adapters.storage.candidate_progress import load_candidate_progress
    from digest.edition_runtime import _strict_cache
    from digest.radar.collector import CollectionInventory

    return CandidatePool(
        load_candidate_progress(cache_dir),
        CollectionInventory(),
        _strict_cache(Path(cache_dir) / "seen_articles.json"),
    )


def _candidate_inputs(
    pool: CandidatePool,
    run_config: Config,
    config: Config,
    priorities: dict[str, int],
    cache_dir: str,
    collection_failed: bool,
) -> tuple[CandidateWork | EmptyWork, dict[str, list[Article]]]:
    progress, inventory, delivered = pool.progress, pool.inventory, pool.delivered
    from digest.adapters.storage.candidate_progress import MAX_BYTES, progress_size
    from digest.application.candidate_lifecycle import checkpoint_candidates, ensure_report_accounting
    from digest.application.candidate_review import begin_packet, merge_candidates, plan_packet
    from digest.domain.editorial.candidate_policy import packet_articles, pending_completed_report
    from digest.radar import AllFeedsFailedError
    from digest.radar.collector import _prune_cache

    merge_candidates(
        progress,
        inventory.eligible_articles(),
        run_config,
        _prune_cache(delivered),
        priorities,
        inventory=inventory,
        delivery_history=delivered,
        cache_dir=cache_dir,
    )
    from digest.reading_preparation import deferred_source_reports

    deferred = (
        deferred_source_reports(progress, Path(cache_dir), config)
        if getattr(getattr(config, "reading_brief", None), "enabled", False)
        else set()
    )
    report = pending_completed_report(progress, skip_reports=deferred)
    packet = (
        next(item for item in reversed(progress.packets) if item.report == report)
        if report is not None
        else plan_packet(progress, config)
    )
    if packet is not None and report is None:
        begin_packet(progress, packet, cache_dir, skipped_empty_reports=deferred)
    else:
        checkpoint_candidates(progress, cache_dir, skipped_empty_reports=deferred)
        if report is not None:
            ensure_report_accounting(progress, report, cache_dir)
    logging.getLogger(__name__).info(
        "Candidate accounting: %d registered, %d eligible, %d never planned, %d technical pending, "
        "%d unselected without editorial reason, %d metadata not-selected, %d duplicates; next packet %d items",
        len(progress.candidates),
        sum(item.eligible for item in progress.candidates.values()),
        sum(item.status == "not_presented" for item in progress.candidates.values()),
        sum(item.status == "technical_pending" for item in progress.candidates.values()),
        sum(item.status == "not_selected_without_editorial_reason" for item in progress.candidates.values()),
        sum(item.status == "not_selected" for item in progress.candidates.values()),
        sum(item.status == "duplicate" for item in progress.candidates.values()),
        len(packet.evidence.items) if packet is not None else 0,
    )
    stored_bytes = progress_size(progress, cache_dir)
    logging.getLogger(__name__).info(
        "Candidate storage: %d bytes used; %d bytes remaining", stored_bytes, MAX_BYTES - stored_bytes
    )
    articles = packet_articles(packet) if packet is not None else {}
    if collection_failed and packet is None:
        raise AllFeedsFailedError("All feeds failed and no eligible saved candidate packet is available.")
    return (CandidateWork(progress, packet) if packet is not None else EmptyWork.NO_CANDIDATES), articles


async def _review_candidates(
    work: CandidateWork,
    articles: dict[str, list[Article]],
    config: Config,
    cache_dir: str,
    *,
    execution: ModelExecution,
) -> ReviewedCandidates:
    from digest.application.candidate_review import reconcile_packet
    from digest.application.review import run_primary_review
    from digest.closing import decide_closing, save_closing
    from digest.domain.editorial.attempts import restore_review
    from digest.presentation.review import primary_cards

    progress, packet = work.progress, work.packet
    if packet.report is not None:
        result = restore_review(packet.report, packet.disposition_attempts)
    else:
        result = await run_primary_review(articles, config, execution=execution)
        reconcile_packet(progress, packet, result, config, cache_dir)
        if getattr(getattr(config, "closing", None), "enabled", False):
            try:
                decision = decide_closing(result, packet, config.closing, config.sources)
                save_closing(decision, result.report, cache_dir)
            except (OSError, ValueError, TypeError, KeyError):
                logging.getLogger(__name__).warning(
                    "Optional closing capture unavailable; main review remains accepted.")
    cards = primary_cards(
        result,
        articles,
        config.radar.language,
        max_cards=config.review.max_selections,
        include_attribution=config.telegram.delivery_mode != "compact",
    )
    return ReviewedCandidates(work, result, cards)


def _save_prepared_fetch_stats(
    source_stats: dict[str, SourceStats],
    fetch_metrics: dict[str, SourceFetchMetrics],
    articles: dict[str, list[Article]],
    config: Config,
    cache_dir: str,
    already_failed: bool,
) -> None:
    from digest.adapters.storage.sources import save_stats

    if not already_failed:
        run_state.record_source_stats(source_stats, fetch_metrics, articles, set())
        save_stats(source_stats, cache_dir, active_sources={source.name for source in config.enabled_sources})


def _preparation_closing(
    cards: list[ArticleSummary],
    result: ResolvedReview | None,
    articles: dict[str, list[Article]],
    config: Config,
    cache_dir: str,
) -> tuple[list[ArticleSummary], ClosingDecision | None]:
    from digest.application.review_request import eligible_ids
    from digest.closing import ClosingDecision, load_closing
    from digest.presentation.review import primary_cards

    if not getattr(getattr(config, "closing", None), "enabled", False):
        return cards, None
    report = result.report if result is not None else None
    decision = (
        load_closing(report, cache_dir)
        if report is not None
        else ClosingDecision("incomplete", "missing_delivery_review")
    )
    if decision.provenance is not None and report is not None and result is not None:
        occurrence = decision.provenance.occurrence
        if decision.provenance.evidence_id not in eligible_ids(
            report.evidence, config.closing, config.sources
        ) or not any(
            (binding.name, binding.url, binding.category)
            == (occurrence.source, occurrence.source_url, occurrence.category)
            for binding in config.closing.approved_sources
        ):
            return cards, ClosingDecision("unavailable", "source_no_longer_eligible_for_closing")
        main_cards = primary_cards(
            result,
            articles,
            config.radar.language,
            max_cards=config.review.max_selections,
            include_attribution=False,
            exclude_ids=frozenset({decision.provenance.evidence_id}),
        )
        if not main_cards:
            return cards, ClosingDecision("unavailable", "closing_would_empty_main_selection")
        cards = main_cards
    return cards, decision


def _handoff_candidate(work: ReviewedCandidates, accepted: AcceptedPreparation, cache_dir: str) -> None:
    from digest.application.candidate_review import mark_prepared

    if accepted.snapshot.review_report != work.report:
        raise ValueError("Accepted preparation does not bind this candidate review; handoff blocked.")
    mark_prepared(work.work.progress, work.work.packet.evidence.bundle_id, cache_dir)


async def _start_preparation(
    config: Config,
    config_path: str,
    *,
    execution: ModelExecution,
    feedback_precollected: bool,
) -> PreparationRun:
    from digest._util import cleanup_stale_tmp
    from digest.adapters.storage.sources import load_source_state, load_stats

    cache_dir = ".cache"
    source_state = load_source_state(cache_dir)
    cleanup_stale_tmp(Path(cache_dir))
    source_stats = load_stats(cache_dir)
    feedback_store, feedback_usable, feedback_collected = await run_state.collect_run_feedback(
        config,
        cache_dir,
        False,
        feedback_precollected,
    )
    run_state.require_attribution_store(config.telegram.delivery_mode == "compact", feedback_usable)
    config, execution = run_state.apply_pending_approvals(
        config,
        config_path,
        cache_dir,
        feedback_store,
        enabled=feedback_usable,
        execution=execution,
    )
    return PreparationRun(
        config, execution, source_state, source_stats, feedback_store, feedback_usable, feedback_collected
    )


async def _collect_and_review(
    run: PreparationRun,
    mode: AnalysisMode,
    config_path: str,
    started_at: float,
) -> tuple[CollectedArticles, ReviewedCandidates | CategoryAnalysis | EmptyWork]:
    """Collect before candidate-report replay; keep mode-specific work concrete."""
    from digest.adapters.storage.feedback import save_feedback
    from digest.adapters.storage.sources import save_stats
    from digest.application.source_scoring import calculate_effective_priorities
    from digest.domain.catalog.source_rules import calculate_feedback_priorities
    from digest.domain.feedback.rules import get_source_feedback_score
    from digest.radar import AllFeedsFailedError, collect
    from digest.reading_preparation import reading_deadline, setup_reading_budget

    config, execution, cache_dir = run.config, run.execution, ".cache"
    setup_reading_budget(config, execution=execution)
    feedback_scores = {}
    for source in config.enabled_sources:
        score = get_source_feedback_score(run.feedback_store, source.name, now=datetime.now(tz=timezone.utc))
        if score is not None:
            feedback_scores[source.name] = score
    if config.adaptive.enabled:
        priorities = calculate_effective_priorities(
            config.effective_sources(run.source_state),
            run.source_stats,
            feedback_scores,
            config.adaptive,
        )
    else:
        priorities = calculate_feedback_priorities(
            config.effective_sources(run.source_state),
            feedback_scores,
            config.adaptive,
        )
    source_config = dataclasses.replace(
        config,
        sources=[
            source for source in config.sources if source.enabled and not run.source_state.is_demoted(source.name)
        ],
    )
    fetch_metrics: dict[str, SourceFetchMetrics] = {}
    pool = _candidate_setup(cache_dir) if mode is not AnalysisMode.CATEGORY else None
    collection_failed = False
    try:
        articles, _cache = await collect(
            source_config,
            effective_priorities=priorities,
            fetch_metrics=fetch_metrics,
            **({"inventory": pool.inventory} if pool is not None else {}),
        )
    except AllFeedsFailedError:
        run_state.save_failed_run_stats(
            run.source_stats,
            fetch_metrics,
            cache_dir,
            {source.name for source in config.enabled_sources},
            dry_run=False,
        )
        if mode is AnalysisMode.CATEGORY:
            raise
        collection_failed = True
        articles = {}
    collected_articles = sum(len(items) for items in articles.values())
    work: CandidateWork | EmptyWork = EmptyWork.NO_CANDIDATES
    if pool is not None:
        work, articles = _candidate_inputs(
            pool,
            source_config,
            config,
            priorities,
            cache_dir,
            collection_failed,
        )
    collected = CollectedArticles(source_config, articles, fetch_metrics, collection_failed, collected_articles)
    if not articles:
        from digest.reconciliation_checkpoint import prepare_current_batch

        await prepare_current_batch(
            pool.progress if pool is not None else None,
            source_config,
            config_path,
            started_at,
            execution=execution,
        )
        logging.getLogger(__name__).info("No eligible articles in this processing packet. Nothing to summarize.")
        run_state.record_source_stats(run.source_stats, fetch_metrics, articles, set())
        save_stats(run.source_stats, cache_dir, active_sources={source.name for source in config.enabled_sources})
        if run.feedback_usable:
            save_feedback(run.feedback_store, cache_dir)
        return collected, EmptyWork.NO_CANDIDATES

    deadline = reading_deadline(config, started_at) if config.reading_brief.enabled else None
    async with asyncio.timeout_at(deadline):
        if isinstance(work, CandidateWork):
            return collected, await _review_candidates(work, articles, config, cache_dir, execution=execution)
        summaries, trends, cards, report = await analysis.analyze_articles(articles, config, execution=execution)
    if not summaries and not cards and report is None:
        logging.getLogger(__name__).error("All category summarizations failed.")
        run_state.save_failed_run_stats(
            run.source_stats,
            fetch_metrics,
            cache_dir,
            {source.name for source in config.enabled_sources},
            dry_run=False,
        )
        # Legacy failure and no candidates retain the same public no_ready projection.
        return collected, EmptyWork.CATEGORY_ANALYSIS_FAILED
    return collected, CategoryAnalysis(summaries, trends, cards, report)


def _snapshot(
    work: ReviewedCandidates | CategoryAnalysis,
    collected: CollectedArticles,
    config: Config,
) -> PreparationSnapshot:
    from digest.application.presentation import combined_summary, publication_intro
    from digest.domain.editorial.attempts import restore_review
    from digest.preparation import PreparationSnapshot

    result = (work.result if isinstance(work, ReviewedCandidates)
              else restore_review(work.report) if work.report is not None else None)
    cards, closing = _preparation_closing(work.cards, result, collected.articles, config, ".cache")
    summaries = work.summaries if isinstance(work, CategoryAnalysis) else []
    trends = work.trends if isinstance(work, CategoryAnalysis) else None
    combined = combined_summary(summaries, trends, isinstance(work, ReviewedCandidates), config.radar.language)
    combined = publication_intro(combined, result, config)
    return PreparationSnapshot(
        top_articles=cards,
        summaries=summaries,
        combined=combined,
        review_report=work.report,
        source_count=len(collected.articles),
        article_count=collected.article_count,
        contributing_sources=sorted({article.source for items in collected.articles.values() for article in items}),
        closing=closing,
    )


def _empty_work_stats(work: EmptyWork, run: PreparationRun, collected: CollectedArticles) -> RunStats:
    articles = collected.article_count if work is EmptyWork.CATEGORY_ANALYSIS_FAILED else 0
    return _empty_stats(len(run.config.enabled_sources), run.feedback_collected, articles)


async def _prepare_category_edition(
    work: CategoryAnalysis,
    collected: CollectedArticles,
    run: PreparationRun,
    *,
    verbose: bool,
    publication_date: date | None,
) -> RunStats:
    """Explicit legacy boundary: preserve its save/no-readback and empty-success behavior."""
    from digest.adapters.storage.sources import save_source_category_map
    from digest.domain.editorial.attempts import restore_review
    from digest.edition_runtime import finish_preparation, save_accepted_preparation

    snapshot = _snapshot(work, collected, run.config)
    # Preserve the category API's empty-result decision separately from strict
    # candidate acceptance, including historically reordered review records.
    selection_complete = (
        bool(snapshot.top_articles)
        or snapshot.review_report is None
        or (restore_review(snapshot.review_report).chosen.review.status == "abstained")
    )
    if selection_complete:
        save_accepted_preparation(snapshot, cache_dir=".cache", publication_date=publication_date)
    _save_prepared_fetch_stats(
        run.source_stats,
        collected.fetch_metrics,
        collected.articles,
        run.config,
        ".cache",
        collected.collection_failed,
    )
    save_source_category_map(run.config.enabled_sources, ".cache")
    return await finish_preparation(
        snapshot,
        run.config,
        run.feedback_collected,
        verbose=verbose,
        publication_date=publication_date,
        selection_complete=selection_complete,
        execution=run.execution,
    )


async def prepare_edition(
    config: Config,
    config_path: str,
    *,
    execution: ModelExecution,
    verbose: bool,
    feedback_precollected: bool,
    publication_date: date | None,
    started_at: float,
) -> RunStats:
    """Feedback/approvals → recovery → review → verified acceptance → presentation/freeze."""
    from digest.adapters.storage.sources import save_source_category_map
    from digest.edition_runtime import (
        ExistingEdition,
        IncompleteSelection,
        NoEdition,
        accept_preparation,
        preparation_stats,
        present_preparation,
        recover_preparation,
    )
    from digest.preparation import AcceptedPreparation

    run = await _start_preparation(
        config,
        config_path,
        execution=execution,
        feedback_precollected=feedback_precollected,
    )
    recovered = recover_preparation(publication_date)
    if isinstance(recovered, ExistingEdition):
        return preparation_stats(recovered, run.feedback_collected)
    if isinstance(recovered, AcceptedPreparation):
        presented = await present_preparation(
            recovered,
            run.config,
            execution=run.execution,
            verbose=verbose,
            publication_date=publication_date,
        )
        return preparation_stats(presented, run.feedback_collected)

    mode = (
        AnalysisMode.REVIEW_LED
        if run.config.review.enabled and run.config.review.review_led_only
        else AnalysisMode.CATEGORY
    )
    collected, work = await _collect_and_review(run, mode, config_path, started_at)
    if isinstance(work, EmptyWork):
        return _empty_work_stats(work, run, collected)
    if isinstance(work, CategoryAnalysis):
        stats = await _prepare_category_edition(
            work,
            collected,
            run,
            verbose=verbose,
            publication_date=publication_date,
        )
    else:
        accepted = accept_preparation(
            _snapshot(work, collected, run.config),
            work.result,
            cache_dir=".cache",
            publication_date=publication_date,
        )
        if isinstance(accepted, AcceptedPreparation):
            _handoff_candidate(work, accepted, ".cache")
        _save_prepared_fetch_stats(
            run.source_stats,
            collected.fetch_metrics,
            collected.articles,
            run.config,
            ".cache",
            collected.collection_failed,
        )
        save_source_category_map(run.config.enabled_sources, ".cache")
        if isinstance(accepted, IncompleteSelection):
            logging.getLogger(__name__).error(
                "Selection did not complete; candidate evidence remains pending and no edition is ready."
            )
            presented = NoEdition("selection_incomplete", accepted.review_status)
        else:
            presented = await present_preparation(
                accepted,
                run.config,
                execution=run.execution,
                verbose=verbose,
                publication_date=publication_date,
            )
        stats = preparation_stats(presented, run.feedback_collected)
    stats.feeds_fetched = len(collected.fetch_metrics)
    stats.new_articles = collected.collected_articles
    stats.duration_seconds = time.monotonic() - started_at
    return stats


async def prepare_sources(
    config: Config,
    config_path: str,
    *,
    execution: ModelExecution,
    verbose: bool,
    feedback_precollected: bool,
    publication_date: date | None,
    started_at: float,
) -> RunStats:
    """Experimental compatibility path ends in a technical handoff, not an edition."""
    from digest.edition_runtime import ExistingEdition, preparation_stats, present_preparation, recover_preparation
    from digest.preparation import AcceptedPreparation
    from digest.reading_preparation import prepare_selected_sources, reading_deadline
    from digest.reconciliation_checkpoint import prepare_current_batch

    run = await _start_preparation(
        config,
        config_path,
        execution=execution,
        feedback_precollected=feedback_precollected,
    )
    recovered = recover_preparation(publication_date)
    if isinstance(recovered, ExistingEdition):
        return preparation_stats(recovered, run.feedback_collected)
    if isinstance(recovered, AcceptedPreparation):
        presented = await present_preparation(
            recovered,
            run.config,
            execution=run.execution,
            verbose=verbose,
            publication_date=publication_date,
        )
        return preparation_stats(presented, run.feedback_collected)
    mode = (
        AnalysisMode.REVIEW_LED
        if run.config.review.enabled and run.config.review.review_led_only
        else AnalysisMode.CATEGORY
    )
    collected, work = await _collect_and_review(run, mode, config_path, started_at)
    if isinstance(work, EmptyWork):
        return _empty_work_stats(work, run, collected)
    deadline = reading_deadline(run.config, started_at)
    if not isinstance(work, ReviewedCandidates):
        raise ValueError("Source reading requires candidate-bound --prepare-edition mode.")
    result = await prepare_selected_sources(
        work.work.progress,
        work.work.packet,
        work.report,
        collected.source_config,
        Path(".cache"),
        deadline,
        prepare_only=True,
        execution=run.execution,
    )
    await prepare_current_batch(
        work.work.progress,
        collected.source_config,
        config_path,
        started_at,
        execution=run.execution,
    )
    _save_prepared_fetch_stats(
        run.source_stats,
        collected.fetch_metrics,
        collected.articles,
        run.config,
        ".cache",
        collected.collection_failed,
    )
    logging.getLogger(__name__).info(
        "Source preparation: %d selected, %d technically complete, %d pending; %s",
        result.selected,
        result.technical_complete,
        result.pending,
        result.status,
    )
    stats = _empty_stats(len(run.config.enabled_sources), run.feedback_collected, collected.article_count)
    stats.edition_status = result.status
    return stats
