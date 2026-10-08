"""Bounded RSS review checkpoint decoding and configured evidence validation.

Optional full-source assembly and extension loading retain their transitional
owner in review_checkpoint; this adapter reads RSS evidence and saved slots only.
"""

from __future__ import annotations

import json
from pathlib import Path

from digest.config import Config
from digest.domain.editorial.reviews import (
    SCHEMA_VERSION,
    EvidenceBundle,
    EvidenceItem,
    EvidenceSelection,
    ModelReview,
    RejectedSelection,
    validate_request_evidence_bundle,
)


def validate_evidence_bundle(bundle: EvidenceBundle, config: Config) -> None:
    """Compatibility adapter for callers supplying configured request limits."""
    validate_request_evidence_bundle(bundle, max_evidence_articles=config.review.max_evidence_articles,
                                     max_excerpt_chars=config.review.max_excerpt_chars)


def load_review_checkpoint(path: Path, config: Config) -> tuple[EvidenceBundle, list[ModelReview]]:
    """Accept version-one reports, including legacy reports without timestamps."""
    if path.stat().st_size > 256000:
        raise ValueError("Checkpoint exceeds 256000-byte budget.")
    try:
        raw = json.loads(path.read_text())
        if type(raw["schema_version"]) is not int or raw["schema_version"] != SCHEMA_VERSION:
            raise ValueError("Unsupported checkpoint schema version.")
        evidence = dict(raw["evidence"])
        evidence["items"] = tuple(EvidenceItem(**item) for item in evidence["items"])
        bundle = EvidenceBundle(**evidence)
        validate_evidence_bundle(bundle, config)
        if not isinstance(raw["reviews"], list) or len(raw["reviews"]) > 3:
            raise ValueError("Invalid checkpoint review count.")
        reviews = []
        for value in raw["reviews"]:
            item = dict(value)
            item["selections"] = [EvidenceSelection(**selection) for selection in item.get("selections", [])]
            item["rejected_items"] = [RejectedSelection(**rejected) for rejected in item.get("rejected_items", [])]
            review = ModelReview(**item)
            if review.slot not in {"primary", "secondary", "third"}:
                raise ValueError("Invalid checkpoint review slot.")
            reviews.append(review)
        if len({review.slot for review in reviews}) != len(reviews):
            raise ValueError("Checkpoint contains duplicate review slots.")
        return bundle, reviews
    except (TypeError, KeyError, AttributeError) as exc:
        raise ValueError("Invalid checkpoint schema.") from exc
