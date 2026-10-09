"""Offline per-ID metadata decisions; synthetic outputs are not quality evidence."""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import asdict, replace
from typing import Any
from unittest.mock import patch

import pytest

from digest.adapters.models.execution import ModelExecution
from digest.application.review import run_primary_review
from digest.application.review_request import build_evidence_bundle, build_review_messages
from digest.config import ReviewConfig
from digest.domain.editorial.attempts import ResolvedReview
from digest.domain.editorial.dispositions import (
    CandidateDisposition,
    capture_review_dispositions,
    validate_disposition_attempt,
)
from digest.domain.editorial.reviews import _parse_review
from scripts.review_fixture import fixture_articles, fixture_config
from tests.factories import make_article


def payload() -> dict[str, Any]:
    bundle = build_evidence_bundle(fixture_articles(), ReviewConfig())
    first = bundle.items[0]
    return {
        "selections": [{"evidence_id": first.evidence_id, "reason": "Concrete architecture evidence.",
                        "quote": first.title, "confidence": "medium"}],
        "limitations": [],
        "dispositions": [{"evidence_id": item.evidence_id, "status": "selected"} if index == 0 else
                         {"evidence_id": item.evidence_id, "status": "not_selected",
                          "reason": "Excerpt gives no specific architecture consequence."}
                         for index, item in enumerate(bundle.items)],
    }


async def run(data: dict[str, Any] | str) -> ResolvedReview:
    model_execution = ModelExecution()
    text = data if isinstance(data, str) else json.dumps(data)
    with patch("digest.application.review.complete", return_value=(text, {})) as complete:
        result = await run_primary_review(fixture_articles(), fixture_config(),
            execution=model_execution)
        report = result.report
    assert complete.call_count == (2 if report.reviews[0].status == "invalid" else 1)
    assert all(call.kwargs["max_output_tokens"] == 4096 for call in complete.call_args_list)
    return result


@pytest.mark.asyncio
async def test_response_owns_exact_dispositions_without_changing_report_wire_fields() -> None:
    data = payload()
    data["dispositions"][1] = {"evidence_id": data["dispositions"][1]["evidence_id"], "status": "duplicate",
                               "retained_id": data["dispositions"][0]["evidence_id"],
                               "reason": "Same announcement and claims already represented by retained item."}
    result = await run(data)
    report = result.report
    assert result.outcome == "selected" and result.chosen.review is report.reviews[0]
    attempt = result.chosen.dispositions
    assert attempt.status == "complete"
    assert not attempt.unresolved_ids and not attempt.errors
    assert len(attempt.dispositions) == len(report.evidence.items)
    assert attempt.response_sha256 == hashlib.sha256(json.dumps(data).encode()).hexdigest()
    review = report.reviews[0]
    assert (attempt.slot, attempt.provider, attempt.model, attempt.bundle_id, attempt.prompt_hash) == (
        review.slot, review.provider, review.model, report.evidence.bundle_id, review.prompt_hash)
    assert attempt.prompt_hash == hashlib.sha256(json.dumps(build_review_messages(
        report.evidence, fixture_config().review, "en"), sort_keys=True).encode()).hexdigest()
    assert "dispositions" not in asdict(review)
    assert "dispositions" not in asdict(report)
    assert attempt.dispositions[0].reason == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["missing", "unknown", "duplicate_id", "self", "cycle", "chain", "contradiction",
                                 "blank", "oversize", "extra", "bad_type", "deferred", "unselected_target"])
async def test_bad_or_unfinished_dispositions_never_become_editorial_rejections(kind: str) -> None:
    data = payload()
    entries = data["dispositions"]
    identity = entries[1]["evidence_id"]
    if kind == "missing":
        entries.pop(1)
    elif kind == "unknown":
        entries[1]["evidence_id"] = "unknown"
    elif kind == "duplicate_id":
        entries[2] = deepcopy(entries[1])
    elif kind in {"self", "cycle", "chain", "unselected_target"}:
        entries[1] = {"evidence_id": identity, "status": "duplicate", "reason": "Overlapping claims.",
                      "retained_id": identity if kind == "self" else entries[2]["evidence_id"]}
        if kind in {"cycle", "chain"}:
            entries[2] = {"evidence_id": entries[2]["evidence_id"], "status": "duplicate",
                          "reason": "Overlapping claims.",
                          "retained_id": identity if kind == "cycle" else entries[0]["evidence_id"]}
    elif kind == "contradiction":
        entries[1] = {"evidence_id": identity, "status": "selected"}
    elif kind == "blank":
        entries[1]["reason"] = " "
    elif kind == "oversize":
        entries[1]["reason"] = "a" * 241
    elif kind == "extra":
        entries[1]["confidence"] = "high"
    elif kind == "bad_type":
        entries[1]["reason"] = []
    elif kind == "deferred":
        entries[1].update(status="deferred", reason="Useful but outside this response's card allowance.")
    result = await run(data)
    report = result.report
    attempt = result.disposition_attempts[0]
    assert report.reviews[0].status == "ok"  # No extra fallback for incomplete metadata.
    assert attempt.status == "incomplete"
    assert identity in attempt.unresolved_ids
    assert not any(item.evidence_id == identity and item.status in {"not_selected", "duplicate"}
                   for item in attempt.dispositions)


