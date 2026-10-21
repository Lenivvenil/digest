"""One retained local attempt per accepted preparation, with no generation replay.

Writers sharing a runtime remain serialized. An existing malformed, mismatched,
reserved or terminal file is never removed or repaired to obtain another attempt.
This local fsync/readback is not the runtime's remote persistence barrier.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import unicodedata
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from digest._serialization import canonical_json_bytes, restore_dataclass, unique_object
from digest.adapters.models.synthesis import _legacy_groq_synthesis_response_format, groq_synthesis_response_format
from digest.adapters.storage.checkpoints import safe_checkpoint_path
from digest.domain.editorial.synthesis import (
    MAX_OUTPUT_TOKENS,
    MAX_RESPONSE_BYTES,
    PROMPT_VERSION,
    EvidenceBinding,
    SynthesisResult,
    build_messages,
    validate_bindings,
    validate_hash,
    validate_publication_date,
    validate_request_bounds,
    validate_response,
)

MAX_RECORD_BYTES = 128 * 1024
STATUSES = frozenset({"reserved", "valid", "failed", "invalid", "abstained", "skipped"})
REASONS = frozenset({
    "unsupported_mode", "binding_unavailable", "route_unavailable", "deadline_unavailable",
    "deadline_exhausted", "pacing_unavailable", "budget_exhausted", "recovery_no_attempt",
    "request_oversized", "reserved", "provider_failed", "timed_out", "cancelled", "response_invalid",
    "response_oversized", "completed", "abstained",
})
USAGE_KEYS = frozenset({
    "prompt_tokens", "completion_tokens", "total_tokens", "reasoning_tokens",
    "rate_limit_limit_requests", "rate_limit_remaining_requests",
    "rate_limit_limit_tokens", "rate_limit_remaining_tokens",
})
_FIELDS = frozenset({
    "schema_version", "prompt_version", "accepted_sha256", "publication_date", "report_sha256", "bundle_id",
    "bindings", "ordered_selected_ids", "publication_language", "request_body", "request_sha256", "messages",
    "configured_route", "controls", "status", "started_at", "completed_at", "response_sha256", "response_text",
    "reported_model", "finish_reason", "usage", "result", "result_sha256", "reason", "sha256",
})
_OUTCOME_FIELDS = frozenset({
    "status", "completed_at", "response_sha256", "response_text", "reported_model", "finish_reason", "usage",
    "result", "result_sha256", "reason", "sha256",
})


def _digest(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _seal(body: dict[str, Any]) -> dict[str, Any]:
    body = {key: value for key, value in body.items() if key != "sha256"}
    # Detach caller-owned mutable values and normalize dataclass tuples to JSON arrays.
    return json.loads(canonical_json_bytes({**body, "sha256": _digest(body)}))  # type: ignore[no-any-return]


def _route() -> dict[str, str]:
    return {"provider": "groq", "model": "openai/gpt-oss-120b"}


def _controls(publication_language: str) -> dict[str, Any]:
    return {"temperature": 0, "max_output_tokens": MAX_OUTPUT_TOKENS, "max_retries": 0,
            "reasoning_effort": "low", "response_format": groq_synthesis_response_format(publication_language)}


def new_record(
    *, accepted_sha256: str, publication_date: str, report_sha256: str | None, bundle_id: str | None,
    bindings: tuple[EvidenceBinding, ...], publication_language: str, request_body: dict[str, Any] | None,
    status: str, started_at: str, reason: str | None = None,
) -> dict[str, Any]:
    """Build a fixed reservation or skipped envelope; building performs no I/O."""
    if status not in {"reserved", "skipped"}:
        raise ValueError("A new synthesis attempt must be reserved or skipped.")
    messages = request_body.get("messages") if request_body is not None else None
    record = _seal({
        "schema_version": 1, "prompt_version": PROMPT_VERSION, "accepted_sha256": accepted_sha256,
        "publication_date": publication_date, "report_sha256": report_sha256, "bundle_id": bundle_id,
        "bindings": [asdict(binding) for binding in bindings],
        "ordered_selected_ids": [binding.evidence.evidence_id for binding in bindings],
        "publication_language": publication_language, "request_body": request_body,
        "request_sha256": _digest(request_body) if request_body is not None else None,
        "messages": messages, "configured_route": _route() if request_body is not None else None,
        "controls": _controls(publication_language) if request_body is not None else None, "status": status,
        "started_at": started_at, "completed_at": started_at if status == "skipped" else None,
        "response_sha256": None, "response_text": None, "reported_model": None, "finish_reason": None,
        "usage": {}, "result": None, "result_sha256": None, "reason": reason,
    })
    _validate(record, accepted_sha256)
    return record


def _label(value: object, maximum: int) -> str | None:
    if (isinstance(value, str) and value.strip() and len(value) <= maximum
            and not any(unicodedata.category(c) == "Cc" for c in value)):
        return value
    return None


def terminal_record(
    reservation: dict[str, Any], *, status: str, completed_at: str, response_text: str | None = None,
    reported_model: str | None = None, finish_reason: str | None = None, usage: dict[str, Any] | None = None,
    result: SynthesisResult | None = None, reason: str | None = None,
) -> dict[str, Any]:
    """Capture bounded diagnostics only; oversized raw text is hashed, never truncated."""
    _validate(reservation, validate_hash(reservation.get("accepted_sha256")))
    if reservation["status"] != "reserved" or status not in STATUSES - {"reserved", "skipped"}:
        raise ValueError("Invalid synthesis terminal transition.")
    response_sha = hashlib.sha256(response_text.encode("utf-8")).hexdigest() if response_text is not None else None
    captured = (response_text
                if response_text is None or len(response_text.encode("utf-8")) <= MAX_RESPONSE_BYTES else None)
    serialized_result = asdict(result) if result is not None else None
    record = _seal({
        **reservation, "status": status, "completed_at": completed_at,
        "response_text": captured, "response_sha256": response_sha,
        "reported_model": _label(reported_model, 200), "finish_reason": _label(finish_reason, 64),
        "usage": {key: value for key, value in (usage or {}).items()
                  if key in USAGE_KEYS and type(value) is int and value >= 0},
        "result": serialized_result, "result_sha256": _digest(serialized_result) if result is not None else None,
        "reason": reason,
    })
    _validate(record, record["accepted_sha256"])
    return record


def attempt_path(accepted_sha256: str, cache_dir: str | Path) -> Path:
    return safe_checkpoint_path(Path(cache_dir) / "synthesis" / f"{validate_hash(accepted_sha256)}.json")


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError("Invalid synthesis timestamp.")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Synthesis timestamp requires an explicit timezone.")
    return parsed


def _validate_request(record: dict[str, Any], bindings: tuple[EvidenceBinding, ...]) -> None:
    body = record["request_body"]
    if body is None:
        if (record["status"] != "skipped"
                or any(record[key] is not None for key in (
                    "request_sha256", "messages", "configured_route", "controls",
                ))):
            raise ValueError("Missing synthesis request binding.")
        return
    controls = _controls(record["publication_language"])
    if canonical_json_bytes(record["controls"]) != canonical_json_bytes(controls):
        # Historical generic-v2 captures remain readable, never a format for new dispatch.
        controls["response_format"] = _legacy_groq_synthesis_response_format()
    if (not isinstance(body, dict) or record["configured_route"] != _route()
            or canonical_json_bytes(record["controls"]) != canonical_json_bytes(controls)):
        raise ValueError("Invalid synthesis configured route or controls.")
    expected_messages = build_messages(
        accepted_sha256=record["accepted_sha256"], publication_date=record["publication_date"],
        report_sha256=record["report_sha256"], bundle_id=record["bundle_id"],
        publication_language=record["publication_language"], bindings=bindings,
    )
    expected_body = {
        "model": _route()["model"], "messages": expected_messages, "temperature": body.get("temperature"),
        "max_completion_tokens": MAX_OUTPUT_TOKENS, "reasoning_effort": "low",
        "response_format": controls["response_format"],
    }
    if (canonical_json_bytes(body) != canonical_json_bytes(expected_body)
            or type(body["temperature"]) not in {int, float} or body["temperature"] != 0
            or type(body["max_completion_tokens"]) is not int or record["messages"] != expected_messages
            or record["request_sha256"] != validate_request_bounds(expected_messages, body)):
        raise ValueError("Synthesis complete request binding mismatch.")


def _validate_outcome(record: dict[str, Any], bindings: tuple[EvidenceBinding, ...]) -> None:
    status = record["status"]
    if status not in STATUSES or record["reason"] is not None and record["reason"] not in REASONS:
        raise ValueError("Invalid synthesis disposition.")
    started = _timestamp(record["started_at"])
    if status == "reserved":
        if record["completed_at"] is not None:
            raise ValueError("Reserved synthesis already has completion time.")
    elif _timestamp(record["completed_at"]) < started:
        raise ValueError("Synthesis completion precedes reservation.")
    if record["reported_model"] is not None and _label(record["reported_model"], 200) != record["reported_model"]:
        raise ValueError("Invalid synthesis reported model.")
    if record["finish_reason"] is not None and _label(record["finish_reason"], 64) != record["finish_reason"]:
        raise ValueError("Invalid synthesis finish reason.")
    usage = record["usage"]
    if (not isinstance(usage, dict) or not set(usage) <= USAGE_KEYS
            or any(type(value) is not int or value < 0 for value in usage.values())):
        raise ValueError("Invalid synthesis numeric usage.")
    text = record["response_text"]
    if text is not None:
        if (not isinstance(text, str) or len(text.encode("utf-8")) > MAX_RESPONSE_BYTES
                or hashlib.sha256(text.encode("utf-8")).hexdigest() != record["response_sha256"]):
            raise ValueError("Invalid synthesis response capture.")
    elif record["response_sha256"] is not None:
        validate_hash(record["response_sha256"])
    if status in {"reserved", "skipped"}:
        if (any(record[key] is not None for key in (
                "response_text", "response_sha256", "reported_model", "finish_reason", "result", "result_sha256",
        )) or usage or status == "skipped" and record["reason"] is None):
            raise ValueError("Unattempted synthesis cannot contain a provider outcome.")
    elif status in {"valid", "abstained"}:
        if text is None or record["result_sha256"] != _digest(record["result"]):
            raise ValueError("Missing synthesis parsed result binding.")
        # The application revalidates protected-content equality with the unchanged
        # translation owner before reuse; storage checks the source-bound wire shape.
        result = validate_response(text, record["finish_reason"], record["publication_language"], bindings,
                                   lambda original, translated: True)
        if (canonical_json_bytes(asdict(result)) != canonical_json_bytes(record["result"])
                or (status == "valid") != (result.status == "comparison")):
            raise ValueError("Synthesis parsed result differs from its captured response.")
    elif record["result"] is not None or record["result_sha256"] is not None:
        raise ValueError("Failed synthesis must not expose a publishable result.")


def _validate(record: object, accepted_sha256: str) -> dict[str, Any]:
    if not isinstance(record, dict) or set(record) != _FIELDS:
        raise ValueError("Invalid synthesis record fields.")
    if (type(record["schema_version"]) is not int or record["schema_version"] != 1
            or record["prompt_version"] != PROMPT_VERSION or record["accepted_sha256"] != accepted_sha256):
        raise ValueError("Unsupported synthesis record or accepted reference mismatch.")
    validate_hash(accepted_sha256)
    validate_publication_date(record["publication_date"])
    for key in ("report_sha256", "bundle_id"):
        if record[key] is not None:
            validate_hash(record[key])
    if record["publication_language"] not in {"en", "ru"}:
        raise ValueError("Invalid synthesis publication language.")
    bindings: tuple[EvidenceBinding, ...] = restore_dataclass(record["bindings"], tuple[EvidenceBinding, ...])
    validate_bindings(bindings, require_pair=record["request_body"] is not None)
    if record["ordered_selected_ids"] != [binding.evidence.evidence_id for binding in bindings]:
        raise ValueError("Synthesis selected evidence order mismatch.")
    _validate_request(record, bindings)
    _validate_outcome(record, bindings)
    if record["sha256"] != _digest({key: value for key, value in record.items() if key != "sha256"}):
        raise ValueError("Synthesis record integrity mismatch.")
    if len(canonical_json_bytes(record)) > MAX_RECORD_BYTES:
        raise ValueError("Synthesis record exceeds its byte budget.")
    return record


def _read(path: Path, accepted_sha256: str) -> dict[str, Any]:
    path = safe_checkpoint_path(path)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_RECORD_BYTES:
            raise ValueError("Synthesis record is not a bounded regular file.")
    except BaseException:
        os.close(descriptor)
        raise
    with os.fdopen(descriptor, "rb") as handle:
        raw = handle.read(MAX_RECORD_BYTES + 1)
    if len(raw) > MAX_RECORD_BYTES:
        raise ValueError("Synthesis record exceeds its byte budget.")
    try:
        record = json.loads(raw, object_pairs_hook=unique_object)
        return _validate(record, accepted_sha256)
    except (RecursionError, TypeError, OverflowError) as exc:
        raise ValueError("Malformed synthesis record.") from exc


def load_attempt(accepted_sha256: str, cache_dir: str | Path) -> dict[str, Any] | None:
    """Only absence returns None; every unsafe retained file remains a blocker."""
    path = attempt_path(accepted_sha256, cache_dir)
    try:
        return _read(path, accepted_sha256)
    except FileNotFoundError:
        return None


def _sync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def reserve_attempt(record: dict[str, Any], cache_dir: str | Path) -> Path:
    """Exclusively retain a reservation or skip, fsync and read back before dispatch."""
    _validate(record, validate_hash(record.get("accepted_sha256")))
    if record["status"] not in {"reserved", "skipped"}:
        raise ValueError("Only a reservation or skip may create a synthesis attempt.")
    path = attempt_path(record["accepted_sha256"], cache_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    safe_checkpoint_path(path)
    # A partial/failed reservation is deliberately retained and cannot be retried.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(canonical_json_bytes(record))
        handle.flush()
        os.fsync(handle.fileno())
    _sync_directory(path.parent)
    if _read(path, record["accepted_sha256"]) != record:
        raise ValueError("Synthesis reservation readback mismatch.")
    return path


def finish_attempt(reservation: dict[str, Any], terminal: dict[str, Any], cache_dir: str | Path) -> Path:
    """Replace only this exact reserved capture; never reopen existing terminal work."""
    accepted_sha = validate_hash(reservation.get("accepted_sha256"))
    _validate(reservation, accepted_sha)
    _validate(terminal, accepted_sha)
    if reservation["status"] != "reserved" or terminal["status"] in {"reserved", "skipped"}:
        raise ValueError("Invalid synthesis terminal transition.")
    if any(reservation[key] != terminal[key] for key in _FIELDS - _OUTCOME_FIELDS):
        raise ValueError("Synthesis terminal record changed its reservation binding.")
    path = attempt_path(accepted_sha, cache_dir)
    if _read(path, accepted_sha) != reservation:
        raise ValueError("Synthesis reservation is missing, changed or already terminal.")
    descriptor, name = tempfile.mkstemp(prefix=f".{accepted_sha}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(canonical_json_bytes(terminal))
            handle.flush()
            os.fsync(handle.fileno())
        # This second check preserves a terminal or changed record encountered during local work.
        if _read(path, accepted_sha) != reservation:
            raise ValueError("Synthesis reservation changed before terminal replacement.")
        safe_checkpoint_path(path)
        os.replace(temporary, path)
        _sync_directory(path.parent)
        if _read(path, accepted_sha) != terminal:
            raise ValueError("Synthesis terminal readback mismatch.")
    finally:
        temporary.unlink(missing_ok=True)
    return path
