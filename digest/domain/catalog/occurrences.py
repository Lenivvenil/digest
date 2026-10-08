"""Immutable source-occurrence value and its existing canonical content identity."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from datetime import datetime

from digest._serialization import canonical_json_bytes
from digest.domain.catalog.articles import Article


@dataclass(frozen=True)
class SourceOccurrence:
    title: str
    link: str
    description: str
    source: str
    category: str
    published: str | None
    source_url: str

    def article(self) -> Article:
        return Article(
            self.title,
            self.link,
            self.description,
            self.source,
            self.category,
            datetime.fromisoformat(self.published) if self.published else None,
        )


def occurrence_sha256(occurrence: SourceOccurrence) -> str:
    """Hash the seven retained fields without changing or validating the occurrence."""
    return hashlib.sha256(canonical_json_bytes(asdict(occurrence))).hexdigest()
