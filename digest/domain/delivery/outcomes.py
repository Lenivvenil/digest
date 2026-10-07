"""Delivery outcomes independent of Telegram transport and receipt persistence."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Literal


@dataclass
class ArticleDeliveryResult:
    """Delivery counts and attribution for cards Telegram actually accepted.

    ``article_source_map`` uses the 8-character callback hashes, while
    ``delivered_hashes`` contains full hashes for the collector's dedup cache.
    Skipped delivery (including missing credentials) has zero attempts.
    """

    attempted: int = 0
    sent: int = 0
    failed: int = 0
    article_source_map: dict[str, str] = field(default_factory=dict)
    delivered_hashes: set[str] = field(default_factory=set)


@dataclass
class IssueDeliveryResult(ArticleDeliveryResult):
    """Confirmed article coverage and the independent whole-issue outcome.

    Article attempts count blocks touched by an attempted chunk; incomplete
    attempted blocks count as failed, including uncertain deliveries. A final
    notice can fail even when every article has been confirmed. ``unknown``
    means Telegram acceptance could not be established and must not be retried.
    """

    outcome: Literal["sent", "failed", "unknown", "skipped"] = "skipped"
    total_chunks: int = 0
    attempted_chunks: int = 0
    confirmed_chunks: int = 0

    @property
    def complete(self) -> bool:
        return self.outcome == "sent" and self.sent > 0 and self.confirmed_chunks == self.total_chunks > 0


@dataclass(frozen=True)
class ArticleCoverage:
    """Article identity and the ordered chunks required for complete delivery."""

    full_hash: str
    source: str
    covering_chunks: tuple[int, ...]


def project_issue_coverage(
    articles: Iterable[ArticleCoverage],
    *,
    outcome: Literal["sent", "failed", "unknown", "skipped"],
    total_chunks: int,
    attempted_chunks: int,
    confirmed_chunks: int,
) -> IssueDeliveryResult:
    """Project article coverage from sequential attempted and confirmed prefixes.

    Only touched articles count as attempts, and only their complete covering
    chunk sets contribute attribution. The transport owns the issue outcome,
    including notice-only failures and uncertainty after accepted chunks.
    """
    result = IssueDeliveryResult(
        outcome=outcome,
        total_chunks=total_chunks,
        attempted_chunks=attempted_chunks,
        confirmed_chunks=confirmed_chunks,
    )
    for article in articles:
        if any(index < result.attempted_chunks for index in article.covering_chunks):
            result.attempted += 1
            if all(index < result.confirmed_chunks for index in article.covering_chunks):
                result.sent += 1
                result.delivered_hashes.add(article.full_hash)
                result.article_source_map[article.full_hash[:8]] = article.source
            else:
                result.failed += 1
    return result
