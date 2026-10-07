"""Lobsters adapter retained for configuration compatibility.

No supported JSON search contract is verified in the maintained server source.
Do not silently substitute an unrelated feed or scrape an undocumented endpoint.
"""

from __future__ import annotations

from typing import Any

import httpx

from digest.domain.investigation.signals import Signal
from digest.irritator.sources import SourceUnavailableError, _register

UNAVAILABLE_REASON = "Lobsters search is unavailable: no supported JSON search contract is verified."


@_register("lobsters")
async def search_lobsters(
    query: str,
    config: Any,
    client: httpx.AsyncClient,
) -> list[Signal]:
    """Report unavailable without a network request or configuration mutation."""
    raise SourceUnavailableError(UNAVAILABLE_REASON)
