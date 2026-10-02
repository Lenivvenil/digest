"""DEV.to adapter retained for configuration compatibility.

The documented /api/articles endpoint lists articles; it does not support
full-text `q` searches. Do not present its popular feed as counter-evidence.
"""

from __future__ import annotations

from typing import Any

import httpx

from digest.irritator.sources import Signal, SourceUnavailableError, _register


@_register("devto")
async def search_devto(
    query: str,
    config: Any,
    client: httpx.AsyncClient,
) -> list[Signal]:
    """Skip unsupported search rather than return unrelated popular articles."""
    raise SourceUnavailableError(
        "DEV.to search is disabled: /api/articles has no documented full-text "
        "query parameter. Remove devto from irritator.sources until a supported "
        "search adapter is available. See https://developers.forem.com/api/v1"
    )
