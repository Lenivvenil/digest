"""Critical persistence and decision-time contracts across discovery's concrete owners."""
from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from digest.adapters.storage import discovery as storage
from digest.adapters.storage import pending_sources
from digest.adapters.telegram import discovery as telegram
from digest.application import discovery as application
from digest.domain.catalog.exploration import ProposalDelivery
from digest.domain.catalog.proposals import PendingSource, proposal_binding

NOW = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)


def _source(name: str, *, age: int = 1) -> PendingSource:
    return PendingSource(name, f"https://example.org/{name}", "Science", (NOW - timedelta(days=age)).isoformat())


def _write_pending(path: Path, sources: list[PendingSource]) -> bytes:
    raw = json.dumps({"pending": [asdict(source) for source in sources]}).encode()
    (path / pending_sources.PENDING_FILE).write_bytes(raw)
    return raw


def _clock(monkeypatch: pytest.MonkeyPatch, *times: datetime) -> Mock:
    clock = Mock(wraps=datetime)
    clock.now.side_effect = times
    monkeypatch.setattr(application, "datetime", clock)
    return clock


def _reserve(path: Path, sources: list[PendingSource]) -> tuple[str, str]:
    _write_pending(path, sources)
    data = storage.load_delivery(str(path))
    data["deliveries"] = {proposal_binding(source): {
        "status": "reserved", "updated_at": NOW.isoformat(), "owner": "owner",
    } for source in sources}
    pending_sha = storage.pending_sha256(str(path))
    data["batch"] = {"owner": "owner", "target": "target", "pending_sha256": pending_sha,
                     "bindings": [proposal_binding(source) for source in sources]}
    storage.save_delivery(data, str(path))
    return pending_sha, storage.delivery_sha256(str(path))


def test_metadata_preserves_unknown_fields_defaults_bytes_and_legacy_naive_dates(tmp_path: Path) -> None:
    legacy = {"schema_version": 1, "deliveries": {"a" * 64: {
        "status": "unknown", "updated_at": "2026-10-07T12:00:00", "future_receipt_field": [1, "é"],
    }}, "history": [{"binding": "legacy", "url": "u", "name": "n", "category": "c", "decision": "approved",
                     "recorded_at": "2026-10-07", "future_history_field": True}],
        "batch": None, "future_top_level": {"a": "é"}}
    path = tmp_path / storage.DELIVERY_FILE
    path.write_text(json.dumps(legacy))
    loaded = storage.load_delivery(str(tmp_path))
    expected = {**legacy, "proposal_areas": {}, "area_offers": {}, "validation_failures": {},
                "attempted_areas": [], "generation": None}
    assert loaded == expected
    storage.save_delivery(loaded, str(tmp_path))
    assert path.read_bytes() == json.dumps(expected, indent=2).encode()
    loaded["area_offers"] = {"science": {"status": "unknown", "updated_at": "2026-10-07T12:00:00"}}
    storage.save_delivery(loaded, str(tmp_path))
    with pytest.raises(ValueError, match="include a timezone"):
        storage.load_delivery(str(tmp_path))
    path.write_bytes(b" " * (storage.METADATA_MAX_BYTES + 1))
    with pytest.raises(ValueError, match="storage bound"):
        storage.load_delivery(str(tmp_path))


def test_pruning_samples_each_eligible_identity_and_aborts_before_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = _write_pending(tmp_path, [_source("expired", age=31), _source("fresh"), _source("boundary", age=30)])
    storage.save_delivery(storage.load_delivery(str(tmp_path)), str(tmp_path))
    metadata = (tmp_path / storage.DELIVERY_FILE).read_bytes()
    clock = _clock(monkeypatch, NOW, NOW + timedelta(microseconds=1))
    with pytest.raises(ValueError, match="Ambiguous or invalid"):
        application.prune_discovery_state(str(tmp_path), NOW, ["science"])
    assert clock.now.call_count == 2
    assert (tmp_path / pending_sources.PENDING_FILE).read_bytes() == original
    assert (tmp_path / storage.DELIVERY_FILE).read_bytes() == metadata


def test_pruning_commits_history_before_pending_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    expired = _source("expired", age=31)
    original = _write_pending(tmp_path, [expired])
    write = Mock(side_effect=OSError("pending replacement failed"))
    monkeypatch.setattr(pending_sources, "save_pending", write)
    clock = _clock(monkeypatch)
    with pytest.raises(OSError, match="pending replacement"):
        application.prune_discovery_state(str(tmp_path), NOW, ["science"])
    assert clock.now.call_count == 0
    assert storage.load_delivery(str(tmp_path))["history"] == [{
        "binding": proposal_binding(expired), "url": expired.url, "name": expired.name,
        "category": expired.category, "decision": "expired", "recorded_at": NOW.isoformat(),
    }]
    assert (tmp_path / pending_sources.PENDING_FILE).read_bytes() == original


