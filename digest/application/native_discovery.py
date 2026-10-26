"""Data-only, explicitly selected discovery prefill; no generation, activation or send.

Worker references are runtime attestations, not authentication by model output.
The runtime persists the reservation before prepare in the same uninterrupted job.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from digest._serialization import canonical_json_bytes, unique_object
from digest.adapters.storage import native as checkpoints
from digest.adapters.storage.issue_paths import safe_issue_path
from digest.domain.catalog.exploration import (
    expire_pending_proposal,
    finish_pruning,
    prune_recent_metadata,
    select_exploration_area,
)
from digest.domain.catalog.proposals import PendingSource, resolve_pending_proposal

if TYPE_CHECKING:
    from digest.application.discovery import DiscoverySession
    from digest.config import Config

MAX_BYTES = 256_000
RESPONSE_MAX_BYTES = 8192


class NativeDiscoveryUnavailable(ValueError):
    """An absent, stale, unbound or previously attempted prefill must not fall back."""


@dataclass(frozen=True)
class NativeDiscoveryInput:
    prefill: Path
    prefill_sha256: str
    controller_ref: str
    reservation_sha256: str = ""


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _text(value: Any, limit: int) -> bool:
    return (isinstance(value, str) and 0 < len(value) <= limit and value == value.strip()
            and all(ord(char) >= 32 and char not in "|\u007f\u0085\u2028\u2029" for char in value))


def _object(raw: bytes, keys: set[str], maximum: int = MAX_BYTES) -> dict[str, Any]:
    if not 0 < len(raw) <= maximum:
        raise ValueError("Native discovery byte bound exceeded.")
    value = json.loads(raw, object_pairs_hook=unique_object)
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError("Native discovery fields differ from the versioned schema.")
    return value


def _read(path: Path) -> bytes:
    path = safe_issue_path(path)
    if not 0 < path.stat().st_size <= MAX_BYTES:
        raise ValueError("Native discovery byte bound exceeded.")
    raw = path.read_bytes()
    if not 0 < len(raw) <= MAX_BYTES:
        raise ValueError("Native discovery byte bound exceeded.")
    return raw


def _instant(value: str) -> datetime:
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo != timezone.utc or stamp.isoformat() != value:
        raise ValueError("Native discovery requires canonical UTC timestamps.")
    return stamp


def _exclusive(path: Path, value: dict[str, Any]) -> str:
    """Publish one bounded record atomically, exclusively and with local fsync."""
    raw = canonical_json_bytes(value)
    if len(raw) > MAX_BYTES:
        raise ValueError("Native discovery byte bound exceeded.")
    path = safe_issue_path(path)
    path.relative_to(Path.cwd().resolve())  # Reject outside-repository output before any mkdir/write.
    path.parent.mkdir(parents=True, exist_ok=True)
    reference = checkpoints.put_bytes(raw, path.parent, "record")
    os.link(safe_issue_path(Path(reference.path)), path)
    checkpoints._sync(path.parent)
    return _sha(raw)


def proposal_context(
    config: Config, pending: list[PendingSource], data: dict[str, Any], requested_area: str,
) -> dict[str, Any]:
    """Preserve the legacy provider prompt's field order and values exactly."""
    categories: dict[str, list[str]] = {}
    for source in config.enabled_sources:
        categories.setdefault(source.category, []).append(source.name)
    return {
        "requested_exploration_area": requested_area,
        "exploration_areas": config.discovery.exploration_areas,
        "categories": categories,
        "configured_urls": sorted({source.url for source in config.sources}),
        "pending_urls": [source.url for source in pending],
        "history": [{key: item[key] for key in ("url", "category", "decision")} for item in data["history"]],
    }


def context(config: Config, pending: list[PendingSource], data: dict[str, Any]) -> dict[str, Any]:
    """Pure semantic snapshot; no main commit, runtime cycle or recipient secrets."""
    preview = copy.deepcopy(data)
    area = select_exploration_area(preview, config.discovery.exploration_areas)
    return {
        "proposal_context": proposal_context(config, pending, data, area),
        "source_registry": [asdict(source) for source in config.sources],
        "pending": [asdict(source) for source in pending],
        # Previewed expiry creates a recorded_at at each observation. Bind decision
        # identity/membership, not that synthetic timestamp; pruning still detects
        # a real history/pending expiry boundary or changed decision.
        "history": [{key: item[key] for key in ("binding", "url", "name", "category", "decision")}
                    for item in data["history"]],
        "area_offers": copy.deepcopy(data["area_offers"]),
        "attempted_areas": list(data["attempted_areas"]),
    }


