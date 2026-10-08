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
from pathlib import Path
from typing import TYPE_CHECKING

from digest.application import analysis, run_state
from digest.application.results import RunStats

if TYPE_CHECKING:
    from digest.adapters.models.execution import ModelExecution
    from digest.closing import ClosingDecision
    from digest.config import Config
    from digest.domain.catalog.sources import SourceStats
    from digest.domain.editorial.candidates import CandidatePacket, CandidateProgress
    from digest.domain.editorial.reviews import BlindReviewReport
    from digest.preparation import PreparationSnapshot
    from digest.radar.collector import Article, CollectionInventory, SourceFetchMetrics
    from digest.radar.summarizer import ArticleSummary, CategorySummary


@dataclass
class SelectedPreparation:
    """Transitional application handoff, not a persisted domain entity.

    Candidate ownership is still being migrated. Optional progress/packet/report
    preserve existing legacy-review modes; the two configurations distinguish
    canonical policy from the enabled, non-demoted collection portfolio.
    """

    config: Config
    execution: ModelExecution
    source_config: Config
    articles: dict[str, list[Article]]
    progress: CandidateProgress | None
    packet: CandidatePacket | None
    summaries: list[CategorySummary]
    trends: str | None
    cards: list[ArticleSummary]
    report: BlindReviewReport | None
    source_stats: dict[str, SourceStats]
    fetch_metrics: dict[str, SourceFetchMetrics]
    collection_failed: bool
    collected_articles: int
    feedback_collected: int
    started_at: float

    @property
    def article_count(self) -> int:
        return sum(len(items) for items in self.articles.values())


def _empty_stats(feeds: int, feedback: int, articles: int = 0) -> RunStats:
    return RunStats(feeds, articles, 0, False, False, False, "", feedback_collected=feedback)


def _candidate_setup(
    review_led_only: bool,
    cache_dir: str,
) -> tuple[CandidateProgress | None, CollectionInventory | None, dict[str, str]]:
    if not review_led_only:
        return None, None, {}
    from digest.adapters.storage.candidate_progress import load_candidate_progress
    from digest.edition_runtime import _strict_cache
    from digest.radar.collector import CollectionInventory

    return (
        load_candidate_progress(cache_dir),
        CollectionInventory(),
        _strict_cache(Path(cache_dir) / "seen_articles.json"),
    )


def _candidate_inputs(
    progress: CandidateProgress | None,
    inventory: CollectionInventory | None,
    run_config: Config,
    config: Config,
    delivered: dict[str, str],
    priorities: dict[str, int],
    cache_dir: str,
    collection_failed: bool,
    allocated: dict[str, list[Article]],
) -> tuple[CandidatePacket | None, BlindReviewReport | None, dict[str, list[Article]]]:
    if progress is None:
        return None, None, allocated
    from digest.adapters.storage.candidate_progress import MAX_BYTES, progress_size
    from digest.application.candidate_lifecycle import checkpoint_candidates, ensure_report_accounting
    from digest.application.candidate_review import begin_packet, merge_candidates, plan_packet
    from digest.domain.editorial.candidate_policy import packet_articles, pending_completed_report
    from digest.radar import AllFeedsFailedError
    from digest.radar.collector import _prune_cache

    if inventory is None:
        raise ValueError("Candidate preparation requires collection accounting.")
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
    return packet, report, articles


async def _analyze_candidate_articles(
    articles: dict[str, list[Article]],
    config: Config,
    progress: CandidateProgress | None,
    packet: CandidatePacket | None,
    cached_report: BlindReviewReport | None,
    cache_dir: str,
    *,
    execution: ModelExecution,
) -> tuple[list[CategorySummary], str | None, list[ArticleSummary], BlindReviewReport | None]:
    if cached_report is not None:
        from digest.presentation.review import primary_cards

        cards = primary_cards(
            cached_report,
            articles,
            config.radar.language,
            max_cards=config.review.max_selections,
            include_attribution=config.telegram.delivery_mode != "compact",
        )
        return [], None, cards, cached_report
    if progress is None or packet is None:
        return await analysis.analyze_articles(articles, config, execution=execution)
    from digest.application.candidate_review import reconcile_packet
    from digest.application.review import run_primary_review
    from digest.closing import ClosingCapture, decide_closing, save_closing
    from digest.domain.editorial.dispositions import CandidateDispositionCapture
    from digest.presentation.review import primary_cards

    capture = CandidateDispositionCapture()

    closing_capture = ClosingCapture() if getattr(getattr(config, "closing", None), "enabled", False) else None
    kwargs = {"closing_capture": closing_capture} if closing_capture is not None else {}
    report = await run_primary_review(articles, config, disposition_capture=capture, **kwargs, execution=execution)
    reconcile_packet(progress, packet, report, config, cache_dir, disposition_capture=capture)
    if closing_capture is not None:
        try:
            decision = decide_closing(report, packet, closing_capture, config.closing, config.sources)
            save_closing(decision, report, cache_dir)
        except (OSError, ValueError, TypeError, KeyError):
            logging.getLogger(__name__).warning("Optional closing capture unavailable; main review remains accepted.")
    cards = primary_cards(
        report,
        articles,
        config.radar.language,
        max_cards=config.review.max_selections,
        include_attribution=config.telegram.delivery_mode != "compact",
    )
    return [], None, cards, report


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
    report: BlindReviewReport | None,
    articles: dict[str, list[Article]],
    config: Config,
    cache_dir: str,
) -> tuple[list[ArticleSummary], ClosingDecision | None]:
    from digest.application.review_request import eligible_ids
    from digest.closing import ClosingDecision, load_closing
    from digest.presentation.review import primary_cards

    if not getattr(getattr(config, "closing", None), "enabled", False):
        return cards, None
    decision = (
        load_closing(report, cache_dir)
        if report is not None
        else ClosingDecision("incomplete", "missing_delivery_review")
    )
    if decision.provenance is not None and report is not None:
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
            report,
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


