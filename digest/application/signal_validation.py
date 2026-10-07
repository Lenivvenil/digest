"""Coordinate pure signal validation and optional bounded HTTP liveness checks."""

from __future__ import annotations

import asyncio
import logging

import httpx

from digest.adapters.http.signal_liveness import check_signal_liveness
from digest.domain.investigation.signals import Signal
from digest.domain.investigation.validation import validate_signals

logger = logging.getLogger(__name__)

_LIVENESS_SEMAPHORE_SIZE = 10


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
    results = await asyncio.gather(*(check_signal_liveness(sem, client, s) for s in valid))
    live = [s for s in results if s is not None]

    dropped = len(valid) - len(live)
    if dropped:
        logger.info("Liveness check: %d/%d signals confirmed live, %d dropped", len(live), len(valid), dropped)
    return live