@pytest.mark.asyncio
async def test_invalid_selection_cannot_validate_selected_disposition() -> None:
    data = payload()
    data["selections"][0]["quote"] = "invented source quote"
    result = await run(data)
    assert len(result.disposition_attempts) == 2
    assert all(attempt.status == "incomplete" and not attempt.dispositions for attempt in result.disposition_attempts)


@pytest.mark.asyncio
async def test_legacy_is_readable_without_fabricated_omission_reasons() -> None:
    data = payload()
    del data["dispositions"]
    bundle = build_evidence_bundle(fixture_articles(), ReviewConfig())
    assert _parse_review(json.dumps(data), bundle)[0]
    result = await run(data)
    report = result.report
    assert report.reviews[0].status == "ok"
    assert not result.disposition_attempts[0].dispositions
    assert set(result.disposition_attempts[0].unresolved_ids) == {item.evidence_id for item in bundle.items}


@pytest.mark.asyncio
async def test_truncation_and_provider_failure_preserve_unresolved_packet() -> None:
    model_execution = ModelExecution()
    result = await run(json.dumps(payload())[:-8])
    assert all(attempt.status == "incomplete" and attempt.unresolved_ids for attempt in result.disposition_attempts)
    with patch("digest.application.review.complete", side_effect=RuntimeError("offline")) as complete:
        result = await run_primary_review(fixture_articles(), fixture_config(),
            execution=model_execution)
    assert complete.call_count == 2
    assert [attempt.slot for attempt in result.disposition_attempts] == ["primary", "secondary"]
    assert all(attempt.response_sha256 is None and attempt.unresolved_ids for attempt in result.disposition_attempts)


@pytest.mark.asyncio
@pytest.mark.parametrize("language,escaped", [("en", False), ("ru", False), ("ru", True)])
async def test_twenty_item_capacity_fixture_with_five_cards(language: str, escaped: bool) -> None:
    model_execution = ModelExecution()
    articles = {"Tech": [make_article(title=f'Architecture "{index}"\\path', link=f"https://example.com/{index}")
                          for index in range(20)]}
    config = fixture_config()
    config.radar.language = language
    texts = []

    async def adapter(role: Any, messages: Any, config: Any, **kwargs: Any) -> tuple[str, dict[str, int]]:
        items = json.loads(messages[1]["content"])["evidence"]["items"]
        reason = "Есть конкретный вывод для архитектуры." if language == "ru" else "Concrete architecture consequence."
        skipped = ("Нет конкретного технического следствия." if language == "ru"
                   else "No technical consequence in excerpt.")
        data = {"selections": [{"evidence_id": item["evidence_id"], "reason": reason,
                                "quote": item["title"], "confidence": "medium"} for item in items[:5]],
                "limitations": [], "dispositions": [
                    {"evidence_id": item["evidence_id"], "status": "selected"} if index < 5 else
                    {"evidence_id": item["evidence_id"], "status": "not_selected", "reason": skipped}
                    for index, item in enumerate(items)]}
        text = json.dumps(data, ensure_ascii=escaped, separators=(",", ":"))
        texts.append(text)
        assert kwargs["max_output_tokens"] == 4096
        # Offline sizing estimate only: chars/3 plus one token per ID char is
        # a sizing heuristic for these fixtures, not a bound or provider tokenizer.
        token_estimate = len(text) / 3 + sum(len(item["evidence_id"]) for item in items)
        assert token_estimate < 4096
        return text, {}

    with patch("digest.application.review.complete", side_effect=adapter) as complete:
        result = await run_primary_review(articles, config, execution=model_execution)
        report = result.report
    assert complete.call_count == 1
    assert len(report.evidence.items) == 20 and len(report.reviews[0].selections) == 5
    assert result.disposition_attempts[0].status == "complete"
    assert len(result.disposition_attempts[0].dispositions) == 20
    assert len(texts) == 1


