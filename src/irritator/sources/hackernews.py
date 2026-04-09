"""Hacker News Algolia API adapter."""

from __future__ import annotations

from typing import Any

import httpx

from src.irritator.sources import Signal, _register

_BASE_URL = "https://hn.algolia.com/api/v1/search"
_TIMEOUT = 10.0
_MAX_RESULTS = 10


@_register("hackernews")
async def search_hackernews(
    query: str,
    config: Any,
    client: httpx.AsyncClient,
) -> list[Signal]:
    """Search Hacker News via the Algolia API."""
    resp = await client.get(
        _BASE_URL,
        params={"query": query, "tags": "story", "hitsPerPage": _MAX_RESULTS},
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()

    signals: list[Signal] = []
    for hit in data.get("hits", []):
        url = hit.get("url") or f"https://news.ycombinator.com/item?id={hit.get('objectID', '')}"
        signals.append(
            Signal(
                url=url,
                title=hit.get("title", ""),
                snippet=hit.get("story_text", "") or hit.get("comment_text", "") or "",
                source_name="hackernews",
                published=hit.get("created_at", ""),
                score=float(hit.get("points", 0) or 0),
            )
        )
    return signals
