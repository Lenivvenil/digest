"""Optional HEAD liveness operation for investigation signals."""

from __future__ import annotations

import asyncio
import logging

import httpx

from digest.adapters.http.public_fetch import fetch_public
from digest.domain.investigation.signals import Signal

logger = logging.getLogger(__name__)

_LIVENESS_TIMEOUT = 5.0
_DEAD_STATUS_CODES = frozenset({404, 410})


async def check_signal_liveness(
    sem: asyncio.Semaphore,
    client: httpx.AsyncClient,
    signal: Signal,
) -> Signal | None:
    """Return signal if URL is likely live, None if unavailable or gone.

    Only 404/410 are treated as confirmed-dead. 401/403/429/5xx may be
    transient or access-controlled; keep those signals. The public acquisition
    boundary performs HEAD without redirects or a response body. The supplied
    client is retained for compatibility only and cannot bypass destination policy.
    """
    async with sem:
        try:
            resp = await fetch_public(
                signal.url, method="HEAD", timeout=_LIVENESS_TIMEOUT, max_bytes=0,
            )
            if resp.status_code in _DEAD_STATUS_CODES:
                logger.debug("Liveness check: confirmed gone (%d): %s", resp.status_code, signal.url[:100])
                return None
            return signal
        except (TimeoutError, httpx.TransportError, httpx.TimeoutException, httpx.InvalidURL, ValueError):
            logger.debug("Liveness check failed (network/url error): %s", signal.url[:100])
            return None
