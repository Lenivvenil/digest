"""Offline synthetic source-bound contracts; no semantic or provider-success claim."""

from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from digest._serialization import canonical_json_bytes
from digest.adapters.models.synthesis import groq_synthesis_response_format
from digest.adapters.storage import synthesis as storage
from digest.domain.editorial.reviews import EvidenceItem
from digest.domain.editorial.synthesis import (
    MAX_MESSAGES_BYTES,
    MAX_MESSAGES_CHARS,
    MAX_REQUEST_BYTES,
    MAX_REQUEST_CHARS,
    MAX_RESPONSE_BYTES,
    EvidenceBinding,
    SynthesisResult,
    build_messages,
    validate_request_bounds,
    validate_response,
)
from digest.llm import openai_request_body
from digest.translation import _invariants, protected_content_equal

# Hand-reviewed source-specific paired witness for the proposed v2 contract.
# Synthetic excerpts are illustrative contract data, not news or a live model result.
WITNESS = {'schema_version': 2,
 'publication_language': 'ru',
 'status': 'comparison',
 'abstention_reason': None,
 'comparison': {'supports': [{'evidence_id': '9c1a8d6ac3e42fe0b1396d1de1771e55',
                              'occurrence_sha256': '08bf71ea80356ccbd755eb0e521db151318ed52ebb91966a94318df03e5b1401',
                              'canonical_observation': 'Accepts payment requests during settlement outages; '
                                                       'settlement waits until recovery.',
                              'presentation_observation': 'Принимает платёжные запросы при сбое расчётов; '
                                                          'расчёты ждут восстановления.'},
                             {'evidence_id': 'd5dea46d92257b8fac32420abfd95a7c',
                              'occurrence_sha256': '5260df614bd3e2540de4a150fea749e83ae94da9aacf798db7d8dde2ff32a1bf',
                              'canonical_observation': 'Accepts invoice requests during ledger outages and '
                                                       'replays them only after recovery.',
                              'presentation_observation': 'Принимает запросы счетов при сбое реестра и '
                                                          'повторно обрабатывает их только после '
                                                          'восстановления.'}],
                'canonical': {'interpretation': 'The useful comparison is acceptance versus completion: '
                                                'examine how each retained request reaches its dependent '
                                                'operation after recovery.',
                              'qualification': 'The excerpts do not establish production-scale recovery '
                                               'performance or end-to-end completion.'},
                'presentation': {'interpretation': 'Полезно сравнить приём запроса и его завершение: как '
                                                   'сохранённый запрос доходит до зависимой операции после '
                                                   'восстановления.',
                                 'qualification': 'Отрывки не подтверждают скорость восстановления в '
                                                  'промышленном масштабе или сквозное завершение.'},
                'premise_ids': ['9c1a8d6ac3e42fe0b1396d1de1771e55', 'd5dea46d92257b8fac32420abfd95a7c']}}

EVIDENCE = [{'evidence': {'evidence_id': '9c1a8d6ac3e42fe0b1396d1de1771e55',
               'title': 'Synthetic Queue A',
               'url': 'https://example.com/a',
               'source': 'Synthetic source A',
               'category': 'Synthetic systems',
               'published': None,
               'excerpt': 'Queue A accepts payment requests during settlement outages and completes '
                          'settlement only after recovery.',
               'excerpt_shortened_or_sanitized': False},
  'occurrence': {'title': 'Synthetic Queue A',
                 'link': 'https://example.com/a',
                 'description': 'Queue A accepts payment requests during settlement outages and completes '
                                'settlement only after recovery.',
                 'source': 'Synthetic source A',
                 'category': 'Synthetic systems',
                 'published': None,
                 'source_url': 'https://example.com/rss'},
  'occurrence_sha256': '08bf71ea80356ccbd755eb0e521db151318ed52ebb91966a94318df03e5b1401'},
 {'evidence': {'evidence_id': 'd5dea46d92257b8fac32420abfd95a7c',
               'title': 'Synthetic Gateway B',
               'url': 'https://example.org/b',
               'source': 'Synthetic source B',
               'category': 'Synthetic systems',
               'published': None,
               'excerpt': 'Gateway B accepts invoice requests during ledger outages and replays them only '
                          'after recovery.',
               'excerpt_shortened_or_sanitized': False},
  'occurrence': {'title': 'Synthetic Gateway B',
                 'link': 'https://example.org/b',
                 'description': 'Gateway B accepts invoice requests during ledger outages and replays them '
                                'only after recovery.',
                 'source': 'Synthetic source B',
                 'category': 'Synthetic systems',
                 'published': None,
                 'source_url': 'https://example.org/rss'},
  'occurrence_sha256': '5260df614bd3e2540de4a150fea749e83ae94da9aacf798db7d8dde2ff32a1bf'}]


@pytest.fixture
def bindings() -> tuple[EvidenceBinding, ...]:
    return tuple(EvidenceBinding(
        EvidenceItem(**item["evidence"]), item["occurrence_sha256"],
        item["occurrence"]["source"], item["occurrence"]["source_url"],
    ) for item in EVIDENCE)


