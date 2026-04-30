"""Lobsters search adapter."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

import httpx

from digest.irritator.sources import Signal, _register

logger = logging.getLogger(__name__)

_BASE_URL = "https://lobste.rs/search.json"
_TIMEOUT = 10.0
_MAX_RESULTS = 10

# Serialise all Lobsters requests — lobste.rs rate-limits aggressively under
# fan-out. Semaphore(1) guarantees at most one in-flight request at a time.
_semaphore: asyncio.Semaphore | None = None

# \w is Unicode-aware in Python 3 by default (equivalent to Ruby's \p{Word}),
# so Cyrillic terms from bilingual LLM prompts are preserved correctly.
_STRIP_RE = re.compile(r"[^\w']")


def _get_semaphore() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(1)
    return _semaphore


def _sanitize_query(q: str) -> str:
    """Strip non-word characters, mirroring the character-stripping portion of
    Lobsters' strip_operators (apostrophes preserved; SQL escaping is server-side)."""
    q = _STRIP_RE.sub(" ", q)
    return " ".join(q.split())


@_register("lobsters")
async def search_lobsters(
    query: str,
    config: Any,
    client: httpx.AsyncClient,
) -> list[Signal]:
    """Search Lobsters via the JSON API."""
    safe_query = _sanitize_query(query)
    if not safe_query:
        logger.debug("Lobsters: query reduced to empty after sanitization, skipping — original: %r", query)
        return []
    async with _get_semaphore():
        resp = await client.get(
            _BASE_URL,
            params={"q": safe_query, "what": "stories", "order": "relevance"},
            timeout=_TIMEOUT,
        )
    resp.raise_for_status()
    data = resp.json()

    signals: list[Signal] = []
    results = data if isinstance(data, list) else data.get("results", [])
    for item in results[:_MAX_RESULTS]:
        signals.append(
            Signal(
                url=item.get("url") or item.get("short_id_url", ""),
                title=item.get("title", ""),
                snippet=item.get("description", "") or item.get("comment_plain", "") or "",
                source_name="lobsters",
                published=item.get("created_at", ""),
                score=float(item.get("score", 0)),
            )
        )
    return signals
