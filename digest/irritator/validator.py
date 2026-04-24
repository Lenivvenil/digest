"""Validate and deduplicate raw signals."""

from __future__ import annotations

import logging
from urllib.parse import urlparse

from digest.irritator.sources import Signal

logger = logging.getLogger(__name__)


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