@pytest.fixture
def response() -> dict[str, Any]:
    return deepcopy(WITNESS)


def messages(bindings: tuple[EvidenceBinding, ...], language: str = "ru") -> list[dict[str, str]]:
    return build_messages(
        accepted_sha256="1" * 64, publication_date="2026-10-13", report_sha256="2" * 64,
        bundle_id="3" * 64, publication_language=language, bindings=bindings,
    )


def request(bindings: tuple[EvidenceBinding, ...], language: str = "ru") -> dict[str, Any]:
    return openai_request_body(
        "openai/gpt-oss-120b", messages(bindings, language), 0, 4096, groq=True,
        reasoning_effort="low", response_format=groq_synthesis_response_format(),
    )


def parse(response: dict[str, Any], bindings: tuple[EvidenceBinding, ...], language: str = "ru") -> SynthesisResult:
    return validate_response(
        json.dumps(response, ensure_ascii=False), "stop", language, bindings, protected_content_equal,
    )


def reservation(bindings: tuple[EvidenceBinding, ...]) -> dict[str, Any]:
    return storage.new_record(
        accepted_sha256="1" * 64, publication_date="2026-10-13", report_sha256="2" * 64,
        bundle_id="3" * 64, bindings=bindings, publication_language="ru", request_body=request(bindings),
        status="reserved", started_at="2026-10-13T00:00:00+00:00",
    )


def terminal(record: dict[str, Any], response: dict[str, Any], bindings: tuple[EvidenceBinding, ...]) -> dict[str, Any]:
    return storage.terminal_record(
        record, status="valid", completed_at="2026-10-13T00:00:01+00:00",
        response_text=json.dumps(response, ensure_ascii=False), reported_model="openai/gpt-oss-120b",
        finish_reason="stop", result=parse(response, bindings), reason="completed",
    )


def reseal(record: dict[str, Any]) -> dict[str, Any]:
    record["sha256"] = hashlib.sha256(canonical_json_bytes({
        key: value for key, value in record.items() if key != "sha256"
    })).hexdigest()
    return record


@pytest.mark.parametrize("language", ["en", "ru"])
def test_hand_reviewed_pair_fits_exact_request_response_record_bounds(
    bindings: tuple[EvidenceBinding, ...], response: dict[str, Any], language: str,
) -> None:
    response["publication_language"] = language
    if language == "en":
        response["comparison"]["presentation"] = None
        for support in response["comparison"]["supports"]:
            support["presentation_observation"] = None
    result = parse(response, bindings, language)
    assert result.comparison is not None
    assert result.comparison.canonical.interpretation == WITNESS["comparison"]["canonical"]["interpretation"]
    normalized = json.loads(canonical_json_bytes(asdict(result)))
    for support, binding in zip(normalized["comparison"]["supports"], bindings, strict=True):
        assert support.pop("quote") == binding.evidence.excerpt
        assert support.pop("field") == "excerpt"
    assert normalized == response
    body = request(bindings, language)
    encoded_messages = canonical_json_bytes(body["messages"])
    encoded_body = canonical_json_bytes(body)
    assert len(encoded_messages) <= MAX_MESSAGES_BYTES
    assert len(encoded_messages.decode()) <= MAX_MESSAGES_CHARS
    assert len(encoded_body) <= MAX_REQUEST_BYTES and len(encoded_body.decode()) <= MAX_REQUEST_CHARS
    assert validate_request_bounds(body["messages"], body) == hashlib.sha256(encoded_body).hexdigest()
    assert len(canonical_json_bytes(response)) < MAX_RESPONSE_BYTES
    record = storage.new_record(
        accepted_sha256="1" * 64, publication_date="2026-10-13", report_sha256="2" * 64,
        bundle_id="3" * 64, bindings=bindings, publication_language=language, request_body=body,
        status="reserved", started_at="2026-10-13T00:00:00+00:00",
    )
    complete = storage.terminal_record(
        record, status="valid", completed_at="2026-10-13T00:00:01+00:00", result=result,
        response_text=canonical_json_bytes(response).decode(), finish_reason="stop",
    )
    assert len(canonical_json_bytes(complete)) < storage.MAX_RECORD_BYTES
    payload = json.loads(body["messages"][1]["content"])
    assert payload["ordered_selected_ids"] == [binding.evidence.evidence_id for binding in bindings]
    assert payload["selected_items"] == [asdict(binding) for binding in bindings]
    assert payload["schema_version"] == 2 and payload["canonical_language"] == "en"


def test_raw_retained_identity_and_accepted_order_are_immutable_input(bindings: tuple[EvidenceBinding, ...]) -> None:
    different = (replace(bindings[1], source="RAW source\u00a0B"), bindings[0])
    payload = json.loads(messages(different)[1]["content"])
    assert payload["selected_items"][0]["source"] == "RAW source\u00a0B"
    assert payload["selected_items"][0]["evidence"]["source"] == bindings[1].evidence.source
    assert payload["ordered_selected_ids"][0] == bindings[1].evidence.evidence_id
    assert messages(different) != messages(bindings)


