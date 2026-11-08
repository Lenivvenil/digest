"""Opt-in versioned full-pool native handoff, sharing existing ready/delivery owners.

This module does not collect, launch workers, schedule, call providers, or send.
The owner must supply independently captured exact worker envelopes. Publication
uses the existing immutable native-ready and prepared-delivery path.
"""

from __future__ import annotations

import base64
from dataclasses import asdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from digest._serialization import restore_dataclass
from digest.adapters.storage import native as storage
from digest.application import native, prepared_delivery
from digest.application.selection_candidate import bounded_selection_transport as codec
from digest.application.selection_candidate import optional_codec
from digest.application.selection_candidate.native_renderer_bridge import render_accepted
from digest.config import Config
from digest.domain.delivery.edition import PreparedArticle
from digest.domain.editorial.candidates import CandidateArticle, CandidatePacket, validate_packet
from digest.domain.editorial.evidence import evidence_bundle_from_items, prepare_evidence_item
from digest.domain.editorial.native import encode
from digest.domain.editorial.summaries import ArticleSummary
from digest.presentation import publication_language

PACKET_SCHEMA = "native-selection-packet/v3"
PROJECTION_SCHEMA = "native-selection-composition/v1"
SOURCE_SCHEMA = "native-selection-source-envelope/v2"
FINAL_SCHEMA = "native-selection-final-envelope/v2"


def source_packet(raw: bytes) -> tuple[dict[str, Any], CandidatePacket]:
    value = storage.decode(raw)
    if (
        set(value)
        != {
            "schema",
            "owner_sha256",
            "owner_execution_ref",
            "publication_date",
            "planned_at",
            "originals",
            "writer_message",
            "configuration",
            "effective_configuration",
            "prior_packet_refs",
        }
        or value["schema"] != PACKET_SCHEMA
    ):
        raise ValueError("Invalid versioned selection packet")
    writer = value["writer_message"]["writer_input"]
    codec.validate_writer(
        writer, value["originals"], publication_date=value["publication_date"], excerpt_limit=writer["excerpt_limit"]
    )
    codec.validate_role_message(
        value["writer_message"], expected_issued_message_sha256=value["writer_message"]["role_message_sha256"]
    )
    config: Config = restore_dataclass(value["effective_configuration"], Config)
    restore_dataclass(value["configuration"], Config)
    if value["writer_message"]["language"] != publication_language(config):
        raise ValueError("Selection language differs from retained configuration")
    if value["writer_message"].get("editorial_context", "") != config.review.editorial_context:
        raise ValueError("Selection editorial context differs from retained configuration")
    if not isinstance(value["owner_execution_ref"], str) or not value["owner_execution_ref"].strip():
        raise ValueError("Actual owner execution reference required")
    policy = optional_codec.decode_policy(value["writer_message"]["optional_policy"])
    if policy != optional_codec.derive_policy(value["originals"], config):
        raise ValueError("Retained optional/source policy differs")
    articles = tuple(CandidateArticle(**a) for _, a in sorted(value["originals"].items()))
    items = []
    for article in articles:
        prepared = prepare_evidence_item(article.article(), max_excerpt_chars=writer["excerpt_limit"])
        if prepared is None:
            raise ValueError("Selection contains unusable source evidence")
        items.append(prepared[0])
    packet = CandidatePacket(
        evidence_bundle_from_items(tuple(items), omitted_articles=0),
        articles,
        {s.name: s.priority for s in config.enabled_sources},
        value["planned_at"],
        prompt_hash=codec.sha(writer),
        max_selections=writer["max_cards"],
    )
    validate_packet(packet)
    for ref in value["prior_packet_refs"]:
        storage.read_reference(restore_dataclass(ref, storage.Reference))
    return value, packet


