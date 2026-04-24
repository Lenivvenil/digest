"""Tests for src.irritator.sources.lobsters."""

from __future__ import annotations

import httpx
import pytest
import respx

from digest.irritator.sources.lobsters import search_lobsters


@pytest.mark.asyncio
class TestSearchLobsters:
    async def test_success_list_format(self) -> None:
        items = [
            {
                "url": "https://example.com/article",
                "title": "Counter signal post",
                "description": "Detailed description",
                "created_at": "2026-01-05T08:00:00Z",
                "score": 30,
            },
        ]
        with respx.mock:
            respx.get("https://lobste.rs/search.json").mock(
                return_value=httpx.Response(200, json=items)
            )
            async with httpx.AsyncClient() as client:
                signals = await search_lobsters("test query", None, client)

        assert len(signals) == 1
        assert signals[0].title == "Counter signal post"
        assert signals[0].source_name == "lobsters"
        assert signals[0].score == 30.0

    async def test_success_dict_format(self) -> None:
        data = {
            "results": [
                {
                    "url": "https://example.com/a",
                    "title": "Result",
                    "description": "",
                    "created_at": "",
                    "score": 10,
                },
            ]
        }
        with respx.mock:
            respx.get("https://lobste.rs/search.json").mock(
                return_value=httpx.Response(200, json=data)
            )
            async with httpx.AsyncClient() as client:
                signals = await search_lobsters("test", None, client)

        assert len(signals) == 1

    async def test_fallback_to_short_id_url(self) -> None:
        items = [{"short_id_url": "https://lobste.rs/s/abc", "title": "T", "created_at": "", "score": 0}]
        with respx.mock:
            respx.get("https://lobste.rs/search.json").mock(
                return_value=httpx.Response(200, json=items)
            )
            async with httpx.AsyncClient() as client:
                signals = await search_lobsters("test", None, client)

        assert signals[0].url == "https://lobste.rs/s/abc"

    async def test_empty_results(self) -> None:
        with respx.mock:
            respx.get("https://lobste.rs/search.json").mock(
                return_value=httpx.Response(200, json=[])
            )
            async with httpx.AsyncClient() as client:
                signals = await search_lobsters("nothing", None, client)

        assert signals == []

    async def test_http_error_raises(self) -> None:
        with respx.mock:
            respx.get("https://lobste.rs/search.json").mock(
                return_value=httpx.Response(500)
            )
            async with httpx.AsyncClient() as client:
                with pytest.raises(httpx.HTTPStatusError):
                    await search_lobsters("fail", None, client)
