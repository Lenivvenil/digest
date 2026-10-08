"""Discovery schema-1 metadata, bounded local writes and exact persisted-pair hashes."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from digest._util import atomic_json_write
from digest.adapters.storage.pending_sources import PENDING_FILE
from digest.domain.catalog.exploration import metadata_stamp as _metadata_stamp

DELIVERY_FILE = "discovery_delivery.json"
METADATA_MAX_BYTES = 256000


def load_delivery(cache_dir: str) -> dict[str, Any]:
    path = Path(cache_dir) / DELIVERY_FILE
    if not path.exists():
        data: dict[str, Any] = {"schema_version": 1, "deliveries": {}, "history": [], "batch": None}
        _load_exploration_metadata(data)
        return data
    if path.stat().st_size > METADATA_MAX_BYTES:
        raise ValueError("Discovery metadata exceeds its storage bound.")
    data = json.loads(path.read_text())
    if (
        not isinstance(data, dict)
        or data.get("schema_version") != 1
        or not isinstance(data.get("deliveries"), dict)
        or not isinstance(data.get("history"), list)
        or data.get("batch") is not None
        and not isinstance(data["batch"], dict)
    ):
        raise ValueError("Invalid discovery delivery metadata.")
    for key, value in data["deliveries"].items():
        if (
            not isinstance(key, str)
            or not re.fullmatch(r"[a-f0-9]{64}", key)
            or not isinstance(value, dict)
            or value.get("status") not in {"reserved", "confirmed", "rejected", "unknown"}
            or not isinstance(value.get("updated_at"), str)
        ):
            raise ValueError("Invalid discovery delivery receipt.")
        if value["status"] == "confirmed" and (type(value.get("message_id")) is not int or value["message_id"] <= 0):
            raise ValueError("Confirmed proposal delivery requires a message receipt.")
        datetime.fromisoformat(value["updated_at"])
    for item in data["history"]:
        if (
            not isinstance(item, dict)
            or item.get("decision") not in {"approved", "rejected", "expired"}
            or any(not isinstance(item.get(key), str) for key in ("binding", "url", "name", "category", "recorded_at"))
        ):
            raise ValueError("Invalid discovery history.")
        datetime.fromisoformat(item["recorded_at"])
    _load_exploration_metadata(data)
    return data


def _area_name(value: Any) -> bool:
    return isinstance(value, str) and 1 <= len(value) <= 80 and value == value.strip()


def _binding(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None


def _cycle(value: Any) -> bool:
    return isinstance(value, str) and 1 <= len(value) <= 160


def _load_exploration_metadata(data: dict[str, Any]) -> None:
    """Optional schema-1 additions never change legacy proposal identities."""
    for key in ("proposal_areas", "area_offers", "validation_failures"):
        if not isinstance(data.setdefault(key, {}), dict):
            raise ValueError(f"Invalid discovery {key} metadata.")
    for binding, area in data["proposal_areas"].items():
        if not _binding(binding) or not _area_name(area):
            raise ValueError("Invalid discovery proposal area.")
    if len(data["area_offers"]) > 16:
        raise ValueError("Discovery area offers exceed the configured area bound.")
    for area, offer in data["area_offers"].items():
        if not _area_name(area) or not isinstance(offer, dict) or offer.get("status") not in ("confirmed", "unknown"):
            raise ValueError("Invalid discovery area offer.")
        _metadata_stamp(offer.get("updated_at"))
    attempted = data.setdefault("attempted_areas", [])
    if (
        not isinstance(attempted, list)
        or len(attempted) > 16
        or any(not _area_name(area) for area in attempted)
        or len(set(attempted)) != len(attempted)
    ):
        raise ValueError("Invalid discovery attempted areas.")
    for binding, failure in data["validation_failures"].items():
        if (
            not _binding(binding)
            or not isinstance(failure, dict)
            or not _cycle(failure.get("failed_cycle"))
            or "skipped_cycle" not in failure
            or failure["skipped_cycle"] is not None
            and (not _cycle(failure["skipped_cycle"]) or failure["skipped_cycle"] == failure["failed_cycle"])
        ):
            raise ValueError("Invalid discovery validation failure.")
        _metadata_stamp(failure.get("failed_at"))
    generation = data.setdefault("generation", None)
    if generation is not None:
        if (
            not isinstance(generation, dict)
            or not _area_name(generation.get("requested_area"))
            or not _cycle(generation.get("cycle"))
            or generation.get("outcome") not in ("started", "failed", "empty", "no_valid_proposals", "proposed")
            or not isinstance(generation.get("bindings"), list)
            or len(generation["bindings"]) > 3
            or any(not _binding(binding) for binding in generation["bindings"])
            or len(set(generation["bindings"])) != len(generation["bindings"])
        ):
            raise ValueError("Invalid discovery generation record.")
        _metadata_stamp(generation.get("requested_at"))


def save_delivery(data: dict[str, Any], cache_dir: str) -> None:
    if len(json.dumps(data, indent=2).encode("utf-8")) > METADATA_MAX_BYTES:
        raise ValueError("Discovery metadata exceeds its storage bound.")
    path = Path(cache_dir) / DELIVERY_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json_write(path, data)


def pending_sha256(cache_dir: str) -> str:
    return hashlib.sha256((Path(cache_dir) / PENDING_FILE).read_bytes()).hexdigest()


def delivery_sha256(cache_dir: str) -> str:
    return hashlib.sha256((Path(cache_dir) / DELIVERY_FILE).read_bytes()).hexdigest()


def verify_persisted_pair(cache_dir: str, pending_sha: str | None, delivery_sha: str | None) -> None:
    """Retain exact bytes and short-circuit read order at the persisted-pair barrier."""
    if (
        not pending_sha
        or not delivery_sha
        or pending_sha256(cache_dir) != pending_sha
        or delivery_sha256(cache_dir) != delivery_sha
    ):
        raise ValueError("Discovery persisted pair hash mismatch.")
