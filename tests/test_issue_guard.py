"""Offline checks of the whole-issue publication barrier."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from digest.delivery.issue_guard import load_guard, reserve

TODAY = datetime(2026, 10, 2, 8, tzinfo=timezone.utc)


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123456")
    path = tmp_path / "config.yaml"
    path.write_text("delivery:\n  mode: compact\n")
    return path


def test_unresolved_reservation_holds_next_day(config: Path, tmp_path: Path) -> None:
    reserve(config, tmp_path, TODAY)
    with pytest.raises(ValueError, match="already reserved or held"):
        reserve(config, tmp_path, TODAY + timedelta(days=1))


def test_terminal_same_day_holds_but_next_day_reuses(config: Path, tmp_path: Path) -> None:
    path, digest = reserve(config, tmp_path, TODAY)
    guard = load_guard(config, digest, tmp_path)
    guard.mark_sending()
    guard.finish("confirmed", accepted_count=2, attempted_count=2)
    previous = path.read_bytes()
    with pytest.raises(ValueError, match="already reserved or held"):
        reserve(config, tmp_path, TODAY)
    _, next_digest = reserve(config, tmp_path, TODAY + timedelta(days=1))
    assert next_digest != digest and path.read_bytes() != previous
    assert load_guard(config, next_digest, tmp_path).state == "reserved"


def test_binding_and_marker_tamper_blocks_loading(
    config: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, digest = reserve(config, tmp_path, TODAY)
    original = config.read_bytes()
    config.write_text("changed: true")
    with pytest.raises(ValueError, match="mismatch"):
        load_guard(config, digest, tmp_path)
    config.write_bytes(original)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "999")
    with pytest.raises(ValueError, match="mismatch"):
        load_guard(config, digest, tmp_path)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123456")
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="mismatch"):
        load_guard(config, digest, tmp_path)


def test_mark_write_failure_prevents_post_and_transition(config: Path, tmp_path: Path) -> None:
    path, digest = reserve(config, tmp_path, TODAY)
    guard = load_guard(config, digest, tmp_path)
    previous = path.read_bytes()
    posts = []
    with patch("digest.delivery.issue_guard.atomic_json_write", side_effect=OSError("disk full")):
        with pytest.raises(OSError):
            guard.mark_sending()
            posts.append("POST")
    assert not posts and guard.state == "reserved" and path.read_bytes() == previous
    with pytest.raises(ValueError, match="Invalid compact"):
        guard.finish("confirmed", accepted_count=1, attempted_count=1)


@pytest.mark.parametrize("outcome,accepted", [("unknown", 0), ("partial", 1)])
def test_ambiguous_delivery_holds_next_day(
    config: Path, tmp_path: Path, outcome: str, accepted: int,
) -> None:
    _, digest = reserve(config, tmp_path, TODAY)
    guard = load_guard(config, digest, tmp_path)
    guard.mark_sending()
    guard.finish(outcome, accepted_count=accepted, attempted_count=2)
    with pytest.raises(ValueError, match="already reserved or held"):
        reserve(config, tmp_path, TODAY + timedelta(days=1))


def test_symlink_and_changed_marker_block_transition(config: Path, tmp_path: Path) -> None:
    path, digest = reserve(config, tmp_path, TODAY)
    guard = load_guard(config, digest, tmp_path)
    original = path.read_bytes()
    path.write_bytes(original + b"\n")
    with pytest.raises(ValueError, match="marker changed"):
        guard.mark_sending()
    path.write_bytes(original)
    path.with_suffix(".json.tmp").symlink_to(config)
    with pytest.raises(ValueError, match="symlinks"):
        guard.mark_sending()


def test_escaped_exception_records_unknown_counts(config: Path, tmp_path: Path) -> None:
    path, digest = reserve(config, tmp_path, TODAY)
    guard = load_guard(config, digest, tmp_path)
    guard.mark_sending()
    guard.finish("unknown")
    marker = json.loads(path.read_bytes())
    assert marker["accepted_count"] is None and marker["attempted_count"] is None
    with pytest.raises(ValueError, match="already reserved or held"):
        reserve(config, tmp_path, TODAY + timedelta(days=1))


def test_no_output_can_close_without_claiming_attempts(config: Path, tmp_path: Path) -> None:
    path, digest = reserve(config, tmp_path, TODAY)
    guard = load_guard(config, digest, tmp_path)
    guard.finish("not_sent")
    marker = json.loads(path.read_bytes())
    assert marker["accepted_count"] == marker["attempted_count"] == 0
    reserve(config, tmp_path, TODAY + timedelta(days=1))