def export_prepared_sources(
    config: Config,
    *,
    enabled: bool,
    originals: dict[str, Any],
    writer_message: dict[str, Any],
    owner_execution_ref: str,
    planned_at: str,
    prior_packet_refs: list[dict[str, str]],
    cache: Path = Path(".cache"),
    now: datetime | None = None,
) -> storage.PendingNative:
    """Export an already admitted full pool; no recollection or model dispatch.

    All-role 128KiB admission and fixed-file reservation remain the owner's job.
    No default config/CLI activation is added by this explicit candidate API.
    """
    if type(enabled) is not bool or not enabled:
        raise ValueError("Explicit selection activation required")
    instant = prepared_delivery._instant(now)
    day = date.fromisoformat(writer_message["writer_input"]["publication_date"])
    owner = native._identity(config, day, instant)
    value = {
        "schema": PACKET_SCHEMA,
        "owner_sha256": owner,
        "owner_execution_ref": owner_execution_ref,
        "publication_date": day.isoformat(),
        "planned_at": planned_at,
        "originals": originals,
        "writer_message": writer_message,
        "configuration": asdict(config),
        "effective_configuration": asdict(config),
        "prior_packet_refs": prior_packet_refs,
    }
    raw = encode(value)
    _, packet = source_packet(raw)
    effective = native._eligible(packet, config, cache, instant, frozen=True)
    value["effective_configuration"] = asdict(effective)
    raw = encode(value)
    source_packet(raw)
    pending = storage.load_pending(cache)
    if pending is not None:
        if pending.schema == 2 and storage.read_reference(pending.packet) == raw:
            return pending
        raise ValueError("Existing native handoff cannot be replaced or upgraded")
    native._guard(cache, day, instant)
    if not 0 < len(raw) <= storage.MAX_BYTES:
        raise ValueError("Selection retained packet exceeds owner checkpoint bound")
    from digest.application.native_selection_lifecycle import retain_selection_pool

    retain_selection_pool(cache, originals, now=instant)
    result = storage.PendingNative(2, owner, day.isoformat(), storage.put_bytes(raw, cache, "selection-packet"))
    storage.save_pending(cache, result)
    return result


def verify_configuration(pending: storage.PendingNative, config: Config) -> None:
    packet, _ = source_packet(storage.read_reference(pending.packet))
    if encode(packet["configuration"]) != encode(asdict(config)):
        raise ValueError("Selection retained configuration changed")


def captured_acceptance(envelope: dict[str, Any]) -> dict[str, Any]:
    """Retain exact worker bytes, not a reserialized semantic approximation."""
    raw = base64.b64decode(envelope["acceptance_base64"], validate=True)
    if not 0 < len(raw) <= codec.MAX_BYTES:
        raise ValueError("Captured acceptance exceeds worker output bound")
    value = storage.decode(raw)
    if encode(value) != encode(envelope["acceptance"]):
        raise ValueError("Captured acceptance bytes differ from semantic envelope")
    return value


