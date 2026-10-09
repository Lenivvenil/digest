"""Prepared edition values and pure shape, identity, window and receipt checks."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import PurePosixPath
from typing import Any

from digest.domain.delivery.outcomes import ArticleCoverage, IssueDeliveryResult, project_issue_coverage
from digest.domain.delivery.supplement import PreparedSupplement
from digest.domain.editorial.summaries import ArticleSummary

SCHEMA_VERSION = 1
READY_SCHEMA_VERSION = 2
SUPPLEMENT_SCHEMA_VERSION = 3


@dataclass(frozen=True)
class _PreparedArticle:
    full_hash: str
    source: str
    covering_chunks: list[int]
    card: ArticleSummary


@dataclass(frozen=True)
class _Edition:
    schema: int
    edition_id: str
    owner_sha256: str
    bot_username: str
    created_at: str
    window_start: str
    window_end: str
    expires_at: str
    canonical_metadata: dict[str, Any]
    presentation_metadata: dict[str, Any]
    checkpoint_refs: dict[str, str]
    producing_engine: dict[str, Any]
    payloads: list[dict[str, Any]]
    articles: list[_PreparedArticle]
    canonical_sha256: str = ""
    presentation_sha256: str = ""
    content_sha256: str = ""


@dataclass(frozen=True, kw_only=True)
class SupplementEdition(_Edition):
    supplement: PreparedSupplement
    current_review_checkpoint: str


@dataclass(frozen=True)
class _Claim:
    schema: int
    ready_sha256: str
    owner_sha256: str
    claim_id: str
    claimed_at: str


@dataclass(frozen=True)
class _ChunkReceipt:
    chunk: int
    message_id: int
    owner_sha256: str


@dataclass
class _Receipts:
    schema: int
    ready_sha256: str
    claim_sha256: str
    state: str
    attempted: int
    confirmed: list[_ChunkReceipt]
    applied: bool


# Existing value definitions retain their names and dataclass behavior.
PreparedArticle = _PreparedArticle
Edition = _Edition
Claim = _Claim
ChunkReceipt = _ChunkReceipt
Receipts = _Receipts


def parse_instant(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("Invalid edition time.")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError("Edition times must use UTC.")
    return parsed


def validate_manifest(
    data: dict[str, Any],
    owner: str,
    now: datetime,
    *,
    content_sha256: str,
    owner_sha256: str,
    canonical_sha256: str,
    presentation_sha256: str,
    fresh: bool = True,
) -> None:
    try:
        if type(data.get("schema")) is not int or data["schema"] not in (1, 2, 3):
            raise ValueError
        if data["content_sha256"] != content_sha256 or data["owner_sha256"] != owner_sha256:
            raise ValueError
        start, end = parse_instant(data["window_start"]), parse_instant(data["window_end"])
        created, expiry = parse_instant(data["created_at"]), parse_instant(data["expires_at"])
        if not (created < expiry and start < expiry <= end) or end - start != timedelta(days=1):
            raise ValueError
        if start.hour or start.minute or start.second or start.microsecond:
            raise ValueError
        if fresh and not (created <= now and start <= now < expiry):
            raise ValueError
        if not isinstance(data["edition_id"], str) or not re.fullmatch(r"[a-f0-9]{32}", data["edition_id"]):
            raise ValueError
        chunks, articles = data["payloads"], data["articles"]
        if not isinstance(chunks, list) or not chunks or not isinstance(articles, list) or not articles:
            raise ValueError
        for payload in chunks:
            if (
                not isinstance(payload, dict)
                or payload.get("chat_id") != owner
                or payload.get("parse_mode") != "MarkdownV2"
                or payload.get("disable_notification") is not False
                or not isinstance(payload.get("text"), str)
                or not 0 < len(payload["text"]) <= 4096
                or set(payload) - {"chat_id", "parse_mode", "disable_notification", "text", "reply_markup"}
            ):
                raise ValueError
            if "reply_markup" in payload and not isinstance(payload["reply_markup"], dict):
                raise ValueError
        hashes = set()
        for article in articles:
            coverage = article["covering_chunks"]
            if (
                not isinstance(article["full_hash"], str)
                or not re.fullmatch(r"[a-f0-9]{32}", article["full_hash"])
                or article["full_hash"] in hashes
                or not isinstance(article["source"], str)
                or not isinstance(coverage, list)
                or not coverage
                or any(type(index) is not int or not 0 <= index < len(chunks) for index in coverage)
                or coverage != sorted(set(coverage))
            ):
                raise ValueError
            hashes.add(article["full_hash"])
            if (
                not isinstance(article["card"], dict)
                or set(article["card"])
                != {
                    "title",
                    "link",
                    "source",
                    "category",
                    "summary",
                }
                or not all(isinstance(value, str) for value in article["card"].values())
            ):
                raise ValueError
        if not isinstance(data["bot_username"], str):
            raise ValueError
        for field in ("canonical_metadata", "presentation_metadata", "checkpoint_refs", "producing_engine"):
            if not isinstance(data[field], dict):
                raise ValueError
        for prefix, digest in (("canonical", canonical_sha256), ("presentation", presentation_sha256)):
            if data[f"{prefix}_sha256"] != digest:
                raise ValueError
        for reference, digest in data["checkpoint_refs"].items():
            validate_checkpoint_reference(reference, digest)
        if data["schema"] == SUPPLEMENT_SCHEMA_VERSION:
            validate_supplement(data)
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise ValueError("Invalid, stale or wrong-owner prepared edition; publishing blocked.") from exc


def validate_supplement(data: dict[str, Any]) -> None:
    """Validate frozen references, coverage and card identities without parsing prose."""
    import json

    from digest._serialization import canonical_json_bytes, restore_dataclass
    from digest.domain.delivery.supplement import fragment_identity, fragment_window

    supplement: PreparedSupplement = restore_dataclass(
        json.loads(canonical_json_bytes(data["supplement"])),
        PreparedSupplement,
    )
    fragment = supplement.fragment
    if (
        fragment.fragment_id != fragment_identity(fragment)
        or not fragment.text.strip()
        or fragment.origin.owner_sha256 != data["owner_sha256"]
        or fragment_window(fragment, parse_instant(data["window_start"]).date()) != "eligible"
        or supplement.coverage.fragment_id != fragment.fragment_id
        or supplement.attempt in data["checkpoint_refs"]
    ):
        raise ValueError("Invalid frozen supplement identity or window.")
    coverage = supplement.coverage.covering_chunks
    if (
        not coverage
        or coverage != tuple(sorted(set(coverage)))
        or any(type(index) is not int or not 0 <= index < len(data["payloads"]) for index in coverage)
    ):
        raise ValueError("Invalid frozen supplement coverage.")
    for path, digest in (
        (supplement.projection, supplement.projection_sha256),
        (fragment.result, fragment.result_sha256),
        (fragment.checkpoint, fragment.origin.checkpoint_sha256),
    ):
        validate_checkpoint_reference(path, digest)
        if data["checkpoint_refs"].get(path) != digest:
            raise ValueError("Frozen supplement lacks an immutable reference.")
    validate_checkpoint_reference(supplement.attempt, supplement.projection_sha256)
    current_review = data["current_review_checkpoint"]
    if (
        not isinstance(current_review, str)
        or current_review
        and (
            not current_review.endswith(".review.json")
            or current_review not in data["checkpoint_refs"]
            or current_review == fragment.checkpoint
        )
    ):
        raise ValueError("Invalid current publication review checkpoint.")
    identities = [article["full_hash"] for article in data["articles"]]
    canonical = list(data["canonical_metadata"]["cards"])
    main_count = len(canonical)
    presentation = data["presentation_metadata"]
    closing = presentation.get("closing")
    if isinstance(closing, dict) and closing.get("card") is not None:
        canonical.append(data["canonical_metadata"]["closing"]["card"])
        if closing["card"] != data["articles"][-1]["card"]:
            raise ValueError("Presented closing card differs from its ordered publication.")
    if (
        not 0 < main_count <= len(identities) <= main_count + 1
        or len(canonical) != len(identities)
        or presentation["cards"] != [article["card"] for article in data["articles"]]
    ):
        raise ValueError("Invalid supplement publication order.")
    from digest.domain.catalog.articles import article_hash

    for original, article in zip(canonical, data["articles"], strict=True):
        card = restore_dataclass(original, ArticleSummary)
        shown = article["card"]
        if article_hash(card.title, card.link) != article["full_hash"] or (
            card.title,
            card.link,
            card.source,
            card.category,
        ) != tuple(shown[key] for key in ("title", "link", "source", "category")):
            raise ValueError("Canonical and presented article identities differ.")


def validate_claim(claim: Claim, ready_sha: str, owner_sha: str) -> None:
    try:
        if (
            claim.ready_sha256 != ready_sha
            or claim.owner_sha256 != owner_sha
            or not isinstance(claim.claim_id, str)
            or not re.fullmatch(r"[a-f0-9]{32}", claim.claim_id)
        ):
            raise ValueError
        parse_instant(claim.claimed_at)
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid edition claim binding; publishing blocked.") from exc


def validate_receipts(value: dict[str, Any], ready_sha: str, claim_sha: str, total: int, owner_sha: str) -> None:
    try:
        accepted, attempted = value["confirmed"], value["attempted"]
        if (
            value["ready_sha256"] != ready_sha
            or value["claim_sha256"] != claim_sha
            or type(value.get("applied")) is not bool
            or value["state"] not in {"sending", "confirmed", "failed", "partial", "unknown"}
            or not isinstance(accepted, list)
            or type(attempted) is not int
            or not 0 <= len(accepted) <= attempted <= total
        ):
            raise ValueError
        for index, receipt in enumerate(accepted):
            if (
                receipt["chunk"] != index
                or type(receipt["message_id"]) is not int
                or receipt["message_id"] <= 0
                or receipt["owner_sha256"] != owner_sha
            ):
                raise ValueError
        if value["state"] == "confirmed" and len(accepted) != total:
            raise ValueError
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Invalid edition receipts; publishing blocked.") from exc


def validate_checkpoint_reference(reference: Any, digest: Any) -> None:
    if (
        not isinstance(reference, str)
        or not reference
        or PurePosixPath(reference).is_absolute()
        or any(part in {"", ".", ".."} for part in reference.split("/"))
        or "\\" in reference
        or not isinstance(digest, str)
        or not re.fullmatch(r"[a-f0-9]{64}", digest)
    ):
        raise ValueError("Invalid prepared edition checkpoint reference.")


def validate_dispatch_identity(data: Edition) -> None:
    """Keep known-ambiguous legacy buttons out of a new dispatch, not history."""
    if data.schema == 1:
        prefixes = [article.full_hash[:8] for article in data.articles]
        if len(prefixes) != len(set(prefixes)):
            raise ValueError("Legacy edition has ambiguous article vote identities; publishing blocked.")


def project_result(data: Edition, receipts: Receipts) -> IssueDeliveryResult:
    if data.schema not in (1, 2, 3):
        raise ValueError("Unsupported prepared edition schema.")
    return project_issue_coverage(
        (
            ArticleCoverage(article.full_hash, article.source, tuple(article.covering_chunks))
            for article in data.articles
        ),
        vote_protocol="legacy8" if data.schema == 1 else "full32",
        outcome=(
            "sent"
            if receipts.state == "confirmed"
            else ("failed" if receipts.state in {"failed", "partial"} else "unknown")
        ),
        total_chunks=len(data.payloads),
        attempted_chunks=receipts.attempted,
        confirmed_chunks=len(receipts.confirmed),
    )


def validate_owner(owner: str) -> None:
    if not re.fullmatch(r"[1-9][0-9]*", owner):
        raise ValueError("Prepared edition requires a positive private TELEGRAM_CHAT_ID.")
