"""The DEV.to listing API must never masquerade as full-text search."""

from __future__ import annotations

import httpx
import pytest
import respx

from digest.irritator.sources import SourceUnavailableError
from digest.irritator.sources.devto import search_devto


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["AI failure criticism", "zzzx_nonexistent_749321", ""])
async def test_unsupported_search_skips_network(query: str) -> None:
    with respx.mock(assert_all_called=False) as router:
        async with httpx.AsyncClient() as client:
            with pytest.raises(SourceUnavailableError, match="DEV.to search is disabled"):
                await search_devto(query, None, client)
    assert not router.calls