@pytest.mark.asyncio
async def test_fallback_capture_binds_only_its_own_response() -> None:
    model_execution = ModelExecution()
    data = payload()
    text = json.dumps(data)
    with patch("digest.application.review.complete", side_effect=[("invalid", {}), (text, {})]):
        result = await run_primary_review(fixture_articles(), fixture_config(),
            execution=model_execution)
        report = result.report
    primary, secondary = result.disposition_attempts
    assert result.chosen.dispositions is secondary
    assert primary.slot == "primary" and primary.status == "incomplete"
    assert secondary.slot == "secondary" and secondary.status == "complete"
    assert secondary.response_sha256 == report.reviews[1].response_sha256
    assert secondary.response_sha256 != primary.response_sha256
    assert secondary.provider == report.reviews[1].provider
    assert secondary.model == report.reviews[1].model


@pytest.mark.asyncio
async def test_capture_rejects_changed_raw_response_or_bundle_binding() -> None:
    data = payload()
    result = await run(data)
    report = result.report
    for bundle, text in [(report.evidence, json.dumps(data) + " "),
                         (replace(report.evidence, bundle_id="other"), json.dumps(data))]:
        attempt = capture_review_dispositions(bundle, report.reviews[0], text)
        assert attempt.status == "incomplete"
        assert not attempt.dispositions
        assert len(attempt.unresolved_ids) == len(bundle.items)
        assert attempt.errors == ("disposition evidence or response binding mismatch",)


@pytest.mark.asyncio
async def test_explicit_metadata_abstention_can_account_for_every_input() -> None:
    data = payload()
    data["selections"] = []
    data["limitations"] = ["None of the supplied excerpts establishes a concrete architecture consequence."]
    data["dispositions"][0].update(status="not_selected", reason="No specific architecture consequence in excerpt.")
    result = await run(data)
    report = result.report
    assert result.outcome == "primary_abstained"
    assert report.reviews[0].status == "abstained"
    assert result.disposition_attempts[0].status == "complete"
    assert all(item.status == "not_selected" and item.reason for item in result.disposition_attempts[0].dispositions)


@pytest.mark.asyncio
async def test_capacity_deferral_preserves_every_omitted_input_as_unresolved() -> None:
    data = payload()
    for item in data["dispositions"][1:]:
        item.update(status="deferred", reason="Useful metadata candidate exceeds this response's card allowance.")
    result = await run(data)
    report = result.report
    attempt = result.disposition_attempts[0]
    assert report.reviews[0].status == "ok"
    assert attempt.status == "incomplete"
    assert set(attempt.unresolved_ids) == {item["evidence_id"] for item in data["dispositions"][1:]}
    assert len(attempt.dispositions) == len(report.evidence.items)
    assert not any(item.status == "not_selected" for item in attempt.dispositions)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["slot", "provider", "model", "bundle_id", "prompt_hash", "response_sha256",
                                 "duplicate_id", "unknown_id", "false_status", "false_unresolved", "selected_reason",
                                 "contradiction", "retained_id", "non_tuple", "invalid_error", "reason_whitespace"])
async def test_persisted_attempt_tampering_is_rejected(kind: str) -> None:
    result = await run(payload())
    report = result.report
    attempt = result.disposition_attempts[0]
    validate_disposition_attempt(attempt, report.evidence, report.reviews[0])
    if kind in {"slot", "provider", "model", "bundle_id", "prompt_hash", "response_sha256"}:
        attempt = replace(attempt, **{kind: "changed"})
    elif kind == "duplicate_id":
        attempt = replace(attempt, dispositions=attempt.dispositions[:-1] + (attempt.dispositions[0],))
    elif kind == "unknown_id":
        changed = replace(attempt.dispositions[0], evidence_id="unknown")
        attempt = replace(attempt, dispositions=(changed,) + attempt.dispositions[1:])
    elif kind == "false_status":
        attempt = replace(attempt, status="incomplete")
    elif kind == "false_unresolved":
        attempt = replace(attempt, unresolved_ids=(attempt.dispositions[0].evidence_id,))
    elif kind in {"selected_reason", "contradiction", "retained_id", "reason_whitespace"}:
        items = list(attempt.dispositions)
        if kind == "selected_reason":
            items[0] = replace(items[0], reason="injected")
        elif kind == "contradiction":
            items[1] = replace(items[1], status="selected", reason="")
        elif kind == "reason_whitespace":
            items[1] = replace(items[1], reason=" whitespace ")
        else:
            items[1] = replace(items[1], retained_id=items[0].evidence_id)
        attempt = replace(attempt, dispositions=tuple(items))
    elif kind == "non_tuple":
        attempt = replace(attempt, dispositions=list(attempt.dispositions))
    else:
        attempt = replace(attempt, errors=("",))
    with pytest.raises(ValueError):
        validate_disposition_attempt(attempt, report.evidence, report.reviews[0])