@pytest.mark.parametrize("finish_reason", [None, "", "length", "MAX_TOKENS", "unknown", "Stop"])
def test_incomplete_and_unknown_completion_rejected(
    bindings: tuple[EvidenceBinding, ...], response: dict[str, Any], finish_reason: str | None,
) -> None:
    with pytest.raises(ValueError):
        validate_response(json.dumps(response), finish_reason, "ru", bindings, protected_content_equal)


def test_uppercase_finish_and_abstention(bindings: tuple[EvidenceBinding, ...]) -> None:
    response = {"schema_version": 2, "publication_language": "ru", "status": "abstained",
                "abstention_reason": "no_supported_relationship", "comparison": None}
    result = validate_response(json.dumps(response), "STOP", "ru", bindings, protected_content_equal)
    assert result.status == "abstained" and result.comparison is None


@pytest.mark.parametrize("mutation", [
    "unknown_id", "duplicate_id", "occurrence", "quote", "title", "field", "one_support", "three_supports",
    "extra_root", "extra_comparison", "extra_support", "extra_prose", "version_bool", "version_old", "language",
    "status", "comparison_null", "reason_not_null", "missing_qualification", "blank", "control", "newline",
    "relationship_long", "qualification_long", "quote_empty", "quote_long", "presentation_null", "prose_type",
    "changed_number", "changed_url", "changed_literal_quote", "changed_qualification_number", "relationship_type",
])
def test_response_hard_contract_failures(
    bindings: tuple[EvidenceBinding, ...], response: dict[str, Any], mutation: str,
) -> None:
    comparison = response["comparison"]
    supports = comparison["supports"]
    prose = comparison["canonical"]
    if mutation == "unknown_id":
        supports[0]["evidence_id"] = "0" * 32
    elif mutation == "duplicate_id":
        supports[1] = deepcopy(supports[0])
    elif mutation == "occurrence":
        supports[0]["occurrence_sha256"] = "0" * 64
    elif mutation in {"quote", "title"}:
        supports[0]["quote"] = "absent" if mutation == "quote" else bindings[0].evidence.title
    elif mutation == "field":
        supports[0]["field"] = "title"
    elif mutation == "one_support":
        supports.pop()
    elif mutation == "three_supports":
        supports.append(deepcopy(supports[0]))
    elif mutation.startswith("extra_"):
        {"extra_root": response, "extra_comparison": comparison,
         "extra_support": supports[0], "extra_prose": prose}[mutation]["extra"] = "bad"
    elif mutation.startswith("version_"):
        response["schema_version"] = True if mutation == "version_bool" else 1
    elif mutation == "language":
        response["publication_language"] = "en"
    elif mutation == "status":
        response["status"] = "unknown"
    elif mutation == "comparison_null":
        response["comparison"] = None
    elif mutation == "reason_not_null":
        response["abstention_reason"] = "insufficient_evidence"
    elif mutation == "missing_qualification":
        del prose["qualification"]
    elif mutation in {"blank", "control", "newline", "relationship_long", "prose_type"}:
        prose["interpretation"] = {"blank": " ", "control": "bad\x01", "newline": "a\nb",
                                 "relationship_long": "x" * 241, "prose_type": 4}[mutation]
    elif mutation == "qualification_long":
        prose["qualification"] = "x" * 161
    elif mutation in {"quote_empty", "quote_long"}:
        supports[0]["quote"] = "" if mutation == "quote_empty" else "x" * 161
    elif mutation == "presentation_null":
        comparison["presentation"] = None
    elif mutation.startswith("changed_"):
        field = "qualification" if mutation == "changed_qualification_number" else "interpretation"
        suffix = {"changed_number": " 200", "changed_qualification_number": " 200",
                  "changed_url": " https://example.com/forged", "changed_literal_quote": ' "new"'}[mutation]
        comparison["presentation"][field] += suffix
    elif mutation == "relationship_type":
        comparison["relationship_type"] = "temporal_trend"
    with pytest.raises(ValueError):
        parse(response, bindings)


@pytest.mark.parametrize("text", [
    "{} trailing", "```json\n{}\n```", "[]", '{"schema_version":1,"schema_version":1}',
    "{" , "NaN", '{"value":Infinity}', "x" * (MAX_RESPONSE_BYTES + 1),
])
def test_only_bounded_strict_json_is_accepted(bindings: tuple[EvidenceBinding, ...], text: str) -> None:
    with pytest.raises(ValueError):
        validate_response(text, "stop", "ru", bindings, protected_content_equal)


