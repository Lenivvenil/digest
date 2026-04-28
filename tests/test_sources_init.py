"""Tests for src.irritator.sources (search_all_sources orchestrator)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from digest.irritator.query_generator import SearchQuery
from digest.irritator.sources import _import_adapters, search_all_sources
from tests.factories import make_signal

# Pre-import all adapter modules so their @_register decorators fire before any
# patch.dict call. Without this, _import_adapters() inside search_all_sources()
# runs during patch.dict context and overwrites mocks with real functions.
_import_adapters()


def _make_query(query: str = "test failure") -> SearchQuery:
    return SearchQuery(query=query, intent="find counter-signals")


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
    async def test_fans_out_to_all_configured_sources(self) -> None:
        mock_hn = AsyncMock(return_value=[_make_signal("hackernews")])
        mock_reddit = AsyncMock(return_value=[_make_signal("reddit")])
        queries = [_make_query()]

        with patch.dict(
            "digest.irritator.sources._ADAPTERS",
            {"hackernews": mock_hn, "reddit": mock_reddit},
            clear=True,
        ):
            async with httpx.AsyncClient() as client:
                signals = await search_all_sources(queries, _make_config(["hackernews", "reddit"]), client)

        assert len(signals) == 2
        mock_hn.assert_called_once()
        mock_reddit.assert_called_once()

    async def test_multiple_queries_fan_to_all_sources(self) -> None:
        mock_hn = AsyncMock(return_value=[_make_signal("hackernews")])
        mock_reddit = AsyncMock(return_value=[_make_signal("reddit")])
        queries = [_make_query("q1"), _make_query("q2")]

        with patch.dict(
            "digest.irritator.sources._ADAPTERS",
            {"hackernews": mock_hn, "reddit": mock_reddit},
            clear=True,
        ):
            async with httpx.AsyncClient() as client:
                signals = await search_all_sources(queries, _make_config(["hackernews", "reddit"]), client)

        assert len(signals) == 4
        assert mock_hn.call_count == 2
        assert mock_reddit.call_count == 2

    async def test_unconfigured_source_adapter_not_called(self) -> None:
        mock_arxiv = AsyncMock(return_value=[_make_signal("arxiv")])
        queries = [_make_query()]

        with patch.dict(
            "digest.irritator.sources._ADAPTERS",
            {"arxiv": mock_arxiv},
            clear=True,
        ):
            async with httpx.AsyncClient() as client:
                # config only has hackernews — arxiv is registered but not configured
                signals = await search_all_sources(queries, _make_config(["hackernews"]), client)

        assert signals == []
        mock_arxiv.assert_not_called()

    async def test_graceful_degradation_on_failure(self) -> None:
        mock_good = AsyncMock(return_value=[_make_signal("hackernews")])
        mock_bad = AsyncMock(side_effect=RuntimeError("API down"))
        queries = [_make_query()]

        with patch.dict(
            "digest.irritator.sources._ADAPTERS",
            {"hackernews": mock_good, "reddit": mock_bad},
            clear=True,
        ):
            async with httpx.AsyncClient() as client:
                signals = await search_all_sources(queries, _make_config(["hackernews", "reddit"]), client)

        assert len(signals) == 1
        assert signals[0].source_name == "hackernews"

    async def test_empty_queries(self) -> None:
        async with httpx.AsyncClient() as client:
            signals = await search_all_sources([], _make_config(), client)
        assert signals == []

    async def test_query_string_passed_to_adapter(self) -> None:
        mock_adapter = AsyncMock(return_value=[])
        queries = [_make_query("AI failure criticism")]

        with patch.dict(
            "digest.irritator.sources._ADAPTERS",
            {"hackernews": mock_adapter},
            clear=True,
        ):
            async with httpx.AsyncClient() as client:
                await search_all_sources(queries, _make_config(["hackernews"]), client)

        called_query = mock_adapter.call_args[0][0]
        assert called_query == "AI failure criticism"
