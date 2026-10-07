"""Shared result of a digest run or prepared edition operation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from digest.domain.editorial.reviews import BlindReviewReport
    from digest.irritator import IrritatorStatus
    from digest.irritator.ranker import RankedSignal
    from digest.radar.summarizer import ArticleSummary


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


@dataclass(frozen=True)
class RadarPreview:
    """Primary-only display content; translated cards are printed when requested."""

    combined: str
    cards: list[ArticleSummary]
    show_cards: bool


@dataclass(frozen=True)
class DigestPreview:
    """Complete legacy preview after optional investigation and presentation."""

    combined: str
    cards: list[ArticleSummary]
    ranked: list[RankedSignal]
    irritator_status: IrritatorStatus
    review_report: BlindReviewReport | None


Preview = RadarPreview | DigestPreview
