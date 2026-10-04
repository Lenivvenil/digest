"""arXiv Atom API adapter."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from time import monotonic
from typing import Any

import feedparser
import httpx

from digest.irritator.query_contract import lexical_atoms
from digest.irritator.sources import Signal, _register, validate_search_response

_BASE_URL = "https://export.arxiv.org/api/query"
_TIMEOUT = 10.0
_MAX_RESULTS = 10


@dataclass
class _ArxivRequests:
    loop: asyncio.AbstractEventLoop
    lock: asyncio.Lock
    next_start: float = 0.0


_requests: _ArxivRequests | None = None


def _request_state() -> _ArxivRequests:
    # The owned pipeline uses one event loop. Do not reuse a lock across closed
    # loops (including separate CLI invocations/tests).
    global _requests
    loop = asyncio.get_running_loop()
    if _requests is None or _requests.loop is not loop:
        _requests = _ArxivRequests(loop, asyncio.Lock())
    return _requests


@_register("arxiv")
async def search_arxiv(
    query: str,
    config: Any,
    client: httpx.AsyncClient,
) -> list[Signal]:
    """Search arXiv via the Atom API."""
    # Each lexical atom gets its own documented all: field; phrases stay quoted.
    search_query = " AND ".join(f"all:{atom}" for atom in lexical_atoms(query))
    state = _request_state()
    # arXiv requires one active connection and >=3 seconds between requests.
    # Hold the lock through the complete response; both orchestration paths use
    # this adapter. Runtime job concurrency coordinates our scheduled processes.
    async with state.lock:
        delay = state.next_start - monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        state.next_start = monotonic() + 3.0
        resp = await client.get(
            _BASE_URL,
            params={
                "search_query": search_query,
                "start": 0,
                "max_results": _MAX_RESULTS,
                "sortBy": "relevance",
            },
            timeout=_TIMEOUT,
        )
    resp.raise_for_status()
    validate_search_response(resp, "arxiv")

    feed = feedparser.parse(resp.text)
    signals: list[Signal] = []
    for entry in feed.entries:
        signals.append(
            Signal(
                url=entry.get("link", ""),
                title=entry.get("title", ""),
                snippet=entry.get("summary", ""),
                source_name="arxiv",
                published=entry.get("published", ""),
                score=0.0,
            )
        )
    return signals
