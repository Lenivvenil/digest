"""One retained native handoff and its immutable input/acceptance checkpoints."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from digest._serialization import canonical_json_bytes, restore_dataclass, unique_object
from digest.adapters.storage.edition import content_sha256, write_record
from digest.adapters.storage.issue_paths import safe_issue_path
from digest.domain.delivery.edition import validate_checkpoint_reference
from digest.domain.delivery.supplement import PendingSupplement, PreparedSupplement

PENDING_FILE = "pending_native.json"
MAX_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True)
class Reference:
    path: str
    sha256: str


@dataclass(frozen=True)
class PendingNative:
    schema: int
    owner_sha256: str
    publication_date: str
    packet: Reference
    response: Reference | None = None
    review: Reference | None = None
    projection: Reference | None = None
    acceptance: Reference | None = None
    archive_evidence: Reference | None = None
    archive: Reference | None = None


def read_input(path: Path) -> bytes:
    path = safe_issue_path(path)
    if not 0 < path.stat().st_size <= MAX_BYTES:
        raise ValueError("Native checkpoint exceeds its byte bound.")
    raw = path.read_bytes()
    if not 0 < len(raw) <= MAX_BYTES:
        raise ValueError("Native checkpoint exceeds its byte bound.")
    return raw


def read_reference(reference: Reference) -> bytes:
    validate_checkpoint_reference(reference.path, reference.sha256)
    raw = read_input(Path.cwd() / reference.path)
    if content_sha256(raw) != reference.sha256:
        raise ValueError("Native checkpoint hash mismatch.")
    return raw


def decode(raw: bytes) -> dict[str, Any]:
    value = json.loads(raw, object_pairs_hook=unique_object)
    if not isinstance(value, dict):
        raise ValueError("Native checkpoint must be an object.")
    return value


def put_bytes(raw: bytes, cache: Path, label: str) -> Reference:
    if not 0 < len(raw) <= MAX_BYTES:
        raise ValueError("Native checkpoint exceeds its byte bound.")
    digest = content_sha256(raw)
    path = safe_issue_path(cache / "native" / f"{digest}.{label}.json")
    reference = Reference(path.resolve().relative_to(Path.cwd().resolve()).as_posix(), digest)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if read_reference(reference) != raw:
            raise ValueError("Native immutable checkpoint changed.")
        _sync(path.parent)
        return reference
    temporary = safe_issue_path(path.with_suffix(path.suffix + ".tmp"))
    with temporary.open("wb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    os.link(temporary, path)  # Atomic, exclusive publication; never replace immutable evidence.
    temporary.unlink()
    _sync(path.parent)
    return reference


def reference_file(path: Path) -> Reference:
    raw = read_input(path)
    with safe_issue_path(path).open("rb") as handle:
        os.fsync(handle.fileno())
    _sync(path.parent)
    reference = Reference(path.resolve().relative_to(Path.cwd().resolve()).as_posix(), content_sha256(raw))
    read_reference(reference)
    return reference


def _sync(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def load_pending(cache: Path) -> PendingNative | None:
    path = safe_issue_path(cache / PENDING_FILE)
    if not path.exists():
        return None
    pending: PendingNative = restore_dataclass(decode(read_input(path)), PendingNative)
    validate_pending(pending)
    return pending


def validate_pending(pending: PendingNative) -> None:
    if pending.schema != 1 or (pending.response is None) != (pending.review is None):
        raise ValueError("Invalid native pending phase.")
    if (pending.projection is None) != (pending.response is None):
        raise ValueError("Native projection lacks original response/review.")
    if pending.acceptance is not None and pending.projection is None:
        raise ValueError("Native acceptance lacks its projection.")
    if (pending.archive is not None or pending.archive_evidence is not None) and pending.acceptance is None:
        raise ValueError("Native archive lacks final acceptance.")
    for reference in references(pending):
        read_reference(reference)
    if pending.projection is not None:
        for reference in supplement_references(decode(read_reference(pending.projection))):
            read_reference(reference)
    packet = decode(read_reference(pending.packet))
    if packet.get("owner_sha256") != pending.owner_sha256 or packet.get("publication_date") != pending.publication_date:
        raise ValueError("Native pending owner/date differs from its immutable source packet.")


def references(pending: PendingNative) -> list[Reference]:
    return [
        item
        for item in (
            pending.packet,
            pending.response,
            pending.review,
            pending.projection,
            pending.acceptance,
            pending.archive_evidence,
            pending.archive,
        )
        if item is not None
    ]


def save_pending(cache: Path, pending: PendingNative) -> None:
    write_record(cache / PENDING_FILE, asdict(pending))
    if load_pending(cache) != pending:
        raise ValueError("Native pending readback mismatch.")


def clear_pending(cache: Path) -> None:
    safe_issue_path(cache / PENDING_FILE).unlink()
    _sync(cache)


def block_pending(cache: Path = Path(".cache")) -> None:
    if load_pending(cache) is not None:
        raise ValueError("Native editorial handoff is pending; resume its project/import phase first.")


def publication_fields(data: dict[str, Any]) -> dict[str, Any]:
    """Non-circular exact publication identity; generated UUID/times/hashes are excluded."""
    result = {
        key: data[key]
        for key in (
            "owner_sha256",
            "bot_username",
            "window_start",
            "canonical_metadata",
            "presentation_metadata",
            "payloads",
            "articles",
        )
    }
    if "supplement" in data:
        result["supplement"] = json.loads(canonical_json_bytes(data["supplement"]))
    return result


def projected_supplement(projected: dict[str, Any]) -> PendingSupplement | None:
    """Read only the immutable projection's fragment; never select today's slot."""
    value = projected["publication"].get("supplement")
    if projected.get("schema") != "native-composition/v3":
        if value is not None:
            raise ValueError("Native supplement requires its explicit composition version.")
        return None
    frozen: PreparedSupplement = restore_dataclass(value, PreparedSupplement)
    from digest.adapters.storage.post_delivery import read_projection

    if read_projection(frozen.projection, frozen.projection_sha256) != frozen.fragment:
        raise ValueError("Native composition differs from the retained supplement projection.")
    validate_checkpoint_reference(frozen.attempt, frozen.projection_sha256)
    return PendingSupplement(frozen.fragment, frozen.projection, frozen.projection_sha256, frozen.attempt)


def supplement_references(projected: dict[str, Any]) -> list[Reference]:
    pending = projected_supplement(projected)
    if pending is None:
        return []
    return [Reference(pending.projection, pending.projection_sha256),
            Reference(pending.fragment.result, pending.fragment.result_sha256),
            Reference(pending.fragment.checkpoint, pending.fragment.origin.checkpoint_sha256)]


def composition_references(pending: PendingNative, projected: dict[str, Any]) -> list[Reference]:
    return [*references(pending), *supplement_references(projected)]


def validate_acceptance(raw: bytes, projection: Reference, projected: dict[str, Any]) -> dict[str, Any]:
    value = decode(raw)
    expected = {
        "schema",
        "projection_sha256",
        "packet_sha256",
        "response_sha256",
        "review_sha256",
        "reviewer_execution_ref",
        "verdict",
        "notes",
    }
    if (
        set(value) != expected
        or value["schema"] != "native-final-acceptance/v1"
        or value["verdict"] != "accept"
        or value["projection_sha256"] != projection.sha256
        or value["reviewer_execution_ref"]
        == projected["publication"]["canonical_metadata"]["native_editorial"]["writer_execution_ref"]
        or any(value[key] != projected["inputs"][key] for key in ("packet_sha256", "response_sha256", "review_sha256"))
        or any(not isinstance(value[key], str) or not value[key].strip() for key in ("reviewer_execution_ref", "notes"))
    ):
        raise ValueError("Exact final native composition lacks editorial acceptance.")
    return value


def validate_native_ready(data: dict[str, Any]) -> None:
    """Re-read approved bytes on restore, claim and send, even for rehashed ready files."""
    projection: Reference = restore_dataclass(data["native_projection"], Reference)
    acceptance: Reference = restore_dataclass(data["native_acceptance"], Reference)
    for reference in (projection, acceptance):
        if data["checkpoint_refs"].get(reference.path) != reference.sha256:
            raise ValueError("Native ready lacks its mandatory acceptance checkpoint.")
    projected = decode(read_reference(projection))
    validate_acceptance(read_reference(acceptance), projection, projected)
    supported = {"native-composition/v3"} if data["schema"] == 5 else {
        "native-composition/v1", "native-composition/v2",
    }
    if (projected.get("schema") not in supported
            or projected.get("publication") != publication_fields(data)):
        raise ValueError("Ready differs from the exact accepted native composition.")
    for field in ("packet", "response", "review"):
        reference = restore_dataclass(projected["references"][field], Reference)
        if (
            data["checkpoint_refs"].get(reference.path) != reference.sha256
            or projected["inputs"][field + "_sha256"] != reference.sha256
        ):
            raise ValueError("Native ready input bindings changed.")
        read_reference(reference)
    for reference in supplement_references(projected):
        if data["checkpoint_refs"].get(reference.path) != reference.sha256:
            raise ValueError("Native ready lacks an immutable supplement reference.")
        read_reference(reference)
