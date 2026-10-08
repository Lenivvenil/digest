"""Offline private ranking evidence; audit retention never changes decisions."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from digest.adapters.models.execution import ModelExecution
from digest.irritator.evidence_stage import (
    MAX_RANKING_CANDIDATES,
    MAX_RANKING_JSON_CHARS,
    MAX_RESPONSE_CHARS,
    MAX_SOURCE_RESULTS,
    SAFE_SOURCES,
    EvidenceIrritatorResult,
    _admit_ranking,
    _parse_narrative,
    _parse_rankings,
    _ranking_signal_payload,
    run_evidence_irritator,
)
from digest.irritator.sources import Signal
from scripts.review_fixture import fixture_config
from tests.factories import make_signal
from tests.test_irritator_evidence import _bundle, _narrative, _offline_client, _queries


def _entry(signal: Signal, relation: str = "context", score: int = 9) -> dict[str, Any]:
    return {
        "url": signal.url,
        "relation": relation,
        "score": score,
        "reasoning": "Private model judgment about the supplied evidence.",
        "quote_id": _ranking_signal_payload(signal)["title"][0]["id"],
    }


def _signals(count: int, *, snippet: str = "Supplied external abstract.") -> list[Signal]:
    return [
        make_signal(url=f"https://external.example/{index}", title=f"Literal title {index}", snippet=snippet)
        for index in range(count)
    ]


def test_old_result_constructor_still_has_no_recorded_audit() -> None:
    legacy = EvidenceIrritatorResult(1, "old-bundle", "empty")
    assert legacy.schema_version == 1 and legacy.ranking_audit is None


def test_admission_trace_preserves_greedy_skips_order_and_candidate_cap() -> None:
    huge = _signals(1, snippet="large " * 4000)[0]
    small = _signals(14, snippet="")
    huge = replace(huge, url="https://external.example/oversized")
    signals = [huge, *small]
    admission = _admit_ranking(signals, 5, 3, {})
    selected, audit = admission.signals, admission.audit
    assert selected == tuple(small[:MAX_RANKING_CANDIDATES])
    assert [item.signal.url for item in audit.candidates] == [signal.url for signal in signals]
    assert [item.admission for item in audit.candidates] == [
        "evidence_budget",
        *(["admitted"] * 12),
        "candidate_limit",
        "candidate_limit",
    ]
    assert all(item.signal == original for item, original in zip(audit.candidates[1:13], selected, strict=True))
    assert all(item.decision is None for item in audit.candidates)
    assert not audit.response_validated
    assert audit.candidates[0].snippet_truncated
    assert audit.candidates[0].signal.snippet == huge.snippet[: len(audit.candidates[0].signal.snippet)]
    assert audit.candidates[0].snippet_chars == len(huge.snippet)
    original = json.dumps(asdict(huge), ensure_ascii=True, sort_keys=True)
    assert audit.candidates[0].signal_sha256 == hashlib.sha256(original.encode()).hexdigest()
    assert audit.candidates[0].signal_json_chars == len(original)


def test_exact_packet_boundary_and_utf8_evidence_remain_unchanged() -> None:
    signal = _signals(1, snippet='Literal  text\nwith "quotes", \\ paths and café 🧪.')[0]
    # Published is not segmented: grow this synthetic field only to reach the exact
    # admission boundary without imposing a new field limit on the real selector.
    original_size = len(json.dumps([_ranking_signal_payload(signal)], ensure_ascii=False))
    exact = replace(signal, published="x" * (MAX_RANKING_JSON_CHARS - original_size + len(signal.published)))
    assert len(json.dumps([_ranking_signal_payload(exact)], ensure_ascii=False)) == MAX_RANKING_JSON_CHARS
    assert _admit_ranking([exact], 5, 3, {}).signals == (exact,)
    over = replace(exact, published=exact.published + "x")
    assert _admit_ranking([over], 5, 3, {}).signals == ()
    audit = _admit_ranking([signal], 5, 3, {}).audit
    assert audit.candidates[0].signal is signal
    assert not audit.candidates[0].title_truncated and not audit.candidates[0].snippet_truncated
    assert audit.candidates[0].ranking_payload_chars == len(
        json.dumps(_ranking_signal_payload(signal), ensure_ascii=False)
    )


@pytest.mark.parametrize(
    "relation,score,disposition",
    [
        ("contradicts", 5, "accepted"),
        ("complicates", 5, "accepted"),
        ("complicates", 4, "below_min_score"),
        ("supports", 10, "non_counter"),
        ("context", 10, "non_counter"),
        ("insufficient", 10, "non_counter"),
    ],
)
def test_every_valid_returned_judgment_is_auditable(relation: str, score: int, disposition: str) -> None:
    bundle = _bundle(fixture_config())
    narrative = _parse_narrative(json.dumps(_narrative(bundle)), bundle)[0][0]
    signals = _signals(2)
    admission = _admit_ranking(signals, 5, 3, {})
    pending = asdict(admission.audit)
    response = json.dumps({"rankings": [_entry(signals[0], relation, score)], "limitations": []})
    actual = _parse_rankings(response, admission, narrative)
    audit = actual.audit
    assert bool(actual.ranked_signals) == (disposition == "accepted")
    assert audit is not admission.audit and asdict(admission.audit) == pending
    assert audit.response_validated
    first, missing = audit.candidates
    assert first.disposition == disposition and first.decision is not None
    assert asdict(first.decision) == {
        **{key: value for key, value in _entry(signals[0], relation, score).items() if key != "url"},
        "quote": signals[0].title,
    }
    assert missing.disposition == "not_returned" and missing.decision is None


@pytest.mark.parametrize(
    "mutation",
    [
        "duplicate_url", "unknown_url", "unknown_quote_id", "cross_url_quote",
        "extra_field", "bad_relation", "float_score", "bool_score", "high_score",
        "blank_reason", "too_many", "invalid_json", "too_long", "bad_limitations",
        "missing_relation", "empty_unexplained",
    ],
)
def test_invalid_ranking_is_atomic_even_below_the_editorial_threshold(mutation: str) -> None:
    bundle = _bundle(fixture_config())
    narrative = _parse_narrative(json.dumps(_narrative(bundle)), bundle)[0][0]
    signals = _signals(2)
    admission = _admit_ranking(signals, 5, 3, {})
    audit = admission.audit
    first, invalid = _entry(signals[0], "complicates"), _entry(signals[1], "supports", 1)
    invalid["reasoning"] = "INVALID PRIVATE SENTINEL"
    changes = {
        "duplicate_url": {"url": signals[0].url},
        "unknown_url": {"url": "https://unknown.example"},
        "unknown_quote_id": {"quote_id": "unknown-id"},
        "cross_url_quote": {"quote_id": first["quote_id"]},
        "extra_field": {"quote": "forbidden free-text quote"},
        "bad_relation": {"relation": "other"},
        "float_score": {"score": 8.0},
        "bool_score": {"score": True},
        "high_score": {"score": 11},
        "blank_reason": {"reasoning": "  "},
    }
    invalid.update(changes.get(mutation, {}))
    if mutation == "missing_relation":
        del invalid["relation"]
    payload: dict[str, Any] = {"rankings": [first, invalid], "limitations": []}
    if mutation == "too_many":
        payload["rankings"] *= 2
    if mutation == "empty_unexplained":
        payload["rankings"] = []
    if mutation == "bad_limitations":
        payload["limitations"] = [False]
    text = json.dumps(payload)
    if mutation == "invalid_json":
        text = "INVALID PRIVATE SENTINEL"
    if mutation == "too_long":
        text = "x" * (MAX_RESPONSE_CHARS + 1)
    with pytest.raises(ValueError):
        _parse_rankings(text, admission, narrative)
    assert not audit.response_validated
    assert all(item.disposition == "pending" and item.decision is None for item in audit.candidates)
    assert "INVALID PRIVATE SENTINEL" not in json.dumps(asdict(audit))


@pytest.mark.asyncio
async def test_empty_response_records_not_returned_but_omitted_candidates_remain_unknown() -> None:
    model_execution = ModelExecution()
    config = fixture_config()
    config.irritator.sources = ["hackernews"]
    bundle = _bundle(config)
    signals = [*_signals(1), replace(_signals(1, snippet="large " * 4000)[0], url="https://other.example")]
    model = AsyncMock(
        side_effect=[
            (json.dumps(_narrative(bundle)), {}),
            (json.dumps(_queries(2)), {}),
            (json.dumps({"rankings": [], "limitations": ["No chosen rankings."]}), {}),
        ]
    )
    with (
        patch("digest.irritator.evidence_stage.complete", model),
        patch("digest.irritator.evidence_stage.search_hackernews", AsyncMock(return_value=signals)) as search,
    ):
        async with _offline_client() as client:
            result = await run_evidence_irritator(bundle, config, client, execution=model_execution)
    audit = result.ranking_audit
    assert audit is not None and audit.response_validated
    assert [item.disposition for item in audit.candidates] == ["not_returned", "not_admitted"]
    assert all(item.query_indices == [0, 1] for item in audit.candidates)
    assert result.status == "incomplete" and not result.ranked_signals
    assert model.await_count == 3 and search.await_count == 2
    ranking_input = json.loads(model.await_args_list[2].args[1][1]["content"])
    assert ranking_input["signals"] == [_ranking_signal_payload(signals[0])]
    assert "ranking_audit" not in json.dumps([call.args[1] for call in model.await_args_list])


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["provider", "timeout", "invalid_response"])
async def test_ranking_failure_keeps_admission_trace_without_decisions(failure: str) -> None:
    model_execution = ModelExecution()
    config = fixture_config()
    config.irritator.sources = ["hackernews"]
    bundle = _bundle(config)
    signals = _signals(2)
    responses: list[Any] = [(json.dumps(_narrative(bundle)), {}), (json.dumps(_queries()), {})]
    if failure == "provider":
        responses.append(RuntimeError("RAW ERROR SENTINEL"))
    elif failure == "invalid_response":
        responses.append(("RAW ERROR SENTINEL", {}))
    model = AsyncMock(side_effect=responses)
    if failure == "timeout":

        async def timeout(*args: Any, **kwargs: Any) -> tuple[str, dict[str, Any]]:
            if responses:
                return responses.pop(0)  # type: ignore[no-any-return]
            await asyncio.sleep(1)
            raise AssertionError("The ranking deadline must have elapsed")

        model.side_effect = timeout
    with (
        patch("digest.irritator.evidence_stage.complete", model),
        patch("digest.irritator.evidence_stage.search_hackernews", AsyncMock(return_value=signals)),
    ):
        async with _offline_client() as client:
            result = await run_evidence_irritator(bundle, config, client,
                execution=model_execution, timeout_seconds=0.05)
    assert result.status == "incomplete" and not result.ranked_signals
    assert result.ranking_audit is not None and not result.ranking_audit.response_validated
    assert all(item.disposition == "pending" and item.decision is None for item in result.ranking_audit.candidates)
    assert model.await_count == 3 and "RAW ERROR SENTINEL" not in json.dumps(asdict(result))


@pytest.mark.asyncio
async def test_all_oversized_candidates_keep_trace_without_ranking_call() -> None:
    model_execution = ModelExecution()
    config = fixture_config()
    config.irritator.sources = ["hackernews"]
    bundle = _bundle(config)
    signals = _signals(2, snippet="large " * 4000)
    model = AsyncMock(side_effect=[(json.dumps(_narrative(bundle)), {}), (json.dumps(_queries()), {})])
    with (
        patch("digest.irritator.evidence_stage.complete", model),
        patch("digest.irritator.evidence_stage.search_hackernews", AsyncMock(return_value=signals)),
    ):
        async with _offline_client() as client:
            result = await run_evidence_irritator(bundle, config, client, execution=model_execution)
    assert result.status == "incomplete" and model.await_count == 2
    assert result.ranking_audit is not None and not result.ranking_audit.response_validated
    assert all(
        item.admission == "evidence_budget" and item.disposition == "not_admitted"
        for item in result.ranking_audit.candidates
    )


def test_whole_omitted_text_allocation_and_serialized_growth_are_bounded() -> None:
    # A conservative adversarial serialization case: all 90 identity fields at
    # existing maxima, JSON-escaped control text, including fully truncated titles.
    count = 3 * len(SAFE_SOURCES) * MAX_SOURCE_RESULTS
    signals = [
        make_signal(
            url=(f"https://e.example/{index}/" + "\x00" * 2048)[:2048],
            title="\x00" * 20000,
            snippet="\x00" * 20000,
            published="\x00" * 80,
        )
        for index in range(count)
    ]
    admission = _admit_ranking(signals, 5, 3, {})
    assert admission.signals == ()
    audit = admission.audit
    assert sum(len(item.signal.title) + len(item.signal.snippet) for item in audit.candidates) <= MAX_RESPONSE_CHARS
    assert all(item.title_truncated and item.snippet_truncated for item in audit.candidates)
    # This includes identifiers, metadata, indentation and escaping, not merely
    # the diagnostic-text allocation. It is a fixture bound, not a new runtime cap.
    serialized = json.dumps(asdict(audit), indent=2).encode()
    assert len(serialized) < 1_400_000


@pytest.mark.asyncio
async def test_query_lineage_binds_the_exact_retained_snapshot() -> None:
    model_execution = ModelExecution()
    config = fixture_config()
    config.irritator.sources = ["hackernews"]
    bundle = _bundle(config)
    signal = _signals(1)[0]
    model = AsyncMock(
        side_effect=[
            (json.dumps(_narrative(bundle)), {}),
            (json.dumps(_queries(3)), {}),
            (json.dumps({"rankings": [_entry(signal)], "limitations": []}), {}),
        ]
    )
    with (
        patch("digest.irritator.evidence_stage.complete", model),
        patch(
            "digest.irritator.evidence_stage.search_hackernews",
            AsyncMock(
                side_effect=[
                    [signal],
                    [replace(signal, snippet="Different retrieved snapshot")],
                    [signal],
                ]
            ),
        ),
    ):
        async with _offline_client() as client:
            result = await run_evidence_irritator(bundle, config, client, execution=model_execution)
    assert result.ranking_audit is not None
    assert len(result.ranking_audit.candidates) == 1
    assert result.ranking_audit.candidates[0].query_indices == [0, 2]
    assert result.ranking_audit.candidates[0].signal == signal


@pytest.mark.asyncio
async def test_ranking_admission_hold_retains_evidence_without_generation(tmp_path: Path) -> None:
    model_execution = ModelExecution()
    from digest.source_admission import RequestAdmission, admit_request
    from tests.test_review_checkpoint import _full_source_evidence

    config = fixture_config()
    config.irritator.sources = ["hackernews"]
    rss = _bundle(config)
    source = _full_source_evidence(tmp_path, rss)
    narrative = _narrative(source)
    narrative["narratives"][0]["quotes"][source.items[0].evidence_id] = source.items[0].excerpt
    model = AsyncMock(side_effect=[(json.dumps(narrative), {}), (json.dumps(_queries(anchor="rollout")), {})])
    admissions = 0

    async def admit(*args: Any, **kwargs: Any) -> RequestAdmission:
        nonlocal admissions
        admissions += 1
        record = await admit_request(*args, **kwargs)
        return replace(record, status="unverified", error_class="test_hold") if admissions == 3 else record

    with (
        patch("digest.irritator.evidence_stage.complete", model),
        patch("digest.source_admission.count_gpt_input", return_value=1000),
        patch("digest.irritator.evidence_stage.admit_request", side_effect=admit),
        patch("digest.irritator.evidence_stage.search_hackernews", AsyncMock(return_value=_signals(1))),
    ):
        async with _offline_client() as client:
            result = await run_evidence_irritator(rss, config, client,
                execution=model_execution, source_evidence=source)
    assert result.status == "incomplete" and model.await_count == 2 and admissions == 3
    assert result.ranking_audit is not None and not result.ranking_audit.response_validated
    assert result.ranking_audit.candidates[0].disposition == "pending"
    assert result.diagnostics[-1].error == "test_hold"


def test_serialized_growth_includes_returned_reasoning_and_quote() -> None:
    bundle = _bundle(fixture_config())
    narrative = _parse_narrative(json.dumps(_narrative(bundle)), bundle)[0][0]
    signal = _signals(1)[0]
    omitted = [
        replace(item, url=(f"https://e.example/{index}/" + "\x00" * 2048)[:2048], published="\x00" * 80)
        for index, item in enumerate(_signals(89, snippet="\x00" * 20000))
    ]
    admission = _admit_ranking([signal, *omitted], 5, 3, {})
    assert admission.signals == (signal,)
    entry = _entry(signal)
    entry["reasoning"] = "𐀀" * 15000
    response = json.dumps({"rankings": [entry], "limitations": []}, ensure_ascii=False)
    assert len(response) <= MAX_RESPONSE_CHARS
    audit = _parse_rankings(response, admission, narrative).audit
    assert audit.candidates[0].decision is not None
    assert audit.candidates[0].decision.reasoning == entry["reasoning"]
    assert len(json.dumps(asdict(audit), indent=2).encode()) < 1_500_000


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["narrative", "queries"])
async def test_failure_before_retrieval_does_not_invent_an_audit(failure: str) -> None:
    model_execution = ModelExecution()
    config = fixture_config()
    bundle = _bundle(config)
    responses: list[Any] = [RuntimeError("PRIVATE ERROR BODY")]
    if failure == "queries":
        responses.insert(0, (json.dumps(_narrative(bundle)), {}))
    with (
        patch("digest.irritator.evidence_stage.complete", AsyncMock(side_effect=responses)),
        patch("digest.irritator.evidence_stage.search_hackernews", AsyncMock()) as search,
    ):
        async with _offline_client() as client:
            result = await run_evidence_irritator(bundle, config, client, execution=model_execution)
    assert result.ranking_audit is None and "PRIVATE ERROR BODY" not in json.dumps(asdict(result))
    search.assert_not_awaited()


@pytest.mark.asyncio
async def test_partial_source_failure_keeps_validated_ranking_trace_and_incomplete_status() -> None:
    model_execution = ModelExecution()
    config = fixture_config()
    config.irritator.sources = ["hackernews", "arxiv"]
    bundle = _bundle(config)
    signal = _signals(1)[0]
    model = AsyncMock(
        side_effect=[
            (json.dumps(_narrative(bundle)), {}),
            (json.dumps(_queries()), {}),
            (json.dumps({"rankings": [_entry(signal)], "limitations": []}), {}),
        ]
    )
    with (
        patch("digest.irritator.evidence_stage.complete", model),
        patch("digest.irritator.evidence_stage.search_hackernews", AsyncMock(return_value=[signal])),
        patch("digest.irritator.evidence_stage.search_arxiv", AsyncMock(side_effect=RuntimeError("PRIVATE ERROR"))),
    ):
        async with _offline_client() as client:
            result = await run_evidence_irritator(bundle, config, client, execution=model_execution)
    assert result.status == "incomplete" and model.await_count == 3
    assert result.ranking_audit is not None and result.ranking_audit.response_validated
    assert result.ranking_audit.candidates[0].disposition == "non_counter"
    assert result.ranking_audit.candidates[0].query_indices == [0]
    assert "PRIVATE ERROR" not in json.dumps(asdict(result))


@pytest.mark.asyncio
async def test_audit_hashing_does_not_reject_a_blocklisted_lone_surrogate() -> None:
    model_execution = ModelExecution()
    config = fixture_config()
    config.irritator.sources = ["hackernews"]
    config.filters.blocklist_keywords = ["BLOCKLISTED"]
    bundle = _bundle(config)
    signal = _signals(1)[0]
    ignored = replace(signal, url="https://ignored.example", title="BLOCKLISTED \ud800")
    model = AsyncMock(
        side_effect=[
            (json.dumps(_narrative(bundle)), {}),
            (json.dumps(_queries()), {}),
            (json.dumps({"rankings": [_entry(signal, "complicates")], "limitations": []}), {}),
        ]
    )
    with (
        patch("digest.irritator.evidence_stage.complete", model),
        patch("digest.irritator.evidence_stage.search_hackernews", AsyncMock(return_value=[signal, ignored])),
    ):
        async with _offline_client() as client:
            result = await run_evidence_irritator(bundle, config, client, execution=model_execution)
    assert result.status == "complete" and model.await_count == 3 and len(result.ranked_signals) == 1
    assert result.ranking_audit is not None and len(result.ranking_audit.candidates) == 1
    assert result.ranking_audit.candidates[0].query_indices == [0]
    assert result.source_attempts[0].status == "complete"


def test_audit_does_not_evaluate_payloads_beyond_existing_candidate_cap(tmp_path: Path) -> None:
    from digest._util import atomic_json_write

    admitted = _signals(MAX_RANKING_CANDIDATES, snippet="")
    unexamined = make_signal(url="https://later.example", title="Unexamined \ud800", snippet="Original abstract.")
    signals = [*admitted, unexamined]
    admission = _admit_ranking(signals, 5, 3, {})
    assert admission.signals == tuple(admitted)
    audit = admission.audit
    record = audit.candidates[-1]
    assert record.admission == "candidate_limit" and record.ranking_payload_chars is None
    assert record.signal == unexamined and not record.title_truncated
    original = json.dumps(asdict(unexamined), ensure_ascii=True, sort_keys=True)
    assert record.signal_sha256 == hashlib.sha256(original.encode()).hexdigest()
    archive = tmp_path / "trace.json"
    atomic_json_write(archive, asdict(audit))
    assert json.loads(archive.read_text())["candidates"][-1]["signal"]["title"] == unexamined.title
