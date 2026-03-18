"""Tests for source scorer data models."""

from __future__ import annotations

from src.source_scorer import DailySnapshot, SourceStats


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
