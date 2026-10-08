"""Preparation retains accepted canonical work across downstream failures."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from digest.application.review_request import build_evidence_bundle
from digest.domain.editorial.reviews import BlindReviewReport, EvidenceSelection, ModelReview
from digest.preparation import (
    PreparationSnapshot,
    clear_preparation,
    load_accepted_preparation,
    load_preparation,
    save_preparation,
)
from digest.radar.summarizer import ArticleSummary, CategorySummary
from scripts.review_fixture import fixture_articles, fixture_config

NOW = datetime(2026, 10, 4, 12, tzinfo=UTC)


def _snapshot() -> PreparationSnapshot:
    bundle = build_evidence_bundle(fixture_articles(), fixture_config().review)
    item = bundle.items[0]
    selection = EvidenceSelection(item.evidence_id, "Reason exactly as accepted", item.title[:100], "high", True)
    review = ModelReview(
        "primary", "provider", "original-model", bundle.bundle_id, "prompt-hash", "ok",
        selections=[selection], usage={"tokens": 123}, resolved_model="resolved-original",
        response_sha256="response-hash", generated_at=NOW.isoformat(), reused_from_checkpoint=True,
    )
    report = BlindReviewReport(1, bundle, [review], "incomplete", None, [], "third model unavailable")
    card = ArticleSummary("Title — untouched", item.url, item.source, item.category, "Exact  summary\nwith spaces")
    category = CategorySummary(item.category, "Legacy canonical summary", 5, [card])
    return PreparationSnapshot([card], [category], "Canonical\n\nRussian: текст  \n", report, 2, 5, [item.source])


def _rehash(record: dict[str, Any]) -> None:
    body = {key: value for key, value in record.items() if key != "sha256"}
    record["sha256"] = hashlib.sha256(json.dumps(
        body, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode()).hexdigest()


def test_roundtrip_restores_exact_dataclasses_and_canonical_report(tmp_path: Path) -> None:
    snapshot = _snapshot()
    path = save_preparation(snapshot, tmp_path, NOW)
    accepted = load_accepted_preparation(tmp_path, NOW)
    assert accepted is not None and accepted.path == path
    assert accepted.sha256 == json.loads(path.read_text())["sha256"]
    assert accepted.snapshot == snapshot
    loaded = load_preparation(tmp_path, NOW)
    assert loaded == snapshot
    assert loaded is not None and loaded.review_report is not None
    assert loaded.top_articles[0] is not snapshot.top_articles[0]
    assert isinstance(loaded.review_report.evidence.items, tuple)
    assert asdict(loaded.review_report) == asdict(snapshot.review_report)
    assert loaded.combined == snapshot.combined


def test_legacy_no_report_and_overwrite_one_pending_snapshot(tmp_path: Path) -> None:
    snapshot = replace(_snapshot(), review_report=None)
    path = save_preparation(snapshot, tmp_path, NOW)
    updated = replace(snapshot, combined="replacement")
    assert save_preparation(updated, tmp_path, NOW) == path
    assert load_preparation(tmp_path, NOW) == updated
    assert list(tmp_path.iterdir()) == [path]


def test_expiry_uses_utc_date_without_removing_canonical_work(tmp_path: Path) -> None:
    snapshot = _snapshot()
    path = save_preparation(snapshot, tmp_path, NOW)
    assert load_preparation(tmp_path, NOW + timedelta(days=1)) is None
    assert path.exists()
    same_utc_day = datetime(2026, 10, 5, 1, tzinfo=timezone(timedelta(hours=5)))
    assert load_preparation(tmp_path, same_utc_day) == snapshot
    with pytest.raises(ValueError, match="Invalid preparation"):
        load_preparation(tmp_path, NOW - timedelta(days=1))


def test_absent_clear_and_naive_clock(tmp_path: Path) -> None:
    assert load_preparation(tmp_path, NOW) is None
    clear_preparation(tmp_path)
    save_preparation(_snapshot(), tmp_path, NOW)
    clear_preparation(tmp_path)
    assert load_preparation(tmp_path, NOW) is None
    with pytest.raises(ValueError, match="timezone-aware"):
        save_preparation(_snapshot(), tmp_path, NOW.replace(tzinfo=None))


@pytest.mark.parametrize("change", ["hash", "field", "type", "version", "quote", "evidence", "count"])
def test_corruption_and_structural_errors_fail_closed(tmp_path: Path, change: str) -> None:
    path = save_preparation(_snapshot(), tmp_path, NOW)
    record = json.loads(path.read_text())
    if change == "hash":
        record["snapshot"]["combined"] = "tampered"
    elif change == "field":
        record["snapshot"]["top_articles"][0]["extra"] = "not allowed"
    elif change == "type":
        record["snapshot"]["article_count"] = True
    elif change == "version":
        record["schema_version"] = 2
    elif change == "quote":
        record["snapshot"]["review_report"]["reviews"][0]["selections"][0]["quote"] = "fabricated claim"
    elif change == "evidence":
        record["snapshot"]["review_report"]["evidence"]["items"][0]["excerpt"] = "changed"
    else:
        record["snapshot"]["summaries"][0]["article_count"] = -1
    if change != "hash":
        _rehash(record)
    path.write_text(json.dumps(record))
    with pytest.raises(ValueError, match="Invalid preparation"):
        load_preparation(tmp_path, NOW)


def test_duplicate_json_keys_rejected(tmp_path: Path) -> None:
    path = save_preparation(_snapshot(), tmp_path, NOW)
    path.write_text(path.read_text().replace('"schema_version": 1', '"schema_version": 1, "schema_version": 1', 1))
    with pytest.raises(ValueError, match="Invalid preparation"):
        load_preparation(tmp_path, NOW)


def test_symlink_cannot_overwrite_outside_snapshot(tmp_path: Path) -> None:
    path = save_preparation(_snapshot(), tmp_path, NOW)
    target = tmp_path / "outside.json"
    target.write_text("untouched")
    path.unlink()
    path.symlink_to(target)
    with pytest.raises(ValueError, match="symlinks"):
        save_preparation(_snapshot(), tmp_path, NOW)
    assert target.read_text() == "untouched"


def test_evening_preparation_resumes_next_morning_for_intended_date(tmp_path: Path) -> None:
    snapshot = _snapshot()
    evening = NOW.replace(hour=22)
    morning = (NOW + timedelta(days=1)).replace(hour=7)
    target = morning.date()
    path = save_preparation(snapshot, tmp_path, evening, publication_date=target)
    record = json.loads(path.read_text())
    assert record["utc_date"] == evening.date().isoformat()
    assert record["publication_date"] == target.isoformat()
    assert load_preparation(tmp_path, evening, publication_date=target) == snapshot
    assert load_preparation(tmp_path, morning, publication_date=target) == snapshot
    assert load_preparation(tmp_path, morning) == snapshot
    assert load_preparation(tmp_path, morning.replace(hour=23, minute=59), publication_date=target) == snapshot
    assert load_preparation(tmp_path, morning + timedelta(days=1), publication_date=target) is None
    assert path.exists()


def test_active_target_mismatch_blocks_load_and_save_without_changes(tmp_path: Path) -> None:
    target = (NOW + timedelta(days=1)).date()
    path = save_preparation(_snapshot(), tmp_path, NOW, publication_date=target)
    original = path.read_bytes()
    for requested in (None, NOW.date(), (NOW + timedelta(days=2)).date()):
        with pytest.raises(ValueError, match="Invalid preparation"):
            load_preparation(tmp_path, NOW, publication_date=requested)
        with pytest.raises(ValueError, match="Invalid preparation"):
            save_preparation(replace(_snapshot(), combined="replacement"), tmp_path, NOW,
                             publication_date=requested)
        assert path.read_bytes() == original


def test_expired_target_allows_new_preparation(tmp_path: Path) -> None:
    save_preparation(_snapshot(), tmp_path, NOW)
    tomorrow = NOW + timedelta(days=1)
    updated = replace(_snapshot(), combined="next edition")
    save_preparation(updated, tmp_path, tomorrow)
    assert load_preparation(tmp_path, tomorrow) == updated


def test_reject_past_publication_and_future_creation_in_same_day(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="past publication date"):
        save_preparation(_snapshot(), tmp_path, NOW, publication_date=(NOW - timedelta(days=1)).date())
    save_preparation(_snapshot(), tmp_path, NOW)
    with pytest.raises(ValueError, match="Invalid preparation"):
        load_preparation(tmp_path, NOW - timedelta(minutes=1))


@pytest.mark.parametrize("field,value", [
    ("publication_date", "2026-10-03"),
    ("publication_date", "2026-10-4"),
    ("created_at", "2026-10-04T12:00:00"),
    ("created_at", "2026-10-04T12:00:00+05:00"),
    ("utc_date", "2026-10-03"),
])
def test_invalid_persisted_dates_rejected(tmp_path: Path, field: str, value: str) -> None:
    path = save_preparation(_snapshot(), tmp_path, NOW)
    record = json.loads(path.read_text())
    record[field] = value
    _rehash(record)
    path.write_text(json.dumps(record))
    with pytest.raises(ValueError, match="Invalid preparation"):
        load_preparation(tmp_path, NOW)
