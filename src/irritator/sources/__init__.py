"""Counter-signal source adapters."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from src.irritator.query_generator import SearchQuery

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


def _register(name: str):  # noqa: ANN202
    """Decorator to register a source adapter."""
    def decorator(func):  # noqa: ANN001, ANN202
        _ADAPTERS[name] = func
        return func
    return decorator


def _import_adapters() -> None:
    """Import all adapter modules so they register themselves."""
    import src.irritator.sources.arxiv  # noqa: F401
    import src.irritator.sources.devto  # noqa: F401
    import src.irritator.sources.hackernews  # noqa: F401
    import src.irritator.sources.lobsters  # noqa: F401
    import src.irritator.sources.reddit  # noqa: F401


async def search_all_sources(
    queries: list[SearchQuery],
    config: Any,
    client: httpx.AsyncClient,
) -> list[Signal]:
    """Search all configured sources for counter-signals.

    Runs searches concurrently with a semaphore limit.
    Failures are logged and skipped (graceful degradation).
    """
    _import_adapters()

    configured = set(config.irritator.sources)
    semaphore = asyncio.Semaphore(_SEMAPHORE_LIMIT)

    async def _run(query: SearchQuery) -> list[Signal]:
        adapter = _ADAPTERS.get(query.target_source)
        if adapter is None or query.target_source not in configured:
            logger.debug(
                "Skipping query for unconfigured source '%s'", query.target_source
            )
            return []
        async with semaphore:
            try:
                return await adapter(query.query, config, client)
            except Exception as exc:
                logger.warning(
                    "Source %s failed for query '%s': %s",
                    query.target_source,
                    query.query[:80],
                    exc,
                )
                return []

    results = await asyncio.gather(*[_run(q) for q in queries])
    signals = [s for batch in results for s in batch]
    logger.info(
        "Fetched %d signals from %d queries across configured sources",
        len(signals),
        len(queries),
    )
    return signals
