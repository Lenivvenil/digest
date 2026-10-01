"""Salvage valid selections while preserving exact evidence and partial status."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from digest.radar.collector import Article
from digest.review import (
    BlindReviewReport,
    EvidenceBundle,
    _parse_review,
    _review_slot,
    build_evidence_bundle,
    build_review_messages,
    canonical_evidence_quote,
    primary_cards,
    run_evidence_review,
    run_primary_review,
)
from digest.review_checkpoint import load_review_checkpoint
from digest.review_resume import _reusable_slots
from scripts.review_fixture import fixture_articles, fixture_config, fixture_response

CAPTURED = Path(__file__).parent / "fixtures" / "partial_review.json"


def _valid_selection(bundle: EvidenceBundle) -> dict[str, str]:
    item = bundle.items[0]
    return {"evidence_id": item.evidence_id, "reason": "Useful evidence", "quote": item.title, "confidence": "medium"}


async def _slot(payload: Any) -> Any:
    config = fixture_config()
    bundle = build_evidence_bundle(fixture_articles(), config.review)
    raw = json.dumps(payload)
    with patch("digest.review.complete", AsyncMock(return_value=(raw, {}))):
        return await _review_slot("primary", config.review.primary, bundle,
                                  build_review_messages(bundle, config.review, "en"), config)


@pytest.mark.asyncio
async def test_captured_response_retains_four_exact_source_selections_and_one_rejection() -> None:
    config = fixture_config()
    bundle, old_reviews = load_review_checkpoint(CAPTURED, config)
    raw = old_reviews[1].rejected_output
    assert raw is not None
    with patch("digest.review.complete", AsyncMock(return_value=(raw, {}))) as complete:
        review = await _review_slot("secondary", config.review.secondary, bundle,
                                    build_review_messages(bundle, config.review, "en"), config)
    complete.assert_awaited_once()
    assert review.status == "partial"
    assert len(review.selections) == 4
    assert [item.typography_normalized for item in review.selections] == [True, True, True, False]
    assert review.rejected_output == raw
    assert review.response_sha256 == hashlib.sha256(raw.encode()).hexdigest() == old_reviews[1].response_sha256
    assert len(review.rejected_items) == 1
    assert review.rejected_items[0].index == 1
    assert review.rejected_items[0].reason == "invalid selection text budget"
    known = {item.evidence_id: item for item in bundle.items}
    for selection in review.selections:
        evidence = known[selection.evidence_id]
        assert selection.quote in evidence.title or selection.quote in evidence.excerpt
    assert "Databricks" in known[review.selections[-1].evidence_id].excerpt
    articles = {}
    for item in bundle.items:
        articles.setdefault(item.category, []).append(Article(
            title=item.title, link=item.url, description=item.excerpt, source=item.source,
            category=item.category, pub_date=None,
        ))
    report = BlindReviewReport(
        1, bundle, [old_reviews[0], review], "incomplete", None, [], "pending_independent_review",
    )
    cards = primary_cards(report, articles, "en")
    assert len(cards) == 4
    assert all("groq/openai/gpt-oss-120b" in card.summary for card in cards)


@pytest.mark.parametrize("source_hyphen", ["-", "\u2010", "\u2011"])
@pytest.mark.parametrize("output_hyphen", ["-", "\u2010", "\u2011"])
def test_only_narrow_hyphens_align_to_original_source(source_hyphen: str, output_hyphen: str) -> None:
    source = f"Multi{source_hyphen}AZ architecture"
    returned, normalized = canonical_evidence_quote(f"Multi{output_hyphen}AZ", "unrelated", source)
    assert returned == source[:8]
    assert normalized is (source_hyphen != output_hyphen)


@pytest.mark.parametrize("quote,source", [
    ("cost−benefit", "cost-benefit"), ("cost–benefit", "cost-benefit"),
    ("cost—benefit", "cost-benefit"), ("Cost-benefit", "cost-benefit"),
    ("foo…bar", "foo...bar"), ("foo bar", "foo  bar"),
    ("a fast decision engine", "an engine that makes quick decisions"),
    ("ｆｏｏ", "foo"), ("x" * 201, "x" * 201), ("", "anything"),
])
def test_no_semantic_minus_general_normalization_paraphrase_or_budget_bypass(quote: str, source: str) -> None:
    with pytest.raises(ValueError):
        canonical_evidence_quote(quote, "", source)


@pytest.mark.asyncio
async def test_unknown_duplicates_and_invalid_items_are_rejected_individually() -> None:
    bundle = build_evidence_bundle(fixture_articles(), fixture_config().review)
    valid = _valid_selection(bundle)
    review = await _slot({"selections": [valid, deepcopy(valid), {**valid, "evidence_id": "untrusted-id"}, 5],
                          "limitations": []})
    assert review.status == "partial" and len(review.selections) == 1
    assert [item.reason for item in review.rejected_items] == [
        "duplicated evidence id", "unknown evidence id", "invalid selection schema",
    ]
    assert [item.index for item in review.rejected_items] == [1, 2, 3]
    assert [item.evidence_id for item in review.rejected_items] == [valid["evidence_id"], None, None]
    with pytest.raises(ValueError, match="duplicated"):
        _parse_review(json.dumps({"selections": [valid, valid], "limitations": []}), bundle, 5)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["count", "limitations", "envelope", "selections_type", "limitation_count"])
async def test_invalid_global_envelope_rejects_every_item(bad: str) -> None:
    bundle = build_evidence_bundle(fixture_articles(), fixture_config().review)
    valid = _valid_selection(bundle)
    payload: dict[str, Any] = {"selections": [valid], "limitations": []}
    if bad == "count":
        payload["selections"] = [valid] * 6
    elif bad == "limitations":
        payload["limitations"] = [False]
    elif bad == "envelope":
        payload["extra"] = "not allowed"
    elif bad == "selections_type":
        payload["selections"] = {"0": valid}
    else:
        payload["limitations"] = ["limitation"] * 6
    review = await _slot(payload)
    assert review.status == "invalid" and not review.selections
    assert review.rejected_items == [] and review.rejected_output == json.dumps(payload)


@pytest.mark.asyncio
async def test_all_invalid_is_invalid_and_preserves_all_reasons() -> None:
    bundle = build_evidence_bundle(fixture_articles(), fixture_config().review)
    valid = _valid_selection(bundle)
    review = await _slot({"selections": [{**valid, "quote": "invented paraphrase"},
                                          {**valid, "evidence_id": "unknown"}], "limitations": []})
    assert review.status == "invalid" and not review.selections
    assert [item.reason for item in review.rejected_items] == [
        "quote is not in supplied evidence", "unknown evidence id",
    ]


async def _partial_primary() -> Any:
    async def response(role: Any, messages: list[dict[str, str]], config: Any, **kwargs: Any) -> tuple:
        text, usage = await fixture_response(role, messages, config, **kwargs)
        raw = json.loads(text)
        raw["selections"][0]["quote"] = "fabricated paraphrase"
        return json.dumps(raw), usage

    with patch("digest.review.complete", side_effect=response) as complete:
        report = await run_primary_review(fixture_articles(), fixture_config())
    complete.assert_awaited_once()
    return report


@pytest.mark.asyncio
async def test_partial_primary_delivers_without_secondary_fallback() -> None:
    report = await _partial_primary()
    assert report.status == "incomplete" and report.reviews[0].status == "partial"
    assert report.reviews[1].attempted_at is None
    assert len(primary_cards(report, fixture_articles(), "en")) == 1


@pytest.mark.asyncio
async def test_partial_checkpoint_reuses_valid_entries_without_third_or_complete_claim(tmp_path: Path) -> None:
    original = await _partial_primary()
    path = tmp_path / "partial.json"
    path.write_text(json.dumps(asdict(original)))
    config = fixture_config()
    bundle, cached = load_review_checkpoint(path, config)
    assert _reusable_slots(bundle, cached, config) == {"primary"}
    with patch("digest.review.complete", side_effect=fixture_response) as complete:
        resumed = await run_evidence_review(bundle, config, cached)
    complete.assert_awaited_once()
    assert complete.call_args.kwargs["provider_override"].model == config.review.secondary.model
    assert resumed.reviews[0].status == "partial" and resumed.reviews[0].reused_from_checkpoint
    assert resumed.reviews[0].rejected_items == original.reviews[0].rejected_items
    assert resumed.reviews[0].rejected_output == original.reviews[0].rejected_output
    assert resumed.reviews[0].attempted_at == original.reviews[0].attempted_at
    assert resumed.status == "incomplete" and resumed.selection_overlap is None and resumed.disputed_ids == []
    assert len(resumed.reviews) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", ["quote", "missing_rejections", "unknown_rejected_id", "reason", "index"])
async def test_partial_checkpoint_is_strictly_revalidated_before_any_request(tamper: str) -> None:
    original = await _partial_primary()
    review = original.reviews[0]
    if tamper == "quote":
        review.selections[0] = replace(review.selections[0], quote="invented", typography_normalized=True)
    elif tamper == "missing_rejections":
        review.rejected_items.clear()
    elif tamper == "unknown_rejected_id":
        review.rejected_items[0] = replace(review.rejected_items[0], evidence_id="untrusted")
    elif tamper == "reason":
        review.rejected_items[0] = replace(review.rejected_items[0], reason="raw untrusted content")
    else:
        review.rejected_items[0] = replace(review.rejected_items[0], index=99)
    with patch("digest.review.complete", AsyncMock()) as complete:
        with pytest.raises(ValueError):
            await run_evidence_review(original.evidence, fixture_config(), original.reviews)
        with pytest.raises(ValueError):
            _reusable_slots(original.evidence, original.reviews, fixture_config())
    complete.assert_not_called()


@pytest.mark.asyncio
async def test_normalized_quotes_remain_exact_when_partial_checkpoint_reused(tmp_path: Path) -> None:
    config = fixture_config()
    bundle, captured = load_review_checkpoint(CAPTURED, config)
    with patch("digest.review.complete", AsyncMock(return_value=(captured[1].rejected_output, {}))):
        original = await run_evidence_review(bundle, config)
    assert original.reviews[0].status == "partial"
    path = tmp_path / "normalized.json"
    path.write_text(json.dumps(asdict(original)))
    bundle, cached = load_review_checkpoint(path, config)
    with patch("digest.review.complete", AsyncMock()) as complete:
        resumed = await run_evidence_review(bundle, config, cached)
    complete.assert_not_called()
    assert resumed.reviews[0].selections == original.reviews[0].selections
    assert resumed.status == "incomplete"
    first = cached[0].selections[0]
    cached[0].selections[0] = replace(first, quote=first.quote.replace("-", "\u2011"))
    with patch("digest.review.complete", AsyncMock()) as complete:
        with pytest.raises(ValueError, match="quote is not in supplied evidence"):
            await run_evidence_review(bundle, config, cached)
    complete.assert_not_called()
