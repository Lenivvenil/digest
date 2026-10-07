"""Search query values shared by investigation and source adapters."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SearchQuery:
    """A search query for finding counter-signals across all configured sources."""

    query: str
    intent: str
