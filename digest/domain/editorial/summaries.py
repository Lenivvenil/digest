"""Canonical and presented article summary value, without model or storage dependencies."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ArticleSummary:
    """Per-article LLM summary used for individual Telegram posts."""

    title: str
    link: str
    source: str
    category: str
    summary: str
