"""Tests for src.irritator.sources.hackernews."""

from __future__ import annotations

import httpx
import pytest
import respx

from src.irritator.sources.hackernews import search_hackernews


def _hn_response(hits: list[dict[str, object]] | None = None) -> dict[str, object]:
    if hits is None:
        hits = [
            {
                "objectID": "123",
                "title": "AI considered harmful",
                "url": "https://example.com/ai",
                "story_text": "Counter argument here",
                "created_at": "2026-01-01T00:00:00Z",
                "points": 42,
            },
        ]
    return {"hits": hits}


@pytest.mark.asyncio
class TestSearchHackerNews:
    async def test_success(self) -> None:
        with respx.mock:
            respx.get("https://hn.algolia.com/api/v1/search").mock(
                return_value=httpx.Response(200, json=_hn_response())
            )
            async with httpx.AsyncClient() as client:
                signals = await search_hackernews("AI risk", None, client)

        assert len(signals) == 1
        assert signals[0].title == "AI considered harmful"
        assert signals[0].url == "https://example.com/ai"
        assert signals[0].source_name == "hackernews"
        assert signals[0].score == 42.0

    async def test_no_url_uses_hn_link(self) -> None:
        hits = [{"objectID": "456", "title": "Self post", "created_at": "", "points": 0}]
        with respx.mock:
            respx.get("https://hn.algolia.com/api/v1/search").mock(
                return_value=httpx.Response(200, json=_hn_response(hits))
            )
            async with httpx.AsyncClient() as client:
                signals = await search_hackernews("test", None, client)

        assert "item?id=456" in signals[0].url

    async def test_empty_results(self) -> None:
        with respx.mock:
            respx.get("https://hn.algolia.com/api/v1/search").mock(
                return_value=httpx.Response(200, json={"hits": []})
            )
            async with httpx.AsyncClient() as client:
                signals = await search_hackernews("nothing", None, client)

        assert signals == []

    async def test_http_error_raises(self) -> None:
        with respx.mock:
            respx.get("https://hn.algolia.com/api/v1/search").mock(
                return_value=httpx.Response(500)
            )
            async with httpx.AsyncClient() as client:
                with pytest.raises(httpx.HTTPStatusError):
                    await search_hackernews("fail", None, client)