def test_duplicate_history_does_not_observe_a_new_decision_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _clock(monkeypatch, NOW)
    source = _source("source")
    application.record_source_history(source, "approved", str(tmp_path))
    before = (tmp_path / storage.DELIVERY_FILE).read_bytes()
    application.record_source_history(source, "approved", str(tmp_path))
    assert clock.now.call_count == 1
    assert (tmp_path / storage.DELIVERY_FILE).read_bytes() == before


@pytest.mark.parametrize("invalid", ["aged", "owner", "absent"])
async def test_entire_batch_is_validated_before_post_and_uses_each_decision_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, invalid: str,
) -> None:
    sources = [_source("first"), _source("second", age=30)]
    pending_sha, delivery_sha = _reserve(tmp_path, sources)
    data = storage.load_delivery(str(tmp_path))
    if invalid == "owner":
        data["deliveries"][proposal_binding(sources[1])]["owner"] = "other"
    elif invalid == "absent":
        data["batch"]["bindings"][1] = "f" * 64
    storage.save_delivery(data, str(tmp_path))
    delivery_sha = storage.delivery_sha256(str(tmp_path))
    clock = _clock(monkeypatch, NOW, NOW + timedelta(microseconds=int(invalid == "aged")))
    send = AsyncMock()
    monkeypatch.setattr(telegram, "send_source_approval_message", send)
    with pytest.raises(ValueError, match="no longer matches"):
        await application.send_reserved_proposals(
            str(tmp_path), "owner", "target", "token", "chat", "fixture_bot",
            pending_sha, delivery_sha, application._empty_counts(),
        )
    assert clock.now.call_count == (1 if invalid == "absent" else 2)
    send.assert_not_awaited()
    assert storage.delivery_sha256(str(tmp_path)) == delivery_sha


async def test_final_receipt_failure_keeps_unknown_and_stops_later_posts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    sources = [_source("first"), _source("second")]
    pending_sha, delivery_sha = _reserve(tmp_path, sources)
    _clock(monkeypatch, NOW, NOW, NOW + timedelta(seconds=1))
    persist = storage.save_delivery
    observed = []

    def fail_after_post(data: dict[str, Any], cache_dir: str) -> None:
        status = data["deliveries"][proposal_binding(sources[0])]["status"]
        observed.append(status)
        if status == "confirmed":
            raise OSError("final receipt replacement failed")
        persist(data, cache_dir)

    async def send(source: PendingSource, *args: str) -> ProposalDelivery:
        saved = storage.load_delivery(str(tmp_path))["deliveries"][proposal_binding(source)]
        assert saved["status"] == "unknown"
        assert saved["updated_at"] == (NOW + timedelta(seconds=1)).isoformat()
        observed.append("POST")
        return ProposalDelivery("confirmed", 7)

    monkeypatch.setattr(storage, "save_delivery", fail_after_post)
    transport = AsyncMock(side_effect=send)
    monkeypatch.setattr(telegram, "send_source_approval_message", transport)
    counts = application._empty_counts()
    with pytest.raises(OSError, match="final receipt replacement"):
        await application.send_reserved_proposals(
            str(tmp_path), "owner", "target", "token", "chat", "fixture_bot", pending_sha, delivery_sha, counts,
        )
    assert observed == ["unknown", "POST", "confirmed"]
    assert transport.await_count == 1 and counts["confirmed"] == 0
    assert [item["status"] for item in storage.load_delivery(str(tmp_path))["deliveries"].values()] == [
        "unknown", "reserved",
    ]


async def test_pending_redirect_keeps_identity_while_generation_records_final_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from digest.config import ProviderConfig
    from scripts.review_fixture import fixture_config

    config = fixture_config()
    config.llm.providers = [ProviderConfig("groq", "fixture", ["summarize"])]
    config.telegram.enabled = True
    session = application.DiscoverySession(config, "owner", "cycle", "target", "token", "chat", str(tmp_path))
    source = _source("pending")
    _write_pending(tmp_path, [source])
    _clock(monkeypatch, NOW, NOW)
    # Storage retention has its own original observation, independent from application identity decisions.
    retention_clock = Mock(wraps=datetime)
    retention_clock.now.return_value = NOW
    monkeypatch.setattr(pending_sources, "datetime", retention_clock)
    monkeypatch.setattr("digest.llm.complete", AsyncMock(return_value=(
        "FEED|https://example.org/generated|Science|Generated", {},
    )))
    monkeypatch.setattr("digest.discovery_feed.validate_feed_url", AsyncMock(side_effect=[
        "https://example.org/pending-redirect", "https://example.org/generated-redirect",
    ]))
    await application.prepare_and_reserve(session)
    pending = pending_sources.load_pending(str(tmp_path), strict=True)
    assert pending[0] == source
    assert [item.url for item in pending] == [source.url, "https://example.org/generated-redirect"]
    data = storage.load_delivery(str(tmp_path))
    assert data["generation"]["bindings"] == [proposal_binding(pending[1])]
    assert data["batch"]["bindings"] == [proposal_binding(source), proposal_binding(pending[1])]
