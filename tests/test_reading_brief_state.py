"""Immutable source and selection binding, atomic progress, and safe state paths."""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from digest.article_source import FetchedArticle
from digest.reading_brief_state import (
    BriefState,
    Page,
    RequestAttempt,
    Route,
    Selection,
    checksum,
    has_unresolved_generation,
    load_source,
    load_state,
    make_spans,
    now,
    save_source,
    save_state,
    state_root,
)
from tests.factories import make_article


def state() -> BriefState:
    return BriefState(Selection.from_article(make_article()), Route("gemini", "gemini-3.8-flash", 1_048_576, 2048),
                      now(), now())


def test_offsets_reconstruct_every_character_without_a_source_window() -> None:
    text = "\n\nOpening.\n\n" + "Word " * 900 + "\n\nFINAL material exception.\n"
    spans = make_spans(text)
    assert "".join(text[span.start:span.end] for span in spans) == text
    assert spans[0].start == 0 and spans[-1].end == len(text)
    assert [span.id for span in spans] == list(range(1, len(spans) + 1))
    assert all(left.end == right.start for left, right in zip(spans, spans[1:], strict=False))


def test_source_snapshot_and_state_roundtrip_are_bound_to_selection(tmp_path: Path) -> None:
    item = state()
    fetched = FetchedArticle("Full public body.\n\nLate exception.", "https://example.com/final", now(), None,
                             "article", ("Text only; uninspected images.",))
    item.source_sha256, original = save_source(tmp_path, item.selection, fetched)
    item.pages = [Page(0, len(original.spans))]
    save_state(tmp_path, item)
    restored = load_state(tmp_path, item.selection.identity)
    assert restored == item and load_source(tmp_path, restored) == original
    assert original.coverage_notes == ["Text only; uninspected images."]
    assert not list(state_root(tmp_path).glob("*.tmp"))
    restored.selection = Selection.from_article(make_article(source="Different feed"))
    with pytest.raises(ValueError, match="selection_mismatch"):
        load_source(tmp_path, restored)


def test_checksum_damage_or_selection_rebinding_is_rejected(tmp_path: Path) -> None:
    item = state()
    save_state(tmp_path, item)
    path = state_root(tmp_path) / f"{item.selection.identity}.json"
    envelope = json.loads(path.read_text())
    envelope["payload"]["attempts"] = 99
    path.write_text(json.dumps(envelope))
    with pytest.raises(ValueError, match="checksum"):
        load_state(tmp_path, item.selection.identity)
    envelope["payload"]["selection"]["title"] = "A different article"
    envelope["sha256"] = checksum(envelope["payload"])
    path.write_text(json.dumps(envelope))
    with pytest.raises(ValueError, match="selection_mismatch"):
        load_state(tmp_path, item.selection.identity)


def test_symlinked_state_or_source_cannot_escape_cache(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (tmp_path / "reading_briefs").symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(ValueError, match="unsafe_state_path"):
        save_state(tmp_path, state())
    assert not list(elsewhere.iterdir())


def test_source_span_tamper_is_rejected_even_if_snapshot_name_is_rehashed(tmp_path: Path) -> None:
    item = state()
    item.source_sha256, _ = save_source(tmp_path, item.selection,
                                       FetchedArticle("Exact original body.", "https://example.com/final", now(),
                                                      None, "article"))
    root = state_root(tmp_path) / "sources"
    payload = json.loads((root / f"{item.source_sha256}.json").read_text())
    payload["spans"][0]["end"] -= 1
    item.source_sha256 = checksum(payload)
    (root / f"{item.source_sha256}.json").write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="span_manifest_mismatch"):
        load_source(tmp_path, item)


def test_request_intents_roundtrip_and_hold_unfinished_generation(tmp_path: Path) -> None:
    item = state()
    item.source_sha256, source = save_source(tmp_path, item.selection,
                                           FetchedArticle("Complete source.", "https://example.com", now(), None,
                                                          "article"))
    count = RequestAttempt("count", item.route, 0, len(source.spans), item.source_sha256, "0" * 64, now(),
                           status="unknown", finished_at=now(), error_class="technical_deadline")
    page = Page(0, len(source.spans), request_attempts=[count])
    item.pages = [page]
    assert not has_unresolved_generation(item)
    generate = RequestAttempt("generate", item.route, 0, len(source.spans), item.source_sha256, "0" * 64, now())
    page.request_attempts.append(generate)
    save_state(tmp_path, item)
    restored = load_state(tmp_path, item.selection.identity)
    assert restored == item and has_unresolved_generation(restored)
    page.request_attempts[-1] = replace(generate, status="definite_failed", finished_at=now())
    assert not has_unresolved_generation(item)


