"""Tests for src.irritator.query_generator."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from digest.irritator.query_generator import (
    SearchQuery,
    _build_prompt,
    _parse_queries,
    generate_queries,
)
from tests.factories import make_narrative

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_narrative(
    claim: str = "AI will replace all developers",
    category: str = "AI",
) -> Any:
    return make_narrative(
        claim=claim,
        category=category,
        implicit_assumptions=["AI is infallible", "Developer skills are commoditized"],
    )


def _valid_query_dicts(n: int = 3) -> list[dict[str, str]]:
    return [
        {
            "query": f"failure of AI replacing developers {i}",
            "intent": f"Find evidence against AI replacement claim {i}",
        }
        for i in range(n)
    ]


def _make_config(
    language: str = "ru",
    queries_per_narrative: int = 3,
    sources: list[str] | None = None,
) -> Any:
    class IrritatorCfg:
        pass
    class RadarCfg:
        pass
    class Cfg:
        irritator = IrritatorCfg()
        radar = RadarCfg()

    Cfg.radar.language = language  # type: ignore[attr-defined]
    Cfg.irritator.queries_per_narrative = queries_per_narrative  # type: ignore[attr-defined]
    Cfg.irritator.sources = sources or ["hackernews", "reddit", "arxiv"]  # type: ignore[attr-defined]
    return Cfg()


# ---------------------------------------------------------------------------
# SearchQuery dataclass
# ---------------------------------------------------------------------------

class TestSearchQueryDataclass:
    def test_no_target_source_field(self) -> None:
        q = SearchQuery(query="test failure", intent="find problems")
        assert not hasattr(q, "target_source")
        assert q.query == "test failure"
        assert q.intent == "find problems"


# ---------------------------------------------------------------------------
# _build_prompt tests
# ---------------------------------------------------------------------------

class TestBuildPrompt:
    def test_russian_prompt(self) -> None:
        n = _make_narrative()
        messages = _build_prompt(n, "ru", 3)

        assert len(messages) == 2
        assert messages[0]["role"] == "system"
        assert "поисковый аналитик" in messages[0]["content"]
        assert n.claim in messages[1]["content"]

    def test_english_prompt(self) -> None:
        n = _make_narrative()
        messages = _build_prompt(n, "en", 5)

        assert "search analyst" in messages[0]["content"]
        assert n.claim in messages[1]["content"]
        assert "5" in messages[1]["content"]

    def test_includes_assumptions(self) -> None:
        n = _make_narrative()
        messages = _build_prompt(n, "en", 3)
        assert "AI is infallible" in messages[1]["content"]
        assert "Developer skills are commoditized" in messages[1]["content"]

    def test_unknown_language_falls_back_to_ru(self) -> None:
        messages = _build_prompt(_make_narrative(), "fr", 3)
        assert "поисковый аналитик" in messages[0]["content"]

    def test_adversarial_instruction_in_english_prompt(self) -> None:
        messages = _build_prompt(_make_narrative(), "en", 3)
        user_content = messages[1]["content"]
        adversarial_markers = ["failure", "didn't work", "criticism", "wrong", "limitations"]
        assert any(m in user_content for m in adversarial_markers), (
            f"Expected adversarial instruction in prompt, got: {user_content[:300]}"
        )

    def test_adversarial_instruction_in_russian_prompt(self) -> None:
        messages = _build_prompt(_make_narrative(), "ru", 3)
        user_content = messages[1]["content"]
        adversarial_markers = ["failure", "criticism", "wrong", "провал", "критик"]
        assert any(m in user_content for m in adversarial_markers), (
            f"Expected adversarial instruction in prompt, got: {user_content[:300]}"
        )

    def test_no_target_source_in_prompt(self) -> None:
        messages = _build_prompt(_make_narrative(), "en", 3)
        user_content = messages[1]["content"]
        assert "target_source" not in user_content


# ---------------------------------------------------------------------------
# _parse_queries tests
# ---------------------------------------------------------------------------

class TestParseQueries:
    def test_valid_json(self) -> None:
        raw = _valid_query_dicts(3)
        result = _parse_queries(raw)
        assert len(result) == 3
        assert isinstance(result[0], SearchQuery)
        assert result[0].query == "failure of AI replacing developers 0"

    def test_not_a_list(self) -> None:
        with pytest.raises(ValueError, match="Expected JSON array"):
            _parse_queries({"query": "x"})

    def test_item_not_dict(self) -> None:
        with pytest.raises(ValueError, match="Query #0 is not a JSON object"):
            _parse_queries(["not a dict"])

    def test_missing_intent_field(self) -> None:
        raw = [{"query": "x"}]
        with pytest.raises(ValueError, match="missing field"):
            _parse_queries(raw)

    def test_missing_query_field(self) -> None:
        raw = [{"intent": "find something"}]
        with pytest.raises(ValueError, match="missing field"):
            _parse_queries(raw)

    def test_no_target_source_required(self) -> None:
        raw = [{"query": "AI failure", "intent": "find failures"}]
        result = _parse_queries(raw)
        assert len(result) == 1
        assert not hasattr(result[0], "target_source")


# ---------------------------------------------------------------------------
# generate_queries (async) tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestGenerateQueries:
    async def test_success(self) -> None:
        dicts = _valid_query_dicts(3)
        mock_complete = AsyncMock(return_value=(json.dumps(dicts), {}))

        with patch("digest.irritator.query_generator.complete", mock_complete):
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

        with patch("digest.irritator.query_generator.complete", mock_complete):
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

        with patch("digest.irritator.query_generator.complete", mock_complete):
            result = await generate_queries([n1, n2], _make_config())

        assert len(result) == 1
        assert "Good claim" in result

    async def test_uses_correct_role(self) -> None:
        from digest.llm import LLMRole

        dicts = _valid_query_dicts(1)
        mock_complete = AsyncMock(return_value=(json.dumps(dicts), {}))

        with patch("digest.irritator.query_generator.complete", mock_complete):
            await generate_queries([_make_narrative()], _make_config())

        assert mock_complete.call_args[0][0] == LLMRole.GENERATE_QUERIES

    async def test_all_fail_returns_empty(self) -> None:
        mock_complete = AsyncMock(side_effect=RuntimeError("All failed"))

        with patch("digest.irritator.query_generator.complete", mock_complete):
            result = await generate_queries([_make_narrative()], _make_config())

        assert result == {}

    async def test_json_in_markdown_fence(self) -> None:
        dicts = _valid_query_dicts(2)
        fenced = f"```json\n{json.dumps(dicts)}\n```"
        mock_complete = AsyncMock(return_value=(fenced, {}))

        with patch("digest.irritator.query_generator.complete", mock_complete):
            result = await generate_queries([_make_narrative()], _make_config())

        assert len(list(result.values())[0]) == 2

    async def test_prompt_sent_to_llm_contains_adversarial_instruction(self) -> None:
        dicts = _valid_query_dicts(1)
        mock_complete = AsyncMock(return_value=(json.dumps(dicts), {}))

        with patch("digest.irritator.query_generator.complete", mock_complete):
            await generate_queries([_make_narrative()], _make_config(language="en"))

        call_args = mock_complete.call_args
        messages = call_args[0][1]
        user_content = next(m["content"] for m in messages if m["role"] == "user")
        adversarial_markers = ["failure", "criticism", "wrong", "didn't work", "limitations"]
        assert any(m in user_content for m in adversarial_markers)
