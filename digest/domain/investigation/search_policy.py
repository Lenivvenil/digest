"""Pure identity and effective limits for the bounded external search policy."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

MAX_QUERIES = 3
SAFE_SOURCES = ("hackernews", "arxiv", "devto")


@dataclass(frozen=True)
class SearchPolicy:
    id: str
    sources: list[str]
    max_queries: int


def build_search_policy(configured_sources: Sequence[str], configured_max_queries: int) -> SearchPolicy:
    """Keep dispatch order and deduplication independent of config order/repeats.

    Legacy-only sources remain registered elsewhere, but are outside this bounded
    policy. A future prepare binds only the sources that will actually dispatch.
    """
    return SearchPolicy(
        id="bounded-hn-arxiv-devto-v1",
        sources=[source for source in SAFE_SOURCES if source in configured_sources],
        max_queries=min(MAX_QUERIES, configured_max_queries),
    )
