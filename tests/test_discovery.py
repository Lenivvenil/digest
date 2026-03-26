"""Tests for the source discovery approval workflow."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import respx
import httpx

from src.discovery import (
    PendingSource,
    add_source_to_config,
    load_pending,
    save_pending,
    send_source_approval_message,
    source_hash,
)


# ---------------------------------------------------------------------------
# source_hash
# ---------------------------------------------------------------------------


def test_source_hash_deterministic() -> None:
    url = "https://example.com/feed"
    assert source_hash(url) == source_hash(url)


def test_source_hash_length() -> None:
    assert len(source_hash("https://example.com/feed")) == 8


def test_source_hash_different_urls() -> None:
    assert source_hash("https://a.com/feed") != source_hash("https://b.com/feed")


# ---------------------------------------------------------------------------
# PendingSource
# ---------------------------------------------------------------------------


def test_pending_source_hash_auto_computed() -> None:
    ps = PendingSource(
        name="Test",
        url="https://test.com/rss",
        category="Tech",
        discovered_at="2026-03-27T10:00:00+00:00",
    )
    assert ps.source_hash == source_hash("https://test.com/rss")


def test_pending_source_explicit_hash() -> None:
    ps = PendingSource(
        name="Test",
        url="https://test.com/rss",
        category="Tech",
        discovered_at="2026-03-27T10:00:00+00:00",
        source_hash="abcd1234",
    )
    assert ps.source_hash == "abcd1234"


# ---------------------------------------------------------------------------
# load_pending / save_pending round-trip
# ---------------------------------------------------------------------------


def test_load_pending_missing_file(tmp_path: Path) -> None:
    assert load_pending(str(tmp_path)) == []


def test_save_and_load_pending_round_trip(tmp_path: Path) -> None:
    ps = PendingSource(
        name="The New Stack",
        url="https://thenewstack.io/feed/",
        category="Cloud & Infrastructure",
        discovered_at="2026-03-27T10:00:00+00:00",
    )
    save_pending([ps], str(tmp_path))
    loaded = load_pending(str(tmp_path))
    assert len(loaded) == 1
    assert loaded[0].name == "The New Stack"
    assert loaded[0].url == "https://thenewstack.io/feed/"
    assert loaded[0].category == "Cloud & Infrastructure"
    assert loaded[0].source_hash == source_hash("https://thenewstack.io/feed/")


def test_save_pending_prunes_old_entries(tmp_path: Path) -> None:
    old_ts = (datetime.now(tz=timezone.utc) - timedelta(days=31)).isoformat()
    fresh_ts = datetime.now(tz=timezone.utc).isoformat()
    old = PendingSource(name="Old", url="https://old.com/feed", category="X", discovered_at=old_ts)
    fresh = PendingSource(name="Fresh", url="https://fresh.com/feed", category="Y", discovered_at=fresh_ts)
    save_pending([old, fresh], str(tmp_path))
    loaded = load_pending(str(tmp_path))
    assert len(loaded) == 1
    assert loaded[0].name == "Fresh"


def test_load_pending_invalid_json(tmp_path: Path) -> None:
    (tmp_path / "pending_sources.json").write_text("not json", encoding="utf-8")
    assert load_pending(str(tmp_path)) == []


def test_load_pending_skips_malformed_entries(tmp_path: Path) -> None:
    data = {
        "pending": [
            {
                "name": "Good Source",
                "url": "https://good.com/feed",
                "category": "Tech",
                "discovered_at": "2026-03-27T10:00:00+00:00",
            },
            {
                # missing "name" key
                "url": "https://bad.com/feed",
                "category": "Tech",
                "discovered_at": "2026-03-27T10:00:00+00:00",
            },
        ]
    }
    (tmp_path / "pending_sources.json").write_text(json.dumps(data), encoding="utf-8")
    loaded = load_pending(str(tmp_path))
    assert len(loaded) == 1
    assert loaded[0].name == "Good Source"


# ---------------------------------------------------------------------------
# send_source_approval_message
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_send_source_approval_message_success() -> None:
    token = "testtoken"
    chat_id = "12345"
    ps = PendingSource(
        name="The New Stack",
        url="https://thenewstack.io/feed/",
        category="Cloud & Infrastructure",
        discovered_at="2026-03-27T10:00:00+00:00",
    )

    route = respx.post(f"https://api.telegram.org/bot{token}/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
    )

    result = await send_source_approval_message(ps, token, chat_id)
    assert result is True
    assert route.called

    sent = json.loads(route.calls[0].request.content)
    assert sent["chat_id"] == chat_id
    assert "The New Stack" in sent["text"]
    # Verify callback_data fits in 64 bytes
    keyboard = sent["reply_markup"]["inline_keyboard"][0]
    for btn in keyboard:
        assert len(btn["callback_data"].encode()) <= 64
    assert any(b["callback_data"].startswith("src:ok:") for b in keyboard)
    assert any(b["callback_data"].startswith("src:no:") for b in keyboard)


@pytest.mark.asyncio
@respx.mock
async def test_send_source_approval_message_network_error() -> None:
    token = "testtoken"
    chat_id = "12345"
    ps = PendingSource(
        name="Test Feed",
        url="https://test.com/feed",
        category="Tech",
        discovered_at="2026-03-27T10:00:00+00:00",
    )

    respx.post(f"https://api.telegram.org/bot{token}/sendMessage").mock(
        side_effect=httpx.ConnectError("refused")
    )

    result = await send_source_approval_message(ps, token, chat_id)
    assert result is False


# ---------------------------------------------------------------------------
# add_source_to_config
# ---------------------------------------------------------------------------


def _make_config(tmp_path: Path, content: str) -> str:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(content, encoding="utf-8")
    return str(config_path)


def test_add_source_to_config_appends_trial_block(tmp_path: Path) -> None:
    config_content = (
        "sources:\n"
        "  - name: \"Existing\"\n"
        "    url: \"https://existing.com/feed\"\n"
        "    category: \"Tech\"\n"
        "    enabled: true\n"
        "    priority: 3\n"
    )
    config_path = _make_config(tmp_path, config_content)

    ps = PendingSource(
        name="The New Stack",
        url="https://thenewstack.io/feed/",
        category="Cloud & Infrastructure",
        discovered_at="2026-03-27T10:00:00+00:00",
    )
    add_source_to_config(config_path, ps)

    result = Path(config_path).read_text(encoding="utf-8")
    assert "The New Stack" in result
    assert "https://thenewstack.io/feed/" in result
    assert "Cloud & Infrastructure" in result
    assert "trial: true" in result
    assert "trial_days: 14" in result
    # Original content preserved
    assert "Existing" in result


def test_add_source_to_config_no_duplicate(tmp_path: Path) -> None:
    config_content = (
        "sources:\n"
        "  - name: \"Existing\"\n"
        "    url: \"https://thenewstack.io/feed/\"\n"
        "    category: \"Tech\"\n"
        "    enabled: true\n"
        "    priority: 3\n"
    )
    config_path = _make_config(tmp_path, config_content)

    ps = PendingSource(
        name="The New Stack",
        url="https://thenewstack.io/feed/",
        category="Cloud",
        discovered_at="2026-03-27T10:00:00+00:00",
    )
    add_source_to_config(config_path, ps)

    result = Path(config_path).read_text(encoding="utf-8")
    # Should not have added a second entry with the same URL
    assert result.count("https://thenewstack.io/feed/") == 1
    # Name from new source not added
    assert "The New Stack" not in result


def test_add_source_no_tmp_file_left(tmp_path: Path) -> None:
    config_content = (
        "sources:\n"
        "  - name: \"A\"\n"
        "    url: \"https://a.com/feed\"\n"
        "    category: \"X\"\n"
        "    enabled: true\n"
        "    priority: 3\n"
    )
    config_path = _make_config(tmp_path, config_content)

    ps = PendingSource(
        name="B",
        url="https://b.com/feed",
        category="Y",
        discovered_at="2026-03-27T10:00:00+00:00",
    )
    add_source_to_config(config_path, ps)

    assert not (tmp_path / "config.yaml.tmp").exists()
    assert not (tmp_path / "config.yaml.bak").exists()