@pytest.mark.parametrize("damage", ["source", "range", "status", "completion", "usage", "hash", "route"])
def test_invalid_request_intent_metadata_is_rejected(tmp_path: Path, damage: str) -> None:
    item = state()
    item.source_sha256, source = save_source(tmp_path, item.selection,
                                           FetchedArticle("Complete source.", "https://example.com", now(), None,
                                                          "article"))
    item.pages = [Page(0, len(source.spans), request_attempts=[
        RequestAttempt("generate", item.route, 0, len(source.spans), item.source_sha256, "0" * 64, now()),
    ])]
    save_state(tmp_path, item)
    path = state_root(tmp_path) / f"{item.selection.identity}.json"
    envelope = json.loads(path.read_text())
    attempt = envelope["payload"]["pages"][0]["request_attempts"][0]
    if damage == "source":
        attempt["source_sha256"] = "1" * 64
    elif damage == "range":
        attempt["start"] = attempt["stop"]
    elif damage == "status":
        attempt["status"] = "retryable"
    elif damage == "completion":
        attempt["finished_at"] = now()
    elif damage == "usage":
        attempt["usage"] = {"thoughts": "private text"}
    elif damage == "hash":
        attempt["request_sha256"] = "invalid"
    else:
        attempt["route"]["max_output_tokens"] = 0
    envelope["sha256"] = checksum(envelope["payload"])
    path.write_text(json.dumps(envelope))
    with pytest.raises(ValueError, match="invalid_request"):
        load_state(tmp_path, item.selection.identity)


def _saved_completed(tmp_path: Path) -> BriefState:
    """Build one accepted owner, without assigning redundant page completion facts."""
    from digest.reading_brief import _messages, _parse_result, _prompt_sha
    from digest.reading_brief_state import _complete_page

    item = state()
    item.source_sha256, source = save_source(
        tmp_path, item.selection,
        FetchedArticle(
            "First finding.\n\nSecond finding.\n\nThird condition.", "https://example.com", now(), None, "article",
        ),
    )
    page = Page(0, len(source.spans))
    request = _prompt_sha(item, _messages(item, source, page), item.route)
    # These valid IDs deliberately are not sorted: freezing must preserve model order.
    raw = json.dumps({
        "coverage": {"first_span_id": 1, "last_span_id": 3},
        "selected_span_ids": [2, 1], "qualification_span_ids": [3, 1],
        "reading_angle": {"text": "Both findings retain their conditions.", "span_ids": [3, 2, 1]},
        "abstain": False,
    })
    result = _parse_result(raw, {"finish_reason": "STOP"}, page, source)
    page.request_attempts = [RequestAttempt(
        "generate", item.route, 0, len(source.spans), item.source_sha256, request, now(),
        status="accepted", finished_at=now(), response_sha256=checksum(raw), finish_reason="STOP",
        usage=(("prompt_tokens", 10), ("completion_tokens", 5), ("total_tokens", 15)),
    )]
    _complete_page(page, 0, raw, result, "null")
    item.pages = [page]
    item.exact_counts[request] = 10
    item.status = "ready"
    save_state(tmp_path, item)
    return item


