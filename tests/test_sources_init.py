"""Tests for src.irritator.sources (search_all_sources orchestrator)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from digest.irritator.query_generator import SearchQuery
from digest.irritator.sources import search_all_sources
from tests.factories import make_signal


def _make_query(source: str = "hackernews", query: str = "test") -> SearchQuery:
    return SearchQuery(query=query, target_source=source, intent="find counter-signals")


def _make_config(sources: list[str] | None = None) -> Any:
    class IrritatorCfg:
        pass
    class Cfg:
        irritator = IrritatorCfg()
    Cfg.irritator.sources = sources or ["hackernews", "reddit"]  # type: ignore[attr-defined]
    return Cfg()


def _make_signal(source: str = "hackernews") -> Any:
    return make_signal(source_name=source, title="Test", snippet="snippet")


@pytest.mark.asyncio
class TestSearchAllSources:
    async def test_dispatches_to_adapter(self) -> None:
        mock_adapter = AsyncMock(return_value=[_make_signal()])
        queries = [_make_query("hackernews")]

        with patch.dict("digest.irritator.sources._ADAPTERS", {"hackernews": mock_adapter}):
            async with httpx.AsyncClient() as client:
                signals = await search_all_sources(queries, _make_config(["hackernews"]), client)

        assert len(signals) == 1
        mock_adapter.assert_called_once()

    async def test_skips_unconfigured_source(self) -> None:
        mock_adapter = AsyncMock(return_value=[_make_signal()])
        queries = [_make_query("arxiv")]

        with patch.dict("digest.irritator.sources._ADAPTERS", {"arxiv": mock_adapter}):
            async with httpx.AsyncClient() as client:
                # config only has hackernews, not arxiv
                signals = await search_all_sources(queries, _make_config(["hackernews"]), client)

        assert signals == []
        mock_adapter.assert_not_called()

    async def test_graceful_degradation_on_failure(self) -> None:
        mock_good = AsyncMock(return_value=[_make_signal("hackernews")])
        mock_bad = AsyncMock(side_effect=RuntimeError("API down"))
        queries = [_make_query("hackernews"), _make_query("reddit")]

        with patch.dict("digest.irritator.sources._ADAPTERS", {"hackernews": mock_good, "reddit": mock_bad}):
            async with httpx.AsyncClient() as client:
                signals = await search_all_sources(queries, _make_config(["hackernews", "reddit"]), client)

        assert len(signals) == 1
        assert signals[0].source_name == "hackernews"

    async def test_empty_queries(self) -> None:
        async with httpx.AsyncClient() as client:
            signals = await search_all_sources([], _make_config(), client)
        assert signals == []

    async def test_multiple_queries_same_source(self) -> None:
        mock_adapter = AsyncMock(return_value=[_make_signal()])
        queries = [_make_query("hackernews", "q1"), _make_query("hackernews", "q2")]

        with patch.dict("digest.irritator.sources._ADAPTERS", {"hackernews": mock_adapter}):
            async with httpx.AsyncClient() as client:
                signals = await search_all_sources(queries, _make_config(["hackernews"]), client)

        assert len(signals) == 2
        assert mock_adapter.call_count == 2
