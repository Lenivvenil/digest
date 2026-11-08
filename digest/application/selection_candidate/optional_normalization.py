"""Owner-only normalization for an ID-only, null-occurrence writer transport.

This does not authorize execution or replace core validation.
The core validates the exact issued writer against externally captured immutable
originals, plus the raw response against its actual execution identity.
The transport is imported lazily to avoid initialization cycles. No I/O or mutation.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, TypedDict

from digest.application.selection_candidate import optional_codec as codec

RAW_OPTIONAL_CONTRACT = (
    "Use bounded-selection-response/v3; optional={closing:null|{id,text},"
    "comparison:null|{supports:[{id,observation},{id,observation}],interpretation,"
    "qualification}}. Copy row IDs only; row slot 1 is null. Closing: enabled, "
    "approved non-main ID; plain text 1..600 chars. Comparison: enabled, two "
    "distinct-source mains in decision order; observations 1..180, interpretation "
    "1..240, qualification null or 1..160 chars. Preserve attribution/limits. "
    "publish=false: generate/review, never render."
)


class Provenance(TypedDict):
    canonical_response_sha256: str
    issued_writer_message_sha256: str
    pool_sha256: str


class NormalizedOptionalView(TypedDict):
    schema: str
    provenance: Provenance
    optional: dict[str, Any]


def _rows(writer: dict[str, Any]) -> dict[str, list[Any]]:
    rows = writer.get("rows")
    if not isinstance(rows, list):
        raise ValueError("Writer rows required")
    result: dict[str, list[Any]] = {}
    for row in rows:
        if not isinstance(row, list) or len(row) != 6 or row[1] is not None:
            raise ValueError("ID-only writer rows require null occurrence slot")
        codec.canonical_id(row[0])
        if row[0] in result:
            raise ValueError("Duplicate writer ID")
        result[row[0]] = row
    return result


def validate_raw_optional(
    optional: dict[str, Any],
    writer: dict[str, Any],
    decisions: list[dict[str, Any]],
    policy: dict[str, Any],
) -> None:
    """Validate ID-only schema/policy/order/text after core decision validation.

    Exact distinct source identity is checked with original source+source_url in
    normalization. The compressed writer source table omits source_url, so it
    cannot safely prove that property on its own.
    """
    rows = _rows(writer)
    codec.validate_policy(policy, {codec.canonical_id(i): {} for i in rows})
    if not isinstance(optional, dict) or set(optional) != {"closing", "comparison"}:
        raise ValueError("Explicit closing/comparison slots required")
    main_ids = [d["id"] for d in decisions if d["status"] == "card"]
    closing = optional["closing"]
    if closing is not None:
        if not isinstance(closing, dict) or set(closing) != {"id", "text"}:
            raise ValueError("Raw closing schema")
        identity = codec.canonical_id(closing["id"])
        if (
            closing["id"] not in rows
            or not policy["closing_enabled"]
            or identity not in policy["closing_ids"]
            or closing["id"] in main_ids
        ):
            raise ValueError("Closing must be approved pool non-main")
        codec._plain(closing["text"], 600)
    comparison = optional["comparison"]
    if comparison is not None:
        if (
            not policy["comparison_enabled"]
            or not isinstance(comparison, dict)
            or set(comparison) != {"supports", "interpretation", "qualification"}
        ):
            raise ValueError("Raw comparison schema or activation")
        supports = comparison["supports"]
        if not isinstance(supports, list) or len(supports) != 2:
            raise ValueError("Exactly two comparison supports required")
        ids = []
        for support in supports:
            if not isinstance(support, dict) or set(support) != {"id", "observation"}:
                raise ValueError("Raw support schema")
            codec.canonical_id(support["id"])
            if support["id"] not in rows:
                raise ValueError("Unknown comparison pool ID")
            row = rows[support["id"]]
            if not isinstance(row[5], str) or not row[5].strip():
                raise ValueError("Comparison requires supplied excerpt support")
            ids.append(support["id"])
            codec._plain(support["observation"], 180)
        if len(set(ids)) != 2 or ids != [i for i in main_ids if i in ids]:
            raise ValueError("Comparison requires two ordered distinct mains")
        codec._plain(comparison["interpretation"], 240)
        if comparison["qualification"] is not None:
            codec._plain(comparison["qualification"], 160)


def normalize_optional_response(
    raw_response: dict[str, Any],
    writer_message: dict[str, Any],
    originals: dict[str, dict[str, Any]],
    *,
    expected_issued_message_sha256: str,
    actual_writer_execution_ref: str,
) -> NormalizedOptionalView:
    """Produce a separate, deterministic legacy-compatible optional view.

    Pins passed in from the response itself are not trusted anchors. The result
    is owner-local evidence, not a replacement writer response.
    """
    from digest.application.selection_candidate import bounded_selection_transport as transport

    response, message, pool = deepcopy((raw_response, writer_message, originals))
    writer = message["writer_input"]
    transport.validate_writer(
        writer, pool, publication_date=writer["publication_date"], excerpt_limit=writer["excerpt_limit"]
    )
    transport.validate_response(
        response,
        writer,
        actual_writer_execution_ref=actual_writer_execution_ref,
        writer_message=message,
        expected_issued_message_sha256=expected_issued_message_sha256,
    )
    issued = codec._sha({k: v for k, v in message.items() if k != "role_message_sha256"})
    if issued != expected_issued_message_sha256 or message.get("role_message_sha256") != issued:
        raise ValueError("Externally issued full writer message pin mismatch")
    writer = message["writer_input"]
    rows = _rows(writer)
    if {codec.canonical_id(i) for i in rows} != set(pool):
        raise ValueError("Original pool identities differ from exact writer rows")
    pool_hash = codec._sha({i: codec._sha(a) for i, a in sorted(pool.items())})
    if writer.get("pool_sha256") != pool_hash:
        raise ValueError("Immutable original pool map hash mismatch")
    if (
        response.get("pool_sha256") != pool_hash
        or response.get("role_message_sha256") != issued
        or response.get("writer_payload_sha256") != codec._sha(writer)
        or message.get("writer_payload_sha256") != codec._sha(writer)
    ):
        raise ValueError("Raw response differs from pinned writer/pool")
    policy = codec.decode_policy(message["optional_policy"])
    optional = response["optional"]
    validate_raw_optional(optional, writer, response["decisions"], policy)
    enriched = deepcopy(optional)
    supports = [enriched["closing"]] if enriched["closing"] is not None else []
    if enriched["comparison"] is not None:
        supports += enriched["comparison"]["supports"]
    for support in supports:
        support["occurrence_sha256_base64url"] = codec._compact(codec._sha(pool[codec.canonical_id(support["id"])]))
    codec.validate_optional(enriched, pool, response["decisions"], policy)
    return {
        "schema": "bounded-selection-normalized-optional/v1",
        "provenance": {
            "canonical_response_sha256": codec._sha(response),
            "issued_writer_message_sha256": issued,
            "pool_sha256": pool_hash,
        },
        "optional": enriched,
    }
