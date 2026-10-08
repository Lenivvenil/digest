"""Raw external signal values shared by investigation and source adapters."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Signal:
    """A raw signal fetched from a counter-signal source."""

    url: str
    title: str
    snippet: str
    source_name: str
    published: str
    score: float
