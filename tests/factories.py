"""Shared test fixtures and factory helpers."""

from __future__ import annotations

from datetime import datetime, timezone

from digest.irritator.narrative_extractor import Narrative
from digest.irritator.ranker import RankedSignal
from digest.irritator.sources import Signal
from digest.radar.collector import Article

# ---------------------------------------------------------------------------
# Factory helpers (not pytest fixtures — call directly in tests)
# ---------------------------------------------------------------------------


def make_signal(
    url: str = "https://example.com/article",
    title: str = "Test Signal",
    snippet: str = "Detailed counter argument",
    source_name: str = "hackernews",
    published: str = "2026-01-01",
    score: float = 10.0,
) -> Signal:
    return Signal(
        url=url,
        title=title,
        snippet=snippet,
        source_name=source_name,
        published=published,
        score=score,
    )


def make_article(
    title: str = "Test Article",
    link: str = "https://example.com/1",
    description: str = "Article description.",
    source: str = "TestSource",
    category: str = "Tech",
    pub_date: datetime | None = None,
) -> Article:
    if pub_date is None:
        pub_date = datetime(2026, 4, 9, 12, 0, 0, tzinfo=timezone.utc)
    return Article(
        title=title,
        link=link,
        description=description,
        source=source,
        category=category,
        pub_date=pub_date,
    )


def make_narrative(
    claim: str = "AI will replace all developers",
    category: str = "AI",
    implicit_assumptions: list[str] | None = None,
    why_worth_challenging: str = "This ignores documented failures.",
) -> Narrative:
    return Narrative(
        claim=claim,
        category=category,
        implicit_assumptions=implicit_assumptions or ["AI is infallible"],
        why_worth_challenging=why_worth_challenging,
    )


def make_ranked_signal(
    url: str = "https://example.com/a",
    title: str = "Counter point",
    score: int = 8,
    reasoning: str = "Compelling argument",
    narrative_claim: str = "AI replaces devs",
) -> RankedSignal:
    return RankedSignal(
        signal=make_signal(url=url, title=title),
        score=score,
        reasoning=reasoning,
        narrative_claim=narrative_claim,
    )
