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
    RESPONSE_INSTRUCTION,
    RESPONSE_VERSION,
    ReconciliationCompletion,
    ReconciliationInput,
    build_reconciliation_input,
    parse_reconciliation_response,
    plan_reconciliation_request,
    verify_reconciliation_input,
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
    verify_reconciliation_input(value, source, state)
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
        verify_reconciliation_input(value, source, state)


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


@pytest.mark.parametrize("damage", ["condition", "text", "offset", "page", "coverage", "bool"])
def test_verifier_rejects_tampering_even_with_recomputed_input_hash(
    saved_synthetic: tuple[Source, BriefState],
    damage: str,
) -> None:
    source, state = saved_synthetic
    value = build_reconciliation_input(source, state)
    if damage == "condition":
        value = replace(value, evidence=value.evidence[:-1])
    elif damage == "text":
        value = replace(value, evidence=(*value.evidence[:-1], replace(value.evidence[-1], text="All clients")))
    elif damage == "offset":
        value = replace(value, evidence=(replace(value.evidence[0], end=2), *value.evidence[1:]))
    elif damage == "page":
        value = replace(value, pages=value.pages[:-1])
    elif damage == "coverage":
        value = replace(value, sparse_coverage=replace(value.sparse_coverage, omitted_span_ids=()))
    elif damage == "bool":
        value = replace(value, evidence=(replace(value.evidence[0], id=True), *value.evidence[1:]))
    value = replace(value, input_sha256=checksum(asdict(replace(value, input_sha256=""))))
    with pytest.raises(ValueError, match="reconciliation_input_mismatch"):
        verify_reconciliation_input(value, source, state)


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
            source,
            state,
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
        source,
        state,
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
        source,
        state,
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


def synthetic_reconciliation_transport(
    value: ReconciliationInput,
    raw: str,
    *,
    provider: str = "gemini",
) -> tuple[source_admission.RequestAdmission, ReconciliationCompletion]:
    """Fabricated fixture metadata, never real provider/admission receipts.

    No LocalRequestPlan is promoted to a completed physical request.
    """
    model = "gemini-3.8-flash" if provider == "gemini" else "openai/gpt-oss-120b"
    route = source_admission.route_profile(provider, model, 2048)
    assert route is not None
    messages = [
        {"role": "system", "content": RESPONSE_INSTRUCTION},
        {"role": "user", "content": json.dumps(asdict(value), ensure_ascii=False, sort_keys=True)},
    ]
    request_hash = source_admission.request_sha256(route, messages, 0.1)
    record = source_admission.estimate_record(route, 100) if provider == "groq" else {}
    admission = source_admission.RequestAdmission(
        provider,
        model,
        request_hash,
        2048,
        route.input_tokens,
        status="admitted",
        method="exact" if provider == "gemini" else "estimated",
        exact_count=400 if provider == "gemini" else None,
        input_estimate=int(record["input_estimate"]) if record else None,
        evidence=record,
    )
    completion = ReconciliationCompletion(
        RESPONSE_VERSION,
        value.input_sha256,
        provider,
        model,
        0.1,
        2048,
        request_hash,
        hashlib.sha256(raw.encode()).hexdigest(),
        "completed",
        "STOP" if provider == "gemini" else "stop",
    )
    return admission, completion


@pytest.mark.parametrize("provider", ["gemini", "groq"])
def test_response_binds_sparse_archive_without_certifying_prose(
    saved_synthetic: tuple[Source, BriefState],
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
) -> None:
    source, state = saved_synthetic
    value = build_reconciliation_input(source, state)
    raw = synthetic_reconciliation_response(value)
    admission, completion = synthetic_reconciliation_transport(value, raw, provider=provider)
    original = copy.deepcopy((asdict(source), asdict(state), asdict(value)))
    monkeypatch.setattr(source_admission, "estimate_request", lambda *args: pytest.fail("Must reuse saved count"))
    monkeypatch.setattr(source_admission, "count_gpt_input", lambda *args: pytest.fail("Must not need tokenizer"))
    result = parse_reconciliation_response(raw, value, source, state, admission=admission, completion=completion)
    assert result.input == value and result.input.pages[-1].abstain
    assert result.qualification_span_ids == (len(source.spans),)
    # A retained archive condition need not be a citation for the chosen prose.
    # This also intentionally demonstrates that valid IDs cannot prove fidelity.
    assert result.angle_span_ids == (1,) and not set(result.qualification_span_ids) & set(result.angle_span_ids)
    assert result.reading_angle and result.semantic_completeness == "unverified"
    assert result.raw_response == raw and result.completion == completion
    assert json.loads(result.admission_json) == asdict(admission)
    admission.evidence["tampered_later"] = "Must not change the stored record"
    assert "tampered_later" not in result.admission_json
    assert original == (asdict(source), asdict(state), asdict(value))
    with pytest.raises(FrozenInstanceError):
        result.reading_angle = "Changed"  # type: ignore[misc]


