"""Tests for src.irritator.ranker."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from src.irritator.narrative_extractor import Narrative
from src.irritator.ranker import (
    RankedSignal,
    _build_prompt,
    _parse_rankings,
    rank_signals,
)
from src.irritator.sources import Signal


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_narrative(claim: str = "AI will replace developers") -> Narrative:
    return Narrative(
        claim=claim,
        category="AI",
        implicit_assumptions=["AI is infallible"],
        why_worth_challenging="Ignores evidence.",
    )


def _make_signal(
    url: str = "https://example.com/a",
    title: str = "Counter evidence",
    source: str = "hackernews",
) -> Signal:
    return Signal(
        url=url, title=title, snippet="Detailed counter argument",
        source_name=source, published="2026-01-01", score=10.0,
    )


def _valid_rankings(n: int = 2, scores: list[int] | None = None) -> list[dict]:
    if scores is None:
        scores = [8, 6]
    return [
        {"index": i, "score": scores[i] if i < len(scores) else 5, "reasoning": f"Reason {i}"}
        for i in range(n)
    ]


def _make_config(language: str = "ru", min_score: int = 7, top_signals: int = 3):
    class IrritatorCfg:
        pass
    class RadarCfg:
        pass
    class Cfg:
        irritator = IrritatorCfg()
        radar = RadarCfg()
    Cfg.radar.language = language
    Cfg.irritator.min_signal_score = min_score
    Cfg.irritator.top_signals = top_signals
    return Cfg()


# ---------------------------------------------------------------------------
# _build_prompt tests
# ---------------------------------------------------------------------------

class TestBuildPrompt:
    def test_russian_prompt(self) -> None:
        n = _make_narrative()
        signals = [_make_signal()]
        messages = _build_prompt(n, signals, "ru")

        assert len(messages) == 2
        assert "контр-сигналов" in messages[0]["content"]
        assert n.claim in messages[1]["content"]
        assert signals[0].title in messages[1]["content"]

    def test_english_prompt(self) -> None:
        messages = _build_prompt(_make_narrative(), [_make_signal()], "en")
        assert "counter-signal" in messages[0]["content"]


# ---------------------------------------------------------------------------
# _parse_rankings tests
# ---------------------------------------------------------------------------

class TestParseRankings:
    def test_valid_rankings(self) -> None:
        signals = [_make_signal("https://a.com"), _make_signal("https://b.com")]
        raw = _valid_rankings(2, [8, 9])
        result = _parse_rankings(raw, signals, "claim", 7)
        assert len(result) == 2
        assert result[0].score == 9  # sorted desc
        assert result[1].score == 8

    def test_filters_below_min_score(self) -> None:
        signals = [_make_signal(), _make_signal("https://b.com")]
        raw = _valid_rankings(2, [8, 3])
        result = _parse_rankings(raw, signals, "claim", 7)
        assert len(result) == 1
        assert result[0].score == 8

    def test_invalid_index_skipped(self) -> None:
        signals = [_make_signal()]
        raw = [{"index": 5, "score": 9, "reasoning": "good"}]
        result = _parse_rankings(raw, signals, "claim", 1)
        assert result == []

    def test_not_a_list(self) -> None:
        with pytest.raises(ValueError, match="Expected JSON array"):
            _parse_rankings({"index": 0}, [], "claim", 1)

    def test_non_dict_items_skipped(self) -> None:
        result = _parse_rankings(["not a dict"], [_make_signal()], "claim", 1)
        assert result == []


# ---------------------------------------------------------------------------
# rank_signals (async) tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestRankSignals:
    async def test_success(self) -> None:
        signals = [_make_signal("https://a.com"), _make_signal("https://b.com")]
        raw = _valid_rankings(2, [9, 8])
        mock_complete = AsyncMock(return_value=(json.dumps(raw), {}))

        with patch("src.irritator.ranker.complete", mock_complete):
            result = await rank_signals(_make_narrative(), signals, _make_config())

        assert len(result) == 2
        assert isinstance(result[0], RankedSignal)
        assert result[0].score == 9

    async def test_empty_signals(self) -> None:
        result = await rank_signals(_make_narrative(), [], _make_config())
        assert result == []

    async def test_respects_top_signals_limit(self) -> None:
        signals = [_make_signal(f"https://{i}.com") for i in range(5)]
        raw = [{"index": i, "score": 10, "reasoning": "great"} for i in range(5)]
        mock_complete = AsyncMock(return_value=(json.dumps(raw), {}))

        with patch("src.irritator.ranker.complete", mock_complete):
            result = await rank_signals(_make_narrative(), signals, _make_config(top_signals=2))

        assert len(result) == 2

    async def test_filters_below_threshold(self) -> None:
        signals = [_make_signal("https://a.com"), _make_signal("https://b.com")]
        raw = _valid_rankings(2, [9, 4])
        mock_complete = AsyncMock(return_value=(json.dumps(raw), {}))

        with patch("src.irritator.ranker.complete", mock_complete):
            result = await rank_signals(_make_narrative(), signals, _make_config(min_score=7))

        assert len(result) == 1

    async def test_llm_failure_propagates(self) -> None:
        mock_complete = AsyncMock(side_effect=RuntimeError("fail"))

        with patch("src.irritator.ranker.complete", mock_complete):
            with pytest.raises(RuntimeError):
                await rank_signals(_make_narrative(), [_make_signal()], _make_config())

    async def test_uses_correct_role(self) -> None:
        from src.llm import LLMRole

        raw = _valid_rankings(1, [8])
        mock_complete = AsyncMock(return_value=(json.dumps(raw), {}))

        with patch("src.irritator.ranker.complete", mock_complete):
            await rank_signals(_make_narrative(), [_make_signal()], _make_config())

        assert mock_complete.call_args[0][0] == LLMRole.RANK_SIGNALS
