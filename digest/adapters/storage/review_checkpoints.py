"""Bounded RSS review checkpoint decoding and distinct archive write policies.

Optional full-source assembly and extension loading retain their transitional
owner in review_checkpoint; resumed archives preserve their existing opaque fields.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from digest._util import atomic_json_write
from digest.config import Config
from digest.domain.editorial.reviews import (
    SCHEMA_VERSION,
    BlindReviewReport,
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


def save_review_archive(path: Path, report: BlindReviewReport) -> None:
    """Atomically replace the delivery sidecar with its existing ASCII JSON bytes."""
    atomic_json_write(path, asdict(report))


def save_resumed_review_archive(path: Path, report: BlindReviewReport, original_content: bytes) -> None:
    """Exclusively write a resumed report while retaining separate source provenance."""
    payload = asdict(report)
    previous = json.loads(original_content)
    # Independent RSS review never upgrades source provenance. Preserve the
    # separately bound passages and any explicit missing-evidence marker as-is.
    for key in ("full_source_required", "full_source_evidence", "full_source_error", "reading_brief_status"):
        if key in previous:
            payload[key] = previous[key]
    with path.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def save_trial_review_archive(path: Path, report: BlindReviewReport) -> None:
    """Directly write the trial's pretty JSON with its existing default text encoding."""
    path.write_text(json.dumps(asdict(report), ensure_ascii=False, indent=2) + "\n")
