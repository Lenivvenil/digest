"""Hacker News Algolia API adapter."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from digest.domain.investigation.signals import Signal
from digest.irritator.query_contract import lexical_atoms
from digest.irritator.sources import _register, validate_search_response
from digest.irritator.sources._response import MAX_SOURCE_RESPONSE_BYTES, read_bounded_response

logger = logging.getLogger(__name__)

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
    lexical_atoms(query)
    async with client.stream(
        "GET",
        _BASE_URL,
        params={
            "query": query,
            "tags": "story",
            "hitsPerPage": _MAX_RESULTS,
            "advancedSyntax": "true",
        },
        timeout=_TIMEOUT,
        follow_redirects=False,
    ) as resp:
        resp.raise_for_status()
        await read_bounded_response(resp, MAX_SOURCE_RESPONSE_BYTES)
    data = validate_search_response(resp, "hackernews")
    hits = data["hits"]
    signals: list[Signal] = []
    unidentified = 0
    for hit in hits:
        if not isinstance(hit, dict):
            raise ValueError("Invalid Hacker News story.")
        url = hit.get("url")
        if not isinstance(url, str) or not url.strip():
            identity = hit.get("objectID")
            if not isinstance(identity, str) or not identity.isdecimal() or int(identity) <= 0:
                unidentified += 1
                continue
            url = f"https://news.ycombinator.com/item?id={identity}"
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
    if unidentified:
        logger.warning("Skipped %d Hacker News hits without a usable URL or story ID", unidentified)
        if not signals:
            raise ValueError("Hacker News response contains no identifiable stories.")
    return signals