def test_abstention_retains_qualifications_without_creating_a_brief(saved_synthetic: tuple[Source, BriefState]) -> None:
    source, state = saved_synthetic
    value = build_reconciliation_input(source, state)
    raw = synthetic_reconciliation_response(value, abstain=True)
    admission, completion = synthetic_reconciliation_transport(value, raw)
    result = parse_reconciliation_response(raw, value, source, state, admission=admission, completion=completion)
    assert result.abstain and result.qualification_span_ids == (len(source.spans),)
    assert result.reading_angle is None and not result.selected_span_ids and not result.angle_span_ids


def test_valid_citations_cannot_detect_a_synthetic_scope_error(saved_synthetic: tuple[Source, BriefState]) -> None:
    source, state = saved_synthetic
    value = build_reconciliation_input(source, state)
    data = json.loads(synthetic_reconciliation_response(value))
    # Deliberately contradict the fixture's retained pilot-client condition.
    data["reading_angle"] = {"text": "All clients qualify.", "span_ids": [1, len(source.spans)]}
    raw = json.dumps(data)
    admission, completion = synthetic_reconciliation_transport(value, raw)
    result = parse_reconciliation_response(raw, value, source, state, admission=admission, completion=completion)
    assert result.reading_angle == "All clients qualify."
    assert result.semantic_completeness == "unverified"


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
    admission, completion = synthetic_reconciliation_transport(value, raw)
    with pytest.raises(ValueError):
        parse_reconciliation_response(raw, value, source, state, admission=admission, completion=completion)


@pytest.mark.parametrize(
    ("field", "changed"),
    [
        ("version", "old-response-protocol"),
        ("input_sha256", "0" * 64),
        ("provider", "groq"),
        ("model", "unknown-model"),
        ("temperature", 0.2),
        ("temperature", False),
        ("temperature", float("nan")),
        ("output_reserve", 2049),
        ("output_reserve", True),
        ("request_sha256", "0" * 64),
        ("response_sha256", "0" * 64),
        ("status", "unknown"),
        ("status", "reserved"),
        ("status", "failed"),
        ("finish_reason", None),
        ("finish_reason", "length"),
        ("finish_reason", "MAX_TOKENS"),
    ],
)
def test_completion_binding_rejects_each_changed_transport_component(
    saved_synthetic: tuple[Source, BriefState],
    field: str,
    changed: Any,
) -> None:
    source, state = saved_synthetic
    value = build_reconciliation_input(source, state)
    raw = synthetic_reconciliation_response(value)
    admission, completion = synthetic_reconciliation_transport(value, raw)
    completion = replace(completion, **{field: changed})
    with pytest.raises(ValueError):
        parse_reconciliation_response(raw, value, source, state, admission=admission, completion=completion)


@pytest.mark.parametrize("damage,provider", [
    ("route", "gemini"), ("model", "gemini"), ("input_limit", "gemini"),
    ("output_reserve", "gemini"), ("request", "gemini"), ("status", "gemini"),
    ("method", "gemini"), ("count", "gemini"), ("error", "gemini"), ("record", "gemini"),
    ("method", "groq"), ("count", "groq"), ("record", "groq"),
])
def test_admitted_status_does_not_replace_route_and_count_proof(
    saved_synthetic: tuple[Source, BriefState],
    provider: str,
    damage: str,
) -> None:
    source, state = saved_synthetic
    value = build_reconciliation_input(source, state)
    raw = synthetic_reconciliation_response(value)
    admission, completion = synthetic_reconciliation_transport(value, raw, provider=provider)
    changes: dict[str, Any] = {
        "route": {"provider": "different"},
        "model": {"model": "different"},
        "input_limit": {"input_limit": 1},
        "output_reserve": {"output_reserve": 1},
        "request": {"request_sha256": "0" * 64},
        "status": {"status": "unverified"},
        "method": {"method": "unverified"},
        "count": {"exact_count" if provider == "gemini" else "input_estimate": True},
        "error": {"error_class": "technical_count_unknown"},
        "record": {"evidence": {"method": "unknown"}},
    }
    admission = replace(admission, **changes[damage])
    with pytest.raises(ValueError):
        parse_reconciliation_response(raw, value, source, state, admission=admission, completion=completion)


