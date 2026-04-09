"""DEV.to public API adapter."""

from __future__ import annotations

from typing import Any

import httpx

from src.irritator.sources import Signal, _register

_BASE_URL = "https://dev.to/api/articles"
_TIMEOUT = 10.0
_MAX_RESULTS = 10


@_register("devto")
async def search_devto(
    query: str,
    config: Any,
    client: httpx.AsyncClient,
) -> list[Signal]:
    """Search DEV.to via the public API."""
    resp = await client.get(
        _BASE_URL,
        params={"tag": query.split()[0].lower() if query else "", "per_page": _MAX_RESULTS},
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()

    signals: list[Signal] = []
    for article in data if isinstance(data, list) else []:
        signals.append(
            Signal(
                url=article.get("url", ""),
                title=article.get("title", ""),
                snippet=article.get("description", "")[:500],
                source_name="devto",
                published=article.get("published_at", ""),
                score=float(article.get("positive_reactions_count", 0)),
            )
        )
    return signals
