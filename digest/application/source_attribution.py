"""Resolve publication credits from the accepted report's immutable source packet."""

from __future__ import annotations

import hashlib
import logging
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING

from digest._serialization import canonical_json_bytes
from digest.domain.catalog.articles import article_hash
from digest.domain.catalog.occurrences import occurrence_sha256
from digest.presentation.source_attribution import attribute_source_card, supports_source_attribution

if TYPE_CHECKING:
    from collections.abc import Sequence

    from digest.domain.catalog.occurrences import SourceOccurrence
    from digest.domain.catalog.sources import SourceConfig
    from digest.domain.editorial.reviews import BlindReviewReport
    from digest.radar.summarizer import ArticleSummary

logger = logging.getLogger(__name__)


def main_attribution_occurrences(
    report: BlindReviewReport | None,
    cards: Sequence[ArticleSummary],
    sources: Sequence[SourceConfig],
    *,
    closing_snapshot: bool,
    cache_dir: str | Path = ".cache",
) -> dict[str, SourceOccurrence]:
    """Resolve main credits before calls; legacy recovery remains explicit."""
    from digest.adapters.storage.candidate_objects import read_packet
    from digest.adapters.storage.checkpoints import safe_checkpoint_path as _safe

    required = closing_snapshot or any(source.enabled and supports_source_attribution(source.url) for source in sources)
    try:
        report_sha = hashlib.sha256(canonical_json_bytes(asdict(report))).hexdigest() if report is not None else None
        path = _safe(Path(cache_dir) / "candidate_reports" / f"{report_sha}.json") if report_sha else None
        if path is None or not path.exists():
            if required:
                raise ValueError(
                    "Source attribution needs the accepted report's immutable candidate packet. Restore "
                    f"{path or 'the bound review report'} and its referenced candidate_sources objects before "
                    "resuming preparation; do not rerun selection. Reconcile unfrozen legacy preparation "
                    "before enabling these feeds."
                )
            return {}  # Accepted legacy reports without supported feeds retain their old behavior.
        assert report_sha is not None
        packet = read_packet(report_sha, cache_dir)
        if packet.report != report:
            raise ValueError("Source attribution packet differs from the accepted review report.")
        occurrences: dict[str, SourceOccurrence] = {}
        for card in cards:
            identity = article_hash(card.title, card.link)
            matches = [item for item in packet.articles if article_hash(item.title, item.link) == identity]
            if len(matches) != 1:
                raise ValueError("Main article has no unique immutable source occurrence for attribution.")
            source = matches[0]
            if supports_source_attribution(source.source_url):
                occurrence = source
                # Validate before any model call.
                attribute_source_card(card, occurrence, occurrence_sha256(occurrence))
                occurrences[identity] = occurrence
        return occurrences
    except (OSError, ValueError) as exc:
        if required:
            raise
        logger.warning(
            "Legacy attribution audit unavailable; retaining accepted presentation without inferring source credit: %s",
            exc,
        )
        return {}
