"""Assemble accepted publication content before archive and readiness effects."""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from digest.adapters.models.execution import ModelExecution
    from digest.domain.editorial.summaries import ArticleSummary
    from digest.irritator import IrritatorStatus
    from digest.irritator.ranker import RankedSignal
    from digest.preparation import PreparationSnapshot
    from digest.translation import ClosingPresentation

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PublicationAssembly:
    """Preflighted main content and optional closing disposition, in publication order.

    Assembly preserves the canonical snapshot. The closing disposition owns its
    optional card; ordered cards and existing metadata are projections, so omission
    cannot leave a second independently maintained publication list behind.
    """

    snapshot: PreparationSnapshot
    combined: str
    main_cards: tuple[ArticleSummary, ...]
    ranked_signals: tuple[RankedSignal, ...]
    irritator_status: IrritatorStatus
    closing: ClosingPresentation | None

    @property
    def cards(self) -> list[ArticleSummary]:
        if self.closing is not None and self.closing.card is not None:
            return [*self.main_cards, self.closing.card]
        return list(self.main_cards)

    def metadata(self) -> tuple[dict[str, Any], dict[str, Any]]:
        """Project the unchanged ready-file metadata only when freezing is reached."""
        canonical: dict[str, Any] = {
            "combined": self.snapshot.combined,
            "cards": [asdict(card) for card in self.snapshot.top_articles],
            "contributing_sources": self.snapshot.contributing_sources,
            "source_count": self.snapshot.source_count,
            "article_count": self.snapshot.article_count,
        }
        presentation: dict[str, Any] = {"combined": self.combined, "cards": [asdict(card) for card in self.cards]}
        closing = getattr(self.snapshot, "closing", None)
        if closing is not None and self.closing is not None:
            canonical["closing"] = asdict(closing)
            presentation["closing"] = asdict(self.closing)
        return canonical, presentation


async def assemble_publication(
    snapshot: PreparationSnapshot,
    config: Any,
    *,
    execution: ModelExecution,
    verbose: bool = False,
) -> PublicationAssembly:
    """Verify provenance, present and preflight required/optional content in order.

    Model/cache effects retain their existing ordering. No archive, ready edition
    or accepted-preparation removal occurs until this validated result is returned.
    """
    from digest.application.investigation import run_irritator
    from digest.application.presentation import deferred_review_status, publication_presentation
    from digest.application.source_attribution import main_attribution_occurrences
    from digest.closing import attribute_closing_card
    from digest.domain.catalog.occurrences import occurrence_sha256
    from digest.irritator import IrritatorStatus
    from digest.presentation.source_attribution import attribute_source_card
    from digest.presentation.telegram import render_compact_issue
    from digest.translation import ClosingPresentation, translate_publication_with_closing

    main_attribution = main_attribution_occurrences(
        snapshot.review_report,
        snapshot.top_articles,
        getattr(config, "sources", ()),
        closing_snapshot=getattr(snapshot, "closing", None) is not None,
    )
    ranked: list[RankedSignal] = []
    if config.review.enabled and config.review.review_led_only:
        status = IrritatorStatus(deferred_review_status(config.radar.language), "deferred")
    else:
        _, ranked, status = await run_irritator(snapshot.summaries, config, verbose, execution=execution)
    closing = getattr(snapshot, "closing", None)
    closing_presentation: ClosingPresentation | None = None
    if closing is not None and closing.status == "selected":
        if closing.card is None:
            raise ValueError("Selected closing decision is missing its canonical card.")
        text, cards, ranked, closing_presentation = await translate_publication_with_closing(
            snapshot.combined,
            snapshot.top_articles,
            ranked,
            closing.card,
            config,
            Path(".cache/translations"),
            selection_binding=asdict(closing),
            execution=execution,
        )
    else:
        text, cards, ranked = await publication_presentation(
            snapshot.combined,
            snapshot.top_articles,
            ranked,
            config,
            Path(".cache/translations"),
            False,
            execution=execution,
        )
        if closing is not None:
            closing_presentation = ClosingPresentation(closing.status, closing.reason)
    # Credits stay outside canonical text and model/cache inputs, but enter both
    # archive and frozen delivery. Missing provenance was checked before calls.
    from digest.radar.collector import article_hash

    cards = [
        attribute_source_card(card, occurrence, occurrence_sha256(occurrence))[0]
        if (occurrence := main_attribution.get(article_hash(card.title, card.link))) is not None
        else card
        for card in cards
    ]
    # Never discard a required main card to satisfy optional placement. Hold the
    # accepted preparation before archive/freeze/send when its credit would split.
    _, main_ranges = render_compact_issue(cards, config, text)
    if any(item.full_hash in main_attribution and len(item.covering_chunks) != 1 for item in main_ranges):
        raise ValueError(
            "Main source attribution spans delivery chunks; accepted preparation retained. "
            "Review its presentation before resuming; no article was sent or discarded."
        )
    if closing_presentation is not None and closing_presentation.card is not None:
        attributed = attribute_closing_card(closing, closing_presentation.card) if closing is not None else None
        if attributed is None:
            closing_presentation = replace(
                closing_presentation,
                status="incomplete",
                reason="attribution_unavailable",
                card=None,
            )
        else:
            assembled = [*cards, attributed]
            try:
                _, ranges = render_compact_issue(assembled, config, text)
            except ValueError as exc:
                logger.warning("Closing presentation omitted after render preflight: %s", exc)
                closing_presentation = replace(
                    closing_presentation,
                    status="incomplete",
                    reason="rendering_failed",
                    card=None,
                )
            else:
                # A visible partial article cannot lose its required credit if
                # a later chunk fails. Omit optional content; leave main intact.
                if any(
                    (item.full_hash in main_attribution or item is ranges[-1]) and len(item.covering_chunks) != 1
                    for item in ranges
                ):
                    closing_presentation = replace(
                        closing_presentation,
                        status="incomplete",
                        reason="attribution_split",
                        card=None,
                    )
                else:
                    closing_presentation = replace(closing_presentation, card=attributed)
    return PublicationAssembly(snapshot, text, tuple(cards), tuple(ranked), status, closing_presentation)
