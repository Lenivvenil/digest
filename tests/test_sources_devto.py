"""Tests for src.irritator.sources.devto."""

from __future__ import annotations

import httpx
import pytest
import respx

from src.irritator.sources.devto import search_devto


@pytest.mark.asyncio
class TestSearchDevto:
    async def test_success(self) -> None:
        articles = [
            {
                "url": "https://dev.to/user/ai-critique",
                "title": "Why AI hype is overblown",
                "description": "A critical look at AI claims",
                "published_at": "2026-01-10T12:00:00Z",
                "positive_reactions_count": 55,
            },
        ]
        with respx.mock:
            respx.get("https://dev.to/api/articles").mock(
                return_value=httpx.Response(200, json=articles)
            )
            async with httpx.AsyncClient() as client:
                signals = await search_devto("AI hype", None, client)

        assert len(signals) == 1
        assert signals[0].title == "Why AI hype is overblown"
        assert signals[0].source_name == "devto"
        assert signals[0].score == 55.0

    async def test_empty_results(self) -> None:
        with respx.mock:
            respx.get("https://dev.to/api/articles").mock(
                return_value=httpx.Response(200, json=[])
            )
            async with httpx.AsyncClient() as client:
                signals = await search_devto("nothing", None, client)

        assert signals == []

    async def test_http_error_raises(self) -> None:
        with respx.mock:
            respx.get("https://dev.to/api/articles").mock(
                return_value=httpx.Response(500)
            )
            async with httpx.AsyncClient() as client:
                with pytest.raises(httpx.HTTPStatusError):
                    await search_devto("fail", None, client)
