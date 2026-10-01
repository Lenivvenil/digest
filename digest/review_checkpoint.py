"""Read bounded report checkpoints without trusting cached model output or evidence."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlparse

from digest.config import Config
from digest.review import (
    MAX_EVIDENCE_JSON_CHARS,
    SCHEMA_VERSION,
    EvidenceBundle,
    EvidenceItem,
    EvidenceSelection,
    ModelReview,
    RejectedSelection,
)


def validate_evidence_bundle(bundle: EvidenceBundle, config: Config) -> None:
    if (type(bundle.schema_version) is not int or bundle.schema_version != SCHEMA_VERSION
            or not isinstance(bundle.items, tuple) or bundle.evidence_kind != "sanitized_rss_excerpt"
            or type(bundle.omitted_articles) is not int or bundle.omitted_articles < 0
            or not bundle.items or len(bundle.items) > config.review.max_evidence_articles):
        raise ValueError("Unsupported or over-budget checkpoint evidence.")
    seen: set[str] = set()
    for item in bundle.items:
        if (not all(isinstance(value, str) for value in (
                item.evidence_id, item.title, item.url, item.source, item.category, item.excerpt))
                or item.published is not None and not isinstance(item.published, str)
                or type(item.excerpt_shortened_or_sanitized) is not bool):
            raise ValueError("Invalid checkpoint evidence fields.")
        if (not item.evidence_id or item.evidence_id in seen
                or len(item.excerpt) > config.review.max_excerpt_chars
                or urlparse(item.url).scheme not in {"http", "https"} or not urlparse(item.url).netloc):
            raise ValueError("Invalid checkpoint evidence identity, URL or budget.")
        seen.add(item.evidence_id)
    if sum(len(json.dumps(asdict(item), ensure_ascii=False)) for item in bundle.items) > MAX_EVIDENCE_JSON_CHARS:
        raise ValueError("Checkpoint evidence exceeds JSON budget.")
    payload = asdict(bundle)
    payload.pop("bundle_id")
    digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    if digest != bundle.bundle_id:
        raise ValueError("Checkpoint evidence hash mismatch.")


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
