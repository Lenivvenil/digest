"""Inactive, local-only adapter to the pinned engine's real compact renderer.

No storage, model, network, task reservation or delivery operation is invoked.
The selected packet is a renderer input projection, never an issued native export.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict
from datetime import date
from typing import Any

from digest.application.native import _optional_policy, _optional_publication, _present
from digest.application.selection_candidate import bounded_selection_transport as transport
from digest.application.selection_candidate import optional_codec
from digest.config import Config
from digest.domain.editorial.candidates import CandidateArticle, CandidatePacket, validate_packet
from digest.domain.editorial.evidence import evidence_bundle_from_items, prepare_evidence_item
from digest.domain.editorial.summaries import ArticleSummary
from digest.presentation import publication_language
from digest.presentation.telegram import render_compact_publication

ENGINE_COMMIT = "58195f4bfc59f41c5fdb4459eb180b5a332ce630"


def _native_optional(kind: str, value: dict[str, Any]) -> dict[str, Any]:
    if kind == "closing":
        return {
            "evidence_id": transport.full_id(value["id"]),
            "occurrence_sha256": transport.full_hash(value["occurrence_sha256_base64url"]),
            "text": value["text"],
        }
    return {
        "supports": [
            {
                "evidence_id": transport.full_id(s["id"]),
                "occurrence_sha256": transport.full_hash(s["occurrence_sha256_base64url"]),
                "observation": s["observation"],
            }
            for s in value["supports"]
        ],
        "interpretation": value["interpretation"],
        "qualification": value["qualification"],
    }


def render_accepted(
    source_message: dict[str, Any],
    acceptance: dict[str, Any],
    originals: dict[str, dict[str, Any]],
    config: Config,
    *,
    publication_date: str,
    planned_at: str,
    actual_writer_execution_ref: str,
    actual_source_execution_ref: str,
    expected_issued_source_message_sha256: str,
) -> dict[str, Any]:
    """Validate exact accepted inputs, then use actual engine presentation logic."""
    transport.validate_acceptance(
        acceptance,
        source_message,
        actual_execution_ref=actual_source_execution_ref,
        prior_execution_refs={actual_writer_execution_ref},
        expected_issued_message_sha256=expected_issued_source_message_sha256,
    )
    if acceptance["verdict"] != "accept":
        raise ValueError("Rejected source review cannot render")
    from digest.application.selection_candidate.shared_original_codec import unpack

    source = unpack(source_message["review_input"])
    if source["writer_execution_ref"] != actual_writer_execution_ref:
        raise ValueError("Writer execution chain mismatch")
    if transport.sha({i: transport.sha(a) for i, a in sorted(originals.items())}) != source["pool_sha256"]:
        raise ValueError("Owner original pool differs from accepted binding")
    captured = source["optional_policy"]
    current = optional_codec.derive_policy(originals, config)
    if transport.encode(captured) != transport.encode(current):
        raise ValueError("Optional/source policy changed after source review")
    if source["publication_date"] != publication_date or source["language"] != publication_language(config):
        raise ValueError("Publication date or language differs from accepted source context")
    proposals = source["proposals"]
    accepted_ids = {v["id"] for v in acceptance["card_verdicts"] if v["verdict"] == "accept"}
    ordered = [p for p in proposals if p["decision"]["id"] in accepted_ids]
    selected_ids = {transport.full_id(p["decision"]["id"]) for p in proposals}
    values = {
        kind: next((p["value"] for p in source["optional_proposals"] if p["kind"] == kind), None)
        for kind in ("closing", "comparison")
    }
    if values["closing"] is not None:
        selected_ids.add(transport.full_id(values["closing"]["id"]))
    occurrences = tuple(CandidateArticle(**originals[i]) for i in sorted(selected_ids))
    evidence_items = []
    for occurrence in occurrences:
        prepared = prepare_evidence_item(occurrence.article(), max_excerpt_chars=500)
        if prepared is None:
            raise ValueError("Native evidence preparation rejected a projected original")
        evidence_items.append(prepared[0])
    packet = CandidatePacket(
        evidence=evidence_bundle_from_items(tuple(evidence_items), omitted_articles=0),
        articles=occurrences,
        priorities={
            a.source: next((s.priority for s in config.enabled_sources if s.name == a.source), 3) for a in occurrences
        },
        planned_at=planned_at,
        prompt_hash=source["writer_payload_sha256"],
        max_selections=source["max_cards"],
    )
    validate_packet(packet)
    cards = []
    for p in ordered:
        d = p["decision"]
        original = originals[transport.full_id(d["id"])]
        if transport.sha(original) != p["occurrence_sha256"] or original != p["original"]:
            raise ValueError("Accepted card original mismatch")
        cards.append(
            ArticleSummary(original["title"], original["link"], original["source"], original["category"], d["text"])
        )
    day = date.fromisoformat(publication_date)
    language = publication_language(config)
    shown, credited = _present(cards, packet, day, language)
    rendered = render_compact_publication(shown, config, "") if shown else None
    if rendered and any(a.full_hash in credited and len(a.covering_chunks) != 1 for a in rendered.articles):
        raise ValueError("Native attribution would split across delivery chunks")
    native_response: dict[str, Any] = {
        "items": [{"evidence_id": transport.full_id(p["decision"]["id"]), "status": "card"} for p in proposals]
    }
    native_review: dict[str, Any] = {"optional_verdicts": []}
    for kind, value in values.items():
        if value is None:
            continue
        native_value = _native_optional(kind, value)
        native_response[kind] = native_value
        verdict = next(v for v in acceptance["optional_verdicts"] if v["kind"] == kind)
        native_review["optional_verdicts"].append(
            {
                "kind": kind,
                "value_sha256": transport.sha(native_value),
                "verdict": verdict["verdict"],
                "notes": verdict["notes"],
            }
        )
    projected_policy = {**captured, "closing_ids": [i for i in captured["closing_ids"] if i in selected_ids]}
    if projected_policy != _optional_policy(packet, config):
        # Approved-ID order is not semantic; compare canonical sets separately.
        if {**projected_policy, "closing_ids": sorted(projected_policy["closing_ids"])} != {
            **_optional_policy(packet, config),
            "closing_ids": sorted(_optional_policy(packet, config)["closing_ids"]),
        }:
            raise ValueError("Native projected policy differs")
    _, shown, rendered, metadata = _optional_publication(
        cards,
        shown,
        rendered,
        packet,
        {"optional_policy": projected_policy},
        native_response,
        native_review,
        config,
        day,
        language,
    )
    expected_optional = optional_codec.expected_composition(
        values,
        originals,
        [p["decision"] for p in proposals],
        captured,
        acceptance["optional_verdicts"],
        [p["decision"]["id"] for p in ordered],
    )
    for kind in ("closing", "comparison"):
        if (expected_optional[kind] is not None) != (metadata[kind]["status"] == "included"):
            raise ValueError("Native renderer withheld or included optional content differently; HOLD")
    chunks = [asdict(chunk) for chunk in rendered.chunks] if rendered else []
    rendered_view = {"schema": "native-rendered-chunks/v1", "engine_commit": ENGINE_COMMIT, "chunks": chunks}
    rendered_text = transport.encode(rendered_view).decode()
    composition = {
        "cards": [
            {"id": p["decision"]["id"], "text": p["decision"]["text"], "link": p["original"]["link"]} for p in ordered
        ],
        "optional": expected_optional,
        "rendered_text": rendered_text,
    }
    if transport.wire_field_bytes(rendered_text) > 16000:
        raise transport.BudgetExceeded("Real native rendered view exceeds admitted final-text budget")
    return {
        "composition": composition,
        "owner_rendered_text_sha256": hashlib.sha256(rendered_text.encode()).hexdigest(),
        "native_optional_status": {kind: metadata[kind]["status"] for kind in ("closing", "comparison")},
        "native_chunks": chunks,
        "native_articles": [asdict(item) for item in rendered.articles] if rendered else [],
        "native_metadata": metadata,
        "shown_cards": [asdict(card) for card in shown],
        "renderer_projection_packet_sha256": transport.sha(asdict(packet)),
        "source_acceptance_sha256": transport.sha(acceptance),
        "qualification": (
            "Real pinned native renderer executed locally; no native packet issued, no delivery or dispatch."
        ),
    }
