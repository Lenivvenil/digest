"""Bounded lexical search through the Forem V1 articles search API."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from time import monotonic
from typing import Any
from urllib.parse import urlsplit

import httpx

from digest.domain.investigation.signals import Signal
from digest.irritator.query_contract import lexical_atoms
from digest.irritator.sources import _register, validate_search_response
from digest.irritator.sources._response import MAX_SOURCE_RESPONSE_BYTES, read_bounded_response

_BASE_URL = "https://dev.to/api/articles/search"
_TIMEOUT = 10.0
_MAX_RESULTS = 10


@dataclass
class _DevtoRequests:
    loop: asyncio.AbstractEventLoop
    lock: asyncio.Lock
    next_start: float = 0.0


_requests: _DevtoRequests | None = None


def _request_state() -> _DevtoRequests:
    # Scheduled processes are coordinated by runtime job concurrency. Within a
    # process both orchestration paths share this state, reset for a new loop.
    global _requests
    loop = asyncio.get_running_loop()
    if _requests is None or _requests.loop is not loop:
        _requests = _DevtoRequests(loop, asyncio.Lock())
    return _requests


def _article_signal(article: Any) -> Signal:
    if (
        not isinstance(article, dict)
        or not all(isinstance(article.get(field), str) for field in ("url", "title", "description", "published_at"))
        or not article["title"].strip()
    ):
        raise ValueError("Invalid DEV.to article fields.")
    url = article["url"]
    try:
        parsed = urlsplit(url)
        valid_url = (
            len(url) <= 2048
            and parsed.scheme in {"http", "https"}
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
            and parsed.port != 0
            and "\\" not in url
            and not any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in url)
        )
    except ValueError:
        valid_url = False
    if not valid_url:
        raise ValueError("Invalid DEV.to article URL.")
    published = article["published_at"]
    try:
        date = datetime.fromisoformat(published)
    except ValueError:
        raise ValueError("Invalid DEV.to publication date.") from None
    if len(published) > 80 or date.tzinfo is None:
        raise ValueError("Invalid DEV.to publication date.")
    return Signal(url, article["title"], article["description"], "devto", published, 0.0)


@_register("devto")
async def search_devto(
    query: str,
    config: Any,
    client: httpx.AsyncClient,
) -> list[Signal]:
    """Return exact search evidence, with no listing or article-body fallback."""
    lexical_atoms(query)
    state = _request_state()
    # Hold through response closure: one active request, >=2 seconds between
    # starts, even when legacy callers use different clients concurrently.
    async with state.lock:
        delay = state.next_start - monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        state.next_start = monotonic() + 2.0
        async with client.stream(
            "GET",
            _BASE_URL,
            params={"q": query, "page": 1, "per_page": _MAX_RESULTS},
            headers={"Accept": "application/vnd.forem.api-v1+json"},
            timeout=_TIMEOUT,
            follow_redirects=False,
        ) as response:
            response.raise_for_status()
            await read_bounded_response(response, MAX_SOURCE_RESPONSE_BYTES)
    articles = validate_search_response(response, "devto")
    if len(articles) > _MAX_RESULTS:
        raise ValueError("Invalid DEV.to search response.")
    return [_article_signal(article) for article in articles]
