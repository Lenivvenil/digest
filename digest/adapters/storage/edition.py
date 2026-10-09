"""Exact prepared-edition bytes, bindings, checkpoint reads and durable local writes."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from digest.adapters.storage.issue_paths import safe_issue_path as _safe
from digest.delivery.issue_guard import ISSUE_FILE, _Record
from digest.delivery.issue_guard import _read as _read_legacy
from digest.domain.delivery.edition import (
    READY_SCHEMA_VERSION,
    SCHEMA_VERSION,
    ChunkReceipt,
    Claim,
    Edition,
    PreparedArticle,
    Receipts,
    validate_checkpoint_reference,
    validate_claim,
    validate_manifest,
    validate_receipts,
)
from digest.domain.editorial.summaries import ArticleSummary

READY_FILE = "prepared_edition.json"
CLAIM_FILE = "prepared_edition_claim.json"
RECEIPTS_FILE = "prepared_edition_receipts.json"


def content_sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def canonical_bytes(data: dict[str, Any]) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def read_record(
    path: Path,
    expected: str | None = None,
    *,
    allowed_schemas: tuple[int, ...] = (SCHEMA_VERSION,),
) -> tuple[dict[str, Any], str]:
    raw = _safe(path).read_bytes()
    digest = content_sha256(raw)
    if expected is not None and digest != expected:
        raise ValueError("Prepared edition hash mismatch; publishing blocked.")
    try:
        value = json.loads(raw)
        if (
            not isinstance(value, dict)
            or type(value.get("schema")) is not int
            or value["schema"] not in allowed_schemas
        ):
            raise ValueError
    except (ValueError, TypeError) as exc:
        raise ValueError("Invalid prepared edition JSON or schema; publishing blocked.") from exc
    return value, digest


def write_record(path: Path, value: dict[str, Any], *, exclusive: bool = False) -> str:
    _safe(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    target = path if exclusive else _safe(path.with_suffix(path.suffix + ".tmp"))
    with target.open("xb" if exclusive else "wb") as handle:
        handle.write(canonical_bytes(value))
        handle.flush()
        os.fsync(handle.fileno())
    if not exclusive:
        target.replace(path)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return content_sha256(path.read_bytes())


def validate_manifest_record(data: dict[str, Any], owner: str, now: datetime, *, fresh: bool = True) -> None:
    try:
        body = {key: value for key, value in data.items() if key != "content_sha256"}
        validate_manifest(
            data,
            owner,
            now,
            content_sha256=content_sha256(canonical_bytes(body)),
            owner_sha256=content_sha256(owner.encode()),
            canonical_sha256=content_sha256(canonical_bytes(data["canonical_metadata"])),
            presentation_sha256=content_sha256(canonical_bytes(data["presentation_metadata"])),
            fresh=fresh,
        )
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise ValueError("Invalid, stale or wrong-owner prepared edition; publishing blocked.") from exc


def load_edition(
    path: Path,
    owner: str,
    now: datetime,
    expected: str | None = None,
    *,
    fresh: bool = False,
) -> tuple[Edition, str]:
    data, digest = read_record(path, expected, allowed_schemas=(1, READY_SCHEMA_VERSION))
    validate_manifest_record(data, owner, now, fresh=fresh)
    try:
        fields = dict(data)
        fields["articles"] = [
            PreparedArticle(item["full_hash"], item["source"], item["covering_chunks"], ArticleSummary(**item["card"]))
            for item in data["articles"]
        ]
        return Edition(**fields), digest
    except TypeError as exc:
        raise ValueError("Invalid prepared edition record; publishing blocked.") from exc


def decode_claim(data: dict[str, Any], ready_sha: str, owner_sha: str) -> Claim:
    try:
        claim = Claim(**data)
        validate_claim(claim, ready_sha, owner_sha)
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid edition claim binding; publishing blocked.") from exc
    return claim


def load_claim(
    path: Path,
    ready_sha: str,
    owner_sha: str,
    expected: str | None = None,
) -> tuple[Claim, str]:
    data, digest = read_record(path, expected)
    return decode_claim(data, ready_sha, owner_sha), digest


def load_receipts(cache: Path, ready_sha: str, claim_sha: str, total: int, owner_sha: str) -> Receipts | None:
    path = cache / RECEIPTS_FILE
    if not path.exists():
        return None
    value, _ = read_record(path)
    try:
        validate_receipts(value, ready_sha, claim_sha, total, owner_sha)
        fields = dict(value)
        fields["confirmed"] = [ChunkReceipt(**item) for item in value["confirmed"]]
        record = Receipts(**fields)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Invalid edition receipts; publishing blocked.") from exc
    return record


def verify_checkpoints(references: dict[str, Any], *, verify_bytes: bool) -> None:
    for reference, digest in references.items():
        validate_checkpoint_reference(reference, digest)
        if verify_bytes:
            path = _safe(Path.cwd() / reference)
            if content_sha256(path.read_bytes()) != digest:
                raise ValueError("Prepared edition checkpoint hash mismatch; publishing blocked.")


def exists(path: Path) -> bool:
    return path.exists()


def load_legacy(cache: Path) -> _Record | None:
    path = cache / ISSUE_FILE
    return _read_legacy(path)[0] if path.exists() else None


def remove_previous_dispatch(cache: Path) -> None:
    for name in (CLAIM_FILE, RECEIPTS_FILE):
        _safe(cache / name).unlink(missing_ok=True)


def freeze_hashes(data: Edition) -> Edition:
    data = replace(
        data,
        canonical_sha256=content_sha256(canonical_bytes(data.canonical_metadata)),
        presentation_sha256=content_sha256(canonical_bytes(data.presentation_metadata)),
    )
    body = asdict(data)
    body.pop("content_sha256")
    return replace(data, content_sha256=content_sha256(canonical_bytes(body)))
