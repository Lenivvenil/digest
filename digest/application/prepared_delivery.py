"""Freeze, claim, send, inspect and apply a prepared edition in the existing effect order.

Ready and claim hashes require external durable publication before sending. Local
atomic files neither provide that barrier nor make interrupted application a transaction.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from digest.adapters.storage import edition as storage
from digest.adapters.storage.edition import CLAIM_FILE, READY_FILE, RECEIPTS_FILE
from digest.adapters.storage.issue_paths import safe_issue_path
from digest.adapters.telegram.prepared import send_prepared_chunk
from digest.domain.delivery.edition import (
    SCHEMA_VERSION,
    ChunkReceipt,
    Claim,
    Edition,
    PreparedArticle,
    Receipts,
    parse_instant,
    project_result,
    validate_owner,
)
from digest.domain.delivery.outcomes import IssueDeliveryResult
from digest.domain.editorial.summaries import ArticleSummary
from digest.presentation.telegram import render_compact_issue

_DISPATCH_SECONDS = 30.0


def _instant(now: datetime | None) -> datetime:
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Prepared editions require a timezone-aware time.")
    return value.astimezone(timezone.utc)


def _owner() -> tuple[str, str]:
    owner = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    validate_owner(owner)
    return owner, storage.content_sha256(owner.encode())


def _legacy_guard(cache: Path, now: datetime) -> None:
    record = storage.load_legacy(cache)
    if record is not None:
        if record.state in {"reserved", "sending", "partial", "unknown"} or (
            record.state == "confirmed" and record.date >= now.date().isoformat()
        ):
            raise ValueError("Legacy compact issue is held or already confirmed today; inspect before publishing.")


def prepare_edition(
    articles: list[ArticleSummary],
    config: Any,
    *,
    cache_dir: str | Path = ".cache",
    notice: str = "",
    canonical_metadata: dict[str, Any] | None = None,
    presentation_metadata: dict[str, Any] | None = None,
    checkpoint_refs: dict[str, Any] | None = None,
    producing_engine: dict[str, Any] | None = None,
    now: datetime | None = None,
    expires_at: datetime | None = None,
    publication_date: date | None = None,
) -> tuple[Path, str]:
    """Freeze an edition once; return the file and hash the runtime must persist."""
    instant = _instant(now)
    intended_day = publication_date if publication_date is not None else instant.date()
    if type(intended_day) is not date or intended_day < instant.date():
        raise ValueError("Publication date must be today or a future UTC date.")
    start = datetime.combine(intended_day, datetime.min.time(), tzinfo=timezone.utc)
    owner, owner_sha = _owner()
    cache = safe_issue_path(Path(cache_dir))
    _legacy_guard(cache, start)
    path = cache / READY_FILE
    if storage.exists(path):
        previous, previous_sha = storage.load_edition(path, owner, instant)
        claim_path = cache / CLAIM_FILE
        if storage.exists(claim_path):
            claim, claim_sha = storage.load_claim(claim_path, previous_sha, owner_sha)
            receipt = storage.load_receipts(cache, claim.ready_sha256, claim_sha, len(previous.payloads), owner_sha)
            if (
                receipt is None
                or receipt.state != "confirmed"
                or not receipt.applied
                or start <= parse_instant(previous.window_start)
            ):
                raise ValueError("Existing claimed edition is held or confirmed today; publishing blocked.")
        elif storage.exists(cache / RECEIPTS_FILE) or instant < parse_instant(previous.expires_at):
            raise ValueError("An eligible ready edition already exists; publishing blocked.")
    elif storage.exists(cache / CLAIM_FILE) or storage.exists(cache / RECEIPTS_FILE):
        raise ValueError("Orphaned edition claim or receipts; publishing blocked.")
    chunks, ranges = render_compact_issue(articles, config, notice)
    end = start + timedelta(days=1)
    payloads = []
    for chunk in chunks:
        payload: dict[str, Any] = {
            "chat_id": owner,
            "text": chunk.text,
            "parse_mode": "MarkdownV2",
            "disable_notification": False,
        }
        if chunk.reply_markup is not None:
            payload["reply_markup"] = chunk.reply_markup
        payloads.append(payload)
    data = Edition(
        schema=SCHEMA_VERSION,
        edition_id=uuid.uuid4().hex,
        owner_sha256=owner_sha,
        bot_username=getattr(config.telegram, "bot_username", ""),
        created_at=instant.isoformat(),
        window_start=start.isoformat(),
        window_end=end.isoformat(),
        expires_at=_instant(expires_at).isoformat() if expires_at else end.isoformat(),
        canonical_metadata=canonical_metadata or {},
        presentation_metadata=presentation_metadata or {},
        checkpoint_refs=checkpoint_refs or {},
        producing_engine=producing_engine or {},
        payloads=payloads,
        articles=[
            PreparedArticle(item.full_hash, item.source, list(item.covering_chunks), card)
            for item, card in zip(ranges, articles, strict=True)
        ],
    )
    data = storage.freeze_hashes(data)
    storage.validate_manifest_record(asdict(data), owner, instant, fresh=False)
    digest = storage.write_record(path, asdict(data))
    # Publish new ready first: interruption during cleanup then fails closed on old bindings.
    storage.remove_previous_dispatch(cache)
    return path, digest


def claim_edition(
    expected_ready_sha256: str,
    *,
    cache_dir: str | Path = ".cache",
    now: datetime | None = None,
) -> tuple[Path, str]:
    """Create a single immutable claim after the ready hash was durably published."""
    instant = _instant(now)
    cache = safe_issue_path(Path(cache_dir))
    owner, owner_sha = _owner()
    _legacy_guard(cache, instant)
    data, digest = storage.load_edition(cache / READY_FILE, owner, instant, expected_ready_sha256, fresh=True)
    storage.verify_checkpoints(data.checkpoint_refs, verify_bytes=True)
    if storage.exists(cache / RECEIPTS_FILE):
        raise ValueError("Edition receipts already exist; automatic replay is blocked.")
    path = cache / CLAIM_FILE
    claim = Claim(SCHEMA_VERSION, digest, owner_sha, uuid.uuid4().hex, instant.isoformat())
    return path, storage.write_record(path, asdict(claim), exclusive=True)


async def send_prepared_edition(
    expected_ready_sha256: str,
    expected_claim_sha256: str,
    *,
    enabled: bool,
    bot_username: str,
    cache_dir: str | Path = ".cache",
    now: datetime | None = None,
) -> IssueDeliveryResult:
    """Send stored bytes once after both external persistence barriers succeeded."""
    if not enabled:
        raise ValueError("Telegram delivery is disabled; prepared edition publishing blocked.")
    instant = _instant(now)
    owner, owner_sha = _owner()
    cache = safe_issue_path(Path(cache_dir))
    data, ready_sha = storage.load_edition(cache / READY_FILE, owner, instant, expected_ready_sha256)
    if bot_username != data.bot_username:
        raise ValueError("Prepared edition bot identity changed; publishing blocked.")
    claim, claim_sha = storage.load_claim(cache / CLAIM_FILE, ready_sha, owner_sha, expected_claim_sha256)
    existing = storage.load_receipts(cache, claim.ready_sha256, claim_sha, len(data.payloads), owner_sha)
    if existing is not None:
        if existing.state == "confirmed" and existing.applied:
            return project_result(data, existing)
        raise ValueError("Edition dispatch is held; automatic replay is blocked.")
    storage.validate_manifest_record(asdict(data), owner, instant)
    storage.verify_checkpoints(data.checkpoint_refs, verify_bytes=True)
    _legacy_guard(cache, instant)
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN is required to send a prepared edition.")
    receipts = Receipts(SCHEMA_VERSION, ready_sha, claim_sha, "sending", 0, [], False)
    path = cache / RECEIPTS_FILE
    storage.write_record(path, asdict(receipts), exclusive=True)
    async with httpx.AsyncClient(follow_redirects=False) as client:
        try:
            async with asyncio.timeout(_DISPATCH_SECONDS):
                for index, payload in enumerate(data.payloads):
                    receipts.attempted = index + 1
                    storage.write_record(path, asdict(receipts))
                    outcome, message_id = await send_prepared_chunk(
                        client,
                        token,
                        payload,
                        owner,
                        timeout_seconds=_DISPATCH_SECONDS,
                    )
                    if outcome == "failed":
                        receipts.state = "partial" if receipts.confirmed else "failed"
                        break
                    if message_id is None:
                        receipts.state = "unknown"
                        break
                    receipts.confirmed.append(ChunkReceipt(index, message_id, owner_sha))
                    storage.write_record(path, asdict(receipts))
                else:
                    receipts.state = "confirmed"
        except (httpx.HTTPError, TimeoutError, ValueError):
            receipts.state = "unknown"
    storage.write_record(path, asdict(receipts))
    return project_result(data, receipts)


def inspect_edition(
    *,
    cache_dir: str | Path = ".cache",
    now: datetime | None = None,
) -> tuple[dict[str, Any] | None, str, str]:
    """Inspect persisted selection without rendering, fetching or generation.

    Status is ready, pending_window, confirmed, held, expired or missing. A claim is held until
    its sender proves completion; callers never regenerate or reclaim it.
    """
    instant = _instant(now)
    cache = safe_issue_path(Path(cache_dir))
    legacy_status = ""
    legacy_day = ""
    legacy = storage.load_legacy(cache)
    if legacy is not None:
        legacy_day = legacy.date
        if legacy.state in {"reserved", "sending", "partial", "unknown"}:
            legacy_status = "held"
        elif legacy.state == "confirmed" and legacy.date >= instant.date().isoformat():
            legacy_status = "confirmed"
    path = cache / READY_FILE
    if not storage.exists(path):
        if storage.exists(cache / CLAIM_FILE) or storage.exists(cache / RECEIPTS_FILE):
            raise ValueError("Orphaned edition claim or receipts; publishing blocked.")
        return None, "", legacy_status or "missing"
    owner, owner_sha = _owner()
    data, ready_sha = storage.load_edition(path, owner, instant)
    if legacy_status == "held" or (
        legacy_status == "confirmed" and legacy_day >= parse_instant(data.window_start).date().isoformat()
    ):
        return asdict(data), ready_sha, legacy_status
    claim_path = cache / CLAIM_FILE
    if storage.exists(claim_path):
        claim, claim_sha = storage.load_claim(claim_path, ready_sha, owner_sha)
        receipts = storage.load_receipts(cache, claim.ready_sha256, claim_sha, len(data.payloads), owner_sha)
        if receipts is not None and not receipts.applied:
            return asdict(data), ready_sha, "held"
        if receipts is not None and receipts.state == "confirmed":
            return asdict(data), ready_sha, "confirmed" if instant < parse_instant(data.window_end) else "expired"
        return asdict(data), ready_sha, "held"
    if storage.exists(cache / RECEIPTS_FILE):
        raise ValueError("Orphaned edition receipts; publishing blocked.")
    if instant < parse_instant(data.created_at):
        raise ValueError("Prepared edition is from the future; publishing blocked.")
    if instant < parse_instant(data.window_start):
        return asdict(data), ready_sha, "pending_window"
    return asdict(data), ready_sha, "ready" if instant < parse_instant(data.expires_at) else "expired"


def mark_applied(expected_ready_sha256: str, *, cache_dir: str | Path = ".cache") -> None:
    """Mark known coverage reconciled only after the caller saves latest state."""
    cache = safe_issue_path(Path(cache_dir))
    owner, owner_sha = _owner()
    data, ready_sha = storage.load_edition(cache / READY_FILE, owner, _instant(None), expected_ready_sha256)
    claim, claim_sha = storage.load_claim(cache / CLAIM_FILE, ready_sha, owner_sha)
    receipts = storage.load_receipts(cache, claim.ready_sha256, claim_sha, len(data.payloads), owner_sha)
    if receipts is None:
        raise ValueError("Cannot apply an edition without transport receipts.")
    receipts.applied = True
    storage.write_record(cache / RECEIPTS_FILE, asdict(receipts))
