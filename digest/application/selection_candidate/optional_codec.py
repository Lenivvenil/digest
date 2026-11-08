"""Inactive full-pool optional codecs; no calls, storage, dispatch or publication.

Main-role execution and exact-message acceptance binding belongs to the transport.
Here compact IDs/hashes are copied by workers and expanded only by owner code.
"""

from __future__ import annotations

import base64
import hashlib
import json
import unicodedata
from copy import deepcopy
from typing import Any

OPTIONAL_CONTRACT = (
    "Use bounded-selection-response/v3; add optional={closing:null|{id,text},"
    "comparison:null|{supports:[{id,observation},...],interpretation,qualification}}. "
    "Copy row IDs only; owner resolves occurrence hashes within the pinned pool. "
    "Closing: enabled, optional_policy.closing_ids_base64url non-main ID; plain text "
    "1..600 chars; do not force. Comparison: enabled, two distinct-source mains in "
    "decision order; plain observations 1..180, interpretation 1..240, qualification "
    "null or 1..160 chars. Preserve attribution/limits. publish=false: generate/review, never render."
)


def _encode(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _sha(value: Any) -> str:
    return hashlib.sha256(_encode(value)).hexdigest()


def _compact(value: str) -> str:
    return base64.urlsafe_b64encode(bytes.fromhex(value)).decode().rstrip("=")


def canonical_id(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("Optional compact ID must be a string")
    try:
        raw = base64.b64decode(value + "==", altchars=b"-_", validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("Invalid optional compact ID") from exc
    if len(raw) != 16 or _compact(raw.hex()) != value:
        raise ValueError("Invalid optional compact ID")
    return raw.hex()


def _plain(value: Any, maximum: int) -> None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > maximum
        or any(unicodedata.category(c) == "Cc" or c in "\u2028\u2029" for c in value)
    ):
        raise ValueError("Invalid optional plain text")


def validate_policy(policy: dict[str, Any], originals: dict[str, dict[str, Any]]) -> None:
    if not isinstance(policy, dict) or set(policy) != {
        "closing_enabled",
        "closing_ids",
        "comparison_enabled",
        "comparison_publish",
    }:
        raise ValueError("Exact optional policy required")
    if any(type(policy[k]) is not bool for k in ("closing_enabled", "comparison_enabled", "comparison_publish")):
        raise ValueError("Optional activation flags must be booleans")
    ids = policy["closing_ids"]
    if (
        not isinstance(ids, list)
        or any(not isinstance(i, str) or i not in originals for i in ids)
        or len(ids) != len(set(ids))
        or not policy["closing_enabled"]
        and ids
    ):
        raise ValueError("Approved closing IDs must be unique exact pool identities")


def _source(value: dict[str, Any], originals: dict[str, dict[str, Any]]) -> dict[str, Any]:
    identity = canonical_id(value["id"])
    if identity not in originals or value["occurrence_sha256_base64url"] != _compact(_sha(originals[identity])):
        raise ValueError("Optional source occurrence binding mismatch")
    return originals[identity]


def validate_optional(
    optional: dict[str, Any],
    originals: dict[str, dict[str, Any]],
    decisions: list[dict[str, Any]],
    policy: dict[str, Any],
) -> None:
    """Validate only after the core has validated complete main decision coverage."""
    validate_policy(policy, originals)
    if not isinstance(optional, dict) or set(optional) != {"closing", "comparison"}:
        raise ValueError("Explicit closing/comparison slots required")
    main_ids = [d["id"] for d in decisions if d["status"] == "card"]
    closing = optional["closing"]
    if closing is not None:
        if not isinstance(closing, dict) or set(closing) != {"id", "occurrence_sha256_base64url", "text"}:
            raise ValueError("Closing schema")
        if (
            not policy["closing_enabled"]
            or canonical_id(closing["id"]) not in policy["closing_ids"]
            or closing["id"] in main_ids
        ):
            raise ValueError("Closing must be approved and distinct from proposed mains")
        _source(closing, originals)
        _plain(closing["text"], 600)
    comparison = optional["comparison"]
    if comparison is not None:
        if (
            not policy["comparison_enabled"]
            or not isinstance(comparison, dict)
            or set(comparison) != {"supports", "interpretation", "qualification"}
        ):
            raise ValueError("Comparison schema or activation policy")
        supports = comparison["supports"]
        if not isinstance(supports, list) or len(supports) != 2:
            raise ValueError("Comparison requires exactly two supports")
        sources = set()
        ids = []
        for support in supports:
            if not isinstance(support, dict) or set(support) != {"id", "occurrence_sha256_base64url", "observation"}:
                raise ValueError("Comparison support schema")
            original = _source(support, originals)
            if not isinstance(original["description"], str) or not original["description"].strip():
                raise ValueError("Comparison requires supplied excerpt support")
            sources.add((original["source"], original["source_url"]))
            ids.append(support["id"])
            _plain(support["observation"], 180)
        if len(set(ids)) != 2 or ids != [identity for identity in main_ids if identity in ids] or len(sources) != 2:
            raise ValueError("Comparison requires two distinct ordered main sources")
        _plain(comparison["interpretation"], 240)
        if comparison["qualification"] is not None:
            _plain(comparison["qualification"], 160)


def source_proposals(
    optional: dict[str, Any],
    originals: dict[str, dict[str, Any]],
    decisions: list[dict[str, Any]],
    policy: dict[str, Any],
) -> list[dict[str, Any]]:
    """Owner supplies exact optional hashes for reviewers to copy, not calculate."""
    validate_optional(optional, originals, decisions, policy)
    proposals = []
    for kind in ("closing", "comparison"):
        value = optional[kind]
        if value is None:
            continue
        supports = [value] if kind == "closing" else value["supports"]
        proposals.append(
            {
                "kind": kind,
                "value": deepcopy(value),
                "value_sha256": _sha(value),
                "publish": kind == "closing" or policy["comparison_publish"],
                "sources": [
                    {
                        "id": support["id"],
                        "occurrence_sha256": _sha(_source(support, originals)),
                        "original": deepcopy(_source(support, originals)),
                    }
                    for support in supports
                ],
            }
        )
    return proposals


def validate_verdicts(verdicts: list[dict[str, Any]], proposals: list[dict[str, Any]]) -> set[str]:
    """The calling transport separately binds reviewer identity and full message."""
    if not isinstance(verdicts, list):
        raise ValueError("Optional verdict list required")
    expected = {p["kind"]: p["value_sha256"] for p in proposals}
    seen = set()
    accepted = set()
    for verdict in verdicts:
        if not isinstance(verdict, dict) or set(verdict) != {"kind", "value_sha256", "verdict", "notes"}:
            raise ValueError("Optional verdict schema")
        kind = verdict["kind"]
        if (
            not isinstance(kind, str)
            or kind not in expected
            or kind in seen
            or verdict["value_sha256"] != expected[kind]
        ):
            raise ValueError("Optional verdict does not bind exact proposal")
        if verdict["verdict"] not in {"accept", "reject"}:
            raise ValueError("Explicit optional verdict required")
        _plain(verdict["notes"], 1000)
        seen.add(kind)
        if verdict["verdict"] == "accept":
            accepted.add(kind)
    if seen != set(expected):
        raise ValueError("Incomplete optional source review")
    return accepted


def expected_composition(
    optional: dict[str, Any],
    originals: dict[str, dict[str, Any]],
    decisions: list[dict[str, Any]],
    policy: dict[str, Any],
    verdicts: list[dict[str, Any]],
    accepted_main_ids: list[str],
) -> dict[str, Any]:
    """Return exact allowed optional data; final renderer owns placement and prose.

    A disabled-publication comparison remains in source evidence, never here.
    No main survivors means no optional publication, matching native semantics.
    """
    proposals = source_proposals(optional, originals, decisions, policy)
    accepted = validate_verdicts(verdicts, proposals)
    main_ids = [d["id"] for d in decisions if d["status"] == "card"]
    if len(set(accepted_main_ids)) != len(accepted_main_ids) or accepted_main_ids != [
        identity for identity in main_ids if identity in accepted_main_ids
    ]:
        raise ValueError("Accepted main IDs must retain proposed order")
    result: dict[str, Any] = {"closing": None, "comparison": None}
    if not accepted_main_ids:
        return result
    if "closing" in accepted:
        value = optional["closing"]
        result["closing"] = {"id": value["id"], "text": value["text"], "link": _source(value, originals)["link"]}
    if "comparison" in accepted and policy["comparison_publish"]:
        value = optional["comparison"]
        ids = [s["id"] for s in value["supports"]]
        if ids == [identity for identity in accepted_main_ids if identity in ids]:
            result["comparison"] = {
                "value": deepcopy(value),
                "links": [_source(s, originals)["link"] for s in value["supports"]],
            }
    return result


def validate_composition(
    actual: dict[str, Any],
    optional: dict[str, Any],
    originals: dict[str, dict[str, Any]],
    decisions: list[dict[str, Any]],
    policy: dict[str, Any],
    verdicts: list[dict[str, Any]],
    accepted_main_ids: list[str],
) -> None:
    expected = expected_composition(optional, originals, decisions, policy, verdicts, accepted_main_ids)
    if _encode(actual) != _encode(expected):
        raise ValueError("Optional composition differs from exact accepted publication policy")


def validate_writer_view(
    optional: dict[str, Any], writer: dict[str, Any], decisions: list[dict[str, Any]], policy: dict[str, Any]
) -> None:
    """Check copied wire bindings before owner-side complete-original validation."""
    rows = {r[0]: r for r in writer["rows"]}
    if not isinstance(optional, dict) or set(optional) != {"closing", "comparison"}:
        raise ValueError("Explicit closing/comparison slots required")
    main_ids = [d["id"] for d in decisions if d["status"] == "card"]
    closing = optional["closing"]
    if closing is not None:
        if not isinstance(closing, dict) or set(closing) != {"id", "occurrence_sha256_base64url", "text"}:
            raise ValueError("Closing schema")
        if (
            not policy["closing_enabled"]
            or canonical_id(closing["id"]) not in policy["closing_ids"]
            or closing["id"] in main_ids
        ):
            raise ValueError("Unapproved or main closing ID")
        _plain(closing["text"], 600)
    comparison = optional["comparison"]
    if comparison is not None:
        if (
            not policy["comparison_enabled"]
            or not isinstance(comparison, dict)
            or set(comparison) != {"supports", "interpretation", "qualification"}
            or not isinstance(comparison["supports"], list)
            or len(comparison["supports"]) != 2
        ):
            raise ValueError("Comparison schema or activation")
        ids = []
        for s in comparison["supports"]:
            if not isinstance(s, dict) or set(s) != {"id", "occurrence_sha256_base64url", "observation"}:
                raise ValueError("Comparison support schema")
            ids.append(s["id"])
            _plain(s["observation"], 180)
        if len(set(ids)) != 2 or ids != [identity for identity in main_ids if identity in ids]:
            raise ValueError("Comparison requires distinct ordered proposed mains")
        _plain(comparison["interpretation"], 240)
        if comparison["qualification"] is not None:
            _plain(comparison["qualification"], 160)
    supports = ([closing] if closing is not None else []) + (comparison["supports"] if comparison is not None else [])
    for s in supports:
        if s["id"] not in rows or s["occurrence_sha256_base64url"] != rows[s["id"]][1]:
            raise ValueError("Optional wire occurrence binding mismatch")


def derive_policy(originals: dict[str, dict[str, Any]], config: Any) -> dict[str, Any]:
    """Apply unchanged native source approvals to the entire eligible owner pool."""
    from digest.application.review_request import closing_source_bindings
    from digest.presentation.source_attribution import supports_source_attribution

    approved = closing_source_bindings(config.closing, config.enabled_sources)
    result = {
        "closing_enabled": config.closing.enabled,
        "closing_ids": [
            identity
            for identity, item in sorted(originals.items())
            if config.closing.enabled
            and (item["source"], item["source_url"], item["category"]) in approved
            and supports_source_attribution(item["source_url"])
        ],
        "comparison_enabled": config.synthesis.enabled,
        "comparison_publish": config.synthesis.publish,
    }
    validate_policy(result, originals)
    return result


def encode_policy(policy: dict[str, Any], originals: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Lossless writer view: avoid duplicating canonical and compact approved IDs."""
    validate_policy(policy, originals)
    return {k: deepcopy(v) for k, v in policy.items() if k != "closing_ids"} | {
        "closing_ids_base64url": [_compact(identity) for identity in policy["closing_ids"]]
    }


def decode_policy(wire: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(wire, dict) or set(wire) != {
        "closing_enabled",
        "closing_ids_base64url",
        "comparison_enabled",
        "comparison_publish",
    }:
        raise ValueError("Exact compact optional policy required")
    if any(
        type(wire[k]) is not bool for k in ("closing_enabled", "comparison_enabled", "comparison_publish")
    ) or not isinstance(wire["closing_ids_base64url"], list):
        raise ValueError("Invalid compact optional policy")
    ids = [canonical_id(identity) for identity in wire["closing_ids_base64url"]]
    if len(set(ids)) != len(ids) or not wire["closing_enabled"] and ids:
        raise ValueError("Invalid compact closing identities")
    return {k: deepcopy(v) for k, v in wire.items() if k != "closing_ids_base64url"} | {"closing_ids": ids}
