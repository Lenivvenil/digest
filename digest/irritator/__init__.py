"""Irritator pipeline: counter-signal extraction."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from digest.irritator.narrative_extractor import Narrative, extract_narratives
from digest.irritator.query_generator import SearchQuery, generate_queries
from digest.irritator.ranker import RankedSignal, rank_signals
from digest.irritator.sources import Signal, search_all_sources
from digest.irritator.validator import validate_signals


@dataclass
class IrritatorStatus:
    """Outcome of one irritator pipeline run.

    level:
      "ok"    — ranked signals exist; status accompanies the signal post
      "empty" — pipeline ran fully, nothing survived filters/ranking
      "error" — a pipeline stage raised an exception
    """

    text: str
    level: Literal["ok", "empty", "error"]


__all__ = [
    "IrritatorStatus",
    "Narrative",
    "RankedSignal",
    "SearchQuery",
    "Signal",
    "extract_narratives",
    "generate_queries",
    "rank_signals",
    "search_all_sources",
    "validate_signals",
]