def _save_candidate_preparation(
    snapshot: PreparationSnapshot,
    packet: CandidatePacket | None,
    cache_dir: str,
    publication_date: date | None,
) -> bool:
    """Return whether canonical preparation was accepted, including genuine abstention."""
    from digest.domain.editorial.reviews import delivery_review as _delivery_review
    from digest.edition_runtime import save_accepted_preparation

    if (
        not snapshot.top_articles
        and snapshot.review_report is not None
        and _delivery_review(snapshot.review_report).status != "abstained"
    ):
        return False
    if not snapshot.top_articles and packet is not None and packet.disposition_attempts:
        report = snapshot.review_report
        if report is None:
            return False
        delivery = _delivery_review(report)
        capture = next((item for item in packet.disposition_attempts if item.slot == delivery.slot), None)
        if capture is None or capture.status != "complete":
            return False  # Deferred or malformed metadata is not an accepted empty editorial decision.
    save_accepted_preparation(snapshot, cache_dir=cache_dir, publication_date=publication_date)
    return True


def _handoff_candidate(
    progress: CandidateProgress | None,
    packet: CandidatePacket | None,
    report: BlindReviewReport | None,
    cache_dir: str,
    publication_date: date | None,
) -> None:
    from digest.application.candidate_review import mark_prepared
    from digest.preparation import load_preparation

    if (
        progress is not None
        and packet is not None
        and report is not None
        and load_preparation(cache_dir, publication_date=publication_date) is not None
    ):
        mark_prepared(progress, packet.evidence.bundle_id, cache_dir)


