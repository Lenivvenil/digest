"""Tests for feedback data models."""

from __future__ import annotations

from src.feedback import ArticleFeedback, FeedbackStore


def test_article_feedback_creation() -> None:
    fb = ArticleFeedback(
        article_hash="abc123",
        source_name="Test Feed",
        rating=1,
        timestamp="2026-03-18T10:00:00",
    )
    assert fb.article_hash == "abc123"
    assert fb.source_name == "Test Feed"
    assert fb.rating == 1
    assert fb.timestamp == "2026-03-18T10:00:00"


def test_feedback_store_defaults() -> None:
    store = FeedbackStore()
    assert store.ratings == []
    assert store.last_update_id == 0


def test_feedback_store_with_ratings() -> None:
    fb1 = ArticleFeedback(
        article_hash="a1",
        source_name="Feed A",
        rating=1,
        timestamp="2026-03-18T10:00:00",
    )
    fb2 = ArticleFeedback(
        article_hash="b2",
        source_name="Feed B",
        rating=-1,
        timestamp="2026-03-18T11:00:00",
    )
    store = FeedbackStore(ratings=[fb1, fb2], last_update_id=42)
    assert len(store.ratings) == 2
    assert store.last_update_id == 42
    assert store.ratings[0].source_name == "Feed A"
    assert store.ratings[1].rating == -1
