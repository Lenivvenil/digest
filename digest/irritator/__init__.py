"""Irritator pipeline: counter-signal extraction."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

import httpx

from digest.irritator.narrative_extractor import Narrative, extract_narratives
from digest.irritator.query_generator import QueryDiagnostics, SearchQuery, generate_queries
from digest.irritator.ranker import RankedSignal, rank_signals
from digest.irritator.sources import SearchDiagnostics, Signal, search_all_sources
from digest.irritator.validator import validate_signals, validate_signals_async

if TYPE_CHECKING:
    from digest.config import Config
    from digest.radar.summarizer import CategorySummary

logger = logging.getLogger(__name__)


IrritatorLevel = Literal["ok", "empty", "error", "incomplete", "deferred"]


@dataclass
class IrritatorDiagnostics:
    queries: QueryDiagnostics = field(default_factory=QueryDiagnostics)
    search: SearchDiagnostics = field(default_factory=SearchDiagnostics)
    ranking_successful: int = 0
    ranking_failed: int = 0


@dataclass
class IrritatorStatus:
    """Outcome of one irritator pipeline run.

    level:
      "ok"    — ranked signals exist; status accompanies the signal post
      "empty" — pipeline ran fully, nothing survived filters/ranking
      "error" — a stage failed without a successful usable result
      "incomplete" — some source/ranking attempts failed; valid results are retained
      "deferred" — stage intentionally postponed until after primary delivery
    """

    text: str
    level: IrritatorLevel
    diagnostics: IrritatorDiagnostics = field(default_factory=IrritatorDiagnostics)


async def run_irritator(
    summaries: "list[CategorySummary]",
    config: "Config",
    client: httpx.AsyncClient,
    *,
    verbose: bool = False,
) -> tuple[list[Narrative], list[RankedSignal], IrritatorStatus]:
    """Run the five-stage Irritator pipeline.

    Returns (narratives, ranked_signals, IrritatorStatus).
    status is always populated so the caller can relay pipeline
    progress to the user even when no counter-signals survive ranking.
    """
    narratives: list[Narrative] = []
    all_ranked: list[RankedSignal] = []
    raw_signal_count = 0
    valid_signal_count = 0
    diagnostics = IrritatorDiagnostics()

    def outcome(text: str, level: IrritatorLevel) -> IrritatorStatus:
        if diagnostics.queries.failed:
            text += (f"; query generation: {diagnostics.queries.successful} successful, "
                     f"{diagnostics.queries.failed} failed")
            if level in {"ok", "empty"}:
                level = "incomplete"
        counts = diagnostics.search
        if counts.successful or counts.failed or counts.unavailable:
            text += (f"; source attempts: {counts.successful} successful, "
                     f"{counts.failed} failed, {counts.unavailable} unavailable")
        if diagnostics.ranking_successful or diagnostics.ranking_failed:
            text += (f"; ranking: {diagnostics.ranking_successful} successful, "
                     f"{diagnostics.ranking_failed} failed")
        return IrritatorStatus(text, level, diagnostics)

    # Stage 1: Narrative extraction
    try:
        narratives = await extract_narratives(summaries, config)
    except Exception as exc:
        logger.error("Irritator: narrative extraction failed (%s)", type(exc).__name__)
        return [], [], outcome(f"narrative extraction failed: {type(exc).__name__}", "error")

    if not narratives:
        logger.warning("Irritator: no narratives extracted from %d summaries", len(summaries))
        return [], [], outcome(f"0 narratives from {len(summaries)} summaries", "empty")

    logger.info("Irritator: extracted %d narratives", len(narratives))

    # Stage 2: Query generation
    try:
        queries_by_narrative = await generate_queries(narratives, config, diagnostics=diagnostics.queries)
        all_queries = [q for qs in queries_by_narrative.values() for q in qs]
    except Exception as exc:
        logger.error("Irritator: query generation failed (%s)", type(exc).__name__)
        return narratives, [], outcome(
            f"{len(narratives)} narratives, query generation failed: {type(exc).__name__}", "error"
        )

    if not all_queries:
        logger.warning("Irritator: no queries generated from %d narratives", len(narratives))
        return narratives, [], outcome(
            f"{len(narratives)} narratives, 0 queries", "empty"
        )

    logger.info("Irritator: generated %d queries", len(all_queries))

    # Stage 3: Signal search
    try:
        raw_signals = await search_all_sources(all_queries, config, client, diagnostics=diagnostics.search)
        raw_signal_count = len(raw_signals)
    except Exception as exc:
        logger.error("Irritator: signal search failed (%s)", type(exc).__name__)
        return narratives, [], outcome(
            f"{len(narratives)} narratives, {len(all_queries)} queries, search failed: {type(exc).__name__}",
            "error",
        )

    source_failures = diagnostics.search.failed + diagnostics.search.unavailable
    if not raw_signals:
        logger.warning("Irritator: no signals found from %d queries", len(all_queries))
        return narratives, [], outcome(
            f"{len(narratives)} narratives, {len(all_queries)} queries, 0 signals",
            ("error" if not diagnostics.search.successful else "incomplete") if source_failures else "empty"
        )

    # Stage 4: Validation (dedup + blocklist + optional liveness)
    try:
        signals = await validate_signals_async(
            raw_signals,
            config.filters.blocklist_keywords,
            client,
            check_liveness=config.irritator.check_liveness,
        )
    except Exception as exc:
        logger.error("Irritator: signal validation failed (%s)", type(exc).__name__)
        return narratives, [], outcome(
            f"{len(narratives)} narratives, {raw_signal_count} signals, validation failed: {type(exc).__name__}",
            "error",
        )
    valid_signal_count = len(signals)
    if not signals:
        logger.warning("Irritator: all %d signals filtered by validation", raw_signal_count)
        return narratives, [], outcome(
            f"{len(narratives)} narratives, {raw_signal_count} signals, all filtered",
            "incomplete" if source_failures else "empty",
        )

    logger.info("Irritator: %d/%d signals passed validation", valid_signal_count, raw_signal_count)

    # Stage 5: Ranking (per narrative; failures are logged and skipped)
    for narrative in narratives:
        try:
            ranked = await rank_signals(narrative, signals, config)
            all_ranked.extend(ranked)
            diagnostics.ranking_successful += 1
        except Exception as exc:
            diagnostics.ranking_failed += 1
            logger.error("Irritator: ranking failed (%s)", type(exc).__name__)

    status_text = (
        f"{len(narratives)} narratives, {raw_signal_count} signals, "
        f"{valid_signal_count} valid, {len(all_ranked)} passed ranking"
    )
    logger.info("Irritator: %s", status_text)

    if verbose and narratives:
        print("\n=== DOMINANT NARRATIVES ===\n")
        for i, n in enumerate(narratives, 1):
            print(f"{i}. {n.claim}")
            print(f"   Category: {n.category}")
            for a in n.implicit_assumptions:
                print(f"     - {a}")
            print(f"   Why challenge: {n.why_worth_challenging}\n")

    if verbose and all_ranked:
        print("\n=== COUNTER-SIGNALS ===\n")
        for r in all_ranked:
            print(f"[{r.score}/10] {r.signal.title}")
            print(f"   {r.signal.url}")
            print(f"   Narrative: {r.narrative_claim[:60]}")
            print(f"   Reasoning: {r.reasoning}\n")

    level: IrritatorLevel = "ok" if all_ranked else "empty"
    if diagnostics.ranking_failed and not diagnostics.ranking_successful:
        level = "error"
    elif source_failures or diagnostics.ranking_failed:
        level = "incomplete"
    return narratives, all_ranked, outcome(status_text, level)


__all__ = [
    "IrritatorStatus",
    "Narrative",
    "RankedSignal",
    "SearchQuery",
    "Signal",
    "extract_narratives",
    "generate_queries",
    "rank_signals",
    "run_irritator",
    "search_all_sources",
    "validate_signals",
    "validate_signals_async",
]
