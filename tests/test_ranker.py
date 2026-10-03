"""Tests for src.irritator.ranker."""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from digest.irritator.narrative_extractor import Narrative
from digest.irritator.ranker import (
    MAX_RANKING_JSON_CHARS,
    RANK_RELATION_CONTRACT,
    RankedSignal,
    _build_prompt,
    _parse_rankings,
    _ranking_signal_packet,
    rank_signals,
)
from digest.irritator.sources import Signal
from tests.factories import make_narrative, make_signal

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_narrative(claim: str = "AI will replace developers") -> Narrative:
    return make_narrative(claim=claim, why_worth_challenging="Ignores evidence.")


def _make_signal(
    url: str = "https://example.com/a",
    title: str = "Counter evidence",
    source: str = "hackernews",
) -> Signal:
    return make_signal(url=url, title=title, source_name=source)


def _valid_rankings(n: int = 2, scores: list[int] | None = None) -> list[dict[str, object]]:
    if scores is None:
        scores = [8, 6]
    return [
        {"index": i, "score": scores[i] if i < len(scores) else 5,
         "relation": "complicates", "reasoning": f"Reason {i}"}
        for i in range(n)
    ]


def _make_config(language: str = "ru", min_score: int = 5, top_signals: int = 3) -> Any:
    class IrritatorCfg:
        pass
    class RadarCfg:
        pass
    class Cfg:
        irritator = IrritatorCfg()
        radar = RadarCfg()
    Cfg.radar.language = language  # type: ignore[attr-defined]
    Cfg.irritator.min_signal_score = min_score  # type: ignore[attr-defined]
    Cfg.irritator.top_signals = top_signals  # type: ignore[attr-defined]
    return Cfg()


# ---------------------------------------------------------------------------
# _build_prompt tests
# ---------------------------------------------------------------------------

class TestBuildPrompt:
    def test_complete_snippet_keeps_late_condition(self) -> None:
        snippet = "The measured benefit was observed in the sample. " * 20
        snippet += " Benefits require expert review of every proposed action."
        signal = make_signal(snippet=snippet)
        messages = _build_prompt(_make_narrative(), [signal], "en")
        packet, indices = _ranking_signal_packet([signal])
        assert indices == {0}
        assert json.loads(packet)[0]["snippet"] == snippet
        assert packet in messages[1]["content"]

    def test_packet_budget_counts_serialization_and_omits_whole_records(self) -> None:
        signal = make_signal(snippet="")
        empty_packet, _ = _ranking_signal_packet([signal])
        signal.snippet = "a" * (MAX_RANKING_JSON_CHARS - len(empty_packet))
        packet, indices = _ranking_signal_packet([signal])
        assert len(packet) == MAX_RANKING_JSON_CHARS
        assert indices == {0}
        assert json.loads(packet)[0]["snippet"] == signal.snippet

        signal.snippet += "\n"
        packet, indices = _ranking_signal_packet([signal])
        assert (packet, indices) == ("[]", set())

    def test_packet_skips_whole_candidate_and_keeps_original_indices(self) -> None:
        signals = [make_signal(snippet="a" * 6000), make_signal(snippet="b" * 3000), make_signal()]
        packet, indices = _ranking_signal_packet(signals)
        assert len(packet) <= MAX_RANKING_JSON_CHARS
        assert indices == {0, 2}
        records = json.loads(packet)
        assert [record["index"] for record in records] == [0, 2]
        assert [record["snippet"] for record in records] == [signals[0].snippet, signals[2].snippet]

    def test_russian_prompt(self) -> None:
        n = _make_narrative()
        signals = [_make_signal()]
        messages = _build_prompt(n, signals, "ru")

        assert len(messages) == 2
        assert "контр-сигналов" in messages[0]["content"]
        assert RANK_RELATION_CONTRACT in messages[0]["content"]
        assert n.claim in messages[1]["content"]
        assert signals[0].title in messages[1]["content"]
        assert all(relation in messages[1]["content"] for relation in (
            "contradicts", "complicates", "supports", "context", "insufficient",
        ))

    def test_english_prompt(self) -> None:
        messages = _build_prompt(_make_narrative(), [_make_signal()], "en")
        assert "counter-signal" in messages[0]["content"]
        assert RANK_RELATION_CONTRACT in messages[0]["content"]
        assert all(relation in messages[1]["content"] for relation in (
            "contradicts", "complicates", "supports", "context", "insufficient",
        ))

    def test_english_prompt_has_calibration_anchors(self) -> None:
        messages = _build_prompt(_make_narrative(), [_make_signal()], "en")
        user_content = messages[1]["content"]
        assert "9-10" in user_content
        assert "direct evidence" in user_content

    def test_russian_prompt_has_calibration_anchors(self) -> None:
        messages = _build_prompt(_make_narrative(), [_make_signal()], "ru")
        user_content = messages[1]["content"]
        assert "9-10" in user_content

    def test_single_criterion_not_conjunction(self) -> None:
        messages = _build_prompt(_make_narrative(), [_make_signal()], "en")
        system_content = messages[0]["content"]
        assert "substance" not in system_content
        assert "credibility" not in system_content


