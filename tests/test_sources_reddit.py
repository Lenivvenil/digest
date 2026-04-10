"""Tests for src.irritator.sources.reddit."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

from src.irritator.sources.reddit import search_reddit


def _make_config(subreddits: list[str] | None = None) -> Any:
    class IrritatorCfg:
        reddit_subreddits = subreddits or ["programming"]
    class Cfg:
        irritator = IrritatorCfg()
    return Cfg()


def _reddit_response(posts: list[dict[str, object]] | None = None) -> dict[str, object]:
    if posts is None:
        posts = [
            {
                "title": "Counter view on AI",
                "permalink": "/r/programming/comments/abc/counter_view/",
                "selftext": "Detailed counter argument",
                "created_utc": 1700000000,
                "score": 100,
            },
        ]
    return {"data": {"children": [{"data": p} for p in posts]}}


@pytest.mark.asyncio
class TestSearchReddit:
    async def test_success(self) -> None:
        with respx.mock:
            respx.get("https://www.reddit.com/r/programming/search.json").mock(
                return_value=httpx.Response(200, json=_reddit_response())
            )
            async with httpx.AsyncClient() as client:
                signals = await search_reddit("AI risk", _make_config(), client)

        assert len(signals) == 1
        assert signals[0].title == "Counter view on AI"
        assert signals[0].source_name == "reddit"
        assert signals[0].score == 100.0

    async def test_multiple_subreddits(self) -> None:
        cfg = _make_config(["programming", "fintech"])
        with respx.mock:
            respx.get("https://www.reddit.com/r/programming+fintech/search.json").mock(
                return_value=httpx.Response(200, json=_reddit_response())
            )
            async with httpx.AsyncClient() as client:
                signals = await search_reddit("test", cfg, client)

        assert len(signals) == 1

    async def test_empty_results(self) -> None:
        with respx.mock:
            respx.get("https://www.reddit.com/r/programming/search.json").mock(
                return_value=httpx.Response(200, json={"data": {"children": []}})
            )
            async with httpx.AsyncClient() as client:
                signals = await search_reddit("nothing", _make_config(), client)

        assert signals == []

    async def test_http_error_raises(self) -> None:
        with respx.mock:
            respx.get("https://www.reddit.com/r/programming/search.json").mock(
                return_value=httpx.Response(429)
            )
            async with httpx.AsyncClient() as client:
                with pytest.raises(httpx.HTTPStatusError):
                    await search_reddit("fail", _make_config(), client)
