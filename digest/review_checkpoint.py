"""Read bounded report checkpoints without trusting cached model output or evidence."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from digest.config import Config
from digest.domain.editorial.reviews import MAX_EVIDENCE_JSON_CHARS as MAX_EVIDENCE_JSON_CHARS
from digest.domain.editorial.reviews import (
    SCHEMA_VERSION,
    EvidenceBundle,
    EvidenceItem,
    EvidenceSelection,
    ModelReview,
    RejectedSelection,
    validate_request_evidence_bundle,
)
from digest.radar.collector import article_hash

MAX_FULL_SOURCE_BYTES = 128000
FULL_SOURCE_KIND = "selected_full_source_passages"
_SHA256 = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class FullSourcePassage:
    """A literal passage selected from an immutable extracted article snapshot."""

    evidence_id: str
    article_id: str
    title: str
    url: str
    source: str
    category: str
    published: str | None
    source_sha256: str
    body_sha256: str
    body_chars: int
    final_url: str
    fetched_at: str
    extraction_status: str
    span_id: int
    start: int
    end: int
    excerpt: str
    excerpt_sha256: str
    roles: tuple[str, ...]
    selection_provider: str
    selection_model: str
    selection_prompt_sha256: tuple[str, ...]


@dataclass(frozen=True)
class FullSourceEvidence:
    """Optional source citations; never a replacement for RSS review evidence."""

    schema_version: int
    bundle_id: str
    evidence_kind: str
    rss_bundle_id: str
    items: tuple[FullSourcePassage, ...]


def _content_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _identity_hash(value: FullSourcePassage | FullSourceEvidence, field: str) -> str:
    payload = asdict(value)
    payload.pop(field)
    return _content_hash(payload)


def _validate_source_passage(item: FullSourcePassage) -> None:
    if not isinstance(item, FullSourcePassage) or not all(isinstance(value, str) and value.strip() for value in (
        item.evidence_id, item.article_id, item.title, item.url, item.source, item.category,
        item.source_sha256, item.body_sha256, item.final_url, item.fetched_at, item.extraction_status,
        item.excerpt, item.excerpt_sha256, item.selection_provider, item.selection_model,
    )):
        raise ValueError("Invalid full-source passage fields.")
    if item.published is not None:
        if not isinstance(item.published, str):
            raise ValueError("Invalid full-source passage date.")
        datetime.fromisoformat(item.published)
    datetime.fromisoformat(item.fetched_at)
    if (item.article_id != article_hash(item.title, item.url)
            or any(urlparse(url).scheme not in {"http", "https"} or not urlparse(url).netloc
                   for url in (item.url, item.final_url))
            or item.extraction_status not in {"article", "main", "body"}):
        raise ValueError("Invalid full-source article identity or provenance.")
    if (not all(type(value) is int for value in (item.body_chars, item.span_id, item.start, item.end))
            or item.span_id < 1 or not 0 <= item.start < item.end <= item.body_chars
            or item.end - item.start != len(item.excerpt)):
        raise ValueError("Invalid full-source passage offsets.")
    if (not isinstance(item.roles, tuple) or not item.roles or len(set(item.roles)) != len(item.roles)
            or any(role not in {"selected", "qualification", "angle_support"} for role in item.roles)
            or not isinstance(item.selection_prompt_sha256, tuple) or not item.selection_prompt_sha256
            or any(not isinstance(digest, str) or not _SHA256.fullmatch(digest)
                   for digest in item.selection_prompt_sha256)):
        raise ValueError("Invalid full-source selection provenance.")
    if (any(not _SHA256.fullmatch(digest) for digest in (
            item.source_sha256, item.body_sha256, item.excerpt_sha256, item.evidence_id))
            or hashlib.sha256(item.excerpt.encode()).hexdigest() != item.excerpt_sha256
            or _identity_hash(item, "evidence_id") != item.evidence_id):
        raise ValueError("Full-source passage hash mismatch.")


def validate_full_source_evidence(bundle: FullSourceEvidence, rss_bundle: EvidenceBundle) -> None:
    """Check self-contained citation integrity without pretending to reload its full body."""
    if (not isinstance(bundle, FullSourceEvidence) or type(bundle.schema_version) is not int
            or bundle.schema_version != SCHEMA_VERSION or bundle.evidence_kind != FULL_SOURCE_KIND
            or bundle.rss_bundle_id != rss_bundle.bundle_id or not isinstance(bundle.items, tuple)
            or not bundle.items):
        raise ValueError("Invalid full-source evidence checkpoint.")
    seen: set[tuple[str, int]] = set()
    for item in bundle.items:
        _validate_source_passage(item)
        identity = (item.source_sha256, item.span_id)
        if identity in seen:
            raise ValueError("Duplicate full-source passage.")
        seen.add(identity)
    if len(json.dumps(asdict(bundle), ensure_ascii=False).encode()) > MAX_FULL_SOURCE_BYTES:
        raise ValueError("Full-source evidence exceeds checkpoint budget.")
    if _identity_hash(bundle, "bundle_id") != bundle.bundle_id:
        raise ValueError("Full-source evidence hash mismatch.")


def build_full_source_evidence(
    rss_bundle: EvidenceBundle, state_dir: Path, article_ids: list[str],
) -> FullSourceEvidence:
    """Bind all delivered-brief citations; never truncate a passage or qualification."""
    from digest.reading_brief import ready_brief_evidence

    items: list[FullSourcePassage] = []
    for identity in dict.fromkeys(article_ids):
        state, source = ready_brief_evidence(state_dir, identity)
        roles: dict[int, set[str]] = {}
        prompts: dict[int, set[str]] = {}
        routes: dict[int, tuple[str, str]] = {}
        for page in state.pages:
            if page.result is None:
                raise ValueError("Incomplete full-source reading brief.")
            for role, selected in (("selected", page.result.selected_span_ids),
                                   ("qualification", page.result.qualification_span_ids),
                                   ("angle_support", page.result.angle_span_ids)):
                for span_id in selected:
                    roles.setdefault(span_id, set()).add(role)
                    prompts.setdefault(span_id, set()).add(page.prompt_sha256)
                    actual_route = page.route or state.route
                    routes[span_id] = (actual_route.provider, actual_route.model)
        for span_id in sorted(roles):
            span = source.spans[span_id - 1]
            excerpt = source.text[span.start:span.end]
            item = FullSourcePassage(
                "", identity, source.selection.title, source.selection.link, source.selection.source,
                source.selection.category, source.source_published or source.selection.pub_date,
                state.source_sha256 or "", source.body_sha256, len(source.text), source.final_url,
                source.fetched_at, source.extraction_status, span.id, span.start, span.end, excerpt,
                hashlib.sha256(excerpt.encode()).hexdigest(), tuple(sorted(roles[span_id])),
                *routes[span_id], tuple(sorted(prompts[span_id])),
            )
            items.append(replace(item, evidence_id=_identity_hash(item, "evidence_id")))
    bundle = FullSourceEvidence(SCHEMA_VERSION, "", FULL_SOURCE_KIND, rss_bundle.bundle_id, tuple(items))
    bundle = replace(bundle, bundle_id=_identity_hash(bundle, "bundle_id"))
    validate_full_source_evidence(bundle, rss_bundle)
    return bundle


def _parse_full_source_evidence(raw: Any, rss_bundle: EvidenceBundle) -> FullSourceEvidence:
    try:
        payload = dict(raw)
        payload["items"] = tuple(FullSourcePassage(**{
            **item, "roles": tuple(item["roles"]), "selection_prompt_sha256": tuple(item["selection_prompt_sha256"]),
        }) for item in payload["items"])
        bundle = FullSourceEvidence(**payload)
        validate_full_source_evidence(bundle, rss_bundle)
        return bundle
    except (TypeError, KeyError, AttributeError) as exc:
        raise ValueError("Invalid full-source evidence checkpoint.") from exc


def load_full_source_evidence(path: Path, rss_bundle: EvidenceBundle, config: Config) -> FullSourceEvidence | None:
    """Load the optional source extension; legacy RSS-only checkpoints return None."""
    if path.stat().st_size > 256000:
        raise ValueError("Checkpoint exceeds 256000-byte budget.")
    content = path.read_bytes()
    if len(content) > 256000:
        raise ValueError("Checkpoint exceeds 256000-byte budget.")
    loaded_bundle, _reviews = load_review_checkpoint(path, config)
    if loaded_bundle != rss_bundle or content != path.read_bytes():
        raise ValueError("Full-source checkpoint RSS bundle mismatch.")
    raw = json.loads(content)
    if "full_source_evidence" not in raw:
        return None
    return _parse_full_source_evidence(raw["full_source_evidence"], rss_bundle)


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