# ---------------------------------------------------------------------------
# _parse_rankings tests
# ---------------------------------------------------------------------------

class TestParseRankings:
    def test_valid_rankings(self) -> None:
        signals = [_make_signal("https://a.com"), _make_signal("https://b.com")]
        raw = _valid_rankings(2, [8, 9])
        result = _parse_rankings(raw, signals, "claim", 5)
        assert len(result) == 2
        assert result[0].score == 9  # sorted desc
        assert result[1].score == 8

    def test_filters_below_min_score(self) -> None:
        signals = [_make_signal(), _make_signal("https://b.com")]
        raw = _valid_rankings(2, [8, 3])
        result = _parse_rankings(raw, signals, "claim", 5)
        assert len(result) == 1
        assert result[0].score == 8

    def test_threshold_5_lets_through_score_5(self) -> None:
        signals = [_make_signal()]
        raw = _valid_rankings(1, [5])
        result = _parse_rankings(raw, signals, "claim", 5)
        assert len(result) == 1
        assert result[0].score == 5

    def test_invalid_index_rejected(self) -> None:
        signals = [_make_signal()]
        raw = _valid_rankings(1, [9])
        raw[0]["index"] = 5
        with pytest.raises(ValueError, match="ranking index"):
            _parse_rankings(raw, signals, "claim", 1)

    def test_not_a_list(self) -> None:
        with pytest.raises(ValueError, match="Expected JSON array"):
            _parse_rankings({"index": 0}, [], "claim", 1)

    def test_non_dict_items_rejected(self) -> None:
        with pytest.raises(ValueError, match="Invalid ranking fields"):
            _parse_rankings(["not a dict"], [_make_signal()], "claim", 1)

    @pytest.mark.parametrize("relation", ["supports", "context", "insufficient"])
    def test_high_scoring_non_counter_relations_excluded(self, relation: str) -> None:
        raw = _valid_rankings(1, [10])
        raw[0]["relation"] = relation
        assert _parse_rankings(raw, [_make_signal()], "claim", 1) == []

    def test_mixed_relations_keep_only_qualifying_counter_signals(self) -> None:
        signals = [_make_signal(f"https://example.com/{index}") for index in range(4)]
        raw = _valid_rankings(4, [10, 5, 9, 4])
        raw[0].update(relation="supports", reasoning="The rollout reduced failed requests.")
        raw[1].update(reasoning="Stateful services require a maintenance window during rollout.")
        raw[2]["relation"] = "contradicts"
        result = _parse_rankings(raw, signals, "The rollout improves reliability.", 5)
        assert [item.signal for item in result] == [signals[2], signals[1]]
        assert set(asdict(result[0])) == {"signal", "score", "reasoning", "narrative_claim"}

    @pytest.mark.parametrize(("field", "value"), [
        ("index", True), ("score", 11), ("relation", "unknown"), ("reasoning", "  "),
    ])
    def test_invalid_non_counter_entry_rejects_whole_response(self, field: str, value: Any) -> None:
        signals = [_make_signal(), _make_signal("https://example.com/support")]
        raw = _valid_rankings(2, [8, 1])
        raw[1]["relation"] = "supports"
        raw[1][field] = value
        with pytest.raises(ValueError):
            _parse_rankings(raw, signals, "claim", 5)

    @pytest.mark.parametrize("mutation", ["missing_relation", "extra_field", "duplicate_index"])
    def test_invalid_entry_shape_or_identity_rejects_whole_response(self, mutation: str) -> None:
        raw = _valid_rankings(2, [1, 8])
        raw[0]["relation"] = "context"
        if mutation == "missing_relation":
            del raw[0]["relation"]
        elif mutation == "extra_field":
            raw[0]["extra"] = "unexpected"
        else:
            raw[1]["index"] = 0
        with pytest.raises(ValueError):
            _parse_rankings(raw, [_make_signal(), _make_signal("https://example.com/2")], "claim", 5)


