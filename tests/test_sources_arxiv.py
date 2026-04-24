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
            respx.get("https://export.arxiv.org/api/query").mock(
                return_value=httpx.Response(200, text=_ARXIV_ATOM)
            )
            async with httpx.AsyncClient() as client:
                signals = await search_arxiv("LLM reasoning limits", None, client)

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

    async def test_http_error_raises(self) -> None:
        with respx.mock:
            respx.get("https://export.arxiv.org/api/query").mock(
                return_value=httpx.Response(503)
            )
            async with httpx.AsyncClient() as client:
                with pytest.raises(httpx.HTTPStatusError):
                    await search_arxiv("fail", None, client)
