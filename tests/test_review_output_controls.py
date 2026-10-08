"""Offline review wire-shape and safe numeric diagnostics; no provider acceptance claim."""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from digest.adapters.models.execution import ModelExecution
from digest.adapters.models.review import groq_review_response_format
from digest.application.review import _review_usage, run_primary_review
from digest.config import ReviewModelConfig
from digest.review_checkpoint import load_review_checkpoint
from scripts.review_fixture import fixture_articles, fixture_config
from tests.test_candidate_dispositions import payload


@pytest.mark.parametrize("value", [None, True, False, -1, "17", 1.5, {}, [], "private reasoning text"])
def test_reasoning_token_usage_rejects_unknown_and_noninteger_values(value: object) -> None:
    assert _review_usage({"completion_tokens_details": {"reasoning_tokens": value, "reasoning": "private"}}) == {}


def test_numeric_usage_is_allowlisted_and_zero_is_distinct_from_absence() -> None:
    assert _review_usage({}) == {}
    assert _review_usage({"reasoning_tokens": 17}) == {}  # Wrong provider nesting is not a measured count.
    assert _review_usage({"completion_tokens_details": "private"}) == {}
    assert _review_usage({"prompt_tokens": 10, "completion_tokens": 30,
                          "completion_tokens_details": {"reasoning_tokens": 0, "text": "private"},
                          "rate_limit_remaining_tokens": 12, "rate_limit_limit_tokens": True,
                          "authorization": "private"}) == {
        "prompt_tokens": 10, "completion_tokens": 30, "reasoning_tokens": 0, "rate_limit_remaining_tokens": 12,
    }


def test_strict_wire_shape_disallows_per_selection_limitations_and_preserves_dispositions() -> None:
    fmt = groq_review_response_format()
    assert fmt["type"] == "json_schema" and fmt["json_schema"]["strict"] is True
    schema = fmt["json_schema"]["schema"]
    assert set(schema["required"]) == {"selections", "limitations", "dispositions"}
    assert schema["additionalProperties"] is False
    selection = schema["properties"]["selections"]["items"]
    assert selection["additionalProperties"] is False
    assert set(selection["properties"]) == set(selection["required"]) == {
        "evidence_id", "reason", "quote", "confidence",
    }
    assert "limitations" not in selection["properties"]
    variants = schema["properties"]["dispositions"]["items"]["anyOf"]
    assert [variant["properties"]["status"]["enum"] for variant in variants] == [
        ["selected"], ["not_selected", "deferred"], ["duplicate"],
    ]
    for variant in variants:
        assert variant["additionalProperties"] is False
        assert set(variant["properties"]) == set(variant["required"])
    assert "maxItems" not in json.dumps(fmt) and "maxLength" not in json.dumps(fmt)


@pytest.mark.asyncio
async def test_only_groq_gptoss_review_gets_controls_and_length_diagnostics_survive(tmp_path: Path) -> None:
    execution = ModelExecution()
    config = fixture_config()
    raw = json.dumps(payload())
    usage = {"finish_reason": "length", "prompt_tokens": 4222, "completion_tokens": 4096,
             "completion_tokens_details": {"reasoning_tokens": 1800, "reasoning": "DO NOT RETAIN"}}
    completion = AsyncMock(side_effect=[RuntimeError("primary unavailable"), (raw, usage)])
    with patch("digest.application.review.complete", completion) as call:
        result = await run_primary_review(fixture_articles(), config, execution=execution)
        report = result.report
    first, second = call.call_args_list
    assert "reasoning_effort" not in first.kwargs and "response_format" not in first.kwargs
    assert second.kwargs["reasoning_effort"] == "low"
    assert second.kwargs["response_format"] == groq_review_response_format()
    assert second.kwargs["max_output_tokens"] == 4096
    assert first.args[1] == second.args[1]  # Same evidence/prompt for primary and fallback.
    assert "limitations belongs only at the top level" in first.args[1][0]["content"]
    assert report.reviews[1].status == "invalid" and not report.reviews[1].selections
    assert report.reviews[1].usage["reasoning_tokens"] == 1800
    assert "DO NOT RETAIN" not in json.dumps(asdict(report))
    assert result.disposition_attempts[1].finish_reason == "length"
    assert len(call.call_args_list) == 2
    checkpoint = tmp_path / "review.json"
    checkpoint.write_text(json.dumps(asdict(report)))
    _, restored = load_review_checkpoint(checkpoint, config)
    assert restored[1].usage == report.reviews[1].usage


@pytest.mark.asyncio
async def test_other_groq_model_retains_ordinary_review_wire() -> None:
    execution = ModelExecution()
    config = fixture_config()
    config.review.primary = ReviewModelConfig("groq", "other-configured-model")
    with patch("digest.application.review.complete", AsyncMock(return_value=(json.dumps(payload()), {}))) as call:
        await run_primary_review(fixture_articles(), config, execution=execution)
    assert "reasoning_effort" not in call.call_args.kwargs
    assert "response_format" not in call.call_args.kwargs
