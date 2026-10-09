"""Strict source restoration and prepared accounting's all-input preflight."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import asdict
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch

import httpx
import pytest
import respx

from digest.adapters.storage import delivery_state
from digest.adapters.storage import feedback as feedback_storage
from digest.adapters.storage import sources as storage
from digest.application import prepared_delivery
from digest.application.delivery import AppliedOutcome, PreparedOutcomePolicy, apply_confirmed_outcome
from digest.domain.catalog.sources import DailySnapshot, SourceStateEntry, SourceStateStore, SourceStats
from digest.domain.delivery.outcomes import IssueDeliveryResult
from digest.domain.feedback.values import FeedbackStore
from digest.edition_runtime import delivery_phase
from digest.radar.summarizer import ArticleSummary

LOADERS: dict[str, Callable[..., Any]] = {
    storage.STATS_FILE: storage.load_stats,
    storage.SOURCE_STATE_FILE: storage.load_source_state,
}


def _stats_record() -> dict[str, Any]:
    return asdict(SourceStats("Source", 2, 1, 3, 1, 98.5, "2026-10-07", [DailySnapshot("2026-10-07", 3, 1, True)]))


def _assert_stats_rejected(tmp_path: Path, record: dict[str, Any]) -> None:
    path = tmp_path / storage.STATS_FILE
    content = json.dumps({"Source": record}).encode()
    path.write_bytes(content)
    with pytest.raises(ValueError):
        storage.load_stats(str(tmp_path), strict=True)
    assert path.read_bytes() == content


def test_strict_stats_roundtrip_preserves_current_writer_values_and_order(tmp_path: Path) -> None:
    stats = {
        "Zürich": SourceStats(
            "Retained display name", 2, 1, 3, 5, 98.5, "2026-10-07",
            [DailySnapshot("2026-10-07", 3, 5, True), DailySnapshot("2024-02-29", 0, 0, False)],
        ),
        "日本語": SourceStats("日本語"),
    }
    storage.save_stats(stats, str(tmp_path))
    path = tmp_path / storage.STATS_FILE
    original = path.read_bytes()
    restored = storage.load_stats(str(tmp_path), strict=True)
    assert restored == stats
    assert list(restored) == list(stats)
    assert restored["Zürich"].name == "Retained display name"
    delivery_state.save_delivery_source_stats(restored, str(tmp_path))
    assert path.read_bytes() == original


def test_strict_lifecycle_roundtrip_preserves_current_writer_values_and_order(tmp_path: Path) -> None:
    state = SourceStateStore(sources={
        "Zürich": SourceStateEntry("2024-02-29", demoted=True),
        "日本語": SourceStateEntry(graduated=True),
    })
    storage.save_source_state(state, str(tmp_path))
    path = tmp_path / storage.SOURCE_STATE_FILE
    original = path.read_bytes()
    restored = storage.load_source_state(str(tmp_path), strict=True)
    assert restored == state
    assert list(restored.sources) == list(state.sources)
    delivery_state.save_delivery_source_state(restored, str(tmp_path))
    assert path.read_bytes() == original


@pytest.mark.parametrize("filename", LOADERS)
def test_only_absent_whole_source_store_is_fresh_state(tmp_path: Path, filename: str) -> None:
    cache = tmp_path / "absent-cache"
    expected = {} if filename == storage.STATS_FILE else SourceStateStore()
    assert LOADERS[filename](str(cache), strict=True) == expected
    assert not cache.exists()


@pytest.mark.parametrize(
    ("filename", "content"),
    [
        (storage.STATS_FILE, b"{"),
        (storage.SOURCE_STATE_FILE, b"{"),
        (storage.STATS_FILE, b"\xff"),
        (storage.SOURCE_STATE_FILE, b"[]"),
        (storage.STATS_FILE, b'{"Source": {}, "Source": {}}'),
        (storage.SOURCE_STATE_FILE, b'{"schema_version": 1, "schema_version": 1, "sources": {}}'),
    ],
    ids=["malformed-stats", "malformed-state", "invalid-utf8", "non-object", "duplicate-source", "duplicate-schema"],
)
def test_strict_source_json_never_resets_invalid_existing_bytes(
    tmp_path: Path, filename: str, content: bytes,
) -> None:
    path = tmp_path / filename
    path.write_bytes(content)
    with pytest.raises(ValueError):
        LOADERS[filename](str(tmp_path), strict=True)
    assert path.read_bytes() == content


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("future_field", 1),
        ("name", 1),
        ("total_fetches", True),
        ("successful_fetches", -1),
        ("avg_description_length", True),
        ("avg_description_length", -0.5),
        ("avg_description_length", float("nan")),
        ("last_seen", "2026-02-30"),
        ("last_seen", "20261007"),
        ("history", {}),
    ],
    ids=["unknown", "name-type", "bool-counter", "negative-counter", "bool-average",
         "negative-average", "nan", "impossible-day", "noncanonical-day", "history-type"],
)
def test_strict_stats_reject_invalid_fields(tmp_path: Path, field: str, value: Any) -> None:
    record = _stats_record()
    record[field] = value
    _assert_stats_rejected(tmp_path, record)


def test_strict_stats_reject_missing_current_writer_field(tmp_path: Path) -> None:
    record = _stats_record()
    del record["total_fetches"]
    _assert_stats_rejected(tmp_path, record)


@pytest.mark.parametrize(
    "snapshot",
    [
        {"date": "2026-10-07", "articles_found": 3, "articles_included": 1},
        asdict(DailySnapshot("2026-10-07", 3, 1, 1)),
        asdict(DailySnapshot("2026-10-07", -1, 1, True)),
        {**asdict(DailySnapshot("2026-10-07", 3, 1, True)), "date": None},
        asdict(DailySnapshot("2026-10-7", 3, 1, True)),
    ],
    ids=["missing", "non-bool-flag", "negative-counter", "null-day", "noncanonical-day"],
)
def test_strict_stats_reject_malformed_history_without_dropping_it(tmp_path: Path, snapshot: dict[str, Any]) -> None:
    record = _stats_record()
    record["history"].append(snapshot)
    _assert_stats_rejected(tmp_path, record)


@pytest.mark.parametrize(
    "record",
    [
        {},
        {"schema_version": 1, "sources": {}, "future_field": 1},
        {"schema_version": 99, "sources": {}},
        {"schema_version": True, "sources": {}},
        {"schema_version": 1, "sources": []},
        {"schema_version": 1, "sources": {"Source": {}}},
        {"schema_version": 1, "sources": {"Source": {**asdict(SourceStateEntry()), "future_field": 1}}},
        {"schema_version": 1, "sources": {"Source": {"trial_started": None, "graduated": "false", "demoted": False}}},
        {"schema_version": 1, "sources": {"Source": asdict(SourceStateEntry("20261007"))}},
    ],
    ids=["missing-root", "unknown-root", "unknown-schema", "bool-schema", "sources-type", "sparse-entry",
         "unknown-entry", "string-flag", "noncanonical-day"],
)
def test_strict_lifecycle_rejects_non_writer_records(tmp_path: Path, record: dict[str, Any]) -> None:
    path = tmp_path / storage.SOURCE_STATE_FILE
    content = json.dumps(record).encode()
    path.write_bytes(content)
    with pytest.raises(ValueError):
        storage.load_source_state(str(tmp_path), strict=True)
    assert path.read_bytes() == content


@pytest.mark.parametrize("filename", LOADERS)
def test_strict_source_read_failure_propagates(tmp_path: Path, filename: str) -> None:
    path = tmp_path / filename
    path.write_text("{}")
    with patch.object(Path, "read_text", side_effect=PermissionError("synthetic access failure")):
        with pytest.raises(PermissionError, match="synthetic access failure"):
            LOADERS[filename](str(tmp_path), strict=True)
    assert path.read_bytes() == b"{}"


def test_strict_source_directory_is_not_missing_state(tmp_path: Path) -> None:
    (tmp_path / storage.STATS_FILE).mkdir()
    with pytest.raises(IsADirectoryError):
        storage.load_stats(str(tmp_path), strict=True)


@pytest.mark.parametrize("ancestor", [False, True], ids=["dangling-file", "ancestor"])
def test_strict_source_paths_reject_symlinks_before_reading(tmp_path: Path, ancestor: bool) -> None:
    target = tmp_path / "target"
    cache = tmp_path / "linked" if ancestor else tmp_path
    if ancestor:
        target.mkdir()
        (target / storage.SOURCE_STATE_FILE).write_text("{}")
    link = cache if ancestor else cache / storage.SOURCE_STATE_FILE
    link.symlink_to(target, target_is_directory=ancestor)
    with patch.object(Path, "read_text", side_effect=AssertionError("unsafe path was read")):
        with pytest.raises(ValueError, match="symlink"):
            storage.load_source_state(str(cache), strict=True)
    assert link.is_symlink()
    assert target.exists() is ancestor


def test_default_stats_loader_retains_permissive_partial_behavior(tmp_path: Path) -> None:
    record = {"Source": {"history": [{"date": "missing fields"}]}, "Malformed": None}
    (tmp_path / storage.STATS_FILE).write_text(json.dumps(record))
    assert storage.load_stats(str(tmp_path)) == {"Source": SourceStats("Source")}
    with pytest.raises(ValueError):
        storage.load_stats(str(tmp_path), strict=True)


def test_default_lifecycle_loader_retains_sparse_and_coercing_behavior(tmp_path: Path) -> None:
    record = {"schema_version": 1, "sources": {"Source": {"graduated": "yes"}, "Malformed": None}}
    (tmp_path / storage.SOURCE_STATE_FILE).write_text(json.dumps(record))
    expected = SourceStateStore(sources={"Source": SourceStateEntry(graduated=True)})
    assert storage.load_source_state(str(tmp_path)) == expected
    with pytest.raises(ValueError):
        storage.load_source_state(str(tmp_path), strict=True)


def _policy(cache: Path, *, adaptive: bool = True, delivered: bool = True) -> PreparedOutcomePolicy:
    outcome = IssueDeliveryResult(
        sent=int(delivered), outcome="sent", total_chunks=1, confirmed_chunks=1,
        delivered_hashes={"a" * 32} if delivered else set(),
        article_source_map={"a" * 32: "Source"} if delivered else {},
    )
    return PreparedOutcomePolicy(outcome, str(cache), date(2026, 10, 7), ["Source"], adaptive, [])


@pytest.mark.parametrize("adaptive", [True, False])
def test_prepared_application_preloads_every_applicable_store_before_first_write(
    tmp_path: Path, adaptive: bool,
) -> None:
    feedback_storage.save_feedback(FeedbackStore(last_update_id=999), str(tmp_path), strict=True)
    storage.save_stats({"Source": SourceStats("Source", total_fetches=2, successful_fetches=1)}, str(tmp_path))
    if not adaptive:
        (tmp_path / storage.SOURCE_STATE_FILE).write_text("corrupt but inapplicable")
    manager = Mock()
    methods = [
        (feedback_storage, "load_feedback"),
        (delivery_state, "load_delivery_cache"),
        (storage, "load_stats"),
        (storage, "load_source_state"),
        (feedback_storage, "save_feedback"),
        (delivery_state, "save_delivery_source_stats"),
        (delivery_state, "save_delivery_source_state"),
        (delivery_state, "save_delivery_cache"),
    ]
    with ExitStack() as stack:
        for owner, name in methods:
            wrapped = stack.enter_context(patch.object(owner, name, wraps=getattr(owner, name)))
            manager.attach_mock(wrapped, name)
        assert apply_confirmed_outcome(_policy(tmp_path, adaptive=adaptive)) == AppliedOutcome()
    expected = ["load_feedback", "load_delivery_cache", "load_stats"]
    if adaptive:
        expected.append("load_source_state")
    expected.extend(["save_feedback", "save_delivery_source_stats"])
    if adaptive:
        expected.append("save_delivery_source_state")
    expected.append("save_delivery_cache")
    assert [call[0] for call in manager.mock_calls] == expected
    manager.load_feedback.assert_called_once_with(str(tmp_path), strict=True)
    manager.load_stats.assert_called_once_with(str(tmp_path), strict=True)
    if adaptive:
        manager.load_source_state.assert_called_once_with(str(tmp_path), strict=True)
    else:
        assert (tmp_path / storage.SOURCE_STATE_FILE).read_text() == "corrupt but inapplicable"
    assert feedback_storage.load_feedback(str(tmp_path), strict=True).last_update_id == 999
    stats = storage.load_stats(str(tmp_path), strict=True)["Source"]
    assert stats.total_fetches == 2 and stats.successful_fetches == 1
    assert stats.articles_included_in_digest == 1
    assert stats.history == [DailySnapshot("2026-10-07", 0, 1, False)]


def test_prepared_application_without_delivered_hashes_never_reads_or_writes(tmp_path: Path) -> None:
    forbidden = Mock(side_effect=AssertionError("zero-coverage application touched accounting"))
    with (
        patch.object(feedback_storage, "load_feedback", forbidden),
        patch.object(delivery_state, "load_delivery_cache", forbidden),
        patch.object(storage, "load_stats", forbidden),
        patch.object(storage, "load_source_state", forbidden),
        patch.object(feedback_storage, "atomic_json_write", forbidden),
        patch.object(delivery_state, "atomic_json_write", forbidden),
    ):
        assert apply_confirmed_outcome(_policy(tmp_path, delivered=False)) == AppliedOutcome()
    forbidden.assert_not_called()
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("filename", LOADERS)
async def test_corrupt_source_preflight_after_transport_preserves_receipts_and_all_accounting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, filename: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "offline-token")
    for name in ("ALL_PROXY", "all_proxy", "HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
        monkeypatch.delenv(name, raising=False)
    config = SimpleNamespace(
        telegram=SimpleNamespace(enabled=True, bot_username="test_bot"),
        radar=SimpleNamespace(language="en"), adaptive=SimpleNamespace(enabled=True), enabled_sources=[],
    )
    monkeypatch.setattr("digest.config.load_config", lambda _: config)
    cache = tmp_path / ".cache"
    feedback_storage.save_feedback(FeedbackStore(last_update_id=999), str(cache), strict=True)
    storage.save_stats({"Source": SourceStats("Source")}, str(cache))
    storage.save_source_state(SourceStateStore(), str(cache))
    delivery_state.save_delivery_cache({"old": "retained timestamp"}, str(cache))
    path, ready = prepared_delivery.prepare_edition(
        [ArticleSummary("Title", "https://example.invalid/article", "Source", "Tech", "Synthetic summary")],
        config, cache_dir=cache, canonical_metadata={"contributing_sources": ["Source"]},
    )
    (cache / filename).write_text("{")
    assert await delivery_phase("claim", "unused.yaml", ready, None) == 0
    claim_path = cache / prepared_delivery.CLAIM_FILE
    claim = hashlib.sha256(claim_path.read_bytes()).hexdigest()
    unchanged = {
        item: item.read_bytes() for item in (
            path, claim_path, cache / "feedback.json", cache / "seen_articles.json",
            cache / storage.STATS_FILE, cache / storage.SOURCE_STATE_FILE,
        )
    }
    writes = Mock(side_effect=AssertionError("accounting write before valid preflight"))
    monkeypatch.setattr(feedback_storage, "atomic_json_write", writes)
    monkeypatch.setattr(delivery_state, "atomic_json_write", writes)
    with respx.mock() as router:
        route = router.post("https://api.telegram.org/botoffline-token/sendMessage").mock(return_value=httpx.Response(
            200, json={"ok": True, "result": {"message_id": 52, "chat": {"id": 123}}},
        ))
        with pytest.raises(ValueError):
            await delivery_phase("send", "unused.yaml", ready, claim)
        assert route.call_count == 1
        receipts_path = cache / prepared_delivery.RECEIPTS_FILE
        receipt_bytes = receipts_path.read_bytes()
        receipts = json.loads(receipt_bytes)
        assert receipts["state"] == "confirmed" and receipts["applied"] is False
        assert receipts["confirmed"][0]["message_id"] == 52
        assert prepared_delivery.inspect_edition(cache_dir=cache)[2] == "held"
        with pytest.raises(ValueError, match="held"):
            await delivery_phase("send", "unused.yaml", ready, claim)
        assert route.call_count == 1
        assert receipts_path.read_bytes() == receipt_bytes
    writes.assert_not_called()
    assert {item: item.read_bytes() for item in unchanged} == unchanged
