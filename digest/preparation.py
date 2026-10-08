"""Persist accepted canonical analysis before translation or rendering can fail.

This is one preparation snapshot retained through its intended UTC publication day.
Callers must prefer an existing ready edition and clear preparation after freezing.
No current model or prompt settings are consulted when restoring accepted work.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, fields
from datetime import date, datetime, timedelta
from pathlib import Path

from digest._serialization import canonical_json_bytes as _canonical
from digest._serialization import restore_dataclass as _restore
from digest._serialization import unique_object as _unique_object
from digest._util import atomic_json_write
from digest._util import utc_instant as _instant
from digest.adapters.storage.checkpoints import safe_checkpoint_path as _safe
from digest.closing import ClosingDecision, validate_closing
from digest.domain.editorial.reviews import BlindReviewReport
from digest.domain.editorial.reviews import validate_canonical_report as _validate_report
from digest.radar.summarizer import ArticleSummary, CategorySummary

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


def _target(publication_date: date | None, instant: datetime) -> str:
    if publication_date is not None and type(publication_date) is not date:
        raise ValueError("Publication date must be a date without a time component.")
    return (publication_date or instant.date()).isoformat()


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


def clear_preparation(cache_dir: str | Path = ".cache") -> None:
    """Remove the preparation only after its ready edition is durably frozen."""
    _safe(Path(cache_dir) / PREPARATION_FILE).unlink(missing_ok=True)