@pytest.mark.parametrize("reason,comparison", [(None, None), ("unknown", None), ("insufficient_evidence", {})])
def test_abstention_cross_fields(bindings: tuple[EvidenceBinding, ...], reason: object, comparison: object) -> None:
    response = {"schema_version": 2, "publication_language": "ru", "status": "abstained",
                "abstention_reason": reason, "comparison": comparison}
    with pytest.raises(ValueError):
        parse(response, bindings)


def test_same_source_supports_and_english_duplicate_presentation_are_rejected(
    bindings: tuple[EvidenceBinding, ...], response: dict[str, Any],
) -> None:
    same = (bindings[0], replace(bindings[1], source=bindings[0].source, source_url=bindings[0].source_url))
    # A third configured source admits input but cannot validate a same-source chosen pair.
    third = replace(bindings[1], evidence=replace(bindings[1].evidence, evidence_id="4" * 32))
    with pytest.raises(ValueError, match="distinct source"):
        parse(response, (*same, third))
    response["publication_language"] = "en"
    with pytest.raises(ValueError, match="canonical prose"):
        parse(response, bindings, "en")


@pytest.mark.parametrize("changed", [
    "count", "six", "duplicate", "same_source", "hash", "evidence_id", "source", "source_url", "url", "boolean",
])
def test_input_contract(bindings: tuple[EvidenceBinding, ...], changed: str) -> None:
    candidate = bindings
    if changed == "count":
        candidate = (bindings[0],)
    elif changed == "six":
        candidate = bindings * 3
    elif changed == "duplicate":
        candidate = (bindings[0], bindings[0])
    elif changed == "same_source":
        candidate = (bindings[0], replace(bindings[1], source=bindings[0].source, source_url=bindings[0].source_url))
    elif changed == "hash":
        candidate = (replace(bindings[0], occurrence_sha256="../bad"), bindings[1])
    elif changed == "evidence_id":
        candidate = (replace(bindings[0], evidence=replace(bindings[0].evidence, evidence_id="A" * 32)), bindings[1])
    elif changed == "source":
        candidate = (replace(bindings[0], source=""), bindings[1])
    elif changed == "source_url":
        candidate = (replace(bindings[0], source_url="https://user:pass@example.com/rss"), bindings[1])
    elif changed == "url":
        candidate = (replace(bindings[0], evidence=replace(bindings[0].evidence, url="file:///x")), bindings[1])
    elif changed == "boolean":
        candidate = (replace(bindings[0], evidence=replace(bindings[0].evidence, excerpt_shortened_or_sanitized=1)),
                     bindings[1])
    with pytest.raises(ValueError):
        messages(candidate)


@pytest.mark.parametrize("field,value", [
    ("accepted_sha256", "A" * 64), ("report_sha256", "0"), ("bundle_id", "../path"),
    ("publication_date", "20261013"), ("publication_date", "2026-02-30"), ("publication_language", "fr"),
])
def test_request_identity_validation(bindings: tuple[EvidenceBinding, ...], field: str, value: str) -> None:
    arguments: dict[str, Any] = {"accepted_sha256": "1" * 64, "publication_date": "2026-10-13",
                                 "report_sha256": "2" * 64, "bundle_id": "3" * 64,
                                 "publication_language": "ru", "bindings": bindings}
    arguments[field] = value
    with pytest.raises(ValueError):
        build_messages(**arguments)


def test_whole_request_including_schema_is_bounded_without_dropping_evidence(
    bindings: tuple[EvidenceBinding, ...],
) -> None:
    large = (replace(bindings[0], evidence=replace(bindings[0].evidence, excerpt="x" * 12_000)), bindings[1])
    with pytest.raises(ValueError, match="serialized bound"):
        messages(large)
    body = request(bindings)
    body["messages"] = []
    with pytest.raises(ValueError, match="differ"):
        validate_request_bounds(messages(bindings), body)
    body = request(bindings)
    body["response_format"]["padding"] = "x" * 16_000
    with pytest.raises(ValueError, match="serialized bound"):
        validate_request_bounds(body["messages"], body)
    body = request(bindings)
    body["response_format"]["padding"] = "Ж" * 3000
    with pytest.raises(ValueError, match="token bound"):
        validate_request_bounds(body["messages"], body)


def test_pure_protected_helper_keeps_exact_translation_multisets() -> None:
    original = '20ms "preview" https://example.com/a 20ms'
    for translated in (original, '20 мс 20 мс "preview" https://example.com/a',
                       '20 мс "preview" https://example.com/a', '20 мс 20 мс "GA" https://example.com/a'):
        assert protected_content_equal(original, translated) == (_invariants(original) == _invariants(translated))


