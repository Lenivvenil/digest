"""User feedback collection and storage for adaptive source management."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ArticleFeedback:
    article_hash: str
    source_name: str
    rating: int
    timestamp: str


@dataclass
class FeedbackStore:
    ratings: list[ArticleFeedback] = field(default_factory=list)
    last_update_id: int = 0