def test_completed_owner_is_deeply_immutable_and_page_has_no_completion_aliases(tmp_path: Path) -> None:
    from dataclasses import FrozenInstanceError, fields

    from digest.reading_brief_state import PageCompletion, completed, page_wire

    item = _saved_completed(tmp_path)
    page = item.pages[0]
    owner = page.request_attempts[0]
    value = completed(page)
    assert value is not None and owner.completion is not None and page.work is None
    assert value.owner is owner and value.result is owner.completion.result
    assert value.result.selected_span_ids == (2, 1)
    assert value.result.qualification_span_ids == (3, 1)
    assert value.result.angle_span_ids == (3, 2, 1)
    assert isinstance(value.usage, tuple) and all(isinstance(pair, tuple) for pair in value.usage)
    assert {field.name for field in fields(PageCompletion)} == {"response", "result", "page_route_spelling"}
    retired = {"prompt_sha256", "result", "response", "response_sha256", "finish_reason", "usage", "route"}
    assert not retired & {field.name for field in fields(Page)}
    assert all(not hasattr(page, name) for name in retired)
    with pytest.raises(FrozenInstanceError):
        owner.finish_reason = "MAX_TOKENS"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        owner.completion.response = "Changed"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        value.result.selected_span_ids = ()  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        value.owner = replace(owner, finish_reason="MAX_TOKENS")  # type: ignore[misc]
    projected = page_wire(item, page)
    projected["result"]["selected_span_ids"].clear()
    projected["usage"]["prompt_tokens"] = 999
    projected["request_attempts"][0]["usage"]["prompt_tokens"] = 888
    assert value.result.selected_span_ids == (2, 1) and dict(value.usage)["prompt_tokens"] == 10
    assert completed(page) == value


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("explicit_primary", [False, True])
def test_decoder_preserves_exact_route_spelling_and_unsorted_arrays_without_writes(
    tmp_path: Path, legacy: bool, explicit_primary: bool,
) -> None:
    from digest.reading_brief import ready_brief_evidence
    from digest.reading_brief_state import LegacyCompletedEvidence, completed, page_wire, state_wire

    item = _saved_completed(tmp_path)
    path = state_root(tmp_path) / f"{item.selection.identity}.json"
    envelope = json.loads(path.read_text())
    page = envelope["payload"]["pages"][0]
    if explicit_primary:
        page["route"] = envelope["payload"]["route"]
    if legacy:
        page["request_attempts"] = []
        page["request_history_version"] = 0
    envelope["sha256"] = checksum(envelope["payload"])
    path.write_text(json.dumps(envelope))
    original = path.read_bytes()
    restored, _ = ready_brief_evidence(tmp_path, item.selection.identity)
    value = completed(restored.pages[0])
    assert value is not None and value.route == item.route
    assert value.page_route_spelling == ("explicit" if explicit_primary else "null")
    assert isinstance(restored.pages[0].work, LegacyCompletedEvidence) is legacy
    assert page_wire(restored, restored.pages[0]) == page
    assert state_wire(restored) == envelope["payload"]
    assert checksum(state_wire(restored)) == envelope["sha256"]
    assert path.read_bytes() == original


@pytest.mark.parametrize("field,value", [
    ("response", "Unowned body"), ("response_sha256", "0" * 64), ("finish_reason", "STOP"),
    ("usage", {"prompt_tokens": 1}),
])
def test_decoder_rejects_orphan_pending_completion_fields(
    tmp_path: Path, field: str, value: object,
) -> None:
    from digest.reading_brief_state import PendingRequest

    item = state()
    item.source_sha256, source = save_source(
        tmp_path, item.selection, FetchedArticle("Complete source.", "https://example.com", now(), None, "article"),
    )
    item.pages = [Page(0, len(source.spans), work=PendingRequest(item.route, "0" * 64))]
    save_state(tmp_path, item)
    path = state_root(tmp_path) / f"{item.selection.identity}.json"
    envelope = json.loads(path.read_text())
    envelope["payload"]["pages"][0][field] = value
    envelope["sha256"] = checksum(envelope["payload"])
    path.write_text(json.dumps(envelope))
    before = path.read_bytes()
    with pytest.raises(ValueError, match="invalid_pending_completion"):
        load_state(tmp_path, item.selection.identity)
    assert path.read_bytes() == before


@pytest.mark.parametrize("damage,expected", [
    ("unknown_citation", "invalid_source_span_ids"),
    ("changed_finding", "result_response_binding_mismatch"),
    ("missing_admission", "uncounted_completed_page"),
])
def test_decoded_owner_still_requires_source_validation(
    tmp_path: Path, damage: str, expected: str,
) -> None:
    from digest.reading_brief import ready_brief_evidence
    from digest.reading_brief_state import completed

    item = _saved_completed(tmp_path)
    path = state_root(tmp_path) / f"{item.selection.identity}.json"
    envelope = json.loads(path.read_text())
    if damage == "unknown_citation":
        envelope["payload"]["pages"][0]["result"]["selected_span_ids"] = [999]
    elif damage == "changed_finding":
        envelope["payload"]["pages"][0]["result"]["reading_angle"] = "Changed finding."
    else:
        envelope["payload"]["exact_counts"] = {}
    envelope["sha256"] = checksum(envelope["payload"])
    path.write_text(json.dumps(envelope))
    before = path.read_bytes()
    restored = load_state(tmp_path, item.selection.identity)
    assert completed(restored.pages[0]) is not None
    with pytest.raises(ValueError, match=expected):
        ready_brief_evidence(tmp_path, item.selection.identity)
    assert path.read_bytes() == before


