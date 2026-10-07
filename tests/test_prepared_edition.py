"""Frozen edition integrity, migration and single-attempt failure boundaries."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import httpx
import pytest
import respx

from digest.delivery import edition
from digest.radar.summarizer import ArticleSummary

NOW = datetime(2026, 10, 4, 12, tzinfo=timezone.utc)
API = "https://api.telegram.org/bottoken/sendMessage"


@pytest.fixture(autouse=True)
def credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    for name in ("ALL_PROXY", "all_proxy", "HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
        monkeypatch.delenv(name, raising=False)


def prepare(tmp_path: Path, *, long: bool = False) -> tuple[dict[str, Any], str, str]:
    config = SimpleNamespace(telegram=SimpleNamespace(bot_username="mybot"), radar=SimpleNamespace(language="en"))
    articles = [ArticleSummary("Frozen title", "https://example.com/1", "Source", "AI", "Frozen summary")]
    if long:
        articles.append(ArticleSummary("Second", "https://example.com/2", "Other", "AI", "word " * 1600))
    path, ready_sha = edition.prepare_edition(
        articles,
        config,
        cache_dir=tmp_path,
        now=NOW,
        canonical_metadata={"cards": [{"title": "Canonical title"}], "config_sha256": "old config"},
        presentation_metadata={"language": "ru"},
        checkpoint_refs={},
        producing_engine={"commit": "old engine", "prompt": "old prompt"},
    )
    _, claim_sha = edition.claim_edition(ready_sha, cache_dir=tmp_path, now=NOW)
    return json.loads(path.read_text()), ready_sha, claim_sha


def success(message_id: int = 1, owner: int = 123) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "result": {"message_id": message_id, "chat": {"id": owner}}})


@respx.mock
async def test_sender_uses_frozen_payload_without_rendering_or_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, ready, claim = prepare(tmp_path)
    renderer = Mock(side_effect=AssertionError("sender rendered"))
    monkeypatch.setattr(edition, "render_compact_issue", renderer)
    route = respx.post(API).mock(return_value=success())
    result = await edition.send_prepared_edition(
        ready, claim, cache_dir=tmp_path, enabled=True, bot_username="mybot", now=NOW
    )
    assert result.complete and result.sent == 1
    assert json.loads(route.calls[0].request.content) == manifest["payloads"][0]
    renderer.assert_not_called()
    assert manifest["canonical_metadata"]["cards"][0]["title"] == "Canonical title"
    assert manifest["producing_engine"]["commit"] == "old engine"
    receipts = json.loads((tmp_path / edition.RECEIPTS_FILE).read_text())
    assert receipts["confirmed"][0]["message_id"] == 1
    edition.mark_applied(ready, cache_dir=tmp_path)
    again = await edition.send_prepared_edition(
        ready, claim, cache_dir=tmp_path, enabled=True, bot_username="mybot", now=NOW + timedelta(days=2)
    )
    assert again.complete and route.call_count == 1


@pytest.mark.parametrize(
    "receipt",
    [
        {"ok": True},
        {"ok": True, "result": {}},
        {"ok": True, "result": {"message_id": 0, "chat": {"id": 123}}},
        {"ok": True, "result": {"message_id": True, "chat": {"id": 123}}},
        {"ok": True, "result": {"message_id": 1, "chat": {"id": 999}}},
        {"ok": True, "result": {"message_id": 1, "chat": {"id": "123"}}},
        {"ok": False},
    ],
)
@respx.mock
async def test_ambiguous_receipt_holds_and_never_replays(tmp_path: Path, receipt: dict[str, Any]) -> None:
    _, ready, claim = prepare(tmp_path)
    route = respx.post(API).mock(return_value=httpx.Response(200, json=receipt))
    result = await edition.send_prepared_edition(
        ready, claim, cache_dir=tmp_path, enabled=True, bot_username="mybot", now=NOW
    )
    assert result.outcome == "unknown" and result.sent == 0
    assert edition.inspect_edition(cache_dir=tmp_path, now=NOW)[2] == "held"
    with pytest.raises(ValueError, match="held"):
        await edition.send_prepared_edition(
            ready, claim, cache_dir=tmp_path, enabled=True, bot_username="mybot", now=NOW
        )
    assert route.call_count == 1


@pytest.mark.parametrize("status", [400, 429, 500, 302])
@respx.mock
async def test_http_errors_never_retry_or_fallback(tmp_path: Path, status: int) -> None:
    _, ready, claim = prepare(tmp_path)
    route = respx.post(API).mock(return_value=httpx.Response(status, json={"ok": False}))
    result = await edition.send_prepared_edition(
        ready, claim, cache_dir=tmp_path, enabled=True, bot_username="mybot", now=NOW
    )
    assert result.sent == 0 and route.call_count == 1
    with pytest.raises(ValueError, match="held"):
        await edition.send_prepared_edition(
            ready, claim, cache_dir=tmp_path, enabled=True, bot_username="mybot", now=NOW
        )


@respx.mock
async def test_partial_receipts_keep_only_complete_article_coverage(tmp_path: Path) -> None:
    manifest, ready, claim = prepare(tmp_path, long=True)
    route = respx.post(API).mock(side_effect=[success(), httpx.ReadTimeout("lost receipt")])
    result = await edition.send_prepared_edition(
        ready, claim, cache_dir=tmp_path, enabled=True, bot_username="mybot", now=NOW
    )
    assert result.confirmed_chunks == 1 and result.attempted_chunks == 2 and result.outcome == "unknown"
    assert result.delivered_hashes == {manifest["articles"][0]["full_hash"]}
    assert route.call_count == 2
    with pytest.raises(ValueError, match="held"):
        await edition.send_prepared_edition(
            ready, claim, cache_dir=tmp_path, enabled=True, bot_username="mybot", now=NOW
        )


@respx.mock
async def test_crash_after_acceptance_before_receipt_is_held(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, ready, claim = prepare(tmp_path)
    original_write = edition._write

    def crash(path: Path, value: dict[str, Any], *, exclusive: bool = False) -> str:
        if value.get("confirmed"):
            raise OSError("disk lost")
        return original_write(path, value, exclusive=exclusive)

    monkeypatch.setattr(edition, "_write", crash)
    route = respx.post(API).mock(return_value=success())
    with pytest.raises(OSError):
        await edition.send_prepared_edition(
            ready, claim, cache_dir=tmp_path, enabled=True, bot_username="mybot", now=NOW
        )
    monkeypatch.setattr(edition, "_write", original_write)
    with pytest.raises(ValueError, match="held"):
        await edition.send_prepared_edition(
            ready, claim, cache_dir=tmp_path, enabled=True, bot_username="mybot", now=NOW
        )
    assert route.call_count == 1


@pytest.mark.parametrize("change", ["ready_hash", "claim_hash", "owner", "expired", "disabled", "tamper"])
@respx.mock
async def test_validation_blocks_before_http(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    manifest, ready, claim = prepare(tmp_path)
    if change == "ready_hash":
        ready = "0" * 64
    elif change == "claim_hash":
        claim = "0" * 64
    elif change == "owner":
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "999")
    elif change == "tamper":
        manifest["payloads"][0]["text"] = "changed"
        (tmp_path / edition.READY_FILE).write_text(json.dumps(manifest))
        ready = hashlib.sha256((tmp_path / edition.READY_FILE).read_bytes()).hexdigest()
    with pytest.raises(ValueError):
        await edition.send_prepared_edition(
            ready,
            claim,
            cache_dir=tmp_path,
            enabled=change != "disabled",
            bot_username="mybot",
            now=NOW + timedelta(days=1) if change == "expired" else NOW,
        )
    assert not respx.calls


def test_inspect_claim_and_no_ready_overwrite(tmp_path: Path) -> None:
    _, ready, _ = prepare(tmp_path)
    assert edition.inspect_edition(cache_dir=tmp_path, now=NOW)[2] == "held"
    with pytest.raises(FileExistsError):
        edition.claim_edition(ready, cache_dir=tmp_path, now=NOW)
    with pytest.raises(ValueError, match="held"):
        prepare(tmp_path)


@pytest.mark.parametrize("state", ["reserved", "sending", "partial", "unknown", "confirmed"])
def test_legacy_migration_blocks_duplicate(tmp_path: Path, state: str) -> None:
    accepted = 1 if state in {"partial", "confirmed"} else 0
    marker = {
        "schema": 1,
        "generation": "a" * 32,
        "date": NOW.date().isoformat(),
        "config_sha256": "b" * 64,
        "owner_sha256": "c" * 64,
        "state": state,
        "accepted_count": accepted,
        "attempted_count": accepted,
    }
    (tmp_path / "compact_issue.json").write_text(json.dumps(marker))
    assert edition.inspect_edition(cache_dir=tmp_path, now=NOW)[2] == ("confirmed" if state == "confirmed" else "held")
    with pytest.raises(ValueError, match="Legacy"):
        prepare(tmp_path)


def test_expired_unclaimed_edition_can_be_replaced(tmp_path: Path) -> None:
    prepare(tmp_path)
    (tmp_path / edition.CLAIM_FILE).unlink()
    assert edition.inspect_edition(cache_dir=tmp_path, now=NOW)[2] == "ready"
    assert edition.inspect_edition(cache_dir=tmp_path, now=NOW + timedelta(days=1))[2] == "expired"
    config = SimpleNamespace(telegram=SimpleNamespace(bot_username=""), radar=SimpleNamespace(language="en"))
    with pytest.raises(ValueError, match="eligible"):
        edition.prepare_edition(
            [ArticleSummary("A", "https://a", "S", "C", "text")], config, cache_dir=tmp_path, now=NOW
        )
    edition.prepare_edition(
        [ArticleSummary("A", "https://a", "S", "C", "text")], config, cache_dir=tmp_path, now=NOW + timedelta(days=1)
    )


@respx.mock
async def test_unapplied_confirmation_holds_across_days_and_cannot_replay(tmp_path: Path) -> None:
    _, ready, claim = prepare(tmp_path)
    route = respx.post(API).mock(return_value=success())
    await edition.send_prepared_edition(
        ready,
        claim,
        cache_dir=tmp_path,
        enabled=True,
        bot_username="mybot",
        now=NOW,
    )
    assert edition.inspect_edition(cache_dir=tmp_path, now=NOW + timedelta(days=1))[2] == "held"
    with pytest.raises(ValueError, match="held"):
        await edition.send_prepared_edition(
            ready,
            claim,
            cache_dir=tmp_path,
            enabled=True,
            bot_username="mybot",
            now=NOW,
        )
    assert route.call_count == 1
    edition.mark_applied(ready, cache_dir=tmp_path)
    assert edition.inspect_edition(cache_dir=tmp_path, now=NOW)[2] == "confirmed"
    assert edition.inspect_edition(cache_dir=tmp_path, now=NOW + timedelta(days=1))[2] == "expired"


@respx.mock
async def test_changed_bot_identity_cannot_send(tmp_path: Path) -> None:
    _, ready, claim = prepare(tmp_path)
    with pytest.raises(ValueError, match="identity"):
        await edition.send_prepared_edition(
            ready,
            claim,
            cache_dir=tmp_path,
            enabled=True,
            bot_username="differentbot",
            now=NOW,
        )
    assert not respx.calls
    assert not (tmp_path / edition.RECEIPTS_FILE).exists()


@respx.mock
async def test_definite_partial_error_differs_from_uncertain_receipt(tmp_path: Path) -> None:
    _, ready, claim = prepare(tmp_path, long=True)
    route = respx.post(API).mock(side_effect=[success(), httpx.Response(400, text="not json")])
    result = await edition.send_prepared_edition(
        ready,
        claim,
        cache_dir=tmp_path,
        enabled=True,
        bot_username="mybot",
        now=NOW,
    )
    assert result.outcome == "failed"
    receipts = json.loads((tmp_path / edition.RECEIPTS_FILE).read_text())
    assert receipts["state"] == "partial" and route.call_count == 2
    edition.mark_applied(ready, cache_dir=tmp_path)
    assert edition.inspect_edition(cache_dir=tmp_path, now=NOW + timedelta(days=5))[2] == "held"


@respx.mock
async def test_checkpoint_bytes_bound_at_claim_and_send(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    checkpoint = tmp_path / "archive.md"
    checkpoint.write_text("canonical approved archive")
    digest = hashlib.sha256(checkpoint.read_bytes()).hexdigest()
    config = SimpleNamespace(telegram=SimpleNamespace(bot_username="mybot"), radar=SimpleNamespace(language="en"))
    path, ready = edition.prepare_edition(
        [ArticleSummary("Title", "https://example.com", "Source", "AI", "Summary")],
        config,
        cache_dir=tmp_path / "cache",
        now=NOW,
        checkpoint_refs={"archive.md": digest},
    )
    checkpoint.write_text("modified")
    with pytest.raises(ValueError, match="checkpoint hash"):
        edition.claim_edition(ready, cache_dir=path.parent, now=NOW)
    checkpoint.write_text("canonical approved archive")
    _, claim = edition.claim_edition(ready, cache_dir=path.parent, now=NOW)
    checkpoint.write_text("modified again")
    with pytest.raises(ValueError, match="checkpoint hash"):
        await edition.send_prepared_edition(
            ready,
            claim,
            cache_dir=path.parent,
            enabled=True,
            bot_username="mybot",
            now=NOW,
        )
    assert not respx.calls


@pytest.mark.parametrize("reference", ["/tmp/foreign", "../outside", "archive/../bad", "./archive", "a//b"])
def test_checkpoint_paths_cannot_escape_runtime(tmp_path: Path, reference: str) -> None:
    config = SimpleNamespace(telegram=SimpleNamespace(bot_username="mybot"), radar=SimpleNamespace(language="en"))
    with pytest.raises(ValueError, match="Invalid"):
        edition.prepare_edition(
            [ArticleSummary("Title", "https://example.com", "Source", "AI", "Summary")],
            config,
            cache_dir=tmp_path,
            now=NOW,
            checkpoint_refs={reference: "a" * 64},
        )


def test_receipt_binding_tampering_blocks_inspection(tmp_path: Path) -> None:
    _, ready, claim = prepare(tmp_path)
    (tmp_path / edition.RECEIPTS_FILE).write_text(
        json.dumps(
            {
                "schema": 1,
                "ready_sha256": ready,
                "claim_sha256": "0" * 64,
                "state": "confirmed",
                "attempted": 1,
                "applied": True,
                "confirmed": [{"chunk": 0, "message_id": 1, "owner_sha256": hashlib.sha256(b"123").hexdigest()}],
            }
        )
    )
    assert claim != "0" * 64
    with pytest.raises(ValueError, match="receipts"):
        edition.inspect_edition(cache_dir=tmp_path, now=NOW)


def test_ready_window_ends_at_utc_midnight_not_twenty_four_hours_after_preparation(tmp_path: Path) -> None:
    config = SimpleNamespace(telegram=SimpleNamespace(bot_username="mybot"), radar=SimpleNamespace(language="en"))
    cards = [ArticleSummary("A", "https://example.com/a", "S", "C", "Claim")]
    before = NOW.replace(hour=23, minute=59)
    midnight = (NOW + timedelta(days=1)).replace(hour=0)
    _, ready = edition.prepare_edition(cards, config, cache_dir=tmp_path, now=before)
    assert edition.inspect_edition(cache_dir=tmp_path, now=before)[2] == "ready"
    assert edition.inspect_edition(cache_dir=tmp_path, now=midnight)[2] == "expired"
    with pytest.raises(ValueError):
        edition.claim_edition(ready, cache_dir=tmp_path, now=midnight)
    with pytest.raises(ValueError):
        edition.prepare_edition(
            cards, config, cache_dir=tmp_path / "other", now=before, expires_at=midnight + timedelta(minutes=1)
        )


def prepare_tomorrow(tmp_path: Path) -> tuple[Path, str]:
    config = SimpleNamespace(telegram=SimpleNamespace(bot_username="mybot"), radar=SimpleNamespace(language="en"))
    return edition.prepare_edition(
        [ArticleSummary("Tomorrow", "https://example.com/tomorrow", "Source", "AI", "Prepared summary")],
        config,
        cache_dir=tmp_path,
        now=NOW,
        publication_date=(NOW + timedelta(days=1)).date(),
    )


@respx.mock
async def test_prepare_tomorrow_after_confirmed_today_and_send_only_in_window(tmp_path: Path) -> None:
    _, ready, claim = prepare(tmp_path)
    route = respx.post(API).mock(return_value=success())
    await edition.send_prepared_edition(
        ready,
        claim,
        cache_dir=tmp_path,
        enabled=True,
        bot_username="mybot",
        now=NOW,
    )
    edition.mark_applied(ready, cache_dir=tmp_path)
    assert edition.inspect_edition(cache_dir=tmp_path, now=NOW)[2] == "confirmed"
    path, tomorrow_ready = prepare_tomorrow(tmp_path)
    manifest = json.loads(path.read_text())
    start = NOW.replace(hour=0) + timedelta(days=1)
    assert manifest["created_at"] == NOW.isoformat()
    assert manifest["window_start"] == start.isoformat()
    assert manifest["window_end"] == (start + timedelta(days=1)).isoformat()
    assert manifest["expires_at"] == manifest["window_end"]
    assert edition.inspect_edition(cache_dir=tmp_path, now=NOW)[2] == "pending_window"
    with pytest.raises(ValueError):
        edition.claim_edition(tomorrow_ready, cache_dir=tmp_path, now=NOW)
    assert not (tmp_path / edition.CLAIM_FILE).exists()
    with pytest.raises(ValueError, match="eligible"):
        prepare_tomorrow(tmp_path)
    frozen_future = path.read_bytes()
    with pytest.raises(ValueError, match="eligible"):
        prepare(tmp_path)  # Default today must not displace the prepared future edition.
    assert path.read_bytes() == frozen_future
    assert edition.inspect_edition(cache_dir=tmp_path, now=start)[2] == "ready"
    _, tomorrow_claim = edition.claim_edition(tomorrow_ready, cache_dir=tmp_path, now=start)
    with pytest.raises(ValueError):
        await edition.send_prepared_edition(
            tomorrow_ready,
            tomorrow_claim,
            cache_dir=tmp_path,
            enabled=True,
            bot_username="mybot",
            now=NOW,
        )
    assert route.call_count == 1
    result = await edition.send_prepared_edition(
        tomorrow_ready,
        tomorrow_claim,
        cache_dir=tmp_path,
        enabled=True,
        bot_username="mybot",
        now=start,
    )
    assert result.complete and route.call_count == 2


def test_future_prepare_preserves_unresolved_claim_hold(tmp_path: Path) -> None:
    prepare(tmp_path)
    ready_bytes = (tmp_path / edition.READY_FILE).read_bytes()
    with pytest.raises(ValueError, match="held"):
        prepare_tomorrow(tmp_path)
    assert (tmp_path / edition.READY_FILE).read_bytes() == ready_bytes


def test_legacy_confirmed_today_allows_tomorrow_but_cannot_hide_future_ready(tmp_path: Path) -> None:
    marker = {
        "schema": 1,
        "generation": "a" * 32,
        "date": NOW.date().isoformat(),
        "config_sha256": "b" * 64,
        "owner_sha256": "c" * 64,
        "state": "confirmed",
        "accepted_count": 1,
        "attempted_count": 1,
    }
    path = tmp_path / "compact_issue.json"
    path.write_text(json.dumps(marker))
    original = path.read_bytes()
    assert edition.inspect_edition(cache_dir=tmp_path, now=NOW)[2] == "confirmed"
    with pytest.raises(ValueError, match="Legacy"):
        prepare(tmp_path)
    _, ready = prepare_tomorrow(tmp_path)
    assert edition.inspect_edition(cache_dir=tmp_path, now=NOW)[2] == "pending_window"
    assert path.read_bytes() == original
    edition.claim_edition(ready, cache_dir=tmp_path, now=NOW.replace(hour=0) + timedelta(days=1))


def test_past_publication_day_rejected(tmp_path: Path) -> None:
    config = SimpleNamespace(telegram=SimpleNamespace(bot_username="mybot"), radar=SimpleNamespace(language="en"))
    with pytest.raises(ValueError, match="Publication date"):
        edition.prepare_edition(
            [ArticleSummary("Title", "https://example.com", "Source", "AI", "Summary")],
            config,
            cache_dir=tmp_path,
            now=NOW,
            publication_date=(NOW - timedelta(days=1)).date(),
        )
    assert not (tmp_path / edition.READY_FILE).exists()
