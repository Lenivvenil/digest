"""Irritator pipeline: counter-signal extraction."""

from src.irritator.narrative_extractor import Narrative, extract_narratives
from src.irritator.query_generator import SearchQuery, generate_queries
from src.irritator.ranker import RankedSignal, rank_signals
from src.irritator.sources import Signal, search_all_sources
from src.irritator.validator import validate_signals

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