def test_provider_schema_uses_only_existing_supported_subset() -> None:
    response_format = groq_synthesis_response_format()
    assert response_format["json_schema"]["name"] == "selected_source_comparison_v2"
    assert response_format["json_schema"]["strict"] is True

    def inspect(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                inspect(item)
        elif isinstance(value, dict):
            if value.get("type") == "object":
                assert value["additionalProperties"] is False
                assert value["required"] == list(value["properties"])
                for item in value["properties"].values():
                    inspect(item)
            else:
                assert set(value) <= {"type", "enum", "items", "anyOf"}
                for key in ("items", "anyOf"):
                    if key in value:
                        inspect(value[key])
    inspect(response_format["json_schema"]["schema"])


def test_retained_reservation_terminal_and_skip_lifecycle(
    tmp_path: Path, bindings: tuple[EvidenceBinding, ...], response: dict[str, Any],
) -> None:
    assert storage.load_attempt("1" * 64, tmp_path) is None
    record = reservation(bindings)
    path = storage.reserve_attempt(record, tmp_path)
    assert storage.load_attempt("1" * 64, tmp_path) == record
    with pytest.raises(FileExistsError):
        storage.reserve_attempt(record, tmp_path)
    complete = terminal(record, response, bindings)
    assert storage.finish_attempt(record, complete, tmp_path) == path
    assert storage.load_attempt("1" * 64, tmp_path) == complete
    retained = path.read_bytes()
    with pytest.raises(ValueError, match="already terminal"):
        storage.finish_attempt(record, complete, tmp_path)
    assert path.read_bytes() == retained
    skip = storage.new_record(
        accepted_sha256="4" * 64, publication_date="2026-10-13", report_sha256=None, bundle_id=None,
        bindings=(), publication_language="en", request_body=None, status="skipped",
        started_at="2026-10-13T00:00:00+00:00", reason="binding_unavailable",
    )
    storage.reserve_attempt(skip, tmp_path)
    assert storage.load_attempt("4" * 64, tmp_path) == skip
    with pytest.raises(FileExistsError):
        storage.reserve_attempt(skip, tmp_path)


def test_exclusive_reservation_race_only_one_winner(tmp_path: Path, bindings: tuple[EvidenceBinding, ...]) -> None:
    record = reservation(bindings)

    def create() -> bool:
        try:
            storage.reserve_attempt(record, tmp_path)
            return True
        except FileExistsError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: create(), range(2))) == [False, True]
    assert storage.load_attempt("1" * 64, tmp_path) == record


@pytest.mark.parametrize("status,reason", [
    ("failed", "provider_failed"), ("invalid", "response_invalid"), ("failed", "timed_out"),
    ("failed", "cancelled"), ("abstained", "abstained"),
])
def test_all_terminal_dispositions_prevent_reopen(
    tmp_path: Path, bindings: tuple[EvidenceBinding, ...], status: str, reason: str,
) -> None:
    record = reservation(bindings)
    storage.reserve_attempt(record, tmp_path)
    response = {"schema_version": 2, "publication_language": "ru", "status": "abstained",
                "abstention_reason": "no_supported_relationship", "comparison": None}
    outcome = storage.terminal_record(
        record, status=status, completed_at="2026-10-13T00:00:01+00:00", reason=reason,
        response_text=json.dumps(response) if status == "abstained" else None,
        result=parse(response, bindings) if status == "abstained" else None,
        finish_reason="stop" if status == "abstained" else None,
    )
    path = storage.finish_attempt(record, outcome, tmp_path)
    before = path.read_bytes()
    assert storage.load_attempt("1" * 64, tmp_path)["status"] == status
    with pytest.raises(FileExistsError):
        storage.reserve_attempt(record, tmp_path)
    assert path.read_bytes() == before


def test_diagnostics_are_bounded_and_allowlisted(bindings: tuple[EvidenceBinding, ...]) -> None:
    record = storage.terminal_record(
        reservation(bindings), status="invalid", completed_at="2026-10-13T00:00:01+00:00",
        response_text="Ж" * MAX_RESPONSE_BYTES, reported_model="x" * 201, finish_reason="bad\nreason",
        usage={"prompt_tokens": 0, "completion_tokens": -1, "reasoning_tokens": True,
               "total_tokens": 7, "secret": "never retain"}, reason="response_oversized",
    )
    assert record["usage"] == {"prompt_tokens": 0, "total_tokens": 7}
    assert record["response_text"] is None and record["response_sha256"] == hashlib.sha256(
        ("Ж" * MAX_RESPONSE_BYTES).encode()).hexdigest()
    assert record["reported_model"] is None and record["finish_reason"] is None
    assert "never retain" not in json.dumps(record)


