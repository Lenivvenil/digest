"""Bound live response detail without losing useful candidate evidence or old reviews."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from digest.adapters.models.execution import ModelExecution
from digest.adapters.storage.candidate_progress import load_candidate_progress
from digest.application.candidate_review import begin_packet, merge_candidates, plan_packet, reconcile_packet
from digest.application.review import run_primary_review
from digest.application.review_request import build_evidence_bundle
from digest.config import _load_review
from digest.domain.editorial.candidate_policy import packet_articles, pending_completed_report
from digest.domain.editorial.candidates import CandidateProgress
from digest.domain.editorial.dispositions import validate_disposition_attempt
from digest.domain.editorial.reviews import EvidenceBundle, _parse_live_review, validated_cached_selections
from digest.presentation.review import primary_cards
from digest.review_checkpoint import load_review_checkpoint
from tests.test_candidate_review import NOW, population


@pytest.mark.parametrize(("section", "expected"), [({}, 5), ({"max_detailed_selections": 3}, 3)])
def test_response_detail_config_default_and_override(section: dict[str, int], expected: int) -> None:
    settings = _load_review({"review": section})
    assert settings.max_detailed_selections == expected
    assert settings.max_selections == 5 and settings.max_output_tokens == 4096


@pytest.mark.parametrize("value", [True, 0, 11])
def test_response_detail_config_rejects_invalid_bounds(value: int) -> None:
    with pytest.raises(ValueError, match="max_detailed_selections"):
        _load_review({"review": {"max_detailed_selections": value}})


def _response(bundle: EvidenceBundle, detailed: int = 5) -> str:
    return json.dumps(
        {
            "selections": [
                {
                    "evidence_id": item.evidence_id,
                    "reason": "Useful synthetic evidence.",
                    "quote": item.title,
                    "confidence": "high",
                }
                for item in bundle.items[:detailed]
            ],
            "limitations": ["Synthetic fixture; RSS metadata only."],
            "dispositions": [
                {"evidence_id": item.evidence_id, "status": "selected"}
                if index < detailed
                else {
                    "evidence_id": item.evidence_id,
                    "status": "deferred",
                    "reason": "Useful evidence deferred because the response detail budget is full.",
                }
                for index, item in enumerate(bundle.items)
            ],
        }
    )


@pytest.mark.asyncio
async def test_twenty_useful_items_keep_fifteen_deferred_after_five_detailed_selections(tmp_path: Path) -> None:
    model_execution = ModelExecution()
    config, articles = population(20)
    assert config.review.max_detailed_selections == 5
    assert config.review.max_selections == 5
    progress = merge_candidates(CandidateProgress(), articles, config, {}, now=NOW)
    packet = plan_packet(progress, config, NOW)
    assert packet is not None and len(packet.evidence.items) == 20
    begin_packet(progress, packet, tmp_path)
    raw = _response(packet.evidence)
    with patch(
        "digest.application.review.complete", AsyncMock(return_value=(raw, {"finish_reason": "stop"}))
    ) as complete:
        result = await run_primary_review(packet_articles(packet), config, execution=model_execution)
        report = result.report
    complete.assert_awaited_once()
    task = json.loads(complete.call_args.args[1][1]["content"])
    assert task["max_detailed_selections"] == 5 and "max_selections" not in task
    assert len(task["evidence"]["items"]) == 20
    assert complete.call_args.kwargs["max_output_tokens"] == 4096
    review = report.reviews[0]
    assert review.status == "ok" and len(review.selections) == 5
    assert len(primary_cards(result, articles, "en", max_cards=config.review.max_selections)) == 5
    assert len(primary_cards(result, articles, "en", max_cards=2)) == 2  # Independent publication capacity.
    selected = {item.evidence_id for item in packet.evidence.items[:5]}
    deferred = {item.evidence_id for item in packet.evidence.items[5:]}
    assert {item.evidence_id for item in review.selections} == selected
    response_hash = hashlib.sha256(raw.encode()).hexdigest()
    assert review.response_sha256 == response_hash
    (attempt,) = result.disposition_attempts
    validate_disposition_attempt(attempt, packet.evidence, review)
    assert attempt.response_sha256 == response_hash and attempt.prompt_hash == packet.prompt_hash
    assert attempt.status == "incomplete" and not attempt.errors
    assert attempt.finish_reason == "stop"
    assert set(attempt.unresolved_ids) == deferred
    assert len(attempt.dispositions) == 20
    assert not any(item.status == "not_selected" for item in attempt.dispositions)
    assert {item.evidence_id for item in attempt.dispositions if item.status == "selected"} == selected
    assert {item.evidence_id for item in attempt.dispositions if item.status == "deferred"} == deferred

    reconcile_packet(progress, packet, result, config, tmp_path)
    restored = load_candidate_progress(tmp_path)
    assert restored.packets[0].report == report
    assert restored.packets[0].disposition_attempts == (attempt,)
    assert pending_completed_report(restored) == report
    assert len(restored.candidates) == 20 and all(item.eligible for item in restored.candidates.values())
    assert {identity for identity, item in restored.candidates.items() if item.status == "selected"} == selected
    assert {
        identity for identity, item in restored.candidates.items() if item.status == "technical_pending"
    } == deferred
    for identity, candidate in restored.candidates.items():
        assert candidate.disposition is not None
        assert candidate.disposition.status == ("selected" if identity in selected else "deferred")
        assert candidate.decision_response_sha256 == response_hash
        assert candidate.decision_prompt_hash == packet.prompt_hash
        assert candidate.delivery_cache_observed_at is None
    next_packet = plan_packet(restored, config, NOW)
    assert next_packet is not None
    assert {item.evidence_id for item in next_packet.evidence.items} == deferred
    assert restored.packets[0].report == report


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("finish_reason", "detailed", "truncated", "error"),
    [
        ("length", 5, False, "provider reported unfinished response"),
        ("MAX_TOKENS", 5, False, "provider reported unfinished response"),
        ("stop", 5, True, "invalid JSON or review contract"),
        ("stop", 6, False, "invalid selection count"),
    ],
    ids=["closed-json-length", "closed-json-max-tokens", "truncated-json", "six-exceeds-detail-budget"],
)
async def test_unfinished_or_oversized_live_response_keeps_every_candidate_pending(
    tmp_path: Path,
    finish_reason: str,
    detailed: int,
    truncated: bool,
    error: str,
) -> None:
    model_execution = ModelExecution()
    config, articles = population(20)
    progress = merge_candidates(CandidateProgress(), articles, config, {}, now=NOW)
    packet = plan_packet(progress, config, NOW)
    assert packet is not None
    begin_packet(progress, packet, tmp_path)
    raw = _response(packet.evidence, detailed)
    if truncated:
        raw = raw[: raw.index('"dispositions"') + 20]
    else:
        assert len(json.loads(raw)["selections"]) == detailed
    with patch(
        "digest.application.review.complete", AsyncMock(return_value=(raw, {"finish_reason": finish_reason}))
    ) as complete:
        result = await run_primary_review(packet_articles(packet), config, execution=model_execution)
        report = result.report
    assert complete.await_count == 2  # Only the existing primary and fallback attempt.
    assert all(call.kwargs["max_output_tokens"] == 4096 for call in complete.call_args_list)
    assert len(result.disposition_attempts) == 2
    identities = {item.evidence_id for item in packet.evidence.items}
    response_hash = hashlib.sha256(raw.encode()).hexdigest()
    for review, attempt in zip(report.reviews, result.disposition_attempts, strict=True):
        assert review.status == "invalid" and review.error == error
        assert not review.selections and not review.rejected_items
        assert review.response_sha256 == response_hash
        assert review.rejected_output == raw and not review.rejected_output_truncated
        validate_disposition_attempt(attempt, packet.evidence, review)
        assert attempt.response_sha256 == response_hash and attempt.finish_reason == finish_reason
        assert attempt.status == "incomplete" and not attempt.dispositions
        assert set(attempt.unresolved_ids) == identities
        if finish_reason in {"length", "MAX_TOKENS"}:
            assert "finish_reason" not in asdict(review)
            assert attempt.errors == ("provider reported unfinished response",)
    assert not primary_cards(result, articles, "en")
    reconcile_packet(progress, packet, result, config, tmp_path)
    restored = load_candidate_progress(tmp_path)
    assert restored.packets[0].report == report
    assert restored.packets[0].disposition_attempts == tuple(result.disposition_attempts)
    assert pending_completed_report(restored) is None
    assert set(restored.candidates) == identities
    assert all(item.status == "technical_pending" and item.eligible for item in restored.candidates.values())
    assert all(item.disposition is None for item in restored.candidates.values())


@pytest.mark.asyncio
async def test_saved_eight_selection_review_remains_strictly_valid_at_new_default(tmp_path: Path) -> None:
    model_execution = ModelExecution()
    config, articles = population(8)
    bundle = build_evidence_bundle(articles, config.review)
    raw = _response(bundle, 8)
    config.review.max_detailed_selections = 8
    with patch("digest.application.review.complete", AsyncMock(return_value=(raw, {}))) as complete:
        result = await run_primary_review(articles, config, execution=model_execution)
        report = result.report
    complete.assert_awaited_once()
    assert len(report.reviews[0].selections) == 8
    path = tmp_path / "old-review.json"
    path.write_text(json.dumps(asdict(report)))
    original = path.read_bytes()
    config.review.max_detailed_selections = 5
    restored_bundle, cached = load_review_checkpoint(path, config)
    selections, limitations = validated_cached_selections(cached[0], restored_bundle)
    assert selections == report.reviews[0].selections and limitations == report.reviews[0].limitations
    assert len(selections) == 8 and path.read_bytes() == original
    with pytest.raises(ValueError, match="invalid selection count"):
        _parse_live_review(raw, restored_bundle, max_detailed_selections=config.review.max_detailed_selections)
    cached[0].selections[-1] = replace(cached[0].selections[-1], quote="Not in this synthetic evidence")
    with pytest.raises(ValueError, match="quote is not in supplied evidence"):
        validated_cached_selections(cached[0], restored_bundle)
