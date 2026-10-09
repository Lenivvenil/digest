"""Frozen delivered-card targets, distinct from the unchanged source evidence bundle."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Literal

from digest._sanitize import sanitize_article
from digest.domain.catalog.articles import article_hash
from digest.domain.catalog.occurrences import SourceOccurrence, occurrence_sha256
from digest.domain.editorial.reviews import EvidenceBundle
from digest.domain.editorial.summaries import ArticleSummary


@dataclass(frozen=True)
class DeliveredCard:
    card_id: str
    canonical: ArticleSummary
    presentation: ArticleSummary
    occurrence: SourceOccurrence
    occurrence_sha256: str
    covering_chunks: tuple[int, ...]


@dataclass(frozen=True)
class DeliveredInvestigationInput:
    edition_id: str
    ready_sha256: str
    claim_sha256: str
    receipts_sha256: str
    owner_sha256: str
    publication_day: str
    canonical_sha256: str
    presentation_sha256: str
    checkpoint_sha256: str
    report_sha256: str
    bundle_id: str
    cards: tuple[DeliveredCard, ...]


@dataclass(frozen=True)
class DeliveredQuote:
    field: Literal["title", "summary"]
    text: str


def validate_delivered_input(origin: DeliveredInvestigationInput, bundle: EvidenceBundle) -> None:
    """Check retained occurrence projection without rebuilding under current config."""
    hashes = (
        origin.ready_sha256,
        origin.claim_sha256,
        origin.receipts_sha256,
        origin.owner_sha256,
        origin.canonical_sha256,
        origin.presentation_sha256,
        origin.checkpoint_sha256,
        origin.report_sha256,
    )
    if (
        any(not re.fullmatch(r"[a-f0-9]{64}", value) for value in hashes)
        or not re.fullmatch(r"[a-f0-9]{32}", origin.edition_id)
        or date.fromisoformat(origin.publication_day).isoformat() != origin.publication_day
        or origin.bundle_id != bundle.bundle_id
        or not origin.cards
        or len({card.card_id for card in origin.cards}) != len(origin.cards)
    ):
        raise ValueError("Invalid delivered investigation origin.")
    evidence = {item.evidence_id: item for item in bundle.items}
    for card in origin.cards:
        item = evidence.get(card.card_id)
        occurrence = card.occurrence
        title, excerpt, source = sanitize_article(occurrence.title, occurrence.description, occurrence.source)
        identity = (occurrence.title, occurrence.link, occurrence.source, occurrence.category)
        if (
            item is None
            or card.card_id != article_hash(occurrence.title, occurrence.link)
            or card.occurrence_sha256 != occurrence_sha256(occurrence)
            or not occurrence.source_url
            or (item.title, item.url, item.source, item.category, item.published)
            != (title, occurrence.link, source, occurrence.category[:200], occurrence.published)
            or not excerpt.startswith(item.excerpt)
            or any(
                (value.title, value.link, value.source, value.category) != identity
                for value in (card.canonical, card.presentation)
            )
            or not card.covering_chunks
            or any(type(index) is not int or index < 0 for index in card.covering_chunks)
            or tuple(sorted(set(card.covering_chunks))) != card.covering_chunks
        ):
            raise ValueError("Delivered card differs from its original source occurrence.")


def validate_delivered_quote(origin: DeliveredInvestigationInput, card_id: str, quote: DeliveredQuote) -> DeliveredCard:
    card = next((item for item in origin.cards if item.card_id == card_id), None)
    if (
        card is None
        or quote.field not in {"title", "summary"}
        or not quote.text.strip()
        or quote.text not in getattr(card.canonical, quote.field)
    ):
        raise ValueError("Narrative quote is not in the delivered card's exact canonical field.")
    return card
