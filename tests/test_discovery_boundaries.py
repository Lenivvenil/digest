"""Critical persistence and decision-time contracts across discovery's concrete owners."""
from __future__ import annotations

import asyncio
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


def _generation_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> application.DiscoverySession:
    from digest.config import ProviderConfig
    from scripts.review_fixture import fixture_config

    config = fixture_config()
    config.llm.providers = [ProviderConfig("groq", "fixture", ["summarize"])]
    config.discovery.exploration_areas = ["science", "history"]
    config.telegram.enabled = True
    clock = Mock(wraps=datetime)
    clock.now.return_value = NOW
    monkeypatch.setattr(application, "datetime", clock)
    retention_clock = Mock(wraps=datetime)
    retention_clock.now.return_value = NOW
    monkeypatch.setattr(pending_sources, "datetime", retention_clock)
    return application.DiscoverySession(config, "owner", "cycle", "target", "token", "chat", str(tmp_path))


async def test_generated_prefix_is_durable_before_cancellation_and_reused_on_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _generation_session(tmp_path, monkeypatch)
    source = PendingSource("First", "https://example.org/final", "Science", NOW.isoformat())
    binding = proposal_binding(source)
    model = AsyncMock(return_value=(
        "FEED|https://example.org/first|Science|First\nFEED|https://example.org/second|Science|Second", {},
    ))
    monkeypatch.setattr("digest.llm.complete", model)
    observed: dict[str, Any] = {}

    async def validate(url: str) -> str:
        if url.endswith("first"):
            return source.url
        # Read both real files inside the next await, before any cancellation cleanup.
        observed["pending"] = pending_sources.load_pending(str(tmp_path), strict=True)
        observed["metadata"] = storage.load_delivery(str(tmp_path))
        observed["bytes"] = [(tmp_path / name).read_bytes()
                             for name in (pending_sources.PENDING_FILE, storage.DELIVERY_FILE)]
        raise asyncio.CancelledError

    validator = AsyncMock(side_effect=validate)
    monkeypatch.setattr("digest.discovery_feed.validate_feed_url", validator)
    with pytest.raises(asyncio.CancelledError):
        await application.prepare_and_reserve(session)
    model.assert_awaited_once()
    assert validator.await_count == 2
    assert observed["pending"] == [source]
    data = observed["metadata"]
    assert data["proposal_areas"] == {binding: "science"}
    assert data["generation"] == {
        "requested_area": "science", "requested_at": NOW.isoformat(), "cycle": "cycle",
        "outcome": "proposed", "bindings": [binding],
    }
    assert data["deliveries"] == {} and data["batch"] is None
    assert observed["bytes"] == [(tmp_path / name).read_bytes()
                                 for name in (pending_sources.PENDING_FILE, storage.DELIVERY_FILE)]

    # Ordinary restart revalidates the saved identity and can use the remaining model slots.
    model.return_value = (f"FEED|{source.url}|Science|Repeated", {})
    validator.side_effect = lambda url: url
    validator.reset_mock()
    prepared = await application.prepare_and_reserve(session)
    assert pending_sources.load_pending(str(tmp_path), strict=True) == [source]
    validator.assert_awaited_once_with(source.url)
    context = json.loads(model.call_args.args[1][-1]["content"].split("\n", 1)[1])
    assert context["pending_urls"] == [source.url]
    assert model.await_count == 2
    assert prepared.counts["duplicates"] == 1 and prepared.counts["prepared"] == 1
    data = storage.load_delivery(str(tmp_path))
    assert data["proposal_areas"] == {binding: "science"}
    assert data["batch"]["bindings"] == [binding]
    assert data["deliveries"][binding]["status"] == "reserved"
    assert prepared.pending_sha == storage.pending_sha256(str(tmp_path))
    assert prepared.delivery_sha == storage.delivery_sha256(str(tmp_path))


