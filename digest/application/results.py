"""Shared result of a digest run or prepared edition operation."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class RunStats:
    feeds_fetched: int  # Attempted feeds, including failed fetches; not success count.
    new_articles: int
    digest_length: int
    telegram_sent: bool
    telegram_partial: bool
    markdown_saved: bool
    markdown_path: str
    sources_promoted: int = 0
    sources_demoted: int = 0
    feedback_collected: int = 0
    duration_seconds: float = 0.0
    required_delivery_failed: bool = False
    review_status: str = "not_requested"
    review_checkpoint: str = ""
    edition_status: str = ""
    ready_sha256: str = ""
