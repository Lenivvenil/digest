"""Tests for src.irritator.query_generator."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from src.irritator.narrative_extractor import Narrative
from src.irritator.query_generator import (
    SearchQuery,
    _build_prompt,
    _parse_queries,
    generate_queries,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_narrative(
    claim: str = "AI will replace all developers",
    category: str = "AI",
) -> Narrative:
    return Narrative(
        claim=claim,
        category=category,
        implicit_assumptions=["AI is infallible", "Developer skills are commoditized"],
        why_worth_challenging="This ignores documented failures.",
    )


def _valid_query_dicts(n: int = 3) -> list[dict]:
    sources = ["hackernews", "reddit", "arxiv"]
    return [
        {
            "query": f"search query {i}",
            "target_source": sources[i % len(sources)],
            "intent": f"Find evidence about {i}",
        }
        for i in range(n)
    ]


def _make_config(
    language: str = "ru",
    queries_per_narrative: int = 3,
    sources: list[str] | None = None,
):
    class IrritatorCfg:
        pass
    class RadarCfg:
        pass
    class Cfg:
        irritator = IrritatorCfg()
        radar = RadarCfg()

    Cfg.radar.language = language
    Cfg.irritator.queries_per_narrative = queries_per_narrative
    Cfg.irritator.sources = sources or ["hackernews", "reddit", "arxiv"]
    return Cfg()


# ---------------------------------------------------------------------------
# _build_prompt tests
# ---------------------------------------------------------------------------

class TestBuildPrompt:
    def test_russian_prompt(self) -> None:
        n = _make_narrative()
        messages = _build_prompt(n, "ru", 3, ["hackernews", "reddit"])

        assert len(messages) == 2
        assert messages[0]["role"] == "system"
        assert "поисковый аналитик" in messages[0]["content"]
        assert n.claim in messages[1]["content"]
        assert "hackernews, reddit" in messages[1]["content"]

    def test_english_prompt(self) -> None:
        n = _make_narrative()
        messages = _build_prompt(n, "en", 5, ["arxiv"])

        assert "search analyst" in messages[0]["content"]
        assert n.claim in messages[1]["content"]
        assert "5" in messages[1]["content"]

    def test_includes_assumptions(self) -> None:
        n = _make_narrative()
        messages = _build_prompt(n, "en", 3, ["hackernews"])
        assert "AI is infallible" in messages[1]["content"]
        assert "Developer skills are commoditized" in messages[1]["content"]

    def test_unknown_language_falls_back_to_ru(self) -> None:
        messages = _build_prompt(_make_narrative(), "fr", 3, ["hackernews"])
        assert "поисковый аналитик" in messages[0]["content"]


# ---------------------------------------------------------------------------
# _parse_queries tests
# ---------------------------------------------------------------------------

class TestParseQueries:
    def test_valid_json(self) -> None:
        raw = _valid_query_dicts(3)
        result = _parse_queries(raw)
        assert len(result) == 3
        assert isinstance(result[0], SearchQuery)
        assert result[0].query == "search query 0"

    def test_not_a_list(self) -> None:
        with pytest.raises(ValueError, match="Expected JSON array"):
            _parse_queries({"query": "x"})

    def test_item_not_dict(self) -> None:
        with pytest.raises(ValueError, match="Query #0 is not a JSON object"):
            _parse_queries(["not a dict"])

    def test_missing_field(self) -> None:
        raw = [{"query": "x", "target_source": "hackernews"}]
        with pytest.raises(ValueError, match="missing field"):
            _parse_queries(raw)


# ---------------------------------------------------------------------------
# generate_queries (async) tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestGenerateQueries:
    async def test_success(self) -> None:
        dicts = _valid_query_dicts(3)
        mock_complete = AsyncMock(return_value=(json.dumps(dicts), {}))

        with patch("src.irritator.query_generator.complete", mock_complete):
            result = await generate_queries([_make_narrative()], _make_config())

        assert len(result) == 1
        claim = _make_narrative().claim
        assert claim in result
        assert len(result[claim]) == 3

    async def test_empty_narratives(self) -> None:
        result = await generate_queries([], _make_config())
        assert result == {}

    async def test_multiple_narratives_parallel(self) -> None:
        dicts = _valid_query_dicts(2)
        mock_complete = AsyncMock(return_value=(json.dumps(dicts), {}))

        n1 = _make_narrative("Claim A")
        n2 = _make_narrative("Claim B")

        with patch("src.irritator.query_generator.complete", mock_complete):
            result = await generate_queries([n1, n2], _make_config())

        assert len(result) == 2
        assert "Claim A" in result
        assert "Claim B" in result
        assert mock_complete.call_count == 2

    async def test_partial_failure_continues(self) -> None:
        dicts = _valid_query_dicts(2)
        mock_complete = AsyncMock(
            side_effect=[
                (json.dumps(dicts), {}),
                RuntimeError("Provider failed"),
            ]
        )

        n1 = _make_narrative("Good claim")
        n2 = _make_narrative("Bad claim")

        with patch("src.irritator.query_generator.complete", mock_complete):
            result = await generate_queries([n1, n2], _make_config())

        assert len(result) == 1
        assert "Good claim" in result

    async def test_uses_correct_role(self) -> None:
        from src.llm import LLMRole

        dicts = _valid_query_dicts(1)
        mock_complete = AsyncMock(return_value=(json.dumps(dicts), {}))

        with patch("src.irritator.query_generator.complete", mock_complete):
            await generate_queries([_make_narrative()], _make_config())

        assert mock_complete.call_args[0][0] == LLMRole.GENERATE_QUERIES

    async def test_all_fail_returns_empty(self) -> None:
        mock_complete = AsyncMock(side_effect=RuntimeError("All failed"))

        with patch("src.irritator.query_generator.complete", mock_complete):
            result = await generate_queries([_make_narrative()], _make_config())

        assert result == {}

    async def test_json_in_markdown_fence(self) -> None:
        dicts = _valid_query_dicts(2)
        fenced = f"```json\n{json.dumps(dicts)}\n```"
        mock_complete = AsyncMock(return_value=(fenced, {}))

        with patch("src.irritator.query_generator.complete", mock_complete):
            result = await generate_queries([_make_narrative()], _make_config())

        assert len(list(result.values())[0]) == 2
