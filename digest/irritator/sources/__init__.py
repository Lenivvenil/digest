"""Counter-signal source adapters."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

import httpx

from digest.irritator.query_generator import SearchQuery

logger = logging.getLogger(__name__)

_SEMAPHORE_LIMIT = 10


@dataclass
class Signal:
    """A raw signal fetched from a counter-signal source."""

    url: str
    title: str
    snippet: str
    source_name: str
    published: str
    score: float


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
) -> list[Signal]:
    """Search all configured sources for counter-signals.

    Each query fans out to every configured source adapter. Runs concurrently
    with a semaphore limit. Failures per (query, source) pair are logged and
    skipped (graceful degradation).
    """
    _import_adapters()

    configured = sorted(config.irritator.sources)
    semaphore = asyncio.Semaphore(_SEMAPHORE_LIMIT)

    async def _run(query: SearchQuery, source_name: str) -> list[Signal]:
        adapter = _ADAPTERS.get(source_name)
        if adapter is None:
            logger.warning(
                "No adapter registered for configured source '%s' — check irritator.sources config",
                source_name,
            )
            return []
        async with semaphore:
            try:
                result: list[Signal] = await adapter(query.query, config, client)
                return result
            except Exception as exc:
                logger.warning(
                    "Source %s failed for query '%s': %s",
                    source_name,
                    query.query[:80],
                    exc,
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