def test_typed_consumers_reject_ambiguous_modern_and_illicit_legacy_owners(tmp_path: Path) -> None:
    from digest.reading_brief_state import decode_page, page_wire
    from digest.reading_reconciliation import build_reconciliation_input

    item = _saved_completed(tmp_path)
    source = load_source(tmp_path, item)
    page = item.pages[0]
    owner = page.request_attempts[0]
    wire = page_wire(item, page)
    page.request_attempts.append(replace(owner, status="unknown", completion=None))
    with pytest.raises(ValueError, match="result_attempt_binding_mismatch"):
        build_reconciliation_input(source, item)

    wire.update(request_history_version=0, request_attempts=[])
    legacy = decode_page(wire, item)
    item.pages = [legacy]
    assert build_reconciliation_input(source, item).pages[0].history_evidence == "legacy_completed_response"
    legacy.request_history_version = 1
    with pytest.raises(ValueError, match="result_attempt_binding_mismatch"):
        build_reconciliation_input(source, item)
    legacy.request_history_version = 0
    legacy.request_attempts.append(owner)
    with pytest.raises(ValueError, match="result_attempt_binding_mismatch"):
        build_reconciliation_input(source, item)


@pytest.mark.parametrize("field,value,error", [
    ("selected_span_ids", [[1]], "invalid_source_span_ids"),
    ("qualification_span_ids", [{"id": 1}], "invalid_source_span_ids"),
    ("reading_angle", ["Mutable prose container"], "invalid_reading_angle"),
    ("abstain", {"value": False}, "invalid_abstention"),
])
def test_decoder_never_freezes_mutable_nested_result_payloads(
    tmp_path: Path, field: str, value: object, error: str,
) -> None:
    item = _saved_completed(tmp_path)
    path = state_root(tmp_path) / f"{item.selection.identity}.json"
    envelope = json.loads(path.read_text())
    envelope["payload"]["pages"][0]["result"][field] = value
    envelope["sha256"] = checksum(envelope["payload"])
    path.write_text(json.dumps(envelope))
    before = path.read_bytes()
    with pytest.raises(ValueError, match=error):
        load_state(tmp_path, item.selection.identity)
    assert path.read_bytes() == before


@pytest.mark.parametrize("target,error", [
    ("page", "invalid_page"), ("attempt", "invalid_request_attempt"), ("result", "invalid_result_schema"),
])
def test_decoder_rejects_nonobject_records_with_controlled_state_error(
    tmp_path: Path, target: str, error: str,
) -> None:
    item = _saved_completed(tmp_path)
    path = state_root(tmp_path) / f"{item.selection.identity}.json"
    envelope = json.loads(path.read_text())
    if target == "page":
        envelope["payload"]["pages"][0] = []
    elif target == "attempt":
        envelope["payload"]["pages"][0]["request_attempts"][0] = []
    else:
        envelope["payload"]["pages"][0]["result"] = []
    envelope["sha256"] = checksum(envelope["payload"])
    path.write_text(json.dumps(envelope))
    before = path.read_bytes()
    with pytest.raises(ValueError, match=error):
        load_state(tmp_path, item.selection.identity)
    assert path.read_bytes() == before


@pytest.mark.parametrize("damage,error", [
    ("legacy_finish", "result_response_binding_mismatch"),
    ("legacy_route", "invalid_page_route"),
    ("route_container", "invalid_page_route"),
    ("prompt", "invalid_page_prompt_digest"),
    ("attempt_hash", "invalid_request_metadata"),
    ("source", "invalid_request_binding"),
])
def test_decoder_rejects_mutable_values_in_scalar_owner_fields(
    tmp_path: Path, damage: str, error: str,
) -> None:
    item = _saved_completed(tmp_path)
    path = state_root(tmp_path) / f"{item.selection.identity}.json"
    envelope = json.loads(path.read_text())
    raw = envelope["payload"]["pages"][0]
    if damage.startswith("legacy_"):
        raw.update(request_history_version=0, request_attempts=[])
        if damage == "legacy_finish":
            raw["finish_reason"] = {}
        else:
            raw["route"] = dict(envelope["payload"]["route"], model=[])
    elif damage == "route_container":
        raw["route"] = []
    elif damage == "prompt":
        raw["prompt_sha256"] = []
    elif damage == "attempt_hash":
        raw["request_attempts"][0]["response_sha256"] = []
    else:
        envelope["payload"]["source_sha256"] = []
        raw["request_attempts"][0]["source_sha256"] = []
    envelope["sha256"] = checksum(envelope["payload"])
    path.write_text(json.dumps(envelope))
    before = path.read_bytes()
    with pytest.raises(ValueError, match=error):
        load_state(tmp_path, item.selection.identity)
    assert path.read_bytes() == before
