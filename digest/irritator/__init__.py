"""Irritator pipeline: counter-signal extraction."""

from digest.irritator.narrative_extractor import Narrative, extract_narratives
from digest.irritator.query_generator import SearchQuery, generate_queries
from digest.irritator.ranker import RankedSignal, rank_signals
from digest.irritator.sources import Signal, search_all_sources
from digest.irritator.validator import validate_signals

__all__ = [
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
