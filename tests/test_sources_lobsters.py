"""Tests for src.irritator.sources.lobsters."""

from __future__ import annotations

import asyncio

import httpx
import pytest
import respx

import digest.irritator.sources.lobsters as lob_module
from digest.irritator.sources.lobsters import _sanitize_query, search_lobsters


@pytest.fixture(autouse=True)
def _reset_semaphore(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset the module-level semaphore before each test.

    Each pytest-asyncio test gets a fresh event loop; a semaphore bound to a
    previous loop cannot be acquired and would cause 'Future attached to a
    different loop' errors or silent hangs.
    """
    monkeypatch.setattr(lob_module, "_semaphore", None)


class TestSanitizeQuery:
    def test_strips_operators(self) -> None:
        assert _sanitize_query("AI: failure (2024)") == "AI failure 2024"

    def test_keeps_apostrophes(self) -> None:
        assert _sanitize_query("didn't work") == "didn't work"

    def test_collapses_whitespace(self) -> None:
        assert _sanitize_query("foo   :   bar") == "foo bar"

    def test_unicode_cyrillic(self) -> None:
        assert _sanitize_query("провал ИИ: 2024") == "провал ИИ 2024"

    def test_all_operators_returns_empty(self) -> None:
        assert _sanitize_query(":::") == ""

    def test_empty_input(self) -> None:
        assert _sanitize_query("") == ""


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

    async def test_429_raises(self) -> None:
        with respx.mock:
            respx.get("https://lobste.rs/search.json").mock(
                return_value=httpx.Response(429)
            )
            async with httpx.AsyncClient() as client:
                with pytest.raises(httpx.HTTPStatusError):
                    await search_lobsters("rate limited", None, client)

    async def test_400_raises(self) -> None:
        with respx.mock:
            respx.get("https://lobste.rs/search.json").mock(
                return_value=httpx.Response(400)
            )
            async with httpx.AsyncClient() as client:
                with pytest.raises(httpx.HTTPStatusError):
                    await search_lobsters("bad query", None, client)

    async def test_query_sanitized_before_send(self) -> None:
        captured: list[str] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request.url.params["q"])
            return httpx.Response(200, json=[])

        with respx.mock:
            respx.get("https://lobste.rs/search.json").mock(side_effect=handler)
            async with httpx.AsyncClient() as client:
                await search_lobsters("AI: failure (2024)", None, client)

        assert captured == ["AI failure 2024"]

    async def test_empty_query_skips_http(self) -> None:
        with respx.mock:
            respx.get("https://lobste.rs/search.json").mock(
                return_value=httpx.Response(200, json=[{"title": "should not appear"}])
            )
            async with httpx.AsyncClient() as client:
                signals = await search_lobsters(":::", None, client)

        assert signals == []
        assert respx.calls.call_count == 0

    async def test_blank_query_skips_http(self) -> None:
        with respx.mock:
            respx.get("https://lobste.rs/search.json").mock(
                return_value=httpx.Response(200, json=[])
            )
            async with httpx.AsyncClient() as client:
                signals = await search_lobsters("", None, client)

        assert signals == []
        assert respx.calls.call_count == 0

    async def test_semaphore_limits_concurrency(self) -> None:
        """Peak concurrent in-flight requests must not exceed 1."""
        peak = [0]
        current = [0]
        call_count = [0]

        async def handler(request: httpx.Request) -> httpx.Response:
            call_count[0] += 1
            current[0] += 1
            peak[0] = max(peak[0], current[0])
            # sleep is load-bearing: without a yield point the event loop never
            # switches tasks and the test trivially passes even without a semaphore
            await asyncio.sleep(0.01)
            current[0] -= 1
            return httpx.Response(200, json=[])

        with respx.mock:
            respx.get("https://lobste.rs/search.json").mock(side_effect=handler)
            async with httpx.AsyncClient() as client:
                await asyncio.gather(
                    *[search_lobsters(f"query {i}", None, client) for i in range(5)]
                )

        assert call_count[0] == 5, f"Expected 5 handler calls, got {call_count[0]}"
        assert peak[0] == 1, f"Expected peak concurrency 1, got {peak[0]}"
