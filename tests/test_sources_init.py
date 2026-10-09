"""Tests for src.irritator.sources (search_all_sources orchestrator)."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from digest.irritator.query_generator import SearchQuery
from digest.irritator.sources import (
    SourceUnavailableError,
    _import_adapters,
    search_all_sources,
    validate_search_response,
)
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
                batch = await search_all_sources(queries, _make_config(["hackernews", "reddit"]), client)

        assert len(batch.signals) == 2
        assert asdict(batch.diagnostics) == {"successful": 2, "failed": 0, "unavailable": 0}
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
                batch = await search_all_sources(
                    queries, _make_config(["reddit", "hackernews", "hackernews"]), client,
                )

        assert [signal.source_name for signal in batch.signals] == ["hackernews", "hackernews", "reddit"] * 2
        assert asdict(batch.diagnostics) == {"successful": 6, "failed": 0, "unavailable": 0}
        assert mock_hn.call_count == 4
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
                batch = await search_all_sources(queries, _make_config(["hackernews"]), client)

        assert batch.signals == []
        assert asdict(batch.diagnostics) == {"successful": 0, "failed": 0, "unavailable": 1}
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
                batch = await search_all_sources(queries, _make_config(["hackernews", "reddit"]), client)

        assert len(batch.signals) == 1
        assert batch.signals[0].source_name == "hackernews"
        assert asdict(batch.diagnostics) == {"successful": 1, "failed": 1, "unavailable": 0}

    async def test_empty_queries(self) -> None:
        async with httpx.AsyncClient() as client:
            batch = await search_all_sources([], _make_config(), client)
        assert batch.signals == []
        assert batch.diagnostics.total == 0

    async def test_outcomes_distinguish_valid_empty_failed_and_unavailable(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        adapters = {
            "hackernews": AsyncMock(return_value=[_make_signal("hackernews")]),
            "arxiv": AsyncMock(return_value=[]),
            "reddit": AsyncMock(side_effect=RuntimeError("private response credential")),
            "devto": AsyncMock(side_effect=SourceUnavailableError("private configuration")),
        }
        with patch.dict("digest.irritator.sources._ADAPTERS", adapters, clear=True):
            async with httpx.AsyncClient() as client:
                batch = await search_all_sources(
                    [_make_query("private search query"), _make_query("second private query")],
                    _make_config([*adapters, "unknown"]), client,
                )

        assert len(batch.signals) == 2
        diagnostics = batch.diagnostics
        assert diagnostics.successful == 4
        assert diagnostics.failed == 2
        assert diagnostics.unavailable == 4
        assert diagnostics.total == 10
        assert "Source reddit failed (RuntimeError)" in caplog.text
        assert "Source devto unavailable (SourceUnavailableError)" in caplog.text
        assert "Source unknown unavailable (SourceUnavailableError)" in caplog.text
        assert "private" not in caplog.text
        assert "credential" not in caplog.text

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


@pytest.mark.parametrize(("source", "body"), [
    ("hackernews", {"hits": []}),
    ("lobsters", []),
    ("lobsters", {"results": []}),
    ("reddit", {"data": {"children": []}}),
])
def test_valid_empty_search_envelopes(source: str, body: Any) -> None:
    assert validate_search_response(httpx.Response(200, json=body), source) == body


@pytest.mark.parametrize(("source", "message", "body"), [
    ("hackernews", "Invalid Hacker News search response.", {"hits": None}),
    ("lobsters", "Invalid Lobsters search response.", {"results": {}}),
    ("reddit", "Invalid Reddit search response.", {"data": []}),
])
def test_invalid_search_envelopes(source: str, message: str, body: Any) -> None:
    with pytest.raises(ValueError) as caught:
        validate_search_response(httpx.Response(200, json=body), source)
    assert str(caught.value) == message


@pytest.mark.parametrize("source", ["hackernews", "lobsters", "reddit"])
def test_non_json_search_response_has_fixed_error(source: str) -> None:
    with pytest.raises(ValueError) as caught:
        validate_search_response(httpx.Response(200, text="private response body"), source)
    assert "private" not in str(caught.value)


def test_valid_empty_atom_feed() -> None:
    root = validate_search_response(
        httpx.Response(200, text='<feed xmlns="http://www.w3.org/2005/Atom"/>'), "arxiv",
    )
    assert root.tag == "{http://www.w3.org/2005/Atom}feed"


@pytest.mark.parametrize("body", [
    "<html>error</html>",
    "<feed>",
    '<feed xmlns="http://www.w3.org/2005/Atom"><entry>'
    '<id>http://arxiv.org/api/errors#incorrect_id_format</id></entry></feed>',
])
def test_invalid_or_error_atom_feed(body: str) -> None:
    with pytest.raises(ValueError, match=r"^Invalid or error arXiv feed\.$"):
        validate_search_response(httpx.Response(200, text=body), "arxiv")