def _source(
    pending: storage.PendingNative, response_raw: bytes, review_raw: bytes, config: Config
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    packet, _ = source_packet(storage.read_reference(pending.packet))
    if encode(packet["effective_configuration"]) != encode(asdict(config)):
        raise ValueError("Selection configuration changed")
    response, envelope = storage.decode(response_raw), storage.decode(review_raw)
    if (
        set(envelope)
        != {
            "schema",
            "message",
            "acceptance",
            "acceptance_base64",
            "actual_execution_ref",
            "actual_writer_execution_ref",
        }
        or envelope["schema"] != SOURCE_SCHEMA
    ):
        raise ValueError("Selection source envelope required")
    writer = packet["writer_message"]
    if response["execution_ref"] != envelope["actual_writer_execution_ref"]:
        raise ValueError("Captured writer differs from separately observed execution")
    if response["execution_ref"] == packet["owner_execution_ref"]:
        raise ValueError("Owner cannot impersonate writer")
    expected_view = codec.make_source_review(
        writer["writer_input"],
        response,
        packet["originals"],
        publication_date=pending.publication_date,
        actual_writer_execution_ref=response["execution_ref"],
        writer_message=writer,
        expected_issued_writer_message_sha256=writer["role_message_sha256"],
    )
    message = envelope["message"]
    expected = codec.complete_review_message(
        expected_view,
        safety_rules=writer["safety_rules"],
        closing_rules=writer["closing_qualification"],
        output_transport=message["output_transport"],
    )
    if encode(message) != encode(expected):
        raise ValueError("Source message differs from exact retained evidence")
    acceptance = captured_acceptance(envelope)
    codec.validate_acceptance(
        acceptance,
        message,
        actual_execution_ref=envelope["actual_execution_ref"],
        prior_execution_refs={packet["owner_execution_ref"], response["execution_ref"]},
        expected_issued_message_sha256=message["role_message_sha256"],
    )
    if acceptance["verdict"] != "accept":
        raise ValueError("Rejected selection source review; HOLD")
    rendered = render_accepted(
        message,
        acceptance,
        packet["originals"],
        config,
        publication_date=pending.publication_date,
        planned_at=packet["planned_at"],
        actual_writer_execution_ref=response["execution_ref"],
        actual_source_execution_ref=envelope["actual_execution_ref"],
        expected_issued_source_message_sha256=message["role_message_sha256"],
    )
    return packet, response, envelope, rendered


def projection(
    pending: storage.PendingNative, response_raw: bytes, review_raw: bytes, config: Config
) -> dict[str, Any]:
    assert pending.response is not None and pending.review is not None
    packet, response, source, rendered = _source(pending, response_raw, review_raw, config)
    meta = rendered["native_metadata"]
    shown = [ArticleSummary(**card) for card in rendered["shown_cards"]]
    inputs = {
        "packet_sha256": pending.packet.sha256,
        "response_sha256": codec.sha(storage.decode(response_raw)),
        "review_sha256": codec.sha(storage.decode(review_raw)),
    }
    # Inputs bind exact bytes, including whitespace, rather than their canonical form.
    from digest.adapters.storage.edition import content_sha256

    inputs.update(response_sha256=content_sha256(response_raw), review_sha256=content_sha256(review_raw))
    owner = prepared_delivery._owner()[0]
    payloads = []
    for chunk in rendered["native_chunks"]:
        payload = {"chat_id": owner, "text": chunk["text"], "parse_mode": "MarkdownV2", "disable_notification": False}
        if chunk["reply_markup"] is not None:
            payload["reply_markup"] = chunk["reply_markup"]
        payloads.append(payload)
    canonical = {
        "language": publication_language(config),
        "cards": [asdict(c) for c in shown],
        "main_count": meta["main_count"],
        "native_optional": {k: meta[k] for k in ("closing", "comparison")},
        "native_editorial": {
            "kind": "external_native_selection",
            "provider": None,
            "model": None,
            "usage": None,
            "writer_execution_ref": response["execution_ref"],
            "reviewer_execution_ref": source["actual_execution_ref"],
            "original_edition_verdict": "accept",
            "inputs": inputs,
        },
        "source_count": len({a["category"] for a in packet["originals"].values()}),
        "article_count": len(packet["originals"]),
        "counts_scope": "complete_retained_eligible_pool",
        "contributing_sources": sorted({a["source"] for a in packet["originals"].values()}),
        "distinct_packet_publishers": len({a["source"] for a in packet["originals"].values()}),
    }
    presentation = {
        "language": publication_language(config),
        "cards": [asdict(c) for c in shown],
        "translation": None,
        "closing": meta["closing"]["status"],
        "comparison": meta["comparison"]["status"],
        "comparison_publication": meta["comparison"]["status"],
        "closing_start": meta["closing_start"],
        "synthesis": meta["synthesis"],
    }
    return {
        "schema": PROJECTION_SCHEMA,
        "inputs": inputs,
        "references": {
            "packet": asdict(pending.packet),
            "response": asdict(pending.response),
            "review": asdict(pending.review),
        },
        "archive_enabled": config.obsidian.enabled,
        "selection_composition": rendered["composition"],
        "owner_rendered_text_sha256": rendered["owner_rendered_text_sha256"],
        "publication": {
            "owner_sha256": pending.owner_sha256,
            "bot_username": config.telegram.bot_username,
            "window_start": datetime.combine(
                date.fromisoformat(pending.publication_date), datetime.min.time(), timezone.utc
            ).isoformat(),
            "canonical_metadata": canonical,
            "presentation_metadata": presentation,
            "payloads": payloads,
            "articles": [
                asdict(PreparedArticle(item["full_hash"], item["source"], list(item["covering_chunks"]), card))
                for item, card in zip(rendered["native_articles"], shown, strict=True)
            ],
        },
    }


def validate_acceptance(raw: bytes, projection_ref: storage.Reference, projected: dict[str, Any]) -> dict[str, Any]:
    envelope = storage.decode(raw)
    if set(envelope) != {
        "schema",
        "projection_sha256",
        "message",
        "acceptance",
        "acceptance_base64",
        "actual_execution_ref",
    } or (envelope["schema"] != FINAL_SCHEMA or envelope["projection_sha256"] != projection_ref.sha256):
        raise ValueError("Exact versioned selection final envelope required")
    refs = {k: restore_dataclass(v, storage.Reference) for k, v in projected["references"].items()}
    packet, _ = source_packet(storage.read_reference(refs["packet"]))
    response = storage.decode(storage.read_reference(refs["response"]))
    source = storage.decode(storage.read_reference(refs["review"]))
    message = envelope["message"]
    expected = codec.make_final_review(
        source["message"],
        source["acceptance"],
        projected["selection_composition"],
        actual_source_execution_ref=source["actual_execution_ref"],
        actual_writer_execution_ref=response["execution_ref"],
        safety_rules=packet["writer_message"]["safety_rules"],
        closing_rules=packet["writer_message"]["closing_qualification"],
        output_transport=message["output_transport"],
        expected_issued_source_message_sha256=source["message"]["role_message_sha256"],
        expected_owner_rendered_text_sha256=projected["owner_rendered_text_sha256"],
    )
    if encode(message) != encode(expected):
        raise ValueError("Final message differs from exact retained composition")
    captured_acceptance(envelope)
    codec.validate_acceptance(
        envelope["acceptance"],
        message,
        actual_execution_ref=envelope["actual_execution_ref"],
        prior_execution_refs={packet["owner_execution_ref"], response["execution_ref"], source["actual_execution_ref"]},
        expected_issued_message_sha256=message["role_message_sha256"],
    )
    if envelope["acceptance"]["verdict"] != "accept":
        raise ValueError("Selection final review rejected; HOLD")
    return envelope


def lifecycle_arguments(pending: storage.PendingNative, projected: dict[str, Any]) -> dict[str, Any]:
    assert pending.response is not None and pending.review is not None and pending.acceptance is not None
    packet, _ = source_packet(storage.read_reference(pending.packet))
    response = storage.decode(storage.read_reference(pending.response))
    decisions = [
        {
            **d,
            "id": codec.full_id(d["id"]),
            "duplicate_of": codec.full_id(d["duplicate_of"]) if d["duplicate_of"] else None,
        }
        for d in response["decisions"]
    ]
    return {
        "packet_ref": asdict(pending.packet),
        "response_ref": asdict(pending.response),
        "review_ref": asdict(pending.review),
        "acceptance_ref": asdict(pending.acceptance),
        "originals": packet["originals"],
        "decisions": decisions,
        "accepted_ids": {codec.full_id(c["id"]) for c in projected["selection_composition"]["cards"]},
    }


def validate_ready(
    data: dict[str, Any],
    projected: dict[str, Any],
    projection_ref: storage.Reference,
    acceptance_ref: storage.Reference,
) -> None:
    from digest.application.native_selection_lifecycle import build_lifecycle_record

    if data["schema"] != 4 or projected["publication"] != storage.publication_fields(data):
        raise ValueError("Selection ready differs from exact approved publication")
    refs = {k: restore_dataclass(v, storage.Reference) for k, v in projected["references"].items()}
    for field, ref in refs.items():
        if projected["inputs"][field + "_sha256"] != ref.sha256 or data["checkpoint_refs"].get(ref.path) != ref.sha256:
            raise ValueError("Selection ready input checkpoint differs")
        storage.read_reference(ref)
    packet, _ = source_packet(storage.read_reference(refs["packet"]))
    pending = storage.PendingNative(
        2,
        packet["owner_sha256"],
        packet["publication_date"],
        refs["packet"],
        refs["response"],
        refs["review"],
        projection_ref,
        acceptance_ref,
    )
    config: Config = restore_dataclass(packet["effective_configuration"], Config)
    if (
        projection(pending, storage.read_reference(refs["response"]), storage.read_reference(refs["review"]), config)
        != projected
    ):
        raise ValueError("Selection ready is not the retained native rendering")
    record = build_lifecycle_record(**lifecycle_arguments(pending, projected))
    raw = encode(record)
    from digest.adapters.storage.edition import content_sha256

    sha = content_sha256(raw)
    path = str(Path(refs["packet"].path).parent / (sha + ".selection-lifecycle.json"))
    if data["checkpoint_refs"].get(path) != sha or storage.read_reference(storage.Reference(path, sha)) != raw:
        raise ValueError("Selection ready lacks exact lifecycle checkpoint")
