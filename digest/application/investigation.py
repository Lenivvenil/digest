"""Synchronous external investigation used by supported legacy workflows."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from digest.config import Config
    from digest.irritator import IrritatorStatus
    from digest.irritator.narrative_extractor import Narrative
    from digest.irritator.ranker import RankedSignal
    from digest.radar.summarizer import CategorySummary


async def run_irritator(
    summaries: list[CategorySummary], config: Config, verbose: bool,
) -> tuple[list[Narrative], list[RankedSignal], IrritatorStatus]:
    """Thin wrapper: creates an AsyncClient and delegates to the public run_irritator()."""
    import httpx

    from digest.irritator import run_irritator

    async with httpx.AsyncClient() as client:
        return await run_irritator(summaries, config, client, verbose=verbose)
