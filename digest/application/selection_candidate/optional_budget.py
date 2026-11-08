"""Offline conservative optional role sizing; synthetic maxima, never reviews."""

from __future__ import annotations

import hashlib
from copy import deepcopy
from typing import Any

from digest.application.selection_candidate import optional_codec as optional


def preflight(
    core: Any,
    message: dict[str, Any],
    articles: dict[str, dict[str, Any]],
    *,
    source_output_transport: dict[str, Any],
    final_output_transport: dict[str, Any],
) -> dict[str, int]:
    writer = message["writer_input"]
    policy = optional.decode_policy(message["optional_policy"])
    optional.validate_policy(policy, articles)

    def size(identity: str) -> int:
        return len(core.encode(core.encode(articles[identity]).decode()))

    largest = sorted(articles, key=size, reverse=True)
    closing_id = max(policy["closing_ids"], key=size) if policy["closing_enabled"] and policy["closing_ids"] else None
    chosen = [identity for identity in largest if identity != closing_id][: writer["max_cards"]]
    # Keep comparison possible without pretending that same-source supports pass.
    if policy["comparison_enabled"] and len(chosen) >= 2:
        first_source = (articles[chosen[0]]["source"], articles[chosen[0]]["source_url"])
        other = next(
            (
                identity
                for identity in largest
                if identity != closing_id
                and (articles[identity]["source"], articles[identity]["source_url"]) != first_source
                and articles[identity]["description"].strip()
            ),
            None,
        )
        if other is not None and other not in chosen:
            chosen[-1] = other
    decisions = [
        {
            "id": row[0],
            "status": "card" if core.full_id(row[0]) in chosen else "capacity_deferred",
            "text": core.worst_field(core.MAX_CARD_UTF8_BYTES) if core.full_id(row[0]) in chosen else "",
            "reason": core.worst_field(core.MAX_REASON_UTF8_BYTES),
            "duplicate_of": None,
        }
        for row in writer["rows"]
    ]
    main_ids = [d["id"] for d in decisions if d["status"] == "card"]
    values: dict[str, Any] = {"closing": None, "comparison": None}
    if closing_id is not None:
        identity = core.short_id(closing_id)
        values["closing"] = {
            "id": identity,
            "occurrence_sha256_base64url": core.short_hash(core.sha(articles[core.full_id(identity)])),
            "text": '"' * 600,
        }
    pair = []
    sources = set()
    for identity in main_ids:
        original = articles[core.full_id(identity)]
        source = (original["source"], original["source_url"])
        if source not in sources and original["description"].strip():
            pair.append(identity)
            sources.add(source)
        if len(pair) == 2:
            break
    if policy["comparison_enabled"] and len(pair) == 2:
        values["comparison"] = {
            "supports": [
                {
                    "id": identity,
                    "occurrence_sha256_base64url": core.short_hash(core.sha(articles[core.full_id(identity)])),
                    "observation": '"' * 180,
                }
                for identity in pair
            ],
            "interpretation": '"' * 240,
            "qualification": '"' * 160,
        }
    raw_values = deepcopy(values)
    if raw_values["closing"] is not None:
        raw_values["closing"].pop("occurrence_sha256_base64url")
    if raw_values["comparison"] is not None:
        for support in raw_values["comparison"]["supports"]:
            support.pop("occurrence_sha256_base64url")
    response = {
        "schema": "bounded-selection-response/v3",
        "execution_ref": "W" * 250,
        "role_message_sha256": message["role_message_sha256"],
        "pool_sha256": writer["pool_sha256"],
        "writer_payload_sha256": core.sha(writer),
        "decisions": decisions,
        "optional": raw_values,
    }
    payload = core.make_source_review(
        writer,
        response,
        articles,
        publication_date=writer["publication_date"],
        actual_writer_execution_ref="W" * 250,
        writer_message=message,
        expected_issued_writer_message_sha256=message["role_message_sha256"],
    )
    source = core.complete_review_message(
        payload,
        safety_rules=message["safety_rules"],
        closing_rules=message["closing_qualification"],
        output_transport=source_output_transport,
    )
    from digest.application.selection_candidate.shared_original_codec import unpack

    payload = unpack(payload)
    acceptance = {
        "schema": "bounded-selection-acceptance/v2",
        "role": "source-reviewer",
        "role_message_sha256": source["role_message_sha256"],
        "input_sha256": source["review_input_sha256"],
        "execution_ref": "S" * 250,
        "verdict": "accept",
        "notes": core.worst_field(512),
        "card_verdicts": [
            {
                "id": p["decision"]["id"],
                "text_sha256": p["text_sha256"],
                "verdict": "accept",
                "notes": core.worst_field(256),
            }
            for p in payload["proposals"]
        ],
        "optional_verdicts": [
            {"kind": p["kind"], "value_sha256": p["value_sha256"], "verdict": "accept", "notes": core.worst_field(256)}
            for p in payload["optional_proposals"]
        ],
    }
    composition = {
        "cards": [
            {"id": p["decision"]["id"], "text": p["decision"]["text"], "link": p["original"]["link"]}
            for p in payload["proposals"]
        ],
        "rendered_text": core.worst_field(16000),
        "optional": optional.expected_composition(
            values, articles, decisions, policy, acceptance["optional_verdicts"], main_ids
        ),
    }
    final = core.make_final_review(
        source,
        acceptance,
        composition,
        actual_source_execution_ref="S" * 250,
        actual_writer_execution_ref="W" * 250,
        safety_rules=message["safety_rules"],
        closing_rules=message["closing_qualification"],
        output_transport=final_output_transport,
        expected_issued_source_message_sha256=source["role_message_sha256"],
        expected_owner_rendered_text_sha256=hashlib.sha256(composition["rendered_text"].encode()).hexdigest(),
    )

    # The valid scenario may swap a largest occurrence for a distinct-source or
    # non-closing one. Add back the complete difference and maximum row-index
    # digits so this cannot understate the five-largest-original baseline.
    def ref_size(identity: str) -> int:
        index = next(i for i, row in enumerate(writer["rows"]) if core.full_id(row[0]) == identity)
        original = articles[identity]
        ref = {
            "triage_row_index": index,
            "description_suffix": original["description"][len(writer["rows"][index][5]) :],
            "remaining_fields": {
                k: v
                for k, v in original.items()
                if k not in {"title", "description", "source", "category", "published"}
            },
        }
        return len(core.encode(core.encode(ref).decode()))

    largest_refs = sorted(articles, key=ref_size, reverse=True)
    original_reserve = max(
        0, sum(ref_size(i) for i in largest_refs[: writer["max_cards"]]) - sum(ref_size(i) for i in chosen)
    )
    if closing_id is not None:
        original_reserve += max(ref_size(i) for i in policy["closing_ids"]) - ref_size(closing_id)

    def link_size(identity: str) -> int:
        return len(core.encode(core.encode(articles[identity]["link"]).decode()))

    def final_size(identity: str) -> int:
        return size(identity) + link_size(identity)

    final_largest = sorted(articles, key=final_size, reverse=True)
    final_reserve = max(
        0, sum(final_size(i) for i in final_largest[: writer["max_cards"]]) - sum(final_size(i) for i in chosen)
    )
    if closing_id is not None:
        final_reserve += max(final_size(i) for i in policy["closing_ids"]) - final_size(closing_id)
    if policy["comparison_publish"] and values["comparison"] is not None:
        final_reserve += max(
            0,
            sum(sorted((link_size(i) for i in articles), reverse=True)[:2])
            - sum(link_size(core.full_id(i)) for i in pair),
        )
    index_reserve = writer["max_cards"] * len(str(max(0, len(articles) - 1)))
    sizes = {
        role + "_wire_bytes": len(core.encode(core.actual_tool_message(value)))
        for role, value in [("writer", message), ("source", source), ("final", final)]
    }
    sizes["source_wire_bytes"] += original_reserve + index_reserve
    sizes["final_wire_bytes"] += final_reserve + index_reserve
    if any(value > core.MAX_BYTES for value in sizes.values()):
        raise core.BudgetExceeded("Conservative optional role preflight exceeds 128KiB; HOLD")
    return {
        **sizes,
        "response_bytes": len(core.encode(response)),
        "originals_tested": len(chosen),
        "optional_proposals_tested": len(payload["optional_proposals"]),
        "original_reserve_bytes": original_reserve,
        "final_reserve_bytes": final_reserve,
        "index_reserve_bytes": index_reserve,
    }
