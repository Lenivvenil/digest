"""Reader context and lossless typography, with explicit limits on semantic checks."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from digest.adapters.models.execution import ModelExecution
from digest.adapters.storage.candidate_progress import load_candidate_progress
from digest.application.candidate_review import (
    begin_packet,
    mark_prepared,
    merge_candidates,
    plan_packet,
    reconcile_packet,
)
from digest.application.review import run_primary_review
from digest.application.review_request import build_evidence_bundle, build_review_messages
from digest.config import _load_review
from digest.domain.editorial.attempts import restore_review
from digest.domain.editorial.candidates import CandidateProgress
from digest.domain.editorial.dispositions import capture_review_dispositions
from digest.domain.editorial.reviews import canonical_evidence_quote
from digest.translation import PROMPT_VERSION, SYSTEM, _parse
from scripts.review_fixture import fixture_articles, fixture_config
from tests.test_candidate_review import NOW, population, report_for


def test_editorial_context_is_optional_bounded_and_operator_owned() -> None:
    assert _load_review({}).editorial_context == ""
    assert _load_review({"review": {"editorial_context": "  "}}).editorial_context == ""
    assert _load_review({"review": {"editorial_context": " Bank operations matter. "}}).editorial_context == (
        "Bank operations matter."
    )
    config = fixture_config()
    bundle = build_evidence_bundle(fixture_articles(), config.review)
    before = build_review_messages(bundle, config.review, "en")
    config.review.editorial_context = ""
    assert build_review_messages(bundle, config.review, "en") == before
    assert "operator_editorial_context" not in json.loads(before[1]["content"])
    config.review.editorial_context = "Payment controls matter without architectural detail."
    after = build_review_messages(bundle, config.review, "en")
    assert json.loads(after[1]["content"])["operator_editorial_context"] == config.review.editorial_context
    assert json.loads(after[1]["content"])["evidence"] == json.loads(before[1]["content"])["evidence"]
    assert before != after


@pytest.mark.parametrize("value", [None, True, 4, [], "x" * 1001])
def test_invalid_operator_context_is_rejected(value: object) -> None:
    with pytest.raises(ValueError, match="editorial_context"):
        _load_review({"review": {"editorial_context": value}})


@pytest.mark.asyncio
async def test_reader_context_binds_identical_primary_and_fallback_prompt() -> None:
    model_execution = ModelExecution()
    config = fixture_config()
    config.review.editorial_context = "Banking and fintech are primary professional interests."
    good = (json.dumps({"selections": [], "limitations": ["Synthetic fixture"]}), {})
    with patch(
        "digest.application.review.complete", AsyncMock(side_effect=[RuntimeError("unavailable"), good])) as call:
        result = await run_primary_review(fixture_articles(), config, execution=model_execution)
        report = result.report
    first, second = call.call_args_list
    assert first.args[1] == second.args[1]
    expected = hashlib.sha256(json.dumps(first.args[1], sort_keys=True).encode()).hexdigest()
    assert {item.prompt_hash for item in report.reviews} == {expected}


def test_context_change_does_not_reopen_terminal_history(tmp_path: Path) -> None:
    config, articles = population(2)
    progress = merge_candidates(CandidateProgress(), articles, config, {}, now=NOW)
    packet = plan_packet(progress, config, NOW)
    assert packet is not None
    begin_packet(progress, packet, tmp_path)
    report = report_for(packet, config, "abstained")
    raw = json.dumps({"selections": [], "limitations": ["Original metadata judgment"], "dispositions": [
        {"evidence_id": item.evidence_id, "status": "not_selected", "reason": "Original relevance judgment"}
        for item in packet.evidence.items]})
    report.reviews[0].response_sha256 = hashlib.sha256(raw.encode()).hexdigest()
    result = restore_review(report, (capture_review_dispositions(packet.evidence, report.reviews[0], raw),))
    reconcile_packet(progress, packet, result, config, tmp_path)
    mark_prepared(progress, packet.evidence.bundle_id, tmp_path)
    frozen = asdict(report)
    config.review.editorial_context = "Banking business and operational relevance do not require architecture detail."
    restored = load_candidate_progress(tmp_path)
    merge_candidates(restored, articles, config, {}, now=NOW, cache_dir=tmp_path)
    assert {item.status for item in restored.candidates.values()} == {"not_selected"}
    assert {item.decision_prompt_hash for item in restored.candidates.values()} == {report.reviews[0].prompt_hash}
    assert plan_packet(restored, config, NOW) is None
    assert asdict(restored.packets[0].report) == frozen


@pytest.mark.parametrize("space", ["\u00a0", "\u202f"])
def test_nonbreaking_space_alignment_returns_exact_original_slice(space: str) -> None:
    source = "Vendor-backed Firm is raising up to $4 billion at a $14.5 billion pre-money valuation."
    quote = (f"Vendor\u2011backed Firm is raising up to $4{space}billion "
             f"at a $14.5{space}billion pre\u2011money valuation")
    literal, normalized = canonical_evidence_quote(quote, "", source)
    assert literal == source[:-1] and normalized
    assert len(literal) == len(quote)
    assert canonical_evidence_quote(literal, "", source) == (literal, False)
    assert canonical_evidence_quote("4 billion", "", f"4{space}billion") == (f"4{space}billion", True)


@pytest.mark.parametrize("quote", ["4  billion", "5 billion", "four billion", "4\tbillion"])
def test_typography_alignment_does_not_collapse_or_reinterpret(quote: str) -> None:
    with pytest.raises(ValueError, match="not in supplied evidence"):
        canonical_evidence_quote(quote, "", "4 billion")


def test_measurement_contract_and_numeric_validator_limits_are_explicit() -> None:
    config = fixture_config()
    prompt = build_review_messages(build_evidence_bundle(fixture_articles(), config.review), config.review, "en")[0]
    assert "comparator, value, unit, statistic or percentile" in prompt["content"]
    assert "omit the whole quantitative claim" in prompt["content"]
    assert PROMPT_VERSION == "presentation-translation-v3"
    assert "Do not add alternative magnitude labels" in SYSTEM
    # The observed class of semantic error still passes digit equality: this is not a truth checker.
    original = {"card": "Store reports sub‑2\u202fms read latency."}
    erroneous = {"translations": [{"id": "card", "text": "Субмиллисекундная задержка чтения менее 2 мс."}]}
    assert _parse(json.dumps(erroneous), original)["card"] == erroneous["translations"][0]["text"]
    # A short literal measurement quotation is already protected against alteration or qualifier loss.
    protected = {"card": 'Store reports "sub-2ms p99" read latency.'}
    changed = {"translations": [{"id": "card", "text": 'Задержка чтения "sub-2ms".'}]}
    with pytest.raises(ValueError, match="protected"):
        _parse(json.dumps(changed), protected)