@pytest.mark.parametrize("failure", ["pending", "metadata"])
async def test_generated_checkpoint_failure_preserves_ordered_disk_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    session = _generation_session(tmp_path, monkeypatch)
    sources = [PendingSource(name, f"https://example.org/{name}", "Science", NOW.isoformat())
               for name in ("first", "second", "third")]
    monkeypatch.setattr("digest.llm.complete", AsyncMock(return_value=(
        "\n".join(f"FEED|{s.url}|{s.category}|{s.name}" for s in sources), {},
    )))
    validator = AsyncMock(side_effect=lambda url: url)
    monkeypatch.setattr("digest.discovery_feed.validate_feed_url", validator)
    persist_pending, persist_metadata = pending_sources.save_pending, storage.save_delivery
    prefix: dict[str, bytes] = {}
    events: list[str] = []
    error = OSError(f"{failure} replacement failed")

    def write_pending(pending: list[PendingSource], cache_dir: str, *, strict: bool = False) -> None:
        assert strict
        if len(pending) == 2:
            events.append("pending:2")
            if failure == "pending":
                raise error
        persist_pending(pending, cache_dir, strict=strict)

    def write_metadata(data: dict[str, Any], cache_dir: str) -> None:
        generation = data["generation"]
        if generation and len(generation["bindings"]) == 2:
            events.append("metadata:2")
            if failure == "metadata":
                raise error
        persist_metadata(data, cache_dir)
        if generation and len(generation["bindings"]) == 1:
            prefix.update({name: (tmp_path / name).read_bytes()
                           for name in (pending_sources.PENDING_FILE, storage.DELIVERY_FILE)})

    monkeypatch.setattr(pending_sources, "save_pending", write_pending)
    monkeypatch.setattr(storage, "save_delivery", write_metadata)
    with pytest.raises(OSError) as raised:
        await application.prepare_and_reserve(session)
    assert raised.value is error
    assert [call.args[0] for call in validator.await_args_list] == [s.url for s in sources[:2]]
    assert events == (["pending:2"] if failure == "pending" else ["pending:2", "metadata:2"])
    kept = sources[:1] if failure == "pending" else sources[:2]
    assert pending_sources.load_pending(str(tmp_path), strict=True) == kept
    assert (tmp_path / storage.DELIVERY_FILE).read_bytes() == prefix[storage.DELIVERY_FILE]
    if failure == "pending":
        assert (tmp_path / pending_sources.PENDING_FILE).read_bytes() == prefix[pending_sources.PENDING_FILE]
    data = storage.load_delivery(str(tmp_path))
    assert data["proposal_areas"] == {proposal_binding(sources[0]): "science"}
    assert data["generation"]["bindings"] == [proposal_binding(sources[0])]
    assert data["deliveries"] == {} and data["batch"] is None

    monkeypatch.setattr(pending_sources, "save_pending", persist_pending)
    monkeypatch.setattr(storage, "save_delivery", persist_metadata)
    monkeypatch.setattr("digest.llm.complete", AsyncMock(return_value=("", {})))
    validator.reset_mock()
    await application.prepare_and_reserve(session)
    assert [call.args[0] for call in validator.await_args_list] == [s.url for s in kept]
    assert pending_sources.load_pending(str(tmp_path), strict=True) == kept
    data = storage.load_delivery(str(tmp_path))
    assert data["proposal_areas"] == {proposal_binding(sources[0]): "science"}
    assert data["batch"]["bindings"] == [proposal_binding(s) for s in kept]


@pytest.mark.parametrize("excluded", ["unsafe", "rejected", "duplicate", "redirect_duplicate"])
async def test_excluded_generated_input_creates_no_successful_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, excluded: str,
) -> None:
    session = _generation_session(tmp_path, monkeypatch)
    source = _source("excluded")
    data = storage.load_delivery(str(tmp_path))
    if excluded == "rejected":
        application.record_source_history(source, "rejected", str(tmp_path))
        data = storage.load_delivery(str(tmp_path))
    pending = [source] if "duplicate" in excluded else []
    requested_url = "https://example.org/redirect" if excluded == "redirect_duplicate" else source.url
    monkeypatch.setattr("digest.llm.complete", AsyncMock(return_value=(
        f"FEED|{requested_url}|Science|Excluded", {},
    )))
    validator = AsyncMock(side_effect=ValueError("unsafe feed")) if excluded == "unsafe" else AsyncMock(
        return_value=source.url,
    )
    monkeypatch.setattr("digest.discovery_feed.validate_feed_url", validator)
    pending_write = Mock(wraps=pending_sources.save_pending)
    monkeypatch.setattr(pending_sources, "save_pending", pending_write)
    assert await application._generate_offers(session, pending, data, NOW, 0, application._empty_counts()) == []
    pending_write.assert_not_called()
    saved = storage.load_delivery(str(tmp_path))
    assert saved["proposal_areas"] == {} and saved["generation"]["bindings"] == []
    assert not (tmp_path / pending_sources.PENDING_FILE).exists()
    assert validator.await_count == int(excluded in {"unsafe", "redirect_duplicate"})
