"""Tests for src.irritator.narrative_extractor."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from digest.irritator.narrative_extractor import (
    Narrative,
    _build_prompt,
    _parse_narratives,
    extract_narratives,
)
from digest.radar.summarizer import CategorySummary

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_summary(category: str = "AI", text: str = "AI is changing everything.", count: int = 5) -> CategorySummary:
    return CategorySummary(category=category, summary_text=text, article_count=count)


def _valid_narrative_dicts(n: int = 2) -> list[dict[str, object]]:
    return [
        {
            "claim": f"Narrative claim {i}",
            "category": "AI" if i % 2 == 0 else "Banking",
            "implicit_assumptions": [f"assumption {i}.1", f"assumption {i}.2"],
            "why_worth_challenging": f"This narrative deserves scrutiny because {i}.",
        }
        for i in range(n)
    ]


def _make_config(language: str = "ru", max_narratives: int = 5) -> Any:
    """Minimal config stub for narrative extractor tests."""
    class IrritatorCfg:
        max_narratives = 5
    class RadarCfg:
        language = "ru"
    class Cfg:
        irritator = IrritatorCfg()
        radar = RadarCfg()
    Cfg.radar.language = language
    Cfg.irritator.max_narratives = max_narratives
    return Cfg()


# ---------------------------------------------------------------------------
# _build_prompt tests
# ---------------------------------------------------------------------------

class TestBuildPrompt:
    def test_russian_prompt(self) -> None:
        summaries = [_make_summary("AI", "AI news here"), _make_summary("Banking", "Banking news")]
        messages = _build_prompt(summaries, "ru", 3)

        assert len(messages) == 2
        assert messages[0]["role"] == "system"
        assert messages[1]["role"] == "user"
        assert "критический аналитик" in messages[0]["content"]
        assert "AI news here" in messages[1]["content"]
        assert "Banking news" in messages[1]["content"]
        assert "3" in messages[1]["content"]

    def test_english_prompt(self) -> None:
        summaries = [_make_summary("AI", "AI news here")]
        messages = _build_prompt(summaries, "en", 5)

        assert "critical analyst" in messages[0]["content"]
        assert "AI news here" in messages[1]["content"]
        assert "5" in messages[1]["content"]

    def test_unknown_language_falls_back_to_ru(self) -> None:
        messages = _build_prompt([_make_summary()], "fr", 3)
        assert "критический аналитик" in messages[0]["content"]

    def test_includes_category_headers(self) -> None:
        summaries = [_make_summary("AI", "text", count=7)]
        messages = _build_prompt(summaries, "en", 3)
        assert "### AI (7 articles)" in messages[1]["content"]


# ---------------------------------------------------------------------------
# _parse_narratives tests
# ---------------------------------------------------------------------------

class TestParseNarratives:
    def test_valid_json(self) -> None:
        raw = _valid_narrative_dicts(2)
        result = _parse_narratives(raw, 5)
        assert len(result) == 2
        assert isinstance(result[0], Narrative)
        assert result[0].claim == "Narrative claim 0"
        assert len(result[0].implicit_assumptions) == 2

    def test_truncates_to_max(self) -> None:
        raw = _valid_narrative_dicts(5)
        result = _parse_narratives(raw, 3)
        assert len(result) == 3

    def test_not_a_list(self) -> None:
        with pytest.raises(ValueError, match="Expected JSON array"):
            _parse_narratives({"claim": "x"}, 5)

    def test_item_not_dict(self) -> None:
        with pytest.raises(ValueError, match="Narrative #0 is not a JSON object"):
            _parse_narratives(["string item"], 5)

    def test_missing_field(self) -> None:
        raw = [{"claim": "x", "category": "AI"}]
        with pytest.raises(ValueError, match="missing field"):
            _parse_narratives(raw, 5)

    def test_assumptions_not_list(self) -> None:
        raw = [{
            "claim": "x",
            "category": "AI",
            "implicit_assumptions": "not a list",
            "why_worth_challenging": "reason",
        }]
        with pytest.raises(ValueError, match="must be a list"):
            _parse_narratives(raw, 5)


# ---------------------------------------------------------------------------
# extract_narratives (async) tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestExtractNarratives:
    async def test_success(self) -> None:
        dicts = _valid_narrative_dicts(2)
        mock_complete = AsyncMock(return_value=(json.dumps(dicts), {"completion_tokens": 100}))

        with patch("digest.irritator.narrative_extractor.complete", mock_complete):
            result = await extract_narratives(
                [_make_summary("AI"), _make_summary("Banking")],
                _make_config(),
            )

        assert len(result) == 2
        assert result[0].claim == "Narrative claim 0"
        mock_complete.assert_called_once()

    async def test_empty_summaries(self) -> None:
        result = await extract_narratives([], _make_config())
        assert result == []

    async def test_truncates_to_max_narratives(self) -> None:
        dicts = _valid_narrative_dicts(5)
        mock_complete = AsyncMock(return_value=(json.dumps(dicts), {}))

        with patch("digest.irritator.narrative_extractor.complete", mock_complete):
            result = await extract_narratives(
                [_make_summary()],
                _make_config(max_narratives=2),
            )

        assert len(result) == 2

    async def test_invalid_json_raises(self) -> None:
        mock_complete = AsyncMock(return_value=("not json at all", {}))

        with patch("digest.irritator.narrative_extractor.complete", mock_complete):
            with pytest.raises(ValueError, match="No valid JSON"):
                await extract_narratives([_make_summary()], _make_config())

    async def test_missing_field_raises(self) -> None:
        bad = [{"claim": "x"}]
        mock_complete = AsyncMock(return_value=(json.dumps(bad), {}))

        with patch("digest.irritator.narrative_extractor.complete", mock_complete):
            with pytest.raises(ValueError, match="missing field"):
                await extract_narratives([_make_summary()], _make_config())

    async def test_llm_failure_propagates(self) -> None:
        mock_complete = AsyncMock(side_effect=RuntimeError("All providers failed"))

        with patch("digest.irritator.narrative_extractor.complete", mock_complete):
            with pytest.raises(RuntimeError, match="All providers failed"):
                await extract_narratives([_make_summary()], _make_config())

    async def test_uses_correct_role_and_temperature(self) -> None:
        from digest.llm import LLMRole

        dicts = _valid_narrative_dicts(1)
        mock_complete = AsyncMock(return_value=(json.dumps(dicts), {}))

        with patch("digest.irritator.narrative_extractor.complete", mock_complete):
            await extract_narratives([_make_summary()], _make_config())

        call_args = mock_complete.call_args
        assert call_args[0][0] == LLMRole.EXTRACT_NARRATIVES
        assert call_args[1]["temperature"] == 0.5

    async def test_json_in_markdown_fence(self) -> None:
        dicts = _valid_narrative_dicts(1)
        fenced = f"```json\n{json.dumps(dicts)}\n```"
        mock_complete = AsyncMock(return_value=(fenced, {}))

        with patch("digest.irritator.narrative_extractor.complete", mock_complete):
            result = await extract_narratives([_make_summary()], _make_config())

        assert len(result) == 1
