"""Pure evidence, selection and review contracts shared across editorial boundaries."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field, replace
from typing import Literal
from urllib.parse import urlparse

from digest._serialization import extract_json as _extract_json

SCHEMA_VERSION = 1
MAX_EVIDENCE_JSON_CHARS = 16000


@dataclass(frozen=True)
class EvidenceItem:
    evidence_id: str
    title: str
    url: str
    source: str
    category: str
    published: str | None
    excerpt: str
    excerpt_shortened_or_sanitized: bool


@dataclass(frozen=True)
class EvidenceBundle:
    schema_version: int
    bundle_id: str
    evidence_kind: str
    omitted_articles: int
    items: tuple[EvidenceItem, ...]


@dataclass(frozen=True)
class EvidenceSelection:
    evidence_id: str
    reason: str
    quote: str
    confidence: Literal["low", "medium", "high"]
    typography_normalized: bool = False


@dataclass(frozen=True)
class RejectedSelection:
    index: int
    reason: str
    evidence_id: str | None = None


@dataclass
class ModelReview:
    slot: str
    provider: str
    model: str
    bundle_id: str
    prompt_hash: str
    status: Literal["ok", "partial", "abstained", "invalid", "unavailable"]
    selections: list[EvidenceSelection] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    error: str = ""
    resolved_model: str | None = None
    response_sha256: str | None = None
    rejected_output: str | None = None
    rejected_output_truncated: bool = False
    attempted_at: str | None = None
    generated_at: str | None = None
    reused_from_checkpoint: bool = False
    rejected_items: list[RejectedSelection] = field(default_factory=list)


@dataclass(frozen=True)
class ReviewReuseIdentity:
    """The configured slot and exact request a cached review must match."""

    slot: str
    provider: str
    model: str
    bundle_id: str
    prompt_hash: str


@dataclass
class BlindReviewReport:
    schema_version: int
    evidence: EvidenceBundle
    reviews: list[ModelReview]
    status: Literal["complete", "incomplete"]
    selection_overlap: float | None
    disputed_ids: list[str]
    third_model_reason: str


def review_prompt_hash(messages: list[dict[str, str]]) -> str:
    """Retain the review-message encoding, including ASCII escapes and JSON spaces."""
    return hashlib.sha256(json.dumps(messages, sort_keys=True).encode()).hexdigest()


def validate_request_evidence_bundle(
    bundle: EvidenceBundle,
    *,
    max_evidence_articles: int,
    max_excerpt_chars: int,
) -> None:
    """Validate RSS request evidence under current limits, separately from stored integrity."""
    if (
        type(bundle.schema_version) is not int
        or bundle.schema_version != SCHEMA_VERSION
        or not isinstance(bundle.items, tuple)
        or bundle.evidence_kind != "sanitized_rss_excerpt"
        or type(bundle.omitted_articles) is not int
        or bundle.omitted_articles < 0
        or not bundle.items
        or len(bundle.items) > max_evidence_articles
    ):
        raise ValueError("Unsupported or over-budget checkpoint evidence.")
    seen: set[str] = set()
    for item in bundle.items:
        if (
            not all(
                isinstance(value, str)
                for value in (item.evidence_id, item.title, item.url, item.source, item.category, item.excerpt)
            )
            or item.published is not None
            and not isinstance(item.published, str)
            or type(item.excerpt_shortened_or_sanitized) is not bool
        ):
            raise ValueError("Invalid checkpoint evidence fields.")
        if (
            not item.evidence_id
            or item.evidence_id in seen
            or len(item.excerpt) > max_excerpt_chars
            or urlparse(item.url).scheme not in {"http", "https"}
            or not urlparse(item.url).netloc
        ):
            raise ValueError("Invalid checkpoint evidence identity, URL or budget.")
        seen.add(item.evidence_id)
    if sum(len(json.dumps(asdict(item), ensure_ascii=False)) for item in bundle.items) > MAX_EVIDENCE_JSON_CHARS:
        raise ValueError("Checkpoint evidence exceeds JSON budget.")
    payload = asdict(bundle)
    payload.pop("bundle_id")
    digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    if digest != bundle.bundle_id:
        raise ValueError("Checkpoint evidence hash mismatch.")


def reusable_model_review(
    review: ModelReview,
    bundle: EvidenceBundle,
    identity: ReviewReuseIdentity | None,
) -> ModelReview | None:
    """Revalidate an eligible exact-request success and retain its recorded provenance."""
    if (
        identity is None
        or identity.bundle_id != bundle.bundle_id
        or review.status not in {"ok", "partial", "abstained"}
        or (review.slot, review.provider, review.model, review.bundle_id, review.prompt_hash)
        != (identity.slot, identity.provider, identity.model, identity.bundle_id, identity.prompt_hash)
    ):
        return None
    selections, limitations = validated_cached_selections(review, bundle)
    return replace(review, selections=selections, limitations=limitations, reused_from_checkpoint=True)


def _parse_review_envelope(
    text: str,
    max_entries: int,
    *,
    allow_closing: bool = False,
) -> tuple[list[object], list[str]]:
    if len(text) > 32000:
        raise ValueError("response exceeds review budget")
    raw = _extract_json(text)
    if allow_closing and isinstance(raw, dict):
        raw = {key: value for key, value in raw.items() if key != "closing"}
    if not isinstance(raw, dict) or set(raw) not in (
        {"selections", "limitations"},
        {"selections", "limitations", "dispositions"},
    ):
        raise ValueError("expected selections and limitations")
    selections, limitations = raw["selections"], raw["limitations"]
    if not isinstance(selections, list) or len(selections) > max_entries:
        raise ValueError("invalid selection count")
    if (
        not isinstance(limitations, list)
        or len(limitations) > 5
        or any(not isinstance(s, str) or not s.strip() or len(s) > 600 for s in limitations)
    ):
        raise ValueError("invalid limitations")
    if not selections and not limitations:
        raise ValueError("abstention needs an explanation")
    return selections, limitations


def _parse_review(text: str, bundle: EvidenceBundle) -> tuple[list[EvidenceSelection], list[str]]:
    """Strict accepted-selection contract, including when revalidating checkpoints."""
    selections, limitations = _parse_review_envelope(text, len(bundle.items))
    known = {item.evidence_id: item for item in bundle.items}
    seen: set[str] = set()
    parsed: list[EvidenceSelection] = []
    for item in selections:
        if not isinstance(item, dict) or set(item) != {"evidence_id", "reason", "quote", "confidence"}:
            raise ValueError("invalid selection schema")
        identity, reason, quote, confidence = (item[k] for k in ["evidence_id", "reason", "quote", "confidence"])
        if not all(isinstance(v, str) for v in [identity, reason, quote, confidence]):
            raise ValueError("selection fields must be strings")
        if identity not in known:
            raise ValueError("unknown evidence id")
        if identity in seen:
            raise ValueError("duplicated evidence id")
        if not reason.strip() or len(reason) > 600 or not quote.strip() or len(quote) > 200:
            raise ValueError("invalid selection text budget")
        if confidence not in {"low", "medium", "high"}:
            raise ValueError("invalid confidence")
        evidence = known[identity]
        if quote not in evidence.title and quote not in evidence.excerpt:
            raise ValueError("quote is not in supplied evidence")
        seen.add(identity)
        parsed.append(EvidenceSelection(identity, reason.strip(), quote, confidence))
    return parsed, limitations


def canonical_evidence_quote(quote: str, title: str, excerpt: str, *, max_length: int = 200) -> tuple[str, bool]:
    """Return the exact source slice after one-to-one hyphen/nonbreaking-space alignment."""
    if not isinstance(quote, str) or not quote.strip() or len(quote) > max_length:
        raise ValueError("invalid selection text budget")
    if quote in title or quote in excerpt:
        return quote, False
    typography = str.maketrans({"\u2010": "-", "\u2011": "-", "\u00a0": " ", "\u202f": " "})
    for source in (title, excerpt):
        start = source.translate(typography).find(quote.translate(typography))
        if start >= 0:
            return source[start : start + len(quote)], True
    raise ValueError("quote is not in supplied evidence")


def _parse_live_selection(item: object, bundle: EvidenceBundle, limitations: list[str]) -> EvidenceSelection:
    """Align narrow typography only after schema, types and budgets validate."""
    text = json.dumps({"selections": [item], "limitations": limitations})
    try:
        return _parse_review(text, bundle)[0][0]
    except ValueError as exc:
        # The strict parser checks schema, types and length before quote matching.
        if str(exc) != "quote is not in supplied evidence" or not isinstance(item, dict):
            raise
        evidence = next(evidence for evidence in bundle.items if evidence.evidence_id == item["evidence_id"])
        quote, normalized = canonical_evidence_quote(item["quote"], evidence.title, evidence.excerpt)
        canonical = {**item, "quote": quote}
        parsed = _parse_review(json.dumps({"selections": [canonical], "limitations": limitations}), bundle)
        return replace(parsed[0][0], typography_normalized=normalized)


def _parse_live_review(
    text: str,
    bundle: EvidenceBundle,
    *,
    max_detailed_selections: int | None = None,
    allow_closing: bool = False,
) -> tuple[list[EvidenceSelection], list[str], list[RejectedSelection]]:
    """Salvage individual entries only after the complete envelope is valid."""
    limit = len(bundle.items) if max_detailed_selections is None else min(len(bundle.items), max_detailed_selections)
    selections, limitations = _parse_review_envelope(text, limit, allow_closing=allow_closing)
    known = {item.evidence_id for item in bundle.items}
    accepted: list[EvidenceSelection] = []
    rejected: list[RejectedSelection] = []
    seen: set[str] = set()
    for index, item in enumerate(selections):
        identity = item.get("evidence_id") if isinstance(item, dict) else None
        known_identity = identity if isinstance(identity, str) and identity in known else None
        try:
            if known_identity is not None and known_identity in seen:
                raise ValueError("duplicated evidence id")
            if known_identity is not None:
                seen.add(known_identity)
            accepted.append(_parse_live_selection(item, bundle, limitations))
        except (ValueError, TypeError, KeyError) as exc:
            reason, _, _ = _rejected_output_diagnostics("", exc)
            rejected.append(RejectedSelection(index, reason, known_identity))
    return accepted, limitations, rejected


def validated_cached_selections(
    review: ModelReview,
    bundle: EvidenceBundle,
) -> tuple[list[EvidenceSelection], list[str]]:
    """Validate against exact evidence membership, independently of publication capacity."""
    selections, limitations = _parse_review(
        json.dumps(
            {
                "selections": [
                    {key: value for key, value in asdict(item).items() if key != "typography_normalized"}
                    for item in review.selections
                ],
                "limitations": review.limitations,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        bundle,
    )
    if any(type(item.typography_normalized) is not bool for item in review.selections):
        raise ValueError("Invalid checkpoint typography provenance.")
    selections = [
        replace(item, typography_normalized=original.typography_normalized)
        for item, original in zip(selections, review.selections, strict=True)
    ]
    expected_status = "ok" if selections else "abstained"
    if review.status == "partial":
        known = {item.evidence_id for item in bundle.items}
        indices = [item.index for item in review.rejected_items]
        if (
            not selections
            or not review.rejected_items
            or len(selections) + len(indices) > len(bundle.items)
            or len(set(indices)) != len(indices)
            or any(type(index) is not int or not 0 <= index < len(bundle.items) for index in indices)
            or any(item.evidence_id is not None and item.evidence_id not in known for item in review.rejected_items)
            or any(
                not isinstance(item.reason, str)
                or _rejected_output_diagnostics("", ValueError(item.reason))[0] != item.reason
                for item in review.rejected_items
            )
        ):
            raise ValueError("Invalid checkpoint partial-review provenance.")
    elif review.status != expected_status or review.rejected_items:
        raise ValueError("Checkpoint review status contradicts its selections.")
    return selections, limitations


def _rejected_output_diagnostics(text: str, exc: Exception) -> tuple[str, str, bool]:
    """Retain bounded untrusted model text, never HTTP error bodies or headers."""
    known_reasons = {
        "response exceeds review budget",
        "expected selections and limitations",
        "invalid selection count",
        "invalid limitations",
        "abstention needs an explanation",
        "invalid selection schema",
        "selection fields must be strings",
        "unknown evidence id",
        "duplicated evidence id",
        "invalid selection text budget",
        "invalid confidence",
        "quote is not in supplied evidence",
        "provider reported unfinished response",
    }
    reason = str(exc) if str(exc) in known_reasons else "invalid JSON or review contract"
    cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    cleaned = re.sub(
        r"(?:sk-[A-Za-z0-9_-]{16,}|gsk_[A-Za-z0-9_-]{16,}|AIza[A-Za-z0-9_-]{20,})",
        "[redacted credential-like text]",
        cleaned,
    )
    cleaned = re.sub(r"(?i)Bearer\s+[A-Za-z0-9._-]{16,}", "Bearer [redacted]", cleaned)
    return reason, cleaned[:32000], len(cleaned) > 32000


def validate_canonical_evidence(bundle: EvidenceBundle) -> None:
    """Validate the stored bundle itself, without constructing an empty report."""
    evidence_payload = asdict(bundle)
    evidence_payload.pop("bundle_id")
    evidence_hash = hashlib.sha256(
        json.dumps(
            evidence_payload,
            ensure_ascii=False,
            sort_keys=True,
        ).encode()
    ).hexdigest()
    known = {item.evidence_id: item for item in bundle.items}
    if (
        bundle.schema_version != 1
        or bundle.evidence_kind != "sanitized_rss_excerpt"
        or bundle.omitted_articles < 0
        or bundle.bundle_id != evidence_hash
        or len(known) != len(bundle.items)
        or any(not key for key in known)
    ):
        raise ValueError("Invalid canonical review evidence or report metadata.")


def validate_canonical_report(report: BlindReviewReport) -> None:
    bundle = report.evidence
    validate_canonical_evidence(bundle)
    known = {item.evidence_id: item for item in bundle.items}
    if (
        report.schema_version != 1
        or len(report.reviews) > 3
        or len({review.slot for review in report.reviews}) != len(report.reviews)
        or any(identity not in known for identity in report.disputed_ids)
        or report.selection_overlap is not None
        and not 0 <= report.selection_overlap <= 1
    ):
        raise ValueError("Invalid canonical review evidence or report metadata.")
    for review in report.reviews:
        if (
            review.slot not in {"primary", "secondary", "third"}
            or review.bundle_id != bundle.bundle_id
            or len({item.evidence_id for item in review.selections}) != len(review.selections)
        ):
            raise ValueError("Invalid canonical review identity.")
        for selection in review.selections:
            evidence = known.get(selection.evidence_id)
            if (
                evidence is None
                or not selection.quote.strip()
                or not selection.reason.strip()
                or selection.quote not in evidence.title
                and selection.quote not in evidence.excerpt
            ):
                raise ValueError("Canonical review quote is not in stored evidence.")
