"""Validate and deduplicate raw signals."""

from __future__ import annotations

import asyncio
import logging
from urllib.parse import urlparse

import httpx

from digest.irritator.sources import Signal

logger = logging.getLogger(__name__)

_LIVENESS_SEMAPHORE_SIZE = 10
_LIVENESS_TIMEOUT = 5.0


_DEAD_STATUS_CODES = frozenset({404, 410})


async def _head_check(
    sem: asyncio.Semaphore,
    client: httpx.AsyncClient,
    signal: Signal,
) -> Signal | None:
    """Return signal if URL is likely live, None if confirmed gone.

    Only 404/410 are treated as confirmed-dead. 401/403/429/5xx may be
    transient or access-controlled; keep those signals. follow_redirects=False
    avoids cross-host redirect chains that could probe unexpected destinations.
    """
    async with sem:
        try:
            resp = await client.head(
                signal.url,
                follow_redirects=False,
                timeout=_LIVENESS_TIMEOUT,
            )
            if resp.status_code in _DEAD_STATUS_CODES:
                logger.debug("Liveness check: confirmed gone (%d): %s", resp.status_code, signal.url[:100])
                return None
            return signal
        except (httpx.TransportError, httpx.TimeoutException, httpx.InvalidURL, ValueError):
            logger.debug("Liveness check failed (network/url error): %s", signal.url[:100])
            return None


async def validate_signals_async(
    signals: list[Signal],
    blocklist: list[str],
    client: httpx.AsyncClient,
    check_liveness: bool = False,
) -> list[Signal]:
    """Deduplicate, blocklist-filter, and optionally verify URL liveness.

    Sync dedup + blocklist runs first (no I/O); HEAD checks follow in parallel
    when check_liveness is True.
    """
    valid = validate_signals(signals, blocklist)
    if not check_liveness or not valid:
        return valid

    sem = asyncio.Semaphore(_LIVENESS_SEMAPHORE_SIZE)
    results = await asyncio.gather(*(_head_check(sem, client, s) for s in valid))
    live = [s for s in results if s is not None]

    dropped = len(valid) - len(live)
    if dropped:
        logger.info("Liveness check: %d/%d signals confirmed live, %d dropped", len(live), len(valid), dropped)
    return live


def validate_signals(
    signals: list[Signal],
    blocklist: list[str],
) -> list[Signal]:
    """Deduplicate by URL and filter out blocked signals.

    Args:
        signals: Raw signals from source adapters.
        blocklist: List of blocked keywords (case-insensitive substring match).

    Returns:
        Deduplicated, filtered list of signals.
    """
    seen_urls: set[str] = set()
    result: list[Signal] = []

    for signal in signals:
        # Normalize URL for dedup
        url = signal.url.rstrip("/").lower()
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)

        # Validate URL has scheme + host
        parsed = urlparse(signal.url)
        if not parsed.scheme or not parsed.netloc:
            logger.debug("Skipping signal with invalid URL: %s", signal.url[:100])
            continue

        # Blocklist check
        text = f"{signal.title} {signal.snippet}".lower()
        if any(word.lower() in text for word in blocklist):
            logger.debug("Blocked signal: %s", signal.title[:80])
            continue

        result.append(signal)

    removed = len(signals) - len(result)
    if removed:
        logger.info("Validated signals: %d kept, %d removed", len(result), removed)
    return result
