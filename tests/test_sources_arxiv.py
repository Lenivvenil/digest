"""Tests for src.irritator.sources.arxiv."""

from __future__ import annotations

import httpx
import pytest
import respx

from digest.irritator.sources.arxiv import search_arxiv

_ARXIV_ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>Limits of LLM Reasoning</title>
    <link href="https://arxiv.org/abs/2401.00001"/>
    <summary>This paper challenges the assumption that...</summary>
    <published>2026-01-15T00:00:00Z</published>
  </entry>
</feed>"""

_ARXIV_EMPTY = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
</feed>"""


@pytest.mark.asyncio
class TestSearchArxiv:
    async def test_success(self) -> None:
        with respx.mock:
            route = respx.get("https://export.arxiv.org/api/query").mock(
                return_value=httpx.Response(200, text=_ARXIV_ATOM)
            )
            async with httpx.AsyncClient() as client:
                signals = await search_arxiv('"LLM reasoning" café limits', None, client)
            assert route.calls[0].request.url.params["search_query"] == (
                'all:"LLM reasoning" AND all:café AND all:limits'
            )

        assert len(signals) == 1
        assert signals[0].title == "Limits of LLM Reasoning"
        assert signals[0].source_name == "arxiv"
        assert "2401.00001" in signals[0].url

    async def test_empty_results(self) -> None:
        with respx.mock:
            respx.get("https://export.arxiv.org/api/query").mock(
                return_value=httpx.Response(200, text=_ARXIV_EMPTY)
            )
            async with httpx.AsyncClient() as client:
                signals = await search_arxiv("nothing", None, client)

        assert signals == []

    async def test_complete_abstract_keeps_late_condition(self) -> None:
        abstract = "Observed benefits in the evaluated sample. " * 20
        abstract += "\nThese benefits apply only when an expert reviews every proposed action."
        title = "Limits of LLM\nReasoning"
        feed = (_ARXIV_ATOM.replace("This paper challenges the assumption that...", abstract)
                .replace("Limits of LLM Reasoning", title))
        with respx.mock:
            respx.get("https://export.arxiv.org/api/query").mock(
                return_value=httpx.Response(200, text=feed)
            )
            async with httpx.AsyncClient() as client:
                signals = await search_arxiv("agent review", None, client)

        assert len(abstract) > 500
        assert signals[0].snippet == abstract
        assert signals[0].title == title

    async def test_http_error_raises(self) -> None:
        with respx.mock:
            respx.get("https://export.arxiv.org/api/query").mock(
                return_value=httpx.Response(503)
            )
            async with httpx.AsyncClient() as client:
                with pytest.raises(httpx.HTTPStatusError):
                    await search_arxiv("fail", None, client)


@pytest.mark.asyncio
async def test_concurrent_arxiv_requests_are_serial_and_spaced_without_real_sleep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    from digest.irritator.sources import arxiv

    clock = [0.0]
    starts = []
    active = [0]
    original_sleep = asyncio.sleep

    async def wait(delay):
        clock[0] += delay
        await original_sleep(0)

    async def response(request):
        active[0] += 1
        assert active[0] == 1
        starts.append(clock[0])
        await original_sleep(0)
        active[0] -= 1
        return httpx.Response(200, request=request, text=_ARXIV_EMPTY)

    monkeypatch.setattr(arxiv, "_requests", None)
    monkeypatch.setattr(arxiv, "monotonic", lambda: clock[0])
    monkeypatch.setattr(arxiv.asyncio, "sleep", wait)
    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as first, \
            httpx.AsyncClient(transport=httpx.MockTransport(response)) as second:
        await asyncio.gather(search_arxiv("first", None, first), search_arxiv("second", None, second))
    assert starts == [0.0, 3.0] and active[0] == 0
