"""Counter-signal source adapters."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar
from xml.etree import ElementTree

import httpx

from digest.irritator.query_generator import SearchQuery

logger = logging.getLogger(__name__)

_SEMAPHORE_LIMIT = 10


class SourceUnavailableError(Exception):
    """A configured source cannot perform searches in this installation."""


@dataclass
class SearchDiagnostics:
    """Outcomes per query/source attempt, including successful empty searches."""

    successful: int = 0
    failed: int = 0
    unavailable: int = 0

    @property
    def total(self) -> int:
        return self.successful + self.failed + self.unavailable


@dataclass
class Signal:
    """A raw signal fetched from a counter-signal source."""

    url: str
    title: str
    snippet: str
    source_name: str
    published: str
    score: float


def validate_search_response(response: httpx.Response, source: str) -> Any:
    """Validate a success body and return its decoded JSON or Atom root."""
    if source == "arxiv":
        message = "Invalid or error arXiv feed."
        try:
            root = ElementTree.fromstring(response.content)
        except ElementTree.ParseError:
            raise ValueError(message) from None
        atom = "{http://www.w3.org/2005/Atom}"
        if root.tag != f"{atom}feed" or any(
            "/api/errors" in (entry.findtext(f"{atom}id") or "") for entry in root.findall(f"{atom}entry")
        ):
            raise ValueError(message)
        return root

    messages = {
        "hackernews": "Invalid Hacker News search response.",
        "lobsters": "Invalid Lobsters search response.",
        "reddit": "Invalid Reddit search response.",
    }
    if source not in messages:
        raise SourceUnavailableError("Source response validation is unavailable.")
    message = messages[source]
    try:
        raw = response.json()
    except ValueError:
        raise ValueError(message) from None
    if source == "hackernews":
        valid = isinstance(raw, dict) and isinstance(raw.get("hits"), list)
    elif source == "lobsters":
        valid = isinstance(raw, list) or (isinstance(raw, dict) and isinstance(raw.get("results"), list))
    else:
        valid = (isinstance(raw, dict) and isinstance(raw.get("data"), dict)
                 and isinstance(raw["data"].get("children"), list))
    if not valid:
        raise ValueError(message)
    return raw


# Adapter registry: source name → async search function
_ADAPTERS: dict[str, Any] = {}


_F = TypeVar("_F", bound=Callable[..., Any])


def _register(name: str) -> Callable[[_F], _F]:
    """Decorator to register a source adapter."""
    def decorator(func: _F) -> _F:
        _ADAPTERS[name] = func
        return func
    return decorator


def _import_adapters() -> None:
    """Import all adapter modules so they register themselves."""
    import digest.irritator.sources.arxiv  # noqa: F401
    import digest.irritator.sources.devto  # noqa: F401
    import digest.irritator.sources.hackernews  # noqa: F401
    import digest.irritator.sources.lobsters  # noqa: F401
    import digest.irritator.sources.reddit  # noqa: F401


async def search_all_sources(
    queries: list[SearchQuery],
    config: Any,
    client: httpx.AsyncClient,
    *,
    diagnostics: SearchDiagnostics | None = None,
) -> list[Signal]:
    """Search all configured sources for counter-signals.

    Each query fans out to every configured source adapter. Runs concurrently
    with a semaphore limit. Failures per (query, source) pair are logged and
    skipped (graceful degradation).
    """
    _import_adapters()

    outcomes = diagnostics if diagnostics is not None else SearchDiagnostics()
    configured = sorted(config.irritator.sources)
    semaphore = asyncio.Semaphore(_SEMAPHORE_LIMIT)

    async def _run(query: SearchQuery, source_name: str) -> list[Signal]:
        adapter = _ADAPTERS.get(source_name)
        if adapter is None:
            outcomes.unavailable += 1
            logger.warning("Source %s unavailable (%s)", source_name, SourceUnavailableError.__name__)
            return []
        async with semaphore:
            try:
                result: list[Signal] = await adapter(query.query, config, client)
                outcomes.successful += 1
                return result
            except SourceUnavailableError as exc:
                outcomes.unavailable += 1
                logger.warning("Source %s unavailable (%s)", source_name, type(exc).__name__)
                return []
            except Exception as exc:
                outcomes.failed += 1
                logger.warning(
                    "Source %s failed (%s)",
                    source_name,
                    type(exc).__name__,
                )
                return []

    tasks = [
        _run(query, source_name)
        for query in queries
        for source_name in configured
    ]
    results = await asyncio.gather(*tasks)
    signals = [s for batch in results for s in batch]
    logger.info(
        "Fetched %d signals from %d queries × %d sources",
        len(signals),
        len(queries),
        len(configured),
    )
    return signals
