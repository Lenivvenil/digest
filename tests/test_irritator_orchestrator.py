"""Tests for digest.irritator.run_irritator() orchestrator."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from digest.adapters.models.execution import ModelExecution
from digest.irritator import IrritatorStatus, run_irritator
from digest.irritator.ranker import MAX_RANKING_JSON_CHARS
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
    @pytest.mark.parametrize(("include_small", "counter_signal"), [(False, False), (True, False), (True, True)])
    async def test_whole_evidence_omissions_are_incomplete(
        self, include_small: bool, counter_signal: bool,
    ) -> None:
        execution = ModelExecution()
        narrative = make_narrative()
        queries = {narrative.claim: [MagicMock()]}
        raw = [make_signal(snippet="x" * MAX_RANKING_JSON_CHARS)]
        if include_small:
            raw.append(make_signal(url="https://example.com/admitted"))
        ranking = [{"index": 1, "score": 8, "relation": "complicates", "reasoning": "A material condition."}]
        model = AsyncMock(return_value=(json.dumps(ranking if counter_signal else []), {}))
        with (
            patch(f"{_MODULE}.extract_narratives", AsyncMock(return_value=[narrative])),
            patch(f"{_MODULE}.generate_queries", AsyncMock(return_value=queries)),
            patch(f"{_MODULE}.search_all_sources", AsyncMock(return_value=raw)),
            patch(f"{_MODULE}.validate_signals_async", AsyncMock(return_value=raw)),
            patch("digest.irritator.ranker.complete", model),
        ):
            _, ranked, status = await run_irritator([], _make_config(), _make_client(), execution=execution)
        assert status.level == "incomplete"
        assert status.diagnostics.ranking_omitted == 1
        assert status.diagnostics.ranking_successful == int(include_small)
        assert status.diagnostics.ranking_failed == 0
        assert "1 whole signals omitted by evidence budget" in status.text
        assert model.await_count == int(include_small)
        assert len(ranked) == int(counter_signal)
        if ranked:
            assert ranked[0].signal is raw[1]

    async def test_narrative_extraction_failure_returns_error(self) -> None:
        execution = ModelExecution()
        cfg = _make_config()
        with patch(f"{_MODULE}.extract_narratives", AsyncMock(side_effect=RuntimeError("boom"))):
            narratives, ranked, status = await run_irritator([], cfg, _make_client(), execution=execution)
        assert status.level == "error"
        assert narratives == []
        assert ranked == []

    async def test_empty_narratives_returns_empty(self) -> None:
        execution = ModelExecution()
        cfg = _make_config()
        with patch(f"{_MODULE}.extract_narratives", AsyncMock(return_value=[])):
            narratives, ranked, status = await run_irritator([], cfg, _make_client(), execution=execution)
        assert status.level == "empty"
        assert narratives == []

    async def test_query_generation_failure_returns_error(self) -> None:
        execution = ModelExecution()
        cfg = _make_config()
        narrative = make_narrative()
        with (
            patch(f"{_MODULE}.extract_narratives", AsyncMock(return_value=[narrative])),
            patch(f"{_MODULE}.generate_queries", AsyncMock(side_effect=RuntimeError("fail"))),
        ):
            narratives, ranked, status = await run_irritator([], cfg, _make_client(), execution=execution)
        assert status.level == "error"
        assert narratives == [narrative]
        assert ranked == []

    async def test_empty_queries_returns_empty(self) -> None:
        execution = ModelExecution()
        cfg = _make_config()
        narrative = make_narrative()
        with (
            patch(f"{_MODULE}.extract_narratives", AsyncMock(return_value=[narrative])),
            patch(f"{_MODULE}.generate_queries", AsyncMock(return_value={})),
        ):
            narratives, ranked, status = await run_irritator([], cfg, _make_client(), execution=execution)
        assert status.level == "empty"

    async def test_signal_search_failure_returns_error(self) -> None:
        execution = ModelExecution()
        cfg = _make_config()
        narrative = make_narrative()
        queries = {narrative.claim: [MagicMock()]}
        with (
            patch(f"{_MODULE}.extract_narratives", AsyncMock(return_value=[narrative])),
            patch(f"{_MODULE}.generate_queries", AsyncMock(return_value=queries)),
            patch(f"{_MODULE}.search_all_sources", AsyncMock(side_effect=RuntimeError("net"))),
        ):
            narratives, ranked, status = await run_irritator([], cfg, _make_client(), execution=execution)
        assert status.level == "error"

    @pytest.mark.parametrize("successful,failed,unavailable,expected", [
        (1, 0, 0, "empty"), (0, 1, 1, "error"), (1, 1, 0, "incomplete"),
    ])
    async def test_empty_search_is_distinct_from_source_failures(
        self, successful: int, failed: int, unavailable: int, expected: str,
    ) -> None:
        execution = ModelExecution()
        cfg = _make_config()
        narrative = make_narrative()
        queries = {narrative.claim: [MagicMock()]}
        async def search(_queries, _config, _client, *, diagnostics):
            diagnostics.successful, diagnostics.failed, diagnostics.unavailable = successful, failed, unavailable
            return []

        with (
            patch(f"{_MODULE}.extract_narratives", AsyncMock(return_value=[narrative])),
            patch(f"{_MODULE}.generate_queries", AsyncMock(return_value=queries)),
            patch(f"{_MODULE}.search_all_sources", AsyncMock(side_effect=search)),
        ):
            _, ranked, status = await run_irritator([], cfg, _make_client(), execution=execution)
        assert status.level == expected
        assert ranked == []

    async def test_all_signals_filtered_returns_empty(self) -> None:
        execution = ModelExecution()
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
            _, ranked, status = await run_irritator([], cfg, _make_client(), execution=execution)
        assert status.level == "empty"
        assert ranked == []

    async def test_ranking_produces_results_returns_ok(self) -> None:
        execution = ModelExecution()
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
            narratives, ranked, status = await run_irritator([], cfg, _make_client(), execution=execution)
        assert status.level == "ok"
        assert len(ranked) == 1
        assert ranked[0] is ranked_signal

    async def test_all_ranking_failures_are_error_not_empty(self) -> None:
        execution = ModelExecution()
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
            _, ranked, status = await run_irritator([], cfg, _make_client(), execution=execution)
        assert status.level == "error"
        assert status.diagnostics.ranking_failed == 1
        assert ranked == []

    async def test_partial_source_and_ranking_failures_preserve_valid_results(self) -> None:
        execution = ModelExecution()
        cfg = _make_config()
        narratives = [make_narrative(), make_narrative(claim="Second source-backed hypothesis")]
        queries = {item.claim: [MagicMock()] for item in narratives}
        raw, ranked_signal = [make_signal()], make_ranked_signal()

        async def search(_queries, _config, _client, *, diagnostics):
            diagnostics.successful, diagnostics.failed = 1, 1
            return raw

        with (
            patch(f"{_MODULE}.extract_narratives", AsyncMock(return_value=narratives)),
            patch(f"{_MODULE}.generate_queries", AsyncMock(return_value=queries)),
            patch(f"{_MODULE}.search_all_sources", AsyncMock(side_effect=search)),
            patch(f"{_MODULE}.validate_signals_async", AsyncMock(return_value=raw)),
            patch(f"{_MODULE}.rank_signals", AsyncMock(side_effect=[RuntimeError("synthetic"), [ranked_signal]])),
        ):
            _, ranked, status = await run_irritator([], cfg, _make_client(), execution=execution)
        assert isinstance(status, IrritatorStatus) and status.level == "incomplete"
        assert ranked == [ranked_signal]
        assert status.diagnostics.search.failed == 1
        assert status.diagnostics.ranking_successful == status.diagnostics.ranking_failed == 1

    async def test_injected_client_passed_to_search_and_validate(self) -> None:
        execution = ModelExecution()
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
            await run_irritator([], cfg, client, execution=execution)
        assert mock_search.call_args[0][2] is client
        assert mock_validate.call_args[0][2] is client


@pytest.mark.asyncio
@pytest.mark.parametrize("partial", [False, True])
async def test_invalid_generated_query_remains_incomplete_through_legacy_pipeline(partial: bool) -> None:
    execution = ModelExecution()
    import json

    from digest.irritator.query_generator import generate_queries
    from scripts.review_fixture import fixture_config

    narratives = [make_narrative(claim="First claim")]
    responses = [(json.dumps([{
        "query": "research on limitations of current AI agents in automating credential theft",
        "intent": "Find limitations",
    }]), {})]
    if partial:
        narratives.append(make_narrative(claim="Second claim"))
        responses.append((json.dumps([{
            "query": '"AI agents" limitations', "intent": "Find limitations",
        }]), {}))
    model = AsyncMock(side_effect=responses)
    search = AsyncMock(return_value=[])
    with (
        patch(f"{_MODULE}.extract_narratives", AsyncMock(return_value=narratives)),
        patch(f"{_MODULE}.generate_queries", generate_queries),
        patch("digest.irritator.query_generator.complete", model),
        patch(f"{_MODULE}.search_all_sources", search),
    ):
        _, ranked, status = await run_irritator([], fixture_config(), _make_client(), execution=execution)
    assert status.level == "incomplete" and ranked == []
    assert status.diagnostics.queries.failed == 1
    assert status.diagnostics.queries.successful == int(partial)
    assert search.await_count == int(partial)
    assert model.await_count == 1 + int(partial)
