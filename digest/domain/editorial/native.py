"""External native editorial values: exact sources, unchanged cards and complete review.

Execution references are runtime attestations, not proof of independent execution.
RSS excerpts remain the only editorial evidence; no provider or English twin is invented.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import date
from typing import Any

from digest._serialization import canonical_json_bytes, restore_dataclass, unique_object
from digest.domain.catalog.articles import article_hash
from digest.domain.catalog.occurrences import occurrence_sha256
from digest.domain.editorial.candidates import CandidatePacket, validate_occurrence, validate_packet
from digest.domain.editorial.evidence import prepare_evidence_item
from digest.domain.editorial.summaries import ArticleSummary


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def encode(value: Any) -> bytes:
    return canonical_json_bytes(value)


def _decode(raw: bytes, keys: set[str], optional: frozenset[str] = frozenset()) -> dict[str, Any]:
    value = json.loads(raw, object_pairs_hook=unique_object)
    if not isinstance(value, dict) or not keys <= set(value) or not set(value) <= keys | optional:
        raise ValueError("Unexpected external document fields")
    return value


def export_packet(
    packet: CandidatePacket,
    *,
    publication_date: date,
    language: str,
    max_excerpt_chars: int,
    owner_sha256: str,
    editorial_context: str,
    optional_policy: dict[str, Any] | None = None,
) -> bytes:
    """Pure export of actual planned evidence and full immutable occurrences."""
    validate_packet(packet)
    if packet.report is not None or packet.disposition_attempts or packet.handed_to_preparation:
        raise ValueError("Only an unreviewed planned packet can be exported")
    if type(publication_date) is not date or language not in {"ru", "en"}:
        raise ValueError("Explicit publication date and ru/en language required")
    if type(max_excerpt_chars) is not int or max_excerpt_chars < 1:
        raise ValueError("Explicit positive planned excerpt limit required")
    originals = {article_hash(item.title, item.link): item for item in packet.articles}
    for item in packet.evidence.items:
        occurrence = originals[item.evidence_id]
        validate_occurrence(occurrence)
        if not occurrence.source_url:
            raise ValueError("Full source feed binding required")
        projected = prepare_evidence_item(occurrence.article(), max_excerpt_chars=max_excerpt_chars)
        if projected is None or projected[0] != item:
            raise ValueError("Evidence differs from its exact source occurrence projection")
    if optional_policy is not None:
        from digest.domain.editorial.native_optional import policy

        policy(optional_policy, set(originals))
    return encode(
        {
            "schema": "native-source-packet/v2" if optional_policy is not None else "native-source-packet/v1",
            **({"optional_policy": optional_policy} if optional_policy is not None else {}),
            "owner_sha256": owner_sha256,
            "editorial_context": editorial_context,
            "publication_date": publication_date.isoformat(),
            "language": language,
            "max_excerpt_chars": max_excerpt_chars,
            "planning_packet": asdict(packet),
            "occurrence_sha256": {identity: occurrence_sha256(item) for identity, item in originals.items()},
        }
    )


def read_packet(raw: bytes) -> tuple[dict[str, Any], CandidatePacket]:
    value = _decode(
        raw,
        {
            "schema",
            "publication_date",
            "language",
            "max_excerpt_chars",
            "owner_sha256",
            "editorial_context",
            "planning_packet",
            "occurrence_sha256",
        },
        frozenset({"optional_policy"}),
    )
    packet: CandidatePacket = restore_dataclass(value["planning_packet"], CandidatePacket)
    expected = export_packet(
        packet,
        publication_date=date.fromisoformat(value["publication_date"]),
        language=value["language"],
        max_excerpt_chars=value["max_excerpt_chars"],
        owner_sha256=value["owner_sha256"],
        editorial_context=value["editorial_context"],
        optional_policy=value.get("optional_policy"),
    )
    if json.loads(expected) != value:
        raise ValueError("Source packet integrity or occurrence hash mismatch")
    return value, packet


def accepted_cards(
    packet_raw: bytes,
    response_raw: bytes,
    review_raw: bytes,
    packet: CandidatePacket,
    language: str,
) -> tuple[list[ArticleSummary], dict[str, Any], dict[str, Any]]:
    response = _decode(
        response_raw,
        {"schema", "packet_sha256", "execution_ref", "language", "items"},
        frozenset({"comparison", "closing"}),
    )
    review = _decode(
        review_raw,
        {
            "schema",
            "packet_sha256",
            "response_sha256",
            "writer_execution_ref",
            "reviewer_execution_ref",
            "verdict",
            "notes",
            "card_verdicts",
        },
        frozenset({"optional_verdicts"}),
    )
    if (
        (response["schema"], review["schema"]) not in (
            ("native-editorial-response/v1", "external-source-review/v2"),
            ("native-editorial-response/v2", "external-source-review/v3"),
        )
        or response["schema"] == "native-editorial-response/v2"
        and read_packet(packet_raw)[0]["schema"] != "native-source-packet/v2"
        or review["schema"] == "external-source-review/v2" and "optional_verdicts" in review
        or response["packet_sha256"] != sha256(packet_raw)
        or review["packet_sha256"] != sha256(packet_raw)
        or review["response_sha256"] != sha256(response_raw)
        or response["language"] != language
        or not isinstance(response["execution_ref"], str)
        or not response["execution_ref"].strip()
        or review["writer_execution_ref"] != response["execution_ref"]
        or not isinstance(review["reviewer_execution_ref"], str)
        or not review["reviewer_execution_ref"].strip()
        or review["reviewer_execution_ref"] == response["execution_ref"]
        or not isinstance(review["verdict"], str)
        or not review["verdict"].strip()
        or not isinstance(review["notes"], str)
        or not review["notes"].strip()
    ):
        raise ValueError("Independent acceptance is absent or does not bind these exact bytes")
    originals = {article_hash(item.title, item.link): item for item in packet.articles}
    if not isinstance(response["items"], list) or len(response["items"]) != len(originals):
        raise ValueError("Native response must account for every exported ID")
    seen: set[str] = set()
    cards = []
    for item in response["items"]:
        if not isinstance(item, dict) or set(item) != {"evidence_id", "status", "text"}:
            raise ValueError("Unexpected native item fields")
        identity, status, text = item["evidence_id"], item["status"], item["text"]
        if not isinstance(identity, str) or identity not in originals or identity in seen:
            raise ValueError("Unknown or duplicate native item ID")
        seen.add(identity)
        if (
            not isinstance(status, str)
            or status not in {"card", "withheld", "insufficient_evidence"}
            or not isinstance(text, str)
            or (status == "card" and (not text.strip() or len(text) > 600))
            or (status != "card" and text != "")
        ):
            raise ValueError("Invalid native card or abstention")
        if status == "card":
            source = originals[identity]
            cards.append(ArticleSummary(source.title, source.link, source.source, source.category, text))
    if len(cards) > packet.max_selections:
        raise ValueError("Native response exceeds the planned publication cap")
    accepted_ids = _accepted_main_ids(cards, review)
    return [card for card in cards if article_hash(card.title, card.link) in accepted_ids], response, review


def _accepted_main_ids(cards: list[ArticleSummary], review: dict[str, Any]) -> set[str]:
    """Require a complete exact-text review, then retain only explicit acceptance."""
    originals = {article_hash(card.title, card.link): card for card in cards}
    verdicts = review["card_verdicts"]
    if not isinstance(verdicts, list) or len(verdicts) != len(originals):
        raise ValueError("Incomplete per-card review: every generated main card needs a verdict")
    seen: set[str] = set()
    accepted: set[str] = set()
    for item in verdicts:
        if not isinstance(item, dict) or set(item) != {"evidence_id", "text_sha256", "verdict", "notes"}:
            raise ValueError("Invalid per-card review fields")
        identity = item["evidence_id"]
        if not isinstance(identity, str) or identity not in originals or identity in seen:
            raise ValueError("Unknown or duplicate per-card review ID")
        seen.add(identity)
        if item["text_sha256"] != sha256(originals[identity].summary.encode()):
            raise ValueError("Per-card review text hash mismatch")
        if not all(isinstance(item[key], str) and item[key].strip() for key in ("verdict", "notes")):
            raise ValueError("Missing per-card verdict or review notes")
        if item["verdict"] == "accept":
            accepted.add(identity)
    return accepted


def comparison_dependency(response: dict[str, Any], accepted_ids: set[str]) -> str:
    """Diagnostic only. Even satisfied dependencies never enable publication."""
    if "comparison" not in response:
        return "not_supplied"
    comparison = response["comparison"]
    ids = comparison.get("evidence_ids") if isinstance(comparison, dict) else None
    if not isinstance(ids, list) or len(ids) < 2 or not all(isinstance(identity, str) for identity in ids):
        return "ineligible_unvalidated_dependencies"
    if len(set(ids)) != len(ids):
        return "ineligible_unvalidated_dependencies"
    generated_ids = {item["evidence_id"] for item in response["items"] if item["status"] == "card"}
    if set(ids) & (generated_ids - accepted_ids):
        return "ineligible_nonaccepted_main_dependency"
    if not set(ids) <= accepted_ids:
        return "ineligible_nonpublished_dependency"
    return "dependencies_satisfied_publication_still_withheld"
