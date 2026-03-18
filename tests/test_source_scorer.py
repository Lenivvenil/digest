"""Tests for source scorer data models and scoring engine."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from src.source_scorer import (
    DailySnapshot,
    SourceStats,
    calculate_score,
    load_stats,
    save_stats,
    update_stats,
)


def test_daily_snapshot_creation() -> None:
    snap = DailySnapshot(
        date="2026-03-18",
        articles_found=5,
        articles_included=3,
        fetch_ok=True,
    )
    assert snap.date == "2026-03-18"
    assert snap.articles_found == 5
    assert snap.articles_included == 3
    assert snap.fetch_ok is True


def test_source_stats_defaults() -> None:
    stats = SourceStats(name="Test Feed")
    assert stats.name == "Test Feed"
    assert stats.total_fetches == 0
    assert stats.successful_fetches == 0
    assert stats.total_articles_found == 0
    assert stats.articles_included_in_digest == 0
    assert stats.avg_description_length == 0.0
    assert stats.last_seen is None
    assert stats.history == []


def test_source_stats_with_history() -> None:
    snap = DailySnapshot(
        date="2026-03-17", articles_found=10, articles_included=4, fetch_ok=True
    )
    stats = SourceStats(
        name="Rich Feed",
        total_fetches=5,
        successful_fetches=4,
        total_articles_found=50,
        articles_included_in_digest=20,
        avg_description_length=150.5,
        last_seen="2026-03-17",
        history=[snap],
    )
    assert stats.total_fetches == 5
    assert stats.successful_fetches == 4
    assert len(stats.history) == 1
    assert stats.history[0].articles_found == 10


# --- load_stats / save_stats ---


def test_load_stats_missing_file(tmp_path: Path) -> None:
    result = load_stats(str(tmp_path))
    assert result == {}


def test_save_and_load_round_trip(tmp_path: Path) -> None:
    stats = {
        "Feed A": SourceStats(
            name="Feed A",
            total_fetches=10,
            successful_fetches=9,
            total_articles_found=50,
            articles_included_in_digest=20,
            avg_description_length=120.5,
            last_seen="2026-03-18",
            history=[
                DailySnapshot(
                    date="2026-03-18",
                    articles_found=5,
                    articles_included=2,
                    fetch_ok=True,
                )
            ],
        )
    }
    save_stats(stats, str(tmp_path))
    loaded = load_stats(str(tmp_path))
    assert "Feed A" in loaded
    s = loaded["Feed A"]
    assert s.name == "Feed A"
    assert s.total_fetches == 10
    assert s.successful_fetches == 9
    assert s.avg_description_length == 120.5
    assert s.last_seen == "2026-03-18"
    assert len(s.history) == 1
    assert s.history[0].articles_found == 5
    assert s.history[0].fetch_ok is True


def test_load_stats_invalid_json(tmp_path: Path) -> None:
    path = tmp_path / "source_stats.json"
    path.write_text("not json", encoding="utf-8")
    result = load_stats(str(tmp_path))
    assert result == {}


def test_load_stats_non_dict(tmp_path: Path) -> None:
    path = tmp_path / "source_stats.json"
    path.write_text("[]", encoding="utf-8")
    result = load_stats(str(tmp_path))
    assert result == {}


# --- update_stats ---


def test_update_stats_new_source() -> None:
    stats: dict[str, SourceStats] = {}
    update_stats(stats, "New Feed", fetch_ok=True, articles_found=5,
                 articles_included=2, avg_desc_len=150.0)
    assert "New Feed" in stats
    s = stats["New Feed"]
    assert s.total_fetches == 1
    assert s.successful_fetches == 1
    assert s.total_articles_found == 5
    assert s.avg_description_length == 150.0
    assert len(s.history) == 1


def test_update_stats_failed_fetch() -> None:
    stats: dict[str, SourceStats] = {}
    update_stats(stats, "Bad Feed", fetch_ok=False, articles_found=0,
                 articles_included=0, avg_desc_len=0.0)
    s = stats["Bad Feed"]
    assert s.total_fetches == 1
    assert s.successful_fetches == 0
    assert s.last_seen is None
    assert len(s.history) == 1
    assert s.history[0].fetch_ok is False


def test_update_stats_caps_history_at_30() -> None:
    stats: dict[str, SourceStats] = {
        "Feed": SourceStats(
            name="Feed",
            history=[
                DailySnapshot(date=f"2026-02-{i:02d}", articles_found=1,
                              articles_included=0, fetch_ok=True)
                for i in range(1, 31)  # 30 entries
            ],
        )
    }
    assert len(stats["Feed"].history) == 30
    update_stats(stats, "Feed", fetch_ok=True, articles_found=3,
                 articles_included=1, avg_desc_len=100.0)
    assert len(stats["Feed"].history) == 30  # Still capped at 30


# --- calculate_score ---


def test_calculate_score_new_source() -> None:
    stats = SourceStats(name="New")
    score = calculate_score(stats)
    assert score == 0.5


def test_calculate_score_perfect_source() -> None:
    today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    stats = SourceStats(
        name="Perfect",
        total_fetches=10,
        successful_fetches=10,
        total_articles_found=50,
        articles_included_in_digest=50,
        avg_description_length=200.0,
        last_seen=today,
    )
    score = calculate_score(stats)
    # reliability=1.0*0.3 + productivity=1.0*0.3 + desc=1.0*0.2 + recency=1.0*0.2 = 1.0
    assert score >= 0.95


def test_calculate_score_dead_source() -> None:
    stats = SourceStats(
        name="Dead",
        total_fetches=10,
        successful_fetches=0,
        total_articles_found=0,
        articles_included_in_digest=0,
        avg_description_length=0.0,
        last_seen=None,
    )
    score = calculate_score(stats)
    # reliability=0 + productivity=0 + desc=0 + recency=0 = 0.0
    assert score <= 0.05


def test_calculate_score_partial() -> None:
    today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    stats = SourceStats(
        name="Partial",
        total_fetches=10,
        successful_fetches=8,
        total_articles_found=40,
        articles_included_in_digest=10,
        avg_description_length=50.0,
        last_seen=today,
    )
    score = calculate_score(stats)
    # Should be moderate
    assert 0.2 < score < 0.8
