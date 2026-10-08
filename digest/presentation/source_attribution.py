"""Reviewed literal source notices applied only to publication copies."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from digest.domain.catalog.occurrences import occurrence_sha256 as _occurrence_sha256

if TYPE_CHECKING:
    from digest.domain.catalog.occurrences import SourceOccurrence
    from digest.domain.editorial.summaries import ArticleSummary


# Reviewed source-specific presentation notices, not source activation or inferred
# licensing facts. Match the exact feed URL; unfamiliar bindings need review.
SOURCE_CREDITS = {
    "https://www.england.nhs.uk/feed/": (
        "NHS England RSS feeds. Open Government Licence v3.0: "
        "https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/"
    ),
    "https://www.gov.uk/search/news-and-communications.atom?organisations%5B%5D=environment-agency": (
        "Contains public sector information licensed under the Open Government Licence v3.0. "
        "https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/"
    ),
}


def supports_source_attribution(source_url: str) -> bool:
    """Whether this exact feed binding has a reviewed presentation notice."""
    return source_url in SOURCE_CREDITS


def attribute_source_card(
    card: ArticleSummary,
    occurrence: SourceOccurrence,
    occurrence_sha256: str,
) -> tuple[ArticleSummary, bool]:
    """Credit a supported exact feed on presentation only; validate frozen identity."""
    if (card.title, card.link, card.source, card.category) != (
        occurrence.title,
        occurrence.link,
        occurrence.source,
        occurrence.category,
    ) or occurrence_sha256 != _occurrence_sha256(occurrence):
        raise ValueError("Source attribution differs from the frozen article occurrence.")
    credit = SOURCE_CREDITS.get(occurrence.source_url)
    if credit is None:
        return card, False  # Other ordinary source presentation is unchanged.
    return replace(card, summary=f"{card.summary} {credit}"), True