@pytest.mark.parametrize("mutation", [
    "extra", "version", "bool_version", "prompt", "accepted", "date", "hash", "report", "bundle", "language",
    "binding_order", "body_extra", "body_model", "body_temperature_bool", "body_tokens_bool", "body_messages",
    "route", "controls", "request_hash", "status", "reason", "naive_time", "backwards_time", "reserved_completed",
    "response", "response_hash", "model", "finish", "usage_bool", "usage_key", "usage_negative", "result",
])
def test_malformed_retained_record_preserves_bytes_and_cannot_reopen(
    tmp_path: Path, bindings: tuple[EvidenceBinding, ...], mutation: str,
) -> None:
    record = reservation(bindings)
    if mutation in {"extra", "version", "bool_version", "prompt", "accepted", "date", "hash", "report", "bundle",
                    "language", "status", "reason", "naive_time", "backwards_time", "reserved_completed", "response",
                    "response_hash", "model", "finish", "usage_bool", "usage_key", "usage_negative", "result"}:
        key, value = {
            "extra": ("extra", 1), "version": ("schema_version", 2), "bool_version": ("schema_version", True),
            "prompt": ("prompt_version", "old"), "accepted": ("accepted_sha256", "4" * 64),
            "date": ("publication_date", "invalid"), "hash": ("sha256", "0" * 64),
            "report": ("report_sha256", "bad"), "bundle": ("bundle_id", "bad"),
            "language": ("publication_language", "de"), "status": ("status", "unknown"),
            "reason": ("reason", "arbitrary diagnostics"), "naive_time": ("started_at", "2026-10-13T00:00:00"),
            "backwards_time": ("completed_at", "2026-10-12T00:00:00+00:00"),
            "reserved_completed": ("completed_at", "2026-10-13T00:00:01+00:00"),
            "response": ("response_text", "unbound"), "response_hash": ("response_sha256", "bad"),
            "model": ("reported_model", "x" * 201), "finish": ("finish_reason", "bad\nreason"),
            "usage_bool": ("usage", {"prompt_tokens": True}), "usage_key": ("usage", {"arbitrary": 1}),
            "usage_negative": ("usage", {"prompt_tokens": -1}), "result": ("result", {}),
        }[mutation]
        record[key] = value
    elif mutation == "binding_order":
        record["ordered_selected_ids"].reverse()
    elif mutation.startswith("body_"):
        key, value = {"body_extra": ("extra", "bad"), "body_model": ("model", "other"),
                      "body_temperature_bool": ("temperature", False),
                      "body_tokens_bool": ("max_completion_tokens", True),
                      "body_messages": ("messages", [])}[mutation]
        record["request_body"][key] = value
    elif mutation == "route":
        record["configured_route"]["provider"] = "other"
    elif mutation == "controls":
        record["controls"]["max_retries"] = 1
    elif mutation == "request_hash":
        record["request_sha256"] = "0" * 64
    if mutation != "hash":
        reseal(record)
    path = storage.attempt_path("1" * 64, tmp_path)
    path.parent.mkdir(parents=True)
    path.write_bytes(canonical_json_bytes(record))
    before = path.read_bytes()
    with pytest.raises(ValueError):
        storage.load_attempt("1" * 64, tmp_path)
    with pytest.raises(FileExistsError):
        storage.reserve_attempt(reservation(bindings), tmp_path)
    assert path.read_bytes() == before


@pytest.mark.parametrize("raw", [b'{"x":1,"x":2}', b'{}', b'[]', b'\xff', b'x' * (storage.MAX_RECORD_BYTES + 1)])
def test_strict_storage_bytes_are_preserved(tmp_path: Path, raw: bytes) -> None:
    path = storage.attempt_path("1" * 64, tmp_path)
    path.parent.mkdir(parents=True)
    path.write_bytes(raw)
    with pytest.raises(ValueError):
        storage.load_attempt("1" * 64, tmp_path)
    assert path.read_bytes() == raw


@pytest.mark.parametrize("parent_symlink", [False, True])
def test_symlink_never_becomes_absence(tmp_path: Path, parent_symlink: bool) -> None:
    target = tmp_path / "target"
    target.mkdir()
    if parent_symlink:
        (tmp_path / "synthesis").symlink_to(target, target_is_directory=True)
    else:
        (tmp_path / "synthesis").mkdir()
        (tmp_path / "synthesis" / f"{'1' * 64}.json").symlink_to(target / "missing")
    with pytest.raises(ValueError, match="symlinks"):
        storage.load_attempt("1" * 64, tmp_path)


