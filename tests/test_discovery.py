"""Tests for the source discovery approval workflow."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
import respx
import yaml

from digest.discovery import (
    PendingSource,
    add_source_to_config,
    load_pending,
    proposal_binding,
    resolve_pending_proposal,
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


@pytest.mark.parametrize("url,expected", [
    ("https://example.com/feed", "ca7a0f39"),
    ("https://example.com/café?q=1", "386024ef"),
])
def test_source_hash_preserves_existing_ids_without_security_use(url: str, expected: str) -> None:
    import hashlib

    with patch("digest.domain.catalog.proposals.hashlib.md5", wraps=hashlib.md5) as md5:
        assert source_hash(url) == expected
    md5.assert_called_once_with(url.encode(), usedforsecurity=False)


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


def _fresh_proposal() -> PendingSource:
    return PendingSource(
        name="Test Feed",
        url="https://test.com/feed",
        category="Tech",
        discovered_at=(datetime.now(tz=timezone.utc) - timedelta(minutes=1)).isoformat(),
    )


@pytest.mark.parametrize("naive", [False, True])
def test_resolve_pending_proposal_accepts_recent_timestamp(naive: bool) -> None:
    source = _fresh_proposal()
    discovered = datetime.now(tz=timezone(timedelta(hours=5))) - timedelta(days=29)
    if naive:
        discovered = discovered.astimezone(timezone.utc).replace(tzinfo=None)
    source.discovered_at = discovered.isoformat()
    assert resolve_pending_proposal([source], source.source_hash) is source


@pytest.mark.parametrize("days_old", [-1, 31])
def test_resolve_pending_proposal_rejects_future_or_expired(days_old: int) -> None:
    source = _fresh_proposal()
    source.discovered_at = (datetime.now(tz=timezone.utc) - timedelta(days=days_old)).isoformat()
    assert resolve_pending_proposal([source], source.source_hash) is None


def test_resolve_pending_proposal_rejects_invalid_timestamp() -> None:
    source = _fresh_proposal()
    source.discovered_at = "invalid timestamp"
    assert resolve_pending_proposal([source], source.source_hash) is None


@pytest.mark.parametrize("field_name", ["name", "url", "category", "discovered_at"])
def test_resolve_pending_proposal_rejects_non_string_fields(
    field_name: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _fresh_proposal()
    monkeypatch.setattr(source, field_name, None)
    assert resolve_pending_proposal([source], source.source_hash) is None


def test_resolve_pending_proposal_rejects_ambiguous_hash_even_if_other_entry_expired() -> None:
    source = _fresh_proposal()
    expired = replace(source, discovered_at="2000-01-01T00:00:00+00:00")
    assert resolve_pending_proposal([source, expired], source.source_hash) is None


def test_resolve_pending_proposal_rejects_hash_url_mismatch() -> None:
    source = _fresh_proposal()
    source.url = "https://different.com/feed"
    assert resolve_pending_proposal([source], source.source_hash) is None


@pytest.mark.parametrize("hash8", ["", "bad-hash", "FFFFFFFF", "00000000"])
def test_resolve_pending_proposal_rejects_invalid_or_unknown_hash(hash8: str) -> None:
    assert resolve_pending_proposal([_fresh_proposal()], hash8) is None


def test_proposal_binding_is_deterministic_and_covers_exact_identity() -> None:
    source = _fresh_proposal()
    expected = proposal_binding(source)
    assert expected == proposal_binding(replace(source))
    assert len(expected) == 64
    changed = [
        replace(source, name="Changed name"),
        replace(source, url="https://changed.com/feed"),
        replace(source, category="Changed category"),
        replace(source, discovered_at=source.discovered_at + "0"),
        replace(source, source_hash="abcdef12"),
    ]
    assert all(proposal_binding(other) != expected for other in changed)


# ---------------------------------------------------------------------------
# load_pending / save_pending round-trip
# ---------------------------------------------------------------------------


def test_load_pending_missing_file(tmp_path: Path) -> None:
    assert load_pending(str(tmp_path)) == []
    assert load_pending(str(tmp_path), strict=True) == []


def test_load_pending_strict_accepts_empty_and_expired_or_future_entries(tmp_path: Path) -> None:
    path = tmp_path / "pending_sources.json"
    path.write_text('{"pending": []}', encoding="utf-8")
    assert load_pending(str(tmp_path), strict=True) == []
    entries = [
        {
            "name": "Test", "url": "https://test.com/feed", "category": "Tech",
            "discovered_at": timestamp,
        }
        for timestamp in ("2000-01-01T00:00:00+00:00", "2999-01-01T00:00:00+00:00")
    ]
    path.write_text(json.dumps({"pending": entries}), encoding="utf-8")
    assert len(load_pending(str(tmp_path), strict=True)) == 2


@pytest.mark.parametrize("content", ["not json", "null", "[]", "{}", '{"pending": null}', '{"pending": {}}'])
def test_load_pending_strict_rejects_invalid_json_or_envelope(tmp_path: Path, content: str) -> None:
    (tmp_path / "pending_sources.json").write_text(content, encoding="utf-8")
    with pytest.raises(ValueError):
        load_pending(str(tmp_path), strict=True)
    assert load_pending(str(tmp_path)) == []


@pytest.mark.parametrize("field_name", ["name", "url", "category", "discovered_at", "source_hash"])
def test_load_pending_strict_rejects_malformed_item_fields(tmp_path: Path, field_name: str) -> None:
    entry = {
        "name": "Test", "url": "https://test.com/feed", "category": "Tech",
        "discovered_at": "2026-10-02T00:00:00+00:00", "source_hash": "abcdef12",
    }
    malformed: dict[str, object] = {**entry, field_name: None}
    (tmp_path / "pending_sources.json").write_text(json.dumps({"pending": [entry, malformed]}), encoding="utf-8")
    with pytest.raises(ValueError):
        load_pending(str(tmp_path), strict=True)


def test_load_pending_strict_propagates_read_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_open(self: Path, *args: object, **kwargs: object) -> None:
        raise PermissionError("cache unreadable")

    monkeypatch.setattr(Path, "open", fail_open)
    with pytest.raises(PermissionError, match="cache unreadable"):
        load_pending(str(tmp_path), strict=True)
    assert load_pending(str(tmp_path)) == []


def test_save_and_load_pending_round_trip(tmp_path: Path) -> None:
    ps = PendingSource(
        name="The New Stack",
        url="https://thenewstack.io/feed/",
        category="Cloud & Infrastructure",
        discovered_at=datetime.now(tz=timezone.utc).isoformat(),
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


def test_save_pending_strict_propagates_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = _fresh_proposal()
    save_pending([original], str(tmp_path))

    def fail_write(path: Path, data: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("digest.adapters.storage.pending_sources.atomic_json_write", fail_write)
    with pytest.raises(OSError, match="disk full"):
        save_pending([], str(tmp_path), strict=True)
    assert load_pending(str(tmp_path)) == [original]
    save_pending([], str(tmp_path))
    assert load_pending(str(tmp_path)) == [original]


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

    result = await send_source_approval_message(ps, token, chat_id, "digest_test_bot")
    assert result.status == "confirmed" and result.message_id == 1
    assert route.called

    sent = json.loads(route.calls[0].request.content)
    assert sent["chat_id"] == chat_id
    assert "The New Stack" in sent["text"]
    keyboard = sent["reply_markup"]["inline_keyboard"][0]
    assert keyboard == [
        {"text": "Add", "url": f"https://t.me/digest_test_bot?start=source_ok_{ps.source_hash}"},
        {"text": "Reject", "url": f"https://t.me/digest_test_bot?start=source_no_{ps.source_hash}"},
    ]
    assert "tap Start" in sent["text"]
    assert "next run" in sent["text"]
    assert "24 hours" in sent["text"]


@pytest.mark.asyncio
@pytest.mark.parametrize("username", ["", "@bad_bot", "https://t.me/bad_bot", "bot?start=injected"])
@respx.mock
async def test_send_source_approval_message_command_fallback(username: str) -> None:
    source = _fresh_proposal()
    route = respx.post("https://api.telegram.org/bottesttoken/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
    )
    assert await send_source_approval_message(source, "testtoken", "12345", username)
    sent = json.loads(route.calls[0].request.content)
    assert "reply_markup" not in sent
    assert f"/source ok {source.source_hash}" in sent["text"]
    assert f"/source no {source.source_hash}" in sent["text"]
    assert "next run" in sent["text"]
    assert "24 hours" in sent["text"]
    assert "Start" not in sent["text"]


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
    assert result.status == "unknown" and result.message_id is None


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


def test_add_source_preserves_yaml_sections_and_escapes_fields(tmp_path: Path) -> None:
    config_path = _make_config(
        tmp_path,
        "sources: []\ntelegram:\n  enabled: false\nfilters:\n  blocklist_keywords: [example]\n",
    )
    source = replace(_fresh_proposal(), name='Feed "quoted":\nother: value', category="A: B # C")
    add_source_to_config(config_path, source)
    first_write = Path(config_path).read_text(encoding="utf-8")
    add_source_to_config(config_path, source)
    assert Path(config_path).read_text(encoding="utf-8") == first_write
    config = yaml.safe_load(first_write)
    assert config["telegram"] == {"enabled": False}
    assert config["filters"] == {"blocklist_keywords": ["example"]}
    assert config["sources"] == [{
        "name": source.name,
        "url": source.url,
        "category": source.category,
        "enabled": True,
        "priority": 3,
        "trial": True,
        "trial_days": 14,
    }]


@pytest.mark.parametrize("inline", [False, True])
def test_add_source_preserves_operator_comments_and_unrelated_formatting(
    tmp_path: Path, inline: bool,
) -> None:
    source_section = (
        "sources: []  # Trial feeds go here\n" if inline else
        "sources:  # Trial feeds go here\n"
        "  # Existing feed must stay enabled\n"
        '  - name: "Existing"\n'
        "    url: https://existing.com/feed  # Stable RSS endpoint\n"
        "    category: Tech\n"
    )
    header = "# Operator-maintained configuration\nradar:\n  language: 'en'\n\n"
    tail = "telegram:  # Leave delivery disabled\n  enabled: false\n\n# End of configuration\n"
    config_path = _make_config(tmp_path, header + source_section + tail)
    add_source_to_config(config_path, _fresh_proposal())
    result = Path(config_path).read_text(encoding="utf-8")
    assert result.startswith(header)
    assert result.endswith(tail)
    assert "# Trial feeds go here" in result
    if not inline:
        assert source_section in result


def test_add_source_preserves_final_line_without_newline(tmp_path: Path) -> None:
    content = "sources:\n- name: Existing\n  url: https://existing.com/feed\n  category: Tech"
    config_path = _make_config(tmp_path, content)
    add_source_to_config(config_path, _fresh_proposal())
    assert len(yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))["sources"]) == 2


def test_add_source_backup_failure_raises_without_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = "sources: []\ntelegram:\n  enabled: false\n"
    config_path = _make_config(tmp_path, original)

    def fail_backup(src: Path, dest: Path) -> None:
        raise OSError("backup denied")

    monkeypatch.setattr("digest.adapters.storage.source_config.shutil.copy2", fail_backup)
    with pytest.raises(OSError, match="backup denied"):
        add_source_to_config(config_path, _fresh_proposal())
    assert Path(config_path).read_text(encoding="utf-8") == original
    assert not (tmp_path / "config.yaml.tmp").exists()


def test_add_source_config_replace_failure_preserves_backup_and_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = "sources: []\ntelegram:\n  enabled: false\n"
    config_path = _make_config(tmp_path, original)

    def fail_replace(self: Path, target: Path) -> Path:
        raise OSError("replace denied")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(OSError, match="replace denied"):
        add_source_to_config(config_path, _fresh_proposal())
    assert Path(config_path).read_text(encoding="utf-8") == original
    assert (tmp_path / "config.yaml.bak").read_text(encoding="utf-8") == original
    assert not (tmp_path / "config.yaml.tmp").exists()


@pytest.mark.parametrize("content", ["[]\n", "sources: {}\n", "sources: [bad]\n"])
def test_add_source_rejects_invalid_config_without_mutation(tmp_path: Path, content: str) -> None:
    config_path = _make_config(tmp_path, content)
    with pytest.raises(ValueError, match="Cannot add a source"):
        add_source_to_config(config_path, _fresh_proposal())
    assert Path(config_path).read_text(encoding="utf-8") == content


@pytest.mark.asyncio
@pytest.mark.parametrize("body,expected", [({"ok": False}, "rejected"), ({"ok": True}, "unknown")])
@respx.mock
async def test_source_delivery_needs_actual_receipt(body, expected):
    respx.post("https://api.telegram.org/bottoken/sendMessage").mock(return_value=httpx.Response(200, json=body))
    result = await send_source_approval_message(_fresh_proposal(), "token", "owner")
    assert result.status == expected and result.message_id is None
