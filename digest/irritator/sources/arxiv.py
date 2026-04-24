"""arXiv Atom API adapter."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import feedparser
import httpx

from digest.irritator.sources import Signal, _register

_BASE_URL = "https://export.arxiv.org/api/query"
_TIMEOUT = 10.0
_MAX_RESULTS = 10


@_register("arxiv")
async def search_arxiv(
    query: str,
    config: Any,
    client: httpx.AsyncClient,
) -> list[Signal]:
    """Search arXiv via the Atom API."""
    resp = await client.get(
        _BASE_URL,
        params={
            "search_query": f"all:{quote(query)}",
            "start": 0,
            "max_results": _MAX_RESULTS,
            "sortBy": "relevance",
        },
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()

    feed = feedparser.parse(resp.text)
    signals: list[Signal] = []
    for entry in feed.entries:
        signals.append(
            Signal(
                url=entry.get("link", ""),
                title=entry.get("title", "").replace("\n", " "),
                snippet=entry.get("summary", "")[:500],
                source_name="arxiv",
                published=entry.get("published", ""),
                score=0.0,
            )
        )
    return signals
