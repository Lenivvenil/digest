"""Persist accepted canonical analysis before translation or rendering can fail.

This is one preparation snapshot retained through its intended UTC publication day.
Callers must prefer an existing ready edition and clear preparation after freezing.
No current model or prompt settings are consulted when restoring accepted work.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import types
from dataclasses import asdict, dataclass, fields, is_dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, get_args, get_origin, get_type_hints

from digest._util import atomic_json_write
from digest.closing import ClosingDecision, validate_closing
from digest.radar.summarizer import ArticleSummary, CategorySummary
from digest.review import BlindReviewReport

PREPARATION_FILE = "pending_preparation.json"
LEGACY_SCHEMA_VERSION = 1
SCHEMA_VERSION = 2
_MAX_BYTES = 4_000_000


@dataclass(frozen=True)
class PreparationSnapshot:
    top_articles: list[ArticleSummary]
    summaries: list[CategorySummary]
    combined: str
    review_report: BlindReviewReport | None
    source_count: int
    article_count: int
    contributing_sources: list[str]
    closing: ClosingDecision | None = None


def _safe(path: Path) -> Path:
    path = Path(os.path.abspath(path))
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError("Preparation checkpoint paths must not contain symlinks.")
    return path


def _instant(now: datetime | None) -> datetime:
    instant = now or datetime.now(UTC)
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("Preparation checkpoint requires a timezone-aware time.")
    return instant.astimezone(UTC)


def _target(publication_date: date | None, instant: datetime) -> str:
    if publication_date is not None and type(publication_date) is not date:
        raise ValueError("Publication date must be a date without a time component.")
    return (publication_date or instant.date()).isoformat()


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _restore(value: Any, expected: Any) -> Any:
    """Decode only the exact declared dataclasses and JSON primitives."""
    origin, args = get_origin(expected), get_args(expected)
    if origin is types.UnionType:
        for candidate in args:
            try:
                return _restore(value, candidate)
            except ValueError:
                continue
        raise ValueError("Invalid optional checkpoint field.")
    if origin is Literal:
        if value not in args or not isinstance(value, str):
            raise ValueError("Invalid checkpoint enum.")
        return value
    if origin in (list, tuple):
        if not isinstance(value, list):
            raise ValueError("Invalid checkpoint collection.")
        items = [_restore(item, args[0]) for item in value]
        return tuple(items) if origin is tuple else items
    if origin is dict:
        if not isinstance(value, dict):
            raise ValueError("Invalid checkpoint mapping.")
        return {_restore(key, args[0]): _restore(item, args[1]) for key, item in value.items()}
    if isinstance(expected, type) and is_dataclass(expected):
        if not isinstance(value, dict) or set(value) != {field.name for field in fields(expected)}:
            raise ValueError("Invalid checkpoint dataclass fields.")
        hints = get_type_hints(expected)
        return expected(**{key: _restore(item, hints[key]) for key, item in value.items()})
    if expected is float:
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError("Invalid checkpoint numeric value.")
        return value
    if type(value) is not expected:
        raise ValueError("Invalid checkpoint primitive field.")
    return value


def _snapshot(payload: object, version: int = LEGACY_SCHEMA_VERSION) -> PreparationSnapshot:
    if not isinstance(payload, dict):
        raise ValueError("Invalid preparation snapshot.")
    legacy_fields = {field.name for field in fields(PreparationSnapshot)} - {"closing"}
    if version == LEGACY_SCHEMA_VERSION:
        if set(payload) != legacy_fields:
            raise ValueError("Invalid legacy preparation fields.")
        payload = {**payload, "closing": None}
    elif version != SCHEMA_VERSION or set(payload) != legacy_fields | {"closing"} or payload["closing"] is None:
        raise ValueError("Invalid versioned preparation fields.")
    snapshot: PreparationSnapshot = _restore(payload, PreparationSnapshot)
    if (snapshot.source_count < 0 or snapshot.article_count < 0
            or len(set(snapshot.contributing_sources)) != len(snapshot.contributing_sources)
            or any(summary.article_count < 0 for summary in snapshot.summaries)):
        raise ValueError("Invalid preparation checkpoint counts or sources.")
    if snapshot.review_report is not None:
        _validate_report(snapshot.review_report)
    if snapshot.closing is not None:
        validate_closing(snapshot.closing, snapshot.review_report)
        if snapshot.closing.card is not None:
            from digest.radar.collector import article_hash

            identity = article_hash(snapshot.closing.card.title, snapshot.closing.card.link)
            if any(article_hash(card.title, card.link) == identity for card in snapshot.top_articles):
                raise ValueError("Closing card duplicates the main selection.")
    return snapshot


def _validate_report(report: BlindReviewReport) -> None:
    bundle = report.evidence
    evidence_payload = asdict(bundle)
    evidence_payload.pop("bundle_id")
    evidence_hash = hashlib.sha256(json.dumps(
        evidence_payload, ensure_ascii=False, sort_keys=True,
    ).encode()).hexdigest()
    known = {item.evidence_id: item for item in bundle.items}
    if (report.schema_version != 1 or bundle.schema_version != 1
            or bundle.evidence_kind != "sanitized_rss_excerpt" or bundle.omitted_articles < 0
            or bundle.bundle_id != evidence_hash or len(known) != len(bundle.items)
            or any(not key for key in known)
            or len(report.reviews) > 3
            or len({review.slot for review in report.reviews}) != len(report.reviews)
            or any(identity not in known for identity in report.disputed_ids)
            or report.selection_overlap is not None and not 0 <= report.selection_overlap <= 1):
        raise ValueError("Invalid canonical review evidence or report metadata.")
    for review in report.reviews:
        if (review.slot not in {"primary", "secondary", "third"} or review.bundle_id != bundle.bundle_id
                or len({item.evidence_id for item in review.selections}) != len(review.selections)):
            raise ValueError("Invalid canonical review identity.")
        for selection in review.selections:
            evidence = known.get(selection.evidence_id)
            if (evidence is None or not selection.quote.strip() or not selection.reason.strip()
                    or selection.quote not in evidence.title and selection.quote not in evidence.excerpt):
                raise ValueError("Canonical review quote is not in stored evidence.")


def save_preparation(
    snapshot: PreparationSnapshot, cache_dir: str | Path = ".cache", now: datetime | None = None,
    publication_date: date | None = None,
) -> Path:
    """Atomically replace the one pending preparation with accepted canonical work."""
    # JSON roundtrip also converts the evidence tuple to its serialized list form.
    payload = json.loads(_canonical(asdict(snapshot)))
    version = LEGACY_SCHEMA_VERSION if snapshot.closing is None else SCHEMA_VERSION
    if version == LEGACY_SCHEMA_VERSION:
        payload.pop("closing")
    _snapshot(payload, version)
    instant = _instant(now)
    target = _target(publication_date, instant)
    if target < instant.date().isoformat():
        raise ValueError("Cannot prepare an edition for a past publication date.")
    # Validate any existing active snapshot before replacing canonical accepted work.
    load_preparation(cache_dir, instant, publication_date)
    body = {"schema_version": version, "utc_date": instant.date().isoformat(),
            "created_at": instant.isoformat(), "publication_date": target, "snapshot": payload}
    record = {**body, "sha256": hashlib.sha256(_canonical(body)).hexdigest()}
    if len(json.dumps(record, indent=2).encode("utf-8")) > _MAX_BYTES:
        raise ValueError("Preparation checkpoint exceeds its 4000000-byte budget.")
    path = _safe(Path(cache_dir) / PREPARATION_FILE)
    _safe(path.with_suffix(path.suffix + ".tmp"))
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json_write(path, record)
    return path


def load_preparation(
    cache_dir: str | Path = ".cache", now: datetime | None = None,
    publication_date: date | None = None,
) -> PreparationSnapshot | None:
    """Restore matching work through its publication day; reject active mismatches."""
    instant = _instant(now)
    today = instant.date().isoformat()
    target = _target(publication_date, instant)
    path = _safe(Path(cache_dir) / PREPARATION_FILE)
    if not path.exists():
        return None
    try:
        if path.stat().st_size > _MAX_BYTES:
            raise ValueError("Checkpoint exceeds its size budget.")
        record = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
        if (not isinstance(record, dict)
                or set(record) != {"schema_version", "utc_date", "created_at",
                                   "publication_date", "snapshot", "sha256"}
                or type(record["schema_version"]) is not int
                or record["schema_version"] not in {LEGACY_SCHEMA_VERSION, SCHEMA_VERSION}
                or not isinstance(record["utc_date"], str)
                or date.fromisoformat(record["utc_date"]).isoformat() != record["utc_date"]
                or not isinstance(record["publication_date"], str)
                or date.fromisoformat(record["publication_date"]).isoformat() != record["publication_date"]
                or not isinstance(record["created_at"], str)):
            raise ValueError("Invalid checkpoint envelope.")
        body = {key: value for key, value in record.items() if key != "sha256"}
        if record["sha256"] != hashlib.sha256(_canonical(body)).hexdigest():
            raise ValueError("Checkpoint hash mismatch.")
        snapshot = _snapshot(record["snapshot"], record["schema_version"])
        created_at = datetime.fromisoformat(record["created_at"])
        if (created_at.tzinfo is None or created_at.utcoffset() != timedelta(0)
                or created_at.isoformat() != record["created_at"]
                or created_at.date().isoformat() != record["utc_date"]
                or created_at > instant or record["publication_date"] < record["utc_date"]):
            raise ValueError("Invalid checkpoint creation or publication date.")
        if record["publication_date"] < today:
            return None
        if record["publication_date"] != target:
            raise ValueError("Active checkpoint publication date differs from the requested edition.")
        return snapshot
    except (ValueError, TypeError, KeyError, UnicodeError) as exc:
        raise ValueError("Invalid preparation checkpoint; inspect or clear it before preparing again.") from exc


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate checkpoint JSON key.")
        result[key] = value
    return result


def clear_preparation(cache_dir: str | Path = ".cache") -> None:
    """Remove the preparation only after its ready edition is durably frozen."""
    _safe(Path(cache_dir) / PREPARATION_FILE).unlink(missing_ok=True)
