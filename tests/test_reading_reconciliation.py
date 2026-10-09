"""Synthetic contract proofs, not provider receipts or semantic acceptance.

Fixtures use existing source/state save/load formats. Actual private saved sources
can use the same public API without putting their full texts into the engine.
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import FrozenInstanceError, asdict, replace
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from digest import llm, source_admission
from digest.reading_brief import _messages, _parse_result, _prompt_sha, _response_sha
from digest.reading_brief_state import (
    BriefState,
    Page,
    RequestAttempt,
    Selection,
    Source,
    checksum,
    load_source,
    load_state,
    now,
    save_source,
    save_state,
    state_root,
)
from digest.reading_brief_tokens import TokenProfileUnavailable
from digest.reading_reconciliation import (
    ReconciliationInput,
    build_reconciliation_input,
    parse_reconciliation_response,
    plan_reconciliation_request,
)
from tests.factories import make_article
from tests.test_reading_brief import fetched, response


def _saved_synthetic(tmp_path: Path, text: str) -> tuple[Source, BriefState]:
    """Persist both provider pages before exercising the reconciliation contract."""
    selection = Selection.from_article(make_article())
    source_hash, source = save_source(tmp_path, selection, fetched(text))
    gemini = source_admission.route_profile("gemini", "gemini-3.8-flash", 2048)
    groq = source_admission.route_profile("groq", "openai/gpt-oss-120b", 2048)
    assert gemini is not None and groq is not None
    state = BriefState(selection, gemini, now(), now(), status="ready", source_sha256=source_hash)
    last = len(source.spans) - 1
    state.pages = [Page(0, last), Page(last, last + 1, route=groq)]
    for index, page in enumerate(state.pages):
        route = page.route or state.route
        messages = _messages(state, source, page)
        page.prompt_sha256 = _prompt_sha(state, messages, route)
        raw, usage = response(messages, abstain=bool(index))
        result = json.loads(raw)
        if index == 0:
            result["reading_angle"]["span_ids"] = [last]
            state.exact_counts[page.prompt_sha256] = 400
        else:
            state.admissions[page.prompt_sha256] = source_admission.estimate_record(route, 100)
        page.response = json.dumps(result)
        page.finish_reason = usage["finish_reason"]
        page.result = _parse_result(page.response, usage, page, source)
        page.response_sha256 = _response_sha(state, page)
        page.request_attempts = [
            RequestAttempt(
                "generate",
                route,
                page.start,
                page.stop,
                source_hash,
                page.prompt_sha256,
                now(),
                status="accepted",
                finished_at=now(),
                response_sha256=checksum(page.response),
                finish_reason=page.finish_reason,
            )
        ]
    save_state(tmp_path, state)
    loaded = load_state(tmp_path, selection.identity)
    return load_source(tmp_path, loaded), loaded


@pytest.fixture
def saved_synthetic(tmp_path: Path) -> tuple[Source, BriefState]:
    return _saved_synthetic(
        tmp_path,
        "Opening claim.\n\nUnselected background one.\n\nUnselected background two.\n\n"
        "Separate supporting finding.\n\nQUALIFICATION: only selected pilot clients qualify.",
    )


@pytest.fixture
def long_saved_synthetic(tmp_path: Path) -> tuple[Source, BriefState]:
    return _saved_synthetic(
        tmp_path,
        "Opening claim.\n\n"
        + ("Unselected background " * 30 + "\n\n") * 60
        + "Separate supporting finding.\n\nQUALIFICATION: only selected pilot clients qualify.",
    )


@pytest.fixture(autouse=True)
def no_model_calls(monkeypatch: pytest.MonkeyPatch) -> Any:
    count = AsyncMock(side_effect=AssertionError("Offline reconciliation cannot count remotely"))
    generate = AsyncMock(side_effect=AssertionError("Offline reconciliation cannot generate"))
    admission = AsyncMock(side_effect=AssertionError("Offline planning cannot admit dispatch"))
    monkeypatch.setattr(llm, "count_gemini_tokens", count)
    monkeypatch.setattr(llm, "complete", generate)
    monkeypatch.setattr(source_admission, "admit_request", admission)
    yield
    count.assert_not_called()
    generate.assert_not_called()
    admission.assert_not_called()


def test_sparse_union_retains_late_abstaining_condition_and_actual_page_proofs(
    long_saved_synthetic: tuple[Source, BriefState],
) -> None:
    source, state = long_saved_synthetic
    before = copy.deepcopy((asdict(source), asdict(state)))
    value = build_reconciliation_input(source, state)
    last = len(source.spans)
    assert value.sparse_coverage.included_span_ids == (1, last - 1, last)
    assert value.sparse_coverage.omitted_span_ids == tuple(range(2, last - 1))
    assert value.technical_page_coverage == "complete" and value.semantic_completeness == "unverified"
    assert value.sparse_coverage.kind == "selected_evidence"
    assert value.sparse_coverage.included_characters < value.source_characters == len(source.text)
    assert value.source_characters > 30_000 and value.evidence[-1].start > 30_000
    assert [page.route.provider for page in value.pages] == ["gemini", "groq"]
    assert value.pages[-1].abstain and value.pages[-1].qualification_span_ids == (last,)
    assert value.pages[0].exact_count == 400 and value.pages[-1].exact_count is None
    assert dict(value.pages[-1].admission) == state.admissions[state.pages[-1].prompt_sha256]
    for frozen, original in zip(value.pages, state.pages, strict=True):
        assert frozen.page_sha256 == checksum(asdict(original))
        assert frozen.history_evidence == "accepted_attempt"
        assert frozen.request_sha256 == original.prompt_sha256
        assert frozen.response_sha256 == original.response_sha256
    for span in value.evidence:
        assert span.text == source.text[span.start : span.end]
    assert before == (asdict(source), asdict(state))
    assert value.input_sha256 == checksum(asdict(replace(value, input_sha256="")))
    with pytest.raises(FrozenInstanceError):
        value.evidence[-1].text = "Altered condition"  # type: ignore[misc]


@pytest.mark.parametrize("ids", [(False,), (0,), (9999,), (2, 2)])
def test_unknown_duplicate_or_noninteger_context_rejected(
    saved_synthetic: tuple[Source, BriefState],
    ids: tuple[int, ...],
) -> None:
    with pytest.raises(ValueError, match="invalid_reconciliation_context_ids"):
        build_reconciliation_input(*saved_synthetic, extra_context_span_ids=ids)


def test_explicit_context_expands_original_order_and_changes_hash(saved_synthetic: tuple[Source, BriefState]) -> None:
    source, state = saved_synthetic
    sparse = build_reconciliation_input(source, state)
    expanded = build_reconciliation_input(source, state, extra_context_span_ids=(3, 2))
    assert expanded.extra_context_span_ids == (2, 3)
    assert expanded.sparse_coverage.included_span_ids == (1, 2, 3, len(source.spans) - 1, len(source.spans))
    assert sparse.input_sha256 != expanded.input_sha256
    assert expanded.pages == sparse.pages


def test_frozen_input_cannot_change_through_original_mutable_lists(
    saved_synthetic: tuple[Source, BriefState],
) -> None:
    source, state = saved_synthetic
    value = build_reconciliation_input(source, state)
    before = asdict(value)
    source.coverage_notes.append("Changed after binding")
    result = state.pages[-1].result
    assert result is not None
    result.qualification_span_ids.clear()
    state.pages[-1].request_attempts.clear()
    state.admissions.clear()
    assert asdict(value) == before
    with pytest.raises(ValueError):
        build_reconciliation_input(source, state)


def test_rebound_source_identity_still_requires_original_page_requests(
    saved_synthetic: tuple[Source, BriefState],
) -> None:
    source, state = saved_synthetic
    changed = source.text.replace("pilot", "other")
    source = replace(source, text=changed, body_sha256=hashlib.sha256(changed.encode()).hexdigest())
    state.source_sha256 = checksum(asdict(source))
    for page in state.pages:
        for attempt in page.request_attempts:
            attempt.source_sha256 = state.source_sha256
    with pytest.raises(ValueError, match="request_prompt_mismatch"):
        build_reconciliation_input(source, state)


@pytest.mark.parametrize(
    "damage",
    [
        "body",
        "source_hash",
        "selection",
        "manifest",
        "missing_page",
        "unknown",
        "pending",
        "request",
        "response",
        "attempt",
        "admission",
    ],
)
def test_incomplete_or_mismatched_evidence_is_rejected(
    saved_synthetic: tuple[Source, BriefState],
    damage: str,
) -> None:
    source, state = saved_synthetic
    if damage == "body":
        source = replace(source, text=source.text + "Changed")
    elif damage == "source_hash":
        state.source_sha256 = "0" * 64
    elif damage == "selection":
        state.selection = replace(state.selection, title="Another selection")
    elif damage == "manifest":
        source.spans.pop()
    elif damage == "missing_page":
        state.pages.pop()
    elif damage == "unknown":
        state.status = "pending"
        state.pages[-1].result = None
        state.pages[-1].request_attempts[-1].status = "unknown"
    elif damage == "pending":
        state.status = "pending"
    elif damage == "request":
        state.pages[-1].prompt_sha256 = "0" * 64
    elif damage == "response":
        state.pages[-1].response = "{}"
    elif damage == "attempt":
        state.pages[-1].request_attempts[-1].source_sha256 = "0" * 64
    elif damage == "admission":
        state.admissions.clear()
    with pytest.raises(ValueError):
        build_reconciliation_input(source, state)


def test_legacy_saved_format_stays_explicit_without_manufacturing_request_history(
    saved_synthetic: tuple[Source, BriefState],
    tmp_path: Path,
) -> None:
    source, state = saved_synthetic
    path = state_root(tmp_path) / f"{state.selection.identity}.json"
    envelope = json.loads(path.read_text())
    for page in envelope["payload"]["pages"]:
        page.pop("request_attempts")
        page.pop("request_history_version")
    envelope["sha256"] = checksum(envelope["payload"])
    path.write_text(json.dumps(envelope))
    original_bytes = path.read_bytes()
    legacy = load_state(tmp_path, state.selection.identity)
    value = build_reconciliation_input(source, legacy)
    assert all(page.history_evidence == "legacy_completed_response" for page in value.pages)
    assert all(not page.request_attempts for page in legacy.pages)
    assert path.read_bytes() == original_bytes
    assert value.evidence == build_reconciliation_input(source, state).evidence


def test_prospective_gemini_wire_is_complete_bound_and_remotely_unverified(
    saved_synthetic: tuple[Source, BriefState],
) -> None:
    source, state = saved_synthetic
    value = build_reconciliation_input(source, state)
    plans = [
        plan_reconciliation_request(
            value,
            instruction=instruction,
            provider="gemini",
            model="gemini-3.8-flash",
            max_output_tokens=output,
            temperature=temperature,
        )
        for instruction, output, temperature in [
            ("Inspect evidence.", 2048, 0.1),
            ("Changed.", 2048, 0.1),
            ("Inspect evidence.", 2049, 0.1),
            ("Inspect evidence.", 2048, 0.2),
        ]
    ]
    assert len({plan.request_sha256 for plan in plans}) == 4
    for plan in plans:
        assert plan.status == "unverified" and not plan.remote_count_performed
        assert plan.error_class == "technical_remote_count_unperformed" and not plan.accounting
        assert plan.input_sha256 == value.input_sha256
        wire = json.loads(plan.wire_json)
        assert json.loads(wire["contents"][0]["parts"][0]["text"]) == json.loads(json.dumps(asdict(value)))
        assert wire["generationConfig"]["maxOutputTokens"] == plan.output_reserve
        prefix = json.dumps([plan.provider, plan.model], separators=(",", ":")).encode()
        assert plan.request_sha256 == hashlib.sha256(prefix + b"\n" + plan.wire_json.encode()).hexdigest()


@pytest.mark.parametrize(("count", "status"), [(100, "estimated_fit"), (20_000, "oversized")])
def test_groq_local_estimate_never_truncates_or_claims_remote_admission(
    saved_synthetic: tuple[Source, BriefState],
    monkeypatch: pytest.MonkeyPatch,
    count: int,
    status: str,
) -> None:
    source, state = saved_synthetic
    value = build_reconciliation_input(source, state)
    monkeypatch.setattr(source_admission, "count_gpt_input", lambda messages: count)
    plan = plan_reconciliation_request(
        value,
        instruction="Inspect all retained conditions.",
        provider="groq",
        model="openai/gpt-oss-120b",
        max_output_tokens=2048,
    )
    assert plan.status == status and not plan.remote_count_performed
    assert plan.input_limit == 5952 and plan.output_reserve == 2048
    assert dict(plan.accounting)["input_estimate"] == (count * 6 + 4) // 5 + 256
    wire = json.loads(plan.wire_json)
    assert json.loads(wire["messages"][1]["content"]) == json.loads(json.dumps(asdict(value)))
    assert wire["max_completion_tokens"] == 2048


def test_missing_local_counter_remains_unverified(
    saved_synthetic: tuple[Source, BriefState],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(*args: Any) -> Any:
        raise TokenProfileUnavailable("No local tokenizer")

    source, state = saved_synthetic
    monkeypatch.setattr(source_admission, "estimate_request", unavailable)
    plan = plan_reconciliation_request(
        build_reconciliation_input(source, state),
        instruction="Inspect.",
        provider="groq",
        model="openai/gpt-oss-120b",
        max_output_tokens=2048,
    )
    assert plan.status == "unverified" and plan.error_class == "technical_tokenizer_profile"
    assert plan.wire_json and not plan.accounting


def synthetic_reconciliation_response(value: ReconciliationInput, *, abstain: bool = False) -> str:
    """Synthetic prose deliberately does not claim source fidelity."""
    return json.dumps(
        {
            "selected_span_ids": [] if abstain else [value.evidence[0].id],
            "qualification_span_ids": sorted({item for page in value.pages for item in page.qualification_span_ids}),
            "reading_angle": None
            if abstain
            else {
                "text": "Synthetic finding with semantic accuracy still unverified.",
                "span_ids": [value.evidence[0].id],
            },
            "abstain": abstain,
        }
    )


def test_abstention_retains_qualifications_without_creating_a_brief(saved_synthetic: tuple[Source, BriefState]) -> None:
    source, state = saved_synthetic
    value = build_reconciliation_input(source, state)
    raw = synthetic_reconciliation_response(value, abstain=True)
    result = parse_reconciliation_response(raw, value)
    assert result.abstain and result.qualification_span_ids == (len(source.spans),)
    assert result.reading_angle is None and not result.selected_span_ids and not result.angle_span_ids
    with pytest.raises(FrozenInstanceError):
        result.abstain = False  # type: ignore[misc]


def test_valid_citations_cannot_detect_a_synthetic_scope_error(saved_synthetic: tuple[Source, BriefState]) -> None:
    source, state = saved_synthetic
    value = build_reconciliation_input(source, state)
    data = json.loads(synthetic_reconciliation_response(value))
    # Deliberately contradict the fixture's retained pilot-client condition.
    data["reading_angle"] = {"text": "All clients qualify.", "span_ids": [1, len(source.spans)]}
    raw = json.dumps(data)
    result = parse_reconciliation_response(raw, value)
    assert result.reading_angle == "All clients qualify."
    assert value.semantic_completeness == "unverified"


@pytest.mark.parametrize(
    "damage",
    [
        "missing_qualification",
        "unknown_qualification",
        "omitted_citation",
        "unknown_selected",
        "bool_id",
        "duplicate_id",
        "empty_selection",
        "empty_prose",
        "empty_citations",
        "missing_prose",
        "extra_angle_field",
        "nonboolean_abstain",
        "abstain_with_prose",
        "missing_field",
        "coverage",
        "model_copied_hash",
        "duplicate_key",
    ],
)
def test_response_rejects_invalid_schema_ids_and_empty_success(
    saved_synthetic: tuple[Source, BriefState],
    damage: str,
) -> None:
    source, state = saved_synthetic
    value = build_reconciliation_input(source, state)
    data = json.loads(synthetic_reconciliation_response(value))
    if damage == "missing_qualification":
        data["qualification_span_ids"] = []
    elif damage == "unknown_qualification":
        data["qualification_span_ids"].append(9999)
    elif damage == "omitted_citation":
        data["reading_angle"]["span_ids"] = [value.sparse_coverage.omitted_span_ids[0]]
    elif damage == "unknown_selected":
        data["selected_span_ids"] = [9999]
    elif damage == "bool_id":
        data["selected_span_ids"] = [True]
    elif damage == "duplicate_id":
        data["reading_angle"]["span_ids"] = [1, 1]
    elif damage == "empty_selection":
        data["selected_span_ids"] = []
    elif damage == "empty_prose":
        data["reading_angle"]["text"] = " \n "
    elif damage == "empty_citations":
        data["reading_angle"]["span_ids"] = []
    elif damage == "missing_prose":
        data["reading_angle"] = None
    elif damage == "extra_angle_field":
        data["reading_angle"]["certified"] = True
    elif damage == "nonboolean_abstain":
        data["abstain"] = 0
    elif damage == "abstain_with_prose":
        data["abstain"] = True
    elif damage == "missing_field":
        del data["qualification_span_ids"]
    elif damage == "coverage":
        data["coverage"] = {"first_span_id": 1, "last_span_id": len(source.spans)}
    elif damage == "model_copied_hash":
        data["input_sha256"] = value.input_sha256
    raw = json.dumps(data)
    if damage == "duplicate_key":
        raw = raw[:-1] + ', "abstain": false}'
    with pytest.raises(ValueError):
        parse_reconciliation_response(raw, value)
