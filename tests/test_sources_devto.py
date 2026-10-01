"""The DEV.to listing API must never masquerade as full-text search."""

from __future__ import annotations

import logging

import httpx
import pytest
import respx

from digest.irritator.sources.devto import search_devto


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["AI failure criticism", "zzzx_nonexistent_749321", ""])
async def test_unsupported_search_skips_network(
    query: str, caplog: pytest.LogCaptureFixture,
) -> None:
    with respx.mock(assert_all_called=False) as router:
        with caplog.at_level(logging.WARNING):
            async with httpx.AsyncClient() as client:
                result = await search_devto(query, None, client)
    assert result == []
    assert not router.calls
    assert "DEV.to search is disabled" in caplog.text