@pytest.mark.asyncio
@pytest.mark.parametrize("finish_reason", ["length", "MAX_TOKENS", "", "stop", "STOP", "end_turn", None])
async def test_provider_finish_reason_bounds_live_cards_and_capture(finish_reason: str | None) -> None:
    model_execution = ModelExecution()
    text = json.dumps(payload())
    usage = {} if finish_reason is None else {"finish_reason": finish_reason}
    with patch("digest.application.review.complete", return_value=(text, usage)) as complete:
        result = await run_primary_review(fixture_articles(), fixture_config(),
            execution=model_execution)
        report = result.report
    terminal = finish_reason is None or finish_reason in {"stop", "STOP", "end_turn"}
    assert complete.call_count == (1 if terminal else 2)
    assert report.reviews[0].status == ("ok" if terminal else "invalid")
    assert bool(report.reviews[0].selections) is terminal
    assert "finish_reason" not in asdict(report.reviews[0])
    attempt = result.disposition_attempts[0]
    assert attempt.finish_reason == finish_reason
    validate_disposition_attempt(attempt, report.evidence, report.reviews[0])
    if finish_reason is None or finish_reason in {"stop", "STOP", "end_turn"}:
        assert attempt.status == "complete"
        assert attempt.dispositions
    else:
        assert attempt.status == "incomplete"
        assert not attempt.dispositions
        assert len(attempt.unresolved_ids) == len(report.evidence.items)
        assert attempt.errors == ("provider reported unfinished response",)


@pytest.mark.asyncio
async def test_stored_truncated_finish_reason_cannot_claim_resolved_dispositions() -> None:
    result = await run(payload())
    report = result.report
    for reason in ("length", "MAX_TOKENS", 123):
        changed = replace(result.disposition_attempts[0], finish_reason=reason)
        with pytest.raises(ValueError):
            validate_disposition_attempt(changed, report.evidence, report.reviews[0])


@pytest.mark.asyncio
async def test_rejected_selection_cannot_be_resolved_by_not_selected_disposition() -> None:
    data = payload()
    rejected_id = data["dispositions"][1]["evidence_id"]
    data["selections"].append({"evidence_id": rejected_id, "reason": "Alleged architecture consequence.",
                               "quote": "invented quote absent from evidence", "confidence": "medium"})
    result = await run(data)
    report = result.report
    review = report.reviews[0]
    attempt = result.disposition_attempts[0]
    assert review.status == "partial"
    assert len(review.selections) == 1
    assert review.rejected_items[0].evidence_id == rejected_id
    assert review.rejected_items[0].reason == "quote is not in supplied evidence"
    assert attempt.status == "incomplete"
    assert rejected_id in attempt.unresolved_ids
    assert not any(item.evidence_id == rejected_id for item in attempt.dispositions)
    assert any("rejected selection output" in error for error in attempt.errors)
    validate_disposition_attempt(attempt, report.evidence, review)

    forged = replace(attempt, dispositions=attempt.dispositions + (
        CandidateDisposition(rejected_id, "not_selected", "No architecture consequence."),),
        unresolved_ids=tuple(identity for identity in attempt.unresolved_ids if identity != rejected_id))
    with pytest.raises(ValueError, match="Rejected selection output"):
        validate_disposition_attempt(forged, report.evidence, review)


@pytest.mark.asyncio
async def test_duplicate_cannot_resolve_to_identity_with_rejected_selection_output() -> None:
    data = payload()
    selected_id = data["selections"][0]["evidence_id"]
    data["selections"].append(deepcopy(data["selections"][0]))
    duplicate_id = data["dispositions"][1]["evidence_id"]
    data["dispositions"][1] = {"evidence_id": duplicate_id, "status": "duplicate",
                               "retained_id": selected_id, "reason": "Same specific announcement."}
    result = await run(data)
    report = result.report
    assert report.reviews[0].status == "partial"
    assert len(report.reviews[0].selections) == 1
    attempt = result.disposition_attempts[0]
    assert {selected_id, duplicate_id} <= set(attempt.unresolved_ids)
    assert not any(item.evidence_id in {selected_id, duplicate_id} for item in attempt.dispositions)
    validate_disposition_attempt(attempt, report.evidence, report.reviews[0])
