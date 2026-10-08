"""Synthetic cases for literal query provenance, independent of provider calls."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import FrozenInstanceError, asdict
from typing import Any

import pytest

from digest.irritator.query_contract import SourceQueryAnchor, find_source_anchor, lexical_atoms


def _evidence(
    title: str = "Orion relay architecture", excerpt: str = "HTTP transport carries encrypted requests",
) -> dict[str, Any]:
    return {
        "bundle_id": "synthetic-bundle",
        "items": [{"evidence_id": "synthetic-item", "title": title, "excerpt": excerpt}],
    }


def test_verdict_seeking_queries_have_no_source_anchor() -> None:
    queries = ["Orion relay failure", "HTTP transport ineffective", "encrypted requests unsafe"]

    assert find_source_anchor(queries, _evidence()) is None


def test_existing_phrase_and_exploratory_queries_record_exact_proof_without_mutation() -> None:
    queries = ["Orion relay failure", "HTTP transport", "encrypted requests unsafe"]
    evidence = _evidence(excerpt="A study of HTTP transport with shared relays")
    before = deepcopy((queries, evidence))

    anchor = find_source_anchor(queries, evidence)

    assert anchor == SourceQueryAnchor(1, "HTTP transport", "synthetic-bundle", "synthetic-item",
                                       "excerpt", 11, 25, "HTTP transport")
    assert (queries, evidence) == before
    assert anchor is not None
    with pytest.raises(FrozenInstanceError):
        anchor.query_index = 2  # type: ignore[misc]


@pytest.mark.parametrize("query", [
    "Orion relay unsafe",
    "architecture HTTP",
    "Orion architecture",
    "relay Orion",
    "HTTP encrypted",
])
def test_partial_query_cross_field_skipped_and_reordered_matches_fail(query: str) -> None:
    assert find_source_anchor([query], _evidence()) is None


def test_cross_article_phrase_does_not_match() -> None:
    evidence = _evidence(title="Orion", excerpt="relay")
    evidence["items"].append({"evidence_id": "second-item", "title": "architecture", "excerpt": "HTTP"})

    assert find_source_anchor(["relay architecture"], evidence) is None


@pytest.mark.parametrize(("query", "source", "matched"), [
    ('"http transport"', "New HTTP transport analysis", "HTTP transport"),
    ('HTTP "transport layer"', "New Http\ttransport\nlayer analysis", "Http\ttransport\nlayer"),
    ('"HTTP  transport"', "New HTTP transport analysis", "HTTP transport"),
    ("облачные сервисы", "Новые Облачные\nсервисы растут", "Облачные\nсервисы"),
    ("安全通信", "新規 安全通信 実験", "安全通信"),
])
def test_quotes_case_whitespace_and_source_language_match(query: str, source: str, matched: str) -> None:
    anchor = find_source_anchor([query], _evidence(title=source))

    assert anchor is not None
    assert anchor.query == query
    assert anchor.matched_text == matched
    assert source[anchor.start:anchor.end] == matched


def test_original_offsets_survive_casefold_length_changes_before_match() -> None:
    source = "İstanbul Straße HTTP transport study"
    assert len(source.casefold()) != len(source)

    anchor = find_source_anchor(["http transport"], _evidence(title=source))

    assert anchor is not None
    assert anchor.start == source.index("HTTP")
    assert anchor.end == source.index("HTTP") + len("HTTP transport")
    assert source[anchor.start:anchor.end] == anchor.matched_text == "HTTP transport"


@pytest.mark.parametrize(("query", "source"), [
    ("HTTP", "OHTTP transport"),
    ("HTTP", "HTTP2 transport"),
    ("C", "C++ language"),
    ("C++", "C+++ language"),
    ("GPT", "GPT-4 model"),
    ("GPT-4", "GPT-40 model"),
    ("GPT-4", "prefix-GPT-4 model"),
    ("NET", ".NET runtime"),
    ("example", "example.com domain"),
    ("3", "3.8 release"),
    ("Node", "Node.js runtime"),
    ("com", "example.com domain"),
    ("HTTP", "path/HTTP endpoint"),
    ("AI", "AI_tools"),
    ("сервис", "сервисы"),
])
def test_technical_and_unicode_word_boundaries_reject_substrings(query: str, source: str) -> None:
    assert find_source_anchor([query], _evidence(title=source, excerpt="")) is None


@pytest.mark.parametrize("query", ["HTTP", "C++", "GPT-4", "C#", "Node.js", "HTTP/3", "it's"])
def test_whole_technical_words_match(query: str) -> None:
    source = f"New {query} research"

    anchor = find_source_anchor([query], _evidence(title=source))

    assert anchor is not None
    assert anchor.matched_text == query


@pytest.mark.parametrize(("query", "source"), [
    ("Engine failure", "Engine failure."),
    ("Engine failure", "Engine failure. Recovery follows."),
    ("Engine failure", "Engine failure..."),
    ("example.com", "The example.com."),
    ("3.8", "Release 3.8."),
    ("C++", "We use C++."),
    ("GPT-4", "We tested GPT-4."),
    ("HTTP", "We use HTTP."),
])
def test_terminal_period_is_prose_punctuation(query: str, source: str) -> None:
    anchor = find_source_anchor([query], _evidence(title=source, excerpt=""))

    assert anchor is not None
    assert source[anchor.start:anchor.end] == anchor.matched_text == query


@pytest.mark.parametrize(("query", "source"), [
    ("OHTTP", "We discuss 'OHTTP' here"),
    ("OHTTP", "We discuss ‘OHTTP’ here"),
    ("Engine failure", "We discuss 'Engine failure'."),
    ("C++", "We discuss ‘C++’."),
    ("O'Reilly", "We discuss 'O'Reilly' here"),
    ("Cloudflare’s", "We discuss ‘Cloudflare’s relay’ here"),
])
def test_surrounding_single_quotes_delimit_source_phrases(query: str, source: str) -> None:
    anchor = find_source_anchor([query], _evidence(title=source, excerpt=""))

    assert anchor is not None
    assert source[anchor.start:anchor.end] == anchor.matched_text == query


@pytest.mark.parametrize(("query", "source"), [
    ("O", "O'Reilly publishes books"),
    ("Reilly", "O'Reilly publishes books"),
    ("Cloudflare", "Cloudflare's relay"),
    ("s", "Cloudflare's relay"),
    ("Cloudflare", "Cloudflare’s relay"),
    ("s", "Cloudflare’s relay"),
    ("HTTP", "We discuss 'OHTTP' here"),
])
def test_internal_apostrophes_do_not_allow_word_fragments(query: str, source: str) -> None:
    assert find_source_anchor([query], _evidence(title=source, excerpt="")) is None


def test_real_negative_source_phrase_can_anchor() -> None:
    anchor = find_source_anchor(["relay failure modes"], _evidence(title="A survey of relay failure modes"))

    assert anchor is not None
    assert anchor.matched_text == "relay failure modes"


def test_trivial_source_phrase_proves_only_provenance() -> None:
    anchor = find_source_anchor(["A"], _evidence(title="A survey of relays"))

    assert anchor is not None
    assert asdict(anchor) == {
        "query_index": 0, "query": "A", "evidence_bundle_id": "synthetic-bundle",
        "evidence_id": "synthetic-item", "field": "title", "start": 0, "end": 1, "matched_text": "A",
    }


def test_only_supplied_source_fields_can_anchor() -> None:
    evidence = _evidence(title="One title", excerpt="One excerpt")
    evidence["items"][0]["url"] = "HTTP transport"
    evidence["items"][0]["summary"] = "HTTP transport"
    evidence["claim"] = "HTTP transport"
    evidence["bundle_id"] = "HTTP transport"
    evidence["unrelated_items"] = [{"title": "HTTP transport"}]

    assert find_source_anchor(["HTTP transport"], evidence) is None


def test_supplied_qualification_context_can_anchor_with_its_own_identity() -> None:
    evidence = _evidence()
    evidence["qualification_context"] = [
        {"evidence_id": "qualification-item", "title": "Qualifications",
         "excerpt": "Some shared relays remain untested"},
    ]

    anchor = find_source_anchor(["shared relays"], evidence)

    assert anchor == SourceQueryAnchor(0, "shared relays", "synthetic-bundle", "qualification-item",
                                       "excerpt", 5, 18, "shared relays")


def test_selection_prefers_query_order_then_items_then_fields() -> None:
    evidence = _evidence(title="Orion relay", excerpt="HTTP transport")
    evidence["items"].append({"evidence_id": "second-item", "title": "HTTP transport", "excerpt": "HTTP transport"})
    evidence["qualification_context"] = [
        {"evidence_id": "qualification-item", "title": "HTTP transport", "excerpt": "HTTP transport"},
    ]

    anchor = find_source_anchor(["HTTP transport", "Orion relay"], evidence)

    assert anchor is not None
    assert (anchor.query_index, anchor.evidence_id, anchor.field) == (0, "synthetic-item", "excerpt")
    evidence["items"][0]["title"] = "HTTP transport"
    anchor = find_source_anchor(["HTTP transport"], evidence)
    assert anchor is not None and anchor.field == "title"


@pytest.mark.parametrize("evidence", [_evidence(), {"bundle_id": "empty", "items": []}, {}])
def test_empty_queries_provide_no_proof(evidence: dict[str, Any]) -> None:
    assert find_source_anchor([], evidence) is None


def test_empty_evidence_provides_no_proof() -> None:
    assert find_source_anchor(["HTTP transport"], {"bundle_id": "empty", "items": []}) is None


@pytest.mark.parametrize("query", [
    "", " HTTP", "HTTP ", "HTTP  transport", "HTTP\ttransport", '"HTTP',
    "HTTP OR transport", "title:HTTP", "HTTP (transport)", "HTTP -failure", "HTTP*",
    "one two three four five six seven eight nine", "x" * 201,
])
def test_malformed_queries_are_rejected_by_lexical_contract(query: str) -> None:
    with pytest.raises(ValueError, match="Invalid lexical query contract"):
        lexical_atoms(query)


def test_malformed_later_query_is_rejected_even_when_an_earlier_query_anchors() -> None:
    with pytest.raises(ValueError, match="Invalid lexical query contract"):
        find_source_anchor(["HTTP transport", "HTTP OR transport"], _evidence())
