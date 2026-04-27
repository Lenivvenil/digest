"""Tests for digest.irritator.run_irritator() orchestrator."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from digest.irritator import IrritatorStatus, run_irritator
from tests.factories import make_narrative, make_ranked_signal, make_signal


def _make_config(check_liveness: bool = False) -> MagicMock:
    cfg = MagicMock()
    cfg.irritator.check_liveness = check_liveness
    cfg.irritator.min_signal_score = 7
    cfg.irritator.top_signals = 3
    cfg.filters.blocklist_keywords = []
    return cfg


def _make_client() -> MagicMock:
    return MagicMock(spec=httpx.AsyncClient)


_MODULE = "digest.irritator"


@pytest.mark.asyncio
class TestRunIrritator:
    async def test_narrative_extraction_failure_returns_error(self) -> None:
        cfg = _make_config()
        with patch(f"{_MODULE}.extract_narratives", AsyncMock(side_effect=RuntimeError("boom"))):
            narratives, ranked, status = await run_irritator([], cfg, _make_client())
        assert status.level == "error"
        assert narratives == []
        assert ranked == []

    async def test_empty_narratives_returns_empty(self) -> None:
        cfg = _make_config()
        with patch(f"{_MODULE}.extract_narratives", AsyncMock(return_value=[])):
            narratives, ranked, status = await run_irritator([], cfg, _make_client())
        assert status.level == "empty"
        assert narratives == []

    async def test_query_generation_failure_returns_error(self) -> None:
        cfg = _make_config()
        narrative = make_narrative()
        with (
            patch(f"{_MODULE}.extract_narratives", AsyncMock(return_value=[narrative])),
            patch(f"{_MODULE}.generate_queries", AsyncMock(side_effect=RuntimeError("fail"))),
        ):
            narratives, ranked, status = await run_irritator([], cfg, _make_client())
        assert status.level == "error"
        assert narratives == [narrative]
        assert ranked == []

    async def test_empty_queries_returns_empty(self) -> None:
        cfg = _make_config()
        narrative = make_narrative()
        with (
            patch(f"{_MODULE}.extract_narratives", AsyncMock(return_value=[narrative])),
            patch(f"{_MODULE}.generate_queries", AsyncMock(return_value={})),
        ):
            narratives, ranked, status = await run_irritator([], cfg, _make_client())
        assert status.level == "empty"

    async def test_signal_search_failure_returns_error(self) -> None:
        cfg = _make_config()
        narrative = make_narrative()
        queries = {narrative.claim: [MagicMock()]}
        with (
            patch(f"{_MODULE}.extract_narratives", AsyncMock(return_value=[narrative])),
            patch(f"{_MODULE}.generate_queries", AsyncMock(return_value=queries)),
            patch(f"{_MODULE}.search_all_sources", AsyncMock(side_effect=RuntimeError("net"))),
        ):
            narratives, ranked, status = await run_irritator([], cfg, _make_client())
        assert status.level == "error"

    async def test_no_signals_found_returns_empty(self) -> None:
        cfg = _make_config()
        narrative = make_narrative()
        queries = {narrative.claim: [MagicMock()]}
        with (
            patch(f"{_MODULE}.extract_narratives", AsyncMock(return_value=[narrative])),
            patch(f"{_MODULE}.generate_queries", AsyncMock(return_value=queries)),
            patch(f"{_MODULE}.search_all_sources", AsyncMock(return_value=[])),
        ):
            _, ranked, status = await run_irritator([], cfg, _make_client())
        assert status.level == "empty"
        assert ranked == []

    async def test_all_signals_filtered_returns_empty(self) -> None:
        cfg = _make_config()
        narrative = make_narrative()
        queries = {narrative.claim: [MagicMock()]}
        raw = [make_signal()]
        with (
            patch(f"{_MODULE}.extract_narratives", AsyncMock(return_value=[narrative])),
            patch(f"{_MODULE}.generate_queries", AsyncMock(return_value=queries)),
            patch(f"{_MODULE}.search_all_sources", AsyncMock(return_value=raw)),
            patch(f"{_MODULE}.validate_signals_async", AsyncMock(return_value=[])),
        ):
            _, ranked, status = await run_irritator([], cfg, _make_client())
        assert status.level == "empty"
        assert ranked == []

    async def test_ranking_produces_results_returns_ok(self) -> None:
        cfg = _make_config()
        narrative = make_narrative()
        queries = {narrative.claim: [MagicMock()]}
        raw = [make_signal()]
        ranked_signal = make_ranked_signal()
        with (
            patch(f"{_MODULE}.extract_narratives", AsyncMock(return_value=[narrative])),
            patch(f"{_MODULE}.generate_queries", AsyncMock(return_value=queries)),
            patch(f"{_MODULE}.search_all_sources", AsyncMock(return_value=raw)),
            patch(f"{_MODULE}.validate_signals_async", AsyncMock(return_value=raw)),
            patch(f"{_MODULE}.rank_signals", AsyncMock(return_value=[ranked_signal])),
        ):
            narratives, ranked, status = await run_irritator([], cfg, _make_client())
        assert status.level == "ok"
        assert len(ranked) == 1
        assert ranked[0] is ranked_signal

    async def test_per_narrative_ranking_failure_does_not_set_error(self) -> None:
        cfg = _make_config()
        narrative = make_narrative()
        queries = {narrative.claim: [MagicMock()]}
        raw = [make_signal()]
        with (
            patch(f"{_MODULE}.extract_narratives", AsyncMock(return_value=[narrative])),
            patch(f"{_MODULE}.generate_queries", AsyncMock(return_value=queries)),
            patch(f"{_MODULE}.search_all_sources", AsyncMock(return_value=raw)),
            patch(f"{_MODULE}.validate_signals_async", AsyncMock(return_value=raw)),
            patch(f"{_MODULE}.rank_signals", AsyncMock(side_effect=RuntimeError("rank fail"))),
        ):
            _, ranked, status = await run_irritator([], cfg, _make_client())
        assert status.level == "empty"
        assert ranked == []

    async def test_returns_typed_tuple(self) -> None:
        cfg = _make_config()
        narrative = make_narrative()
        queries = {narrative.claim: [MagicMock()]}
        raw = [make_signal()]
        ranked_signal = make_ranked_signal()
        with (
            patch(f"{_MODULE}.extract_narratives", AsyncMock(return_value=[narrative])),
            patch(f"{_MODULE}.generate_queries", AsyncMock(return_value=queries)),
            patch(f"{_MODULE}.search_all_sources", AsyncMock(return_value=raw)),
            patch(f"{_MODULE}.validate_signals_async", AsyncMock(return_value=raw)),
            patch(f"{_MODULE}.rank_signals", AsyncMock(return_value=[ranked_signal])),
        ):
            result = await run_irritator([], cfg, _make_client())
        narratives, ranked, status = result
        assert isinstance(narratives, list)
        assert isinstance(ranked, list)
        assert isinstance(status, IrritatorStatus)

    async def test_injected_client_passed_to_search_and_validate(self) -> None:
        cfg = _make_config()
        narrative = make_narrative()
        queries = {narrative.claim: [MagicMock()]}
        raw = [make_signal()]
        client = _make_client()
        mock_search = AsyncMock(return_value=raw)
        mock_validate = AsyncMock(return_value=raw)
        with (
            patch(f"{_MODULE}.extract_narratives", AsyncMock(return_value=[narrative])),
            patch(f"{_MODULE}.generate_queries", AsyncMock(return_value=queries)),
            patch(f"{_MODULE}.search_all_sources", mock_search),
            patch(f"{_MODULE}.validate_signals_async", mock_validate),
            patch(f"{_MODULE}.rank_signals", AsyncMock(return_value=[])),
        ):
            await run_irritator([], cfg, client)
        assert mock_search.call_args[0][2] is client
        assert mock_validate.call_args[0][2] is client