async def _collect_and_select(
    config: Config,
    config_path: str,
    *,
    execution: ModelExecution,
    verbose: bool,
    feedback_precollected: bool,
    publication_date: date | None,
    started_at: float,
) -> SelectedPreparation | RunStats:
    """Recover accepted work first; otherwise acquire one bounded candidate selection."""
    from digest._util import cleanup_stale_tmp
    from digest.adapters.storage.feedback import save_feedback
    from digest.adapters.storage.sources import load_source_state, load_stats, save_stats
    from digest.application.source_scoring import calculate_effective_priorities
    from digest.domain.catalog.source_rules import calculate_feedback_priorities
    from digest.domain.feedback.rules import get_source_feedback_score
    from digest.edition_runtime import resume_preparation
    from digest.radar import AllFeedsFailedError, collect
    from digest.reading_preparation import reading_deadline, setup_reading_budget

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
    resumed = await resume_preparation(
        config,
        feedback_collected,
        verbose=verbose,
        publication_date=publication_date,
        execution=execution,
    )
    if resumed is not None:
        return resumed
    setup_reading_budget(config, execution=execution)

    feedback_scores = {}
    for source in config.enabled_sources:
        score = get_source_feedback_score(feedback_store, source.name, now=datetime.now(tz=timezone.utc))
        if score is not None:
            feedback_scores[source.name] = score
    if config.adaptive.enabled:
        priorities = calculate_effective_priorities(
            config.effective_sources(source_state),
            source_stats,
            feedback_scores,
            config.adaptive,
        )
    else:
        priorities = calculate_feedback_priorities(
            config.effective_sources(source_state),
            feedback_scores,
            config.adaptive,
        )
    source_config = dataclasses.replace(
        config,
        sources=[source for source in config.sources if source.enabled and not source_state.is_demoted(source.name)],
    )
    fetch_metrics: dict[str, SourceFetchMetrics] = {}
    progress, inventory, delivered = _candidate_setup(
        config.review.enabled and config.review.review_led_only, cache_dir
    )
    collection_failed = False
    try:
        articles, _cache = await collect(
            source_config,
            effective_priorities=priorities,
            fetch_metrics=fetch_metrics,
            **({"inventory": inventory} if inventory is not None else {}),
        )
    except AllFeedsFailedError:
        run_state.save_failed_run_stats(
            source_stats, fetch_metrics, cache_dir, {source.name for source in config.enabled_sources}, dry_run=False
        )
        if progress is None:
            raise
        collection_failed = True
        articles = {}
    collected_articles = sum(len(items) for items in articles.values())
    packet, cached_report, articles = _candidate_inputs(
        progress,
        inventory,
        source_config,
        config,
        delivered,
        priorities,
        cache_dir,
        collection_failed,
        articles,
    )
    if not articles:
        from digest.reconciliation_checkpoint import prepare_current_batch

        await prepare_current_batch(progress, source_config, config_path, started_at, execution=execution)
        logging.getLogger(__name__).info("No eligible articles in this processing packet. Nothing to summarize.")
        run_state.record_source_stats(source_stats, fetch_metrics, articles, set())
        save_stats(source_stats, cache_dir, active_sources={source.name for source in config.enabled_sources})
        if feedback_usable:
            save_feedback(feedback_store, cache_dir)
        return _empty_stats(len(config.enabled_sources), feedback_collected)

    deadline = reading_deadline(config, started_at) if config.reading_brief.enabled else None
    async with asyncio.timeout_at(deadline):
        summaries, trends, cards, report = await _analyze_candidate_articles(
            articles,
            config,
            progress,
            packet,
            cached_report,
            cache_dir,
            execution=execution,
        )
    if not summaries and not cards and report is None:
        logging.getLogger(__name__).error("All category summarizations failed.")
        run_state.save_failed_run_stats(
            source_stats, fetch_metrics, cache_dir, {source.name for source in config.enabled_sources}, dry_run=False
        )
        # CLI preparation requires compact mode, so legacy failure messages are not sent.
        return _empty_stats(
            len(config.enabled_sources), feedback_collected, sum(len(items) for items in articles.values())
        )
    return SelectedPreparation(
        config,
        execution,
        source_config,
        articles,
        progress,
        packet,
        summaries,
        trends,
        cards,
        report,
        source_stats,
        fetch_metrics,
        collection_failed,
        collected_articles,
        feedback_collected,
        started_at,
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
    """Ordinary preparation: recover, select, accept, present and freeze; never send."""
    from digest.adapters.storage.sources import save_source_category_map
    from digest.application.presentation import combined_summary, publication_intro
    from digest.edition_runtime import finish_preparation
    from digest.preparation import PreparationSnapshot

    selected = await _collect_and_select(
        config,
        config_path,
        verbose=verbose,
        feedback_precollected=feedback_precollected,
        publication_date=publication_date,
        started_at=started_at,
        execution=execution,
    )
    if isinstance(selected, RunStats):
        return selected
    config, execution = selected.config, selected.execution
    cards, closing = _preparation_closing(selected.cards, selected.report, selected.articles, config, ".cache")
    combined = combined_summary(
        selected.summaries,
        selected.trends,
        config.review.enabled and config.review.review_led_only,
        config.radar.language,
    )
    combined = publication_intro(combined, selected.report, config)
    snapshot = PreparationSnapshot(
        top_articles=cards,
        summaries=selected.summaries,
        combined=combined,
        review_report=selected.report,
        source_count=len(selected.articles),
        article_count=selected.article_count,
        contributing_sources=sorted({article.source for items in selected.articles.values() for article in items}),
        closing=closing,
    )
    accepted = _save_candidate_preparation(snapshot, selected.packet, ".cache", publication_date)
    _handoff_candidate(selected.progress, selected.packet, selected.report, ".cache", publication_date)
    _save_prepared_fetch_stats(
        selected.source_stats, selected.fetch_metrics, selected.articles, config, ".cache", selected.collection_failed
    )
    save_source_category_map(config.enabled_sources, ".cache")
    stats = await finish_preparation(
        snapshot,
        config,
        selected.feedback_collected,
        verbose=verbose,
        publication_date=publication_date,
        selection_complete=accepted,
        execution=execution,
    )
    stats.feeds_fetched = len(selected.fetch_metrics)
    stats.new_articles = selected.collected_articles
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
    """Experimental source work ends in a technical handoff, not an edition."""
    from digest.reading_preparation import prepare_selected_sources, reading_deadline
    from digest.reconciliation_checkpoint import prepare_current_batch

    selected = await _collect_and_select(
        config,
        config_path,
        verbose=verbose,
        feedback_precollected=feedback_precollected,
        publication_date=publication_date,
        started_at=started_at,
        execution=execution,
    )
    if isinstance(selected, RunStats):
        return selected
    execution = selected.execution
    result = await prepare_selected_sources(
        selected.progress,
        selected.packet,
        selected.report,
        selected.source_config,
        Path(".cache"),
        reading_deadline(selected.config, started_at),
        prepare_only=True,
        execution=execution,
    )
    await prepare_current_batch(selected.progress, selected.source_config, config_path, started_at, execution=execution)
    _save_prepared_fetch_stats(
        selected.source_stats,
        selected.fetch_metrics,
        selected.articles,
        selected.config,
        ".cache",
        selected.collection_failed,
    )
    logging.getLogger(__name__).info(
        "Source preparation: %d selected, %d technically complete, %d pending; %s",
        result.selected,
        result.technical_complete,
        result.pending,
        result.status,
    )
    stats = _empty_stats(len(selected.config.enabled_sources), selected.feedback_collected, selected.article_count)
    stats.edition_status = result.status
    return stats