def preview_context(config: Config, cache: Path, now: datetime) -> dict[str, Any]:
    """Preview the same expiry/rotation policy without changing retained state."""
    from digest.adapters.storage.discovery import load_delivery
    from digest.adapters.storage.pending_sources import load_pending

    pending = load_pending(str(cache), strict=True)
    data = load_delivery(str(cache))
    prune_recent_metadata(data, config.discovery.exploration_areas, now=now)
    kept = []
    for source in pending:
        if expire_pending_proposal(data, source, now=now):
            continue
        if resolve_pending_proposal(pending, source.source_hash, now=now) is None:
            raise ValueError("Ambiguous or invalid pending source identity.")
        kept.append(source)
    finish_pruning(data, kept)
    return context(config, kept, data)


def _slot_path(cache: Path, slot: str) -> Path:
    # Canonical timestamp validation precedes this fixed, non-model-selected path.
    instant = _instant(slot)
    if instant.second or instant.microsecond:
        raise ValueError("Native discovery scheduled slot must identify a whole UTC minute.")
    return cache / "native_discovery" / instant.strftime("%Y-%m-%dT%H-%M-%SZ")


def export_request(
    config: Config, *, cache: Path, slot: datetime, controller_ref: str, now: datetime | None = None,
) -> checkpoints.Reference:
    """Retain one immutable request/intent; never launch or regenerate a worker."""
    instant = _instant((now or datetime.now(timezone.utc)).isoformat())
    expiry = (slot + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    if not _text(controller_ref, 256) or not slot - timedelta(days=1) <= instant < expiry:
        raise ValueError("Native discovery needs an owner and a current/upcoming UTC slot day.")
    slot_text = _instant(slot.isoformat()).isoformat()
    snapshot = preview_context(config, cache, instant)
    request = {
        "schema": "native-discovery-request/v1", "controller_ref": controller_ref,
        "slot": slot_text, "expires_at": expiry.isoformat(),
        "created_at": instant.isoformat(), "context": snapshot,
        "context_sha256": _sha(canonical_json_bytes(snapshot)),
    }
    path = _slot_path(cache, slot_text) / "request.json"
    if path.exists():
        existing = _request(_read(path))
        if any(existing[key] != request[key] for key in request if key != "created_at"):
            raise ValueError("Native discovery slot already has a different immutable request.")
    else:
        _exclusive(path, request)
    return checkpoints.reference_file(path)


def _request(raw: bytes) -> dict[str, Any]:
    value = _object(raw, {"schema", "controller_ref", "slot", "expires_at", "created_at", "context", "context_sha256"})
    if (value["schema"] != "native-discovery-request/v1" or not _text(value["controller_ref"], 256)
            or not isinstance(value["context"], dict)
            or value["context_sha256"] != _sha(canonical_json_bytes(value["context"]))):
        raise ValueError("Invalid native discovery request binding.")
    slot, created, expires = (_instant(value[key]) for key in ("slot", "created_at", "expires_at"))
    expiry = (slot + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    if not slot - timedelta(days=1) <= created < expires or expires != expiry:
        raise ValueError("Invalid native discovery request window.")
    return value


def _response(raw: bytes, request_sha: str, execution_ref: str) -> list[dict[str, str]]:
    value = _object(raw, {"schema", "request_sha256", "execution_ref", "proposals"}, RESPONSE_MAX_BYTES)
    proposals = value["proposals"]
    if (value["schema"] != "native-discovery-response/v1" or value["request_sha256"] != request_sha
            or not _text(execution_ref, 256) or value["execution_ref"] != execution_ref
            or not isinstance(proposals, list) or len(proposals) > 3):
        raise ValueError("Invalid native discovery response binding.")
    for proposal in proposals:
        if (not isinstance(proposal, dict) or set(proposal) != {"url", "category", "name"}
                or any(not _text(proposal[key], limit)
                       for key, limit in (("url", 2048), ("category", 80), ("name", 160)))):
            raise ValueError("Invalid native discovery proposal fields.")
    return proposals


def bind_prefill(
    request_path: Path, response_path: Path, *, execution_ref: str, controller_ref: str,
    cache: Path, now: datetime | None = None,
) -> checkpoints.Reference:
    """Bind observed worker provenance and raw bytes, not semantic acceptance.

    Only the trusted controller supplies execution_ref after observing that worker's
    terminal output. A model self-assertion or Git author is not that observation.
    """
    instant = _instant((now or datetime.now(timezone.utc)).isoformat())
    request_raw, response_raw = _read(request_path), _read(response_path)
    request = _request(request_raw)
    if (request["controller_ref"] != controller_ref
            or not _instant(request["created_at"]) <= instant < _instant(request["expires_at"])):
        raise ValueError("Native prefill owner changed or result arrived after its UTC slot day.")
    _response(response_raw, _sha(request_raw), execution_ref)
    value = {
        "schema": "native-discovery-prefill/v1", "request_raw": request_raw.decode("utf-8"),
        "request_sha256": _sha(request_raw), "response_raw": response_raw.decode("utf-8"),
        "response_sha256": _sha(response_raw), "execution_ref": execution_ref, "bound_at": instant.isoformat(),
    }
    path = _slot_path(cache, request["slot"]) / "prefill.json"
    _exclusive(path, value)  # No replacement/rebinding, including a different worker.
    return checkpoints.reference_file(path)


def _prefill(native: NativeDiscoveryInput, now: datetime) -> tuple[dict[str, Any], list[dict[str, str]]]:
    raw = _read(native.prefill)
    if not re.fullmatch(r"[0-9a-f]{64}", native.prefill_sha256) or _sha(raw) != native.prefill_sha256:
        raise ValueError("Native discovery trusted prefill hash mismatch.")
    value = _object(raw, {"schema", "request_raw", "request_sha256", "response_raw", "response_sha256",
                          "execution_ref", "bound_at"})
    if value["schema"] != "native-discovery-prefill/v1":
        raise ValueError("Unsupported native discovery prefill.")
    request_raw, response_raw = value["request_raw"].encode("utf-8"), value["response_raw"].encode("utf-8")
    request = _request(request_raw)
    if (value["request_sha256"] != _sha(request_raw) or value["response_sha256"] != _sha(response_raw)
            or request["controller_ref"] != native.controller_ref
            or not _instant(request["created_at"]) <= _instant(value["bound_at"]) < _instant(request["expires_at"])
            or _instant(value["bound_at"]) > now
            or not _instant(request["slot"]) <= now < _instant(request["expires_at"])):
        raise ValueError("Native discovery prefill is stale, early, or unbound.")
    return request, _response(response_raw, value["request_sha256"], value["execution_ref"])


def reserve_prefill(session: DiscoverySession, native: NativeDiscoveryInput, *, now: datetime | None = None) -> str:
    """Reserve once; runtime must persist/read back the exact record before prepare."""
    instant = _instant((now or datetime.now(timezone.utc)).isoformat())
    try:
        request, _ = _prefill(native, instant)
    except (FileNotFoundError, ValueError, TypeError, KeyError, AttributeError) as exc:
        raise NativeDiscoveryUnavailable(str(exc)) from exc
    cache = Path(session.cache_dir)
    # Corrupt operational pending/delivery state is not an ordinary unavailable prefill.
    if request["context"] != preview_context(session.config, cache, instant):
        raise NativeDiscoveryUnavailable("Native discovery context changed before reservation.")
    retained = checkpoints.put_bytes(_read(native.prefill), cache, "discovery-prefill")
    record = {
        "schema": "native-discovery-reservation/v1", "slot": request["slot"],
        "owner": session.owner, "cycle": session.cycle, "controller_ref": native.controller_ref,
        "prefill": asdict(retained), "reserved_at": instant.isoformat(),
    }
    try:
        return _exclusive(_slot_path(cache, request["slot"]) / "reserved.json", record)
    except FileExistsError as exc:
        raise NativeDiscoveryUnavailable("Native discovery slot already reserved; hold generation.") from exc


def consume_prefill(
    session: DiscoverySession, native: NativeDiscoveryInput, snapshot: dict[str, Any], now: datetime, remaining: int,
) -> str:
    """Admit only a persisted, owned reservation once, then use the ordinary parser."""
    try:
        request, proposals = _prefill(native, now)
        root = _slot_path(Path(session.cache_dir), request["slot"])
        raw = _read(root / "reserved.json")
        record = _object(raw, {"schema", "slot", "owner", "cycle", "controller_ref", "prefill", "reserved_at"})
        if (not native.reservation_sha256 or _sha(raw) != native.reservation_sha256
                or record["schema"] != "native-discovery-reservation/v1" or record["slot"] != request["slot"]
                or record["owner"] != session.owner or record["cycle"] != session.cycle
                or record["controller_ref"] != native.controller_ref or request["context"] != snapshot
                or not _instant(request["slot"]) <= _instant(record["reserved_at"]) <= now
                or type(remaining) is not int or not 1 <= remaining <= 3):
            raise ValueError("Native discovery reservation, owner or context mismatch.")
        reference = record["prefill"]
        if (not isinstance(reference, dict) or set(reference) != {"path", "sha256"}
                or reference["sha256"] != native.prefill_sha256
                or checkpoints.read_reference(checkpoints.Reference(**reference)) != _read(native.prefill)):
            raise ValueError("Native discovery retained prefill changed.")
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        raise NativeDiscoveryUnavailable(str(exc)) from exc
    try:
        _exclusive(root / "started.json", {
            "schema": "native-discovery-consumption/v1", "reservation_sha256": native.reservation_sha256,
            "prefill_sha256": native.prefill_sha256, "owner": session.owner, "cycle": session.cycle,
            "started_at": now.isoformat(), "admitted_count": min(remaining, len(proposals)),
            "deferred": proposals[remaining:],
        })
    except FileExistsError as exc:
        raise NativeDiscoveryUnavailable("Native discovery consumption was already attempted; hold.") from exc
    return "\n".join("FEED|{url}|{category}|{name}".format(**proposal) for proposal in proposals[:remaining])
