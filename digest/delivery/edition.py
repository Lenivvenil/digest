"""Immutable prepared editions and fail-closed, single-attempt Telegram transport.

The caller must durably publish both returned hashes outside this process before
sending. Local files are not a substitute for that external serialization barrier.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from digest.delivery.issue_guard import ISSUE_FILE, _safe
from digest.delivery.issue_guard import _read as _read_legacy
from digest.delivery.telegram import _render_compact_issue
from digest.domain.delivery.outcomes import ArticleCoverage, IssueDeliveryResult, project_issue_coverage
from digest.radar.summarizer import ArticleSummary

READY_FILE = "prepared_edition.json"
CLAIM_FILE = "prepared_edition_claim.json"
RECEIPTS_FILE = "prepared_edition_receipts.json"
SCHEMA_VERSION = 1
_DISPATCH_SECONDS = 30.0


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


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _canonical(data: dict[str, Any]) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def _instant(now: datetime | None) -> datetime:
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Prepared editions require a timezone-aware time.")
    return value.astimezone(timezone.utc)


def _owner() -> tuple[str, str]:
    owner = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not re.fullmatch(r"[1-9][0-9]*", owner):
        raise ValueError("Prepared edition requires a positive private TELEGRAM_CHAT_ID.")
    return owner, _sha(owner.encode())


def _read(path: Path, expected: str | None = None) -> tuple[dict[str, Any], str]:
    raw = _safe(path).read_bytes()
    digest = _sha(raw)
    if expected is not None and digest != expected:
        raise ValueError("Prepared edition hash mismatch; publishing blocked.")
    try:
        value = json.loads(raw)
        if not isinstance(value, dict) or type(value.get("schema")) is not int or value["schema"] != SCHEMA_VERSION:
            raise ValueError
    except (ValueError, TypeError) as exc:
        raise ValueError("Invalid prepared edition JSON or schema; publishing blocked.") from exc
    return value, digest


def _write(path: Path, value: dict[str, Any], *, exclusive: bool = False) -> str:
    _safe(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    target = path if exclusive else _safe(path.with_suffix(path.suffix + ".tmp"))
    with target.open("xb" if exclusive else "wb") as handle:
        handle.write(_canonical(value))
        handle.flush()
        os.fsync(handle.fileno())
    if not exclusive:
        target.replace(path)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return _sha(path.read_bytes())


def _time(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("Invalid edition time.")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError("Edition times must use UTC.")
    return parsed


def _validate_manifest(data: dict[str, Any], owner: str, now: datetime, *, fresh: bool = True) -> None:
    try:
        body = {key: value for key, value in data.items() if key != "content_sha256"}
        if data["content_sha256"] != _sha(_canonical(body)) or data["owner_sha256"] != _sha(owner.encode()):
            raise ValueError
        start, end = _time(data["window_start"]), _time(data["window_end"])
        created, expiry = _time(data["created_at"]), _time(data["expires_at"])
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
        for prefix in ("canonical", "presentation"):
            if data[f"{prefix}_sha256"] != _sha(_canonical(data[f"{prefix}_metadata"])):
                raise ValueError
        _checkpoints(data["checkpoint_refs"], verify_bytes=False)
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise ValueError("Invalid, stale or wrong-owner prepared edition; publishing blocked.") from exc


def _load_edition(
    path: Path,
    owner: str,
    now: datetime,
    expected: str | None = None,
    *,
    fresh: bool = False,
) -> tuple[_Edition, str]:
    data, digest = _read(path, expected)
    _validate_manifest(data, owner, now, fresh=fresh)
    try:
        fields = dict(data)
        fields["articles"] = [
            _PreparedArticle(item["full_hash"], item["source"], item["covering_chunks"], ArticleSummary(**item["card"]))
            for item in data["articles"]
        ]
        return _Edition(**fields), digest
    except TypeError as exc:
        raise ValueError("Invalid prepared edition record; publishing blocked.") from exc


def _legacy_guard(cache: Path, now: datetime) -> None:
    path = cache / ISSUE_FILE
    if path.exists():
        record, _ = _read_legacy(path)
        if record.state in {"reserved", "sending", "partial", "unknown"} or (
            record.state == "confirmed" and record.date >= now.date().isoformat()
        ):
            raise ValueError("Legacy compact issue is held or already confirmed today; inspect before publishing.")


def _receipt(cache: Path, ready_sha: str, claim_sha: str, total: int) -> _Receipts | None:
    path = cache / RECEIPTS_FILE
    if not path.exists():
        return None
    value, _ = _read(path)
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
                or receipt["owner_sha256"] != _owner()[1]
            ):
                raise ValueError
        if value["state"] == "confirmed" and len(accepted) != total:
            raise ValueError
        fields = dict(value)
        fields["confirmed"] = [_ChunkReceipt(**item) for item in accepted]
        record = _Receipts(**fields)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Invalid edition receipts; publishing blocked.") from exc
    return record


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
    cache = _safe(Path(cache_dir))
    _legacy_guard(cache, start)
    path = cache / READY_FILE
    if path.exists():
        previous, previous_sha = _load_edition(path, owner, instant)
        claim_path = cache / CLAIM_FILE
        if claim_path.exists():
            claim, claim_sha = _load_claim(claim_path, previous_sha, owner_sha)
            receipt = _receipt(cache, claim.ready_sha256, claim_sha, len(previous.payloads))
            if (
                receipt is None
                or receipt.state != "confirmed"
                or not receipt.applied
                or start <= _time(previous.window_start)
            ):
                raise ValueError("Existing claimed edition is held or confirmed today; publishing blocked.")
        elif (cache / RECEIPTS_FILE).exists() or instant < _time(previous.expires_at):
            raise ValueError("An eligible ready edition already exists; publishing blocked.")
    elif (cache / CLAIM_FILE).exists() or (cache / RECEIPTS_FILE).exists():
        raise ValueError("Orphaned edition claim or receipts; publishing blocked.")
    chunks, ranges = _render_compact_issue(articles, config, notice)
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
    data = _Edition(
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
            _PreparedArticle(item.full_hash, item.source, list(item.covering_chunks), card)
            for item, card in zip(ranges, articles, strict=True)
        ],
    )
    data = replace(
        data,
        canonical_sha256=_sha(_canonical(data.canonical_metadata)),
        presentation_sha256=_sha(_canonical(data.presentation_metadata)),
    )
    body = asdict(data)
    body.pop("content_sha256")
    data = replace(data, content_sha256=_sha(_canonical(body)))
    _validate_manifest(asdict(data), owner, instant, fresh=False)
    digest = _write(path, asdict(data))
    # Publish new ready first: interruption during cleanup then fails closed on old bindings.
    for name in (CLAIM_FILE, RECEIPTS_FILE):
        _safe(cache / name).unlink(missing_ok=True)
    return path, digest


def _validate_claim(data: dict[str, Any], ready_sha: str, owner_sha: str) -> _Claim:
    try:
        claim = _Claim(**data)
        if (
            claim.ready_sha256 != ready_sha
            or claim.owner_sha256 != owner_sha
            or not isinstance(claim.claim_id, str)
            or not re.fullmatch(r"[a-f0-9]{32}", claim.claim_id)
        ):
            raise ValueError
        _time(claim.claimed_at)
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid edition claim binding; publishing blocked.") from exc
    return claim


def _load_claim(
    path: Path,
    ready_sha: str,
    owner_sha: str,
    expected: str | None = None,
) -> tuple[_Claim, str]:
    data, digest = _read(path, expected)
    return _validate_claim(data, ready_sha, owner_sha), digest


def claim_edition(
    expected_ready_sha256: str,
    *,
    cache_dir: str | Path = ".cache",
    now: datetime | None = None,
) -> tuple[Path, str]:
    """Create a single immutable claim after the ready hash was durably published."""
    instant = _instant(now)
    cache = _safe(Path(cache_dir))
    owner, owner_sha = _owner()
    _legacy_guard(cache, instant)
    data, digest = _load_edition(cache / READY_FILE, owner, instant, expected_ready_sha256, fresh=True)
    _checkpoints(data.checkpoint_refs, verify_bytes=True)
    if (cache / RECEIPTS_FILE).exists():
        raise ValueError("Edition receipts already exist; automatic replay is blocked.")
    path = cache / CLAIM_FILE
    claim = _Claim(SCHEMA_VERSION, digest, owner_sha, uuid.uuid4().hex, instant.isoformat())
    return path, _write(path, asdict(claim), exclusive=True)


def _result(data: _Edition, receipts: _Receipts) -> IssueDeliveryResult:
    return project_issue_coverage(
        (
            ArticleCoverage(article.full_hash, article.source, tuple(article.covering_chunks))
            for article in data.articles
        ),
        outcome=(
            "sent"
            if receipts.state == "confirmed"
            else ("failed" if receipts.state in {"failed", "partial"} else "unknown")
        ),
        total_chunks=len(data.payloads),
        attempted_chunks=receipts.attempted,
        confirmed_chunks=len(receipts.confirmed),
    )


def _accepted(response: httpx.Response, owner: str) -> int | None:
    value = response.json()
    if response.status_code != 200 or not isinstance(value, dict) or value.get("ok") is not True:
        return None
    receipt = value.get("result")
    if not isinstance(receipt, dict) or type(receipt.get("message_id")) is not int or receipt["message_id"] <= 0:
        return None
    chat = receipt.get("chat")
    if not isinstance(chat, dict) or type(chat.get("id")) is not int or str(chat["id"]) != owner:
        return None
    return int(receipt["message_id"])


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
    cache = _safe(Path(cache_dir))
    data, ready_sha = _load_edition(cache / READY_FILE, owner, instant, expected_ready_sha256)
    if bot_username != data.bot_username:
        raise ValueError("Prepared edition bot identity changed; publishing blocked.")
    claim, claim_sha = _load_claim(cache / CLAIM_FILE, ready_sha, owner_sha, expected_claim_sha256)
    existing = _receipt(cache, claim.ready_sha256, claim_sha, len(data.payloads))
    if existing is not None:
        if existing.state == "confirmed" and existing.applied:
            return _result(data, existing)
        raise ValueError("Edition dispatch is held; automatic replay is blocked.")
    _validate_manifest(asdict(data), owner, instant)
    _checkpoints(data.checkpoint_refs, verify_bytes=True)
    _legacy_guard(cache, instant)
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN is required to send a prepared edition.")
    receipts = _Receipts(SCHEMA_VERSION, ready_sha, claim_sha, "sending", 0, [], False)
    path = cache / RECEIPTS_FILE
    _write(path, asdict(receipts), exclusive=True)
    async with httpx.AsyncClient(follow_redirects=False) as client:
        try:
            async with asyncio.timeout(_DISPATCH_SECONDS):
                for index, payload in enumerate(data.payloads):
                    receipts.attempted = index + 1
                    _write(path, asdict(receipts))
                    response = await client.post(
                        f"https://api.telegram.org/bot{token}/sendMessage", json=payload, timeout=_DISPATCH_SECONDS
                    )
                    if 400 <= response.status_code < 500:
                        receipts.state = "partial" if receipts.confirmed else "failed"
                        break
                    message_id = _accepted(response, owner)
                    if message_id is None:
                        receipts.state = "unknown"
                        break
                    receipts.confirmed.append(_ChunkReceipt(index, message_id, owner_sha))
                    _write(path, asdict(receipts))
                else:
                    receipts.state = "confirmed"
        except (httpx.HTTPError, TimeoutError, ValueError):
            receipts.state = "unknown"
    _write(path, asdict(receipts))
    return _result(data, receipts)


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
    cache = _safe(Path(cache_dir))
    legacy_path = cache / ISSUE_FILE
    legacy_status = ""
    legacy_day = ""
    if legacy_path.exists():
        legacy, _ = _read_legacy(legacy_path)
        legacy_day = legacy.date
        if legacy.state in {"reserved", "sending", "partial", "unknown"}:
            legacy_status = "held"
        elif legacy.state == "confirmed" and legacy.date >= instant.date().isoformat():
            legacy_status = "confirmed"
    path = cache / READY_FILE
    if not path.exists():
        if (cache / CLAIM_FILE).exists() or (cache / RECEIPTS_FILE).exists():
            raise ValueError("Orphaned edition claim or receipts; publishing blocked.")
        return None, "", legacy_status or "missing"
    owner, owner_sha = _owner()
    data, ready_sha = _load_edition(path, owner, instant)
    if legacy_status == "held" or (
        legacy_status == "confirmed" and legacy_day >= _time(data.window_start).date().isoformat()
    ):
        return asdict(data), ready_sha, legacy_status
    claim_path = cache / CLAIM_FILE
    if claim_path.exists():
        claim, claim_sha = _load_claim(claim_path, ready_sha, owner_sha)
        receipts = _receipt(cache, claim.ready_sha256, claim_sha, len(data.payloads))
        if receipts is not None and not receipts.applied:
            return asdict(data), ready_sha, "held"
        if receipts is not None and receipts.state == "confirmed":
            return asdict(data), ready_sha, "confirmed" if instant < _time(data.window_end) else "expired"
        return asdict(data), ready_sha, "held"
    if (cache / RECEIPTS_FILE).exists():
        raise ValueError("Orphaned edition receipts; publishing blocked.")
    if instant < _time(data.created_at):
        raise ValueError("Prepared edition is from the future; publishing blocked.")
    if instant < _time(data.window_start):
        return asdict(data), ready_sha, "pending_window"
    return asdict(data), ready_sha, "ready" if instant < _time(data.expires_at) else "expired"


def mark_applied(expected_ready_sha256: str, *, cache_dir: str | Path = ".cache") -> None:
    """Mark known coverage reconciled only after the caller saves latest state."""
    cache = _safe(Path(cache_dir))
    owner, owner_sha = _owner()
    data, ready_sha = _load_edition(cache / READY_FILE, owner, _instant(None), expected_ready_sha256)
    claim, claim_sha = _load_claim(cache / CLAIM_FILE, ready_sha, owner_sha)
    receipts = _receipt(cache, claim.ready_sha256, claim_sha, len(data.payloads))
    if receipts is None:
        raise ValueError("Cannot apply an edition without transport receipts.")
    receipts.applied = True
    _write(cache / RECEIPTS_FILE, asdict(receipts))


def _checkpoints(references: dict[str, Any], *, verify_bytes: bool) -> None:
    for reference, digest in references.items():
        if (
            not isinstance(reference, str)
            or not reference
            or Path(reference).is_absolute()
            or any(part in {"", ".", ".."} for part in reference.split("/"))
            or "\\" in reference
            or not isinstance(digest, str)
            or not re.fullmatch(r"[a-f0-9]{64}", digest)
        ):
            raise ValueError("Invalid prepared edition checkpoint reference.")
        if verify_bytes:
            path = _safe(Path.cwd() / reference)
            if _sha(path.read_bytes()) != digest:
                raise ValueError("Prepared edition checkpoint hash mismatch; publishing blocked.")