# ---------------------------------------------------------------------------
# rank_signals (async) tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestRankSignals:
    async def test_omitted_candidate_cannot_be_ranked_and_indices_do_not_shift(self) -> None:
        signals = [make_signal(snippet="x" * MAX_RANKING_JSON_CHARS), _make_signal()]
        raw = _valid_rankings(1, [8])
        raw[0]["index"] = 1
        mock_complete = AsyncMock(return_value=(json.dumps(raw), {}))
        with patch("digest.irritator.ranker.complete", mock_complete):
            result = await rank_signals(_make_narrative(), signals, _make_config())
        assert result[0].signal is signals[1]
        assert mock_complete.await_count == 1
        raw[0]["index"] = 0
        with pytest.raises(ValueError, match="ranking index"):
            _parse_rankings(raw, signals, "claim", 5)

    async def test_no_model_call_when_every_candidate_exceeds_budget(self) -> None:
        signals = [make_signal(snippet="x" * MAX_RANKING_JSON_CHARS)]
        mock_complete = AsyncMock()
        with patch("digest.irritator.ranker.complete", mock_complete):
            result = await rank_signals(_make_narrative(), signals, _make_config())
        assert result == []
        mock_complete.assert_not_awaited()

    async def test_success(self) -> None:
        signals = [_make_signal("https://a.com"), _make_signal("https://b.com")]
        raw = _valid_rankings(2, [9, 8])
        mock_complete = AsyncMock(return_value=(json.dumps(raw), {}))

        with patch("digest.irritator.ranker.complete", mock_complete):
            result = await rank_signals(_make_narrative(), signals, _make_config())

        assert len(result) == 2
        assert isinstance(result[0], RankedSignal)
        assert result[0].score == 9

    async def test_empty_signals(self) -> None:
        result = await rank_signals(_make_narrative(), [], _make_config())
        assert result == []

    async def test_respects_top_signals_limit(self) -> None:
        signals = [_make_signal(f"https://{i}.com") for i in range(5)]
        raw = _valid_rankings(5, [10] * 5)
        mock_complete = AsyncMock(return_value=(json.dumps(raw), {}))

        with patch("digest.irritator.ranker.complete", mock_complete):
            result = await rank_signals(_make_narrative(), signals, _make_config(top_signals=2))

        assert len(result) == 2

    async def test_filters_below_threshold(self) -> None:
        signals = [_make_signal("https://a.com"), _make_signal("https://b.com")]
        raw = _valid_rankings(2, [9, 4])
        mock_complete = AsyncMock(return_value=(json.dumps(raw), {}))

        with patch("digest.irritator.ranker.complete", mock_complete):
            result = await rank_signals(_make_narrative(), signals, _make_config(min_score=5))

        assert len(result) == 1

    async def test_score_5_passes_default_threshold(self) -> None:
        signals = [_make_signal("https://a.com")]
        raw = _valid_rankings(1, [5])
        mock_complete = AsyncMock(return_value=(json.dumps(raw), {}))

        with patch("digest.irritator.ranker.complete", mock_complete):
            result = await rank_signals(_make_narrative(), signals, _make_config(min_score=5))

        assert len(result) == 1
        assert result[0].score == 5

    async def test_all_non_counter_relations_return_empty_without_another_request(self) -> None:
        signals = [_make_signal(f"https://example.com/{index}") for index in range(3)]
        raw = _valid_rankings(3, [10] * 3)
        for item, relation in zip(raw, ["supports", "context", "insufficient"], strict=True):
            item["relation"] = relation
        mock_complete = AsyncMock(return_value=(json.dumps(raw), {}))
        with patch("digest.irritator.ranker.complete", mock_complete):
            result = await rank_signals(_make_narrative(), signals, _make_config())
        assert result == []
        assert mock_complete.await_count == 1

    async def test_llm_failure_propagates(self) -> None:
        mock_complete = AsyncMock(side_effect=RuntimeError("fail"))

        with patch("digest.irritator.ranker.complete", mock_complete):
            with pytest.raises(RuntimeError):
                await rank_signals(_make_narrative(), [_make_signal()], _make_config())

    async def test_uses_correct_role(self) -> None:
        from digest.llm import LLMRole

        raw = _valid_rankings(1, [8])
        mock_complete = AsyncMock(return_value=(json.dumps(raw), {}))

        with patch("digest.irritator.ranker.complete", mock_complete):
            await rank_signals(_make_narrative(), [_make_signal()], _make_config())

        assert mock_complete.call_args[0][0] == LLMRole.RANK_SIGNALS