@pytest.mark.parametrize("provider", ["gemini", "groq"])
@pytest.mark.parametrize("damage", ["zero", "oversized", "other_method_count", "saved_method", "saved_tokenizer"])
def test_completed_response_requires_valid_saved_accounting(
    saved_synthetic: tuple[Source, BriefState],
    provider: str,
    damage: str,
) -> None:
    source, state = saved_synthetic
    value = build_reconciliation_input(source, state)
    raw = synthetic_reconciliation_response(value)
    admission, completion = synthetic_reconciliation_transport(value, raw, provider=provider)
    route = source_admission.route_profile(provider, completion.model, completion.output_reserve)
    assert route is not None
    if damage in {"zero", "oversized"}:
        count = 0 if damage == "zero" else route.input_tokens + 1
        if provider == "gemini":
            admission = replace(admission, exact_count=count)
        else:
            record = source_admission.estimate_record(route, count)
            admission = replace(admission, input_estimate=int(record["input_estimate"]), evidence=record)
    elif damage == "other_method_count":
        admission = replace(admission, **{"input_estimate" if provider == "gemini" else "exact_count": 100})
    elif damage == "saved_method":
        admission.evidence["method"] = "unrecognized-estimator"
    else:
        admission.evidence["tokenizer_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="invalid_reconciliation_admission"):
        parse_reconciliation_response(raw, value, source, state, admission=admission, completion=completion)


@pytest.mark.parametrize("damage", ["source", "page", "input_version", "raw", "instruction"])
def test_response_rechecks_original_source_pages_protocol_and_exact_raw_text(
    saved_synthetic: tuple[Source, BriefState],
    damage: str,
) -> None:
    source, state = saved_synthetic
    value = build_reconciliation_input(source, state)
    raw = synthetic_reconciliation_response(value)
    admission, completion = synthetic_reconciliation_transport(value, raw)
    if damage == "source":
        source = replace(source, text=source.text + " Changed")
    elif damage == "page":
        state.pages[0].response = "Changed"
    elif damage == "input_version":
        value = replace(value, version="old-input-protocol")
        value = replace(value, input_sha256=checksum(asdict(replace(value, input_sha256=""))))
        admission, completion = synthetic_reconciliation_transport(value, raw)
    elif damage == "raw":
        raw += " "  # Equivalent JSON is not the same returned raw response.
    elif damage == "instruction":
        plan = plan_reconciliation_request(
            value,
            source,
            state,
            instruction="Old protocol",
            provider="gemini",
            model="gemini-3.8-flash",
            max_output_tokens=2048,
        )
        admission = replace(admission, request_sha256=plan.request_sha256)
        completion = replace(completion, request_sha256=plan.request_sha256)
    with pytest.raises(ValueError):
        parse_reconciliation_response(raw, value, source, state, admission=admission, completion=completion)


def test_prospective_fit_cannot_substitute_for_completion_or_admission(
    saved_synthetic: tuple[Source, BriefState],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, state = saved_synthetic
    value = build_reconciliation_input(source, state)
    raw = synthetic_reconciliation_response(value)
    monkeypatch.setattr(source_admission, "count_gpt_input", lambda messages: 100)
    plan = plan_reconciliation_request(
        value,
        source,
        state,
        instruction=RESPONSE_INSTRUCTION,
        provider="groq",
        model="openai/gpt-oss-120b",
        max_output_tokens=2048,
    )
    assert plan.status == "estimated_fit"
    admission, completion = synthetic_reconciliation_transport(value, raw, provider="groq")
    assert plan.request_sha256 == completion.request_sha256
    with pytest.raises(ValueError, match="missing_reconciliation_completion_evidence"):
        parse_reconciliation_response(raw, value, source, state, admission=plan, completion=completion)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="missing_reconciliation_completion_evidence"):
        parse_reconciliation_response(raw, value, source, state, admission=admission, completion=plan)  # type: ignore[arg-type]