def test_storage_path_rejects_traversal_and_nonregular_files(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        storage.load_attempt("../escape", tmp_path)
    path = storage.attempt_path("1" * 64, tmp_path)
    path.mkdir(parents=True)
    with pytest.raises(ValueError, match="regular file"):
        storage.load_attempt("1" * 64, tmp_path)


@pytest.mark.parametrize("boundary", ["fsync", "readback"])
def test_reservation_failure_retains_no_repeat_authority(
    tmp_path: Path, bindings: tuple[EvidenceBinding, ...], boundary: str,
) -> None:
    record = reservation(bindings)
    target = "os.fsync" if boundary == "fsync" else "_read"
    with patch(f"digest.adapters.storage.synthesis.{target}", side_effect=OSError("synthetic storage failure")):
        with pytest.raises(OSError):
            storage.reserve_attempt(record, tmp_path)
    assert storage.load_attempt("1" * 64, tmp_path) == record
    with pytest.raises(FileExistsError):
        storage.reserve_attempt(record, tmp_path)


@pytest.mark.parametrize("boundary", ["fsync", "replace", "readback"])
def test_terminal_failure_never_reopens_retained_attempt(
    tmp_path: Path, bindings: tuple[EvidenceBinding, ...], response: dict[str, Any], boundary: str,
) -> None:
    record = reservation(bindings)
    path = storage.reserve_attempt(record, tmp_path)
    outcome = terminal(record, response, bindings)
    original_read = storage._read
    calls = 0

    def failed_read(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise OSError("synthetic post-replacement readback failure")
        return original_read(*args, **kwargs)

    target = {"fsync": "os.fsync", "replace": "os.replace", "readback": "_read"}[boundary]
    failure = failed_read if boundary == "readback" else OSError("synthetic terminal failure")
    with patch(f"digest.adapters.storage.synthesis.{target}", side_effect=failure):
        with pytest.raises(OSError):
            storage.finish_attempt(record, outcome, tmp_path)
    assert storage.load_attempt("1" * 64, tmp_path) == (outcome if boundary == "readback" else record)
    retained = path.read_bytes()
    with pytest.raises(FileExistsError):
        storage.reserve_attempt(record, tmp_path)
    assert path.read_bytes() == retained
    assert not list(path.parent.glob("*.tmp"))


def test_terminal_cannot_change_identity_or_publish_a_different_response(
    tmp_path: Path, bindings: tuple[EvidenceBinding, ...], response: dict[str, Any],
) -> None:
    record = reservation(bindings)
    path = storage.reserve_attempt(record, tmp_path)
    outcome = terminal(record, response, bindings)
    bad_result = deepcopy(outcome)
    bad_result["result"]["comparison"]["canonical"]["qualification"] = "Different qualification."
    bad_result["result_sha256"] = hashlib.sha256(canonical_json_bytes(bad_result["result"])).hexdigest()
    reseal(bad_result)
    with pytest.raises(ValueError, match="differs"):
        storage.finish_attempt(record, bad_result, tmp_path)
    outcome["started_at"] = "2026-10-12T00:00:00+00:00"
    reseal(outcome)
    with pytest.raises(ValueError, match="changed its reservation"):
        storage.finish_attempt(record, outcome, tmp_path)
    assert json.loads(path.read_bytes()) == record


def test_deeply_nested_response_is_a_validation_error(bindings: tuple[EvidenceBinding, ...]) -> None:
    with pytest.raises(ValueError):
        validate_response("[" * 2000 + "]" * 2000, "stop", "ru", bindings, protected_content_equal)
    with pytest.raises(ValueError, match="Unsupported"):
        validate_response("{}", "stop", "fr", bindings, protected_content_equal)


@pytest.mark.parametrize("field,value", [("interpretation", "x" * 241), ("qualification", "\u2028")])
def test_russian_prose_is_bounded_independently(
    bindings: tuple[EvidenceBinding, ...], response: dict[str, Any], field: str, value: str,
) -> None:
    response["comparison"]["presentation"][field] = value
    with pytest.raises(ValueError, match="plain-text"):
        parse(response, bindings)


@pytest.mark.parametrize("mutation", [
    "no_text", "result_hash", "parsed_status", "completed_before_started", "completed_wrong_type", "completed_long",
    "failed_with_result", "missing_request", "request_type", "missing_messages", "reserved_result",
])
def test_terminal_record_has_no_ambiguous_publishable_state(
    tmp_path: Path, bindings: tuple[EvidenceBinding, ...], response: dict[str, Any], mutation: str,
) -> None:
    record = terminal(reservation(bindings), response, bindings)
    key, value = {
        "no_text": ("response_text", None), "result_hash": ("result_sha256", "0" * 64),
        "parsed_status": ("status", "abstained"),
        "completed_before_started": ("completed_at", "2026-10-12T00:00:00+00:00"),
        "completed_wrong_type": ("completed_at", 1), "completed_long": ("completed_at", "x" * 41),
        "failed_with_result": ("status", "failed"), "missing_request": ("request_body", None),
        "request_type": ("request_body", []), "missing_messages": ("messages", None),
        "reserved_result": ("status", "reserved"),
    }[mutation]
    record[key] = value
    if mutation == "reserved_result":
        record["completed_at"] = None
    path = storage.attempt_path("1" * 64, tmp_path)
    path.parent.mkdir(parents=True)
    path.write_bytes(canonical_json_bytes(reseal(record)))
    with pytest.raises(ValueError):
        storage.load_attempt("1" * 64, tmp_path)


def test_invalid_state_transitions_and_skips_without_reason(bindings: tuple[EvidenceBinding, ...]) -> None:
    record = reservation(bindings)
    with pytest.raises(ValueError, match="terminal transition"):
        storage.terminal_record(record, status="reserved", completed_at="2026-10-13T00:00:01+00:00")
    with pytest.raises(ValueError, match="reserved or skipped"):
        storage.new_record(
            accepted_sha256="1" * 64, publication_date="2026-10-13", report_sha256=None, bundle_id=None,
            bindings=(), publication_language="en", request_body=None, status="valid",
            started_at="2026-10-13T00:00:00+00:00",
        )
    with pytest.raises(ValueError, match="Unattempted"):
        storage.new_record(
            accepted_sha256="1" * 64, publication_date="2026-10-13", report_sha256=None, bundle_id=None,
            bindings=(), publication_language="en", request_body=None, status="skipped",
            started_at="2026-10-13T00:00:00+00:00",
        )


def test_skip_record_byte_ceiling_does_not_truncate_evidence(bindings: tuple[EvidenceBinding, ...]) -> None:
    large = (replace(bindings[0], evidence=replace(bindings[0].evidence, excerpt="x" * storage.MAX_RECORD_BYTES)),)
    with pytest.raises(ValueError, match="byte budget"):
        storage.new_record(
            accepted_sha256="1" * 64, publication_date="2026-10-13", report_sha256=None, bundle_id=None,
            bindings=large, publication_language="en", request_body=None, status="skipped",
            started_at="2026-10-13T00:00:00+00:00", reason="request_oversized",
        )


def test_deep_and_wrong_primitive_storage_inputs_are_rejected(
    tmp_path: Path, bindings: tuple[EvidenceBinding, ...],
) -> None:
    path = storage.attempt_path("1" * 64, tmp_path)
    path.parent.mkdir(parents=True)
    for raw in (b"[" * 2000 + b"]" * 2000, canonical_json_bytes(reseal({**reservation(bindings), "status": []}))):
        path.write_bytes(raw)
        with pytest.raises(ValueError):
            storage.load_attempt("1" * 64, tmp_path)
        assert path.read_bytes() == raw


def test_post_write_verification_mismatch_still_retains_bytes(
    tmp_path: Path, bindings: tuple[EvidenceBinding, ...], response: dict[str, Any],
) -> None:
    record = reservation(bindings)
    with patch.object(storage, "_read", return_value={}):
        with pytest.raises(ValueError, match="reservation readback"):
            storage.reserve_attempt(record, tmp_path)
    assert storage.load_attempt("1" * 64, tmp_path) == record
    outcome = terminal(record, response, bindings)
    with patch.object(storage, "_read", side_effect=[record, record, {}]):
        with pytest.raises(ValueError, match="terminal readback"):
            storage.finish_attempt(record, outcome, tmp_path)
    assert storage.load_attempt("1" * 64, tmp_path) == outcome


def test_changed_reservation_detected_before_replace(
    tmp_path: Path, bindings: tuple[EvidenceBinding, ...], response: dict[str, Any],
) -> None:
    record = reservation(bindings)
    storage.reserve_attempt(record, tmp_path)
    outcome = terminal(record, response, bindings)
    with patch.object(storage, "_read", side_effect=[record, outcome]):
        with pytest.raises(ValueError, match="changed before"):
            storage.finish_attempt(record, outcome, tmp_path)
    assert storage.load_attempt("1" * 64, tmp_path) == record


def test_builder_detaches_mutable_request_and_terminal_inputs(bindings: tuple[EvidenceBinding, ...]) -> None:
    body = request(bindings)
    record = storage.new_record(
        accepted_sha256="1" * 64, publication_date="2026-10-13", report_sha256="2" * 64,
        bundle_id="3" * 64, bindings=bindings, publication_language="ru", request_body=body,
        status="reserved", started_at="2026-10-13T00:00:00+00:00",
    )
    body["messages"][0]["content"] = "changed after capture"
    assert record["messages"][0]["content"] != body["messages"][0]["content"]
    assert record["request_body"] == request(bindings)


@pytest.mark.parametrize("failure", [RecursionError, OverflowError])
def test_response_parser_resource_failures_are_validation_errors(
    bindings: tuple[EvidenceBinding, ...], failure: type[Exception],
) -> None:
    # Binding validation decodes its own small dataclass payloads before response parsing.
    original = json.loads

    def loads(text: Any, *args: Any, **kwargs: Any) -> Any:
        if text == "{}":
            raise failure("synthetic parser boundary")
        return original(text, *args, **kwargs)

    with patch("digest.domain.editorial.synthesis.json.loads", side_effect=loads):
        with pytest.raises(ValueError, match="parsing bound"):
            validate_response("{}", "stop", "ru", bindings, protected_content_equal)


def test_invalid_response_unicode_and_finish_types_are_validation_errors(bindings: tuple[EvidenceBinding, ...]) -> None:
    with pytest.raises(ValueError, match="encoding"):
        validate_response("\ud800", "stop", "ru", bindings, protected_content_equal)
    with pytest.raises(ValueError, match="Incomplete"):
        validate_response("{}", [], "ru", bindings, protected_content_equal)


def test_support_cannot_be_only_excerpt_whitespace(
    bindings: tuple[EvidenceBinding, ...], response: dict[str, Any],
) -> None:
    assert " " in bindings[0].evidence.excerpt
    response["comparison"]["supports"][0]["quote"] = " "
    with pytest.raises(ValueError):
        parse(response, bindings)
