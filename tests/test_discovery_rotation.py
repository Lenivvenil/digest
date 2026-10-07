"""Offline discovery rotation, persistence and finite validation retry contracts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from digest.config import Config, ProviderConfig
from digest.discovery import (
    DELIVERY_FILE,
    PENDING_FILE,
    PendingSource,
    ProposalDelivery,
    load_delivery,
    load_pending,
    proposal_binding,
    prune_discovery_state,
    record_source_history,
    save_delivery,
    save_pending,
    select_exploration_area,
)
from digest.main import discover_sources
from scripts.review_fixture import fixture_config


@pytest.fixture
def discovery_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_RUN_ID", "run-0")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fixture-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "fixture-owner")
    config = fixture_config()
    config.llm.providers = [ProviderConfig("groq", "fixture-model", ["summarize"])]
    config.telegram.enabled = True
    monkeypatch.setattr("digest.config.load_config", lambda _: config)
    Path("config.yaml").write_text("unchanged professional portfolio\n")
    return config


def _requested_area(messages: list[dict[str, str]]) -> str:
    context = json.loads(messages[-1]["content"].split("\n", 1)[1])
    return str(context["requested_exploration_area"])


async def _send_prepared() -> int:
    hashes = [hashlib.sha256((Path(".cache") / filename).read_bytes()).hexdigest()
              for filename in (PENDING_FILE, DELIVERY_FILE)]
    return await discover_sources("config.yaml", phase="send", pending_sha=hashes[0], delivery_sha=hashes[1])


@pytest.mark.asyncio
async def test_empty_invalid_and_failed_generations_complete_two_fair_passes(
    discovery_config: Config, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = discovery_config
    original_sources = [asdict(source) for source in config.sources]
    requested = []
    outcomes = []

    async def generate(role: Any, messages: list[dict[str, str]], *args: Any, **kwargs: Any) -> tuple[str, dict]:
        area = _requested_area(messages)
        requested.append(area)
        persisted = load_delivery(".cache")
        assert persisted["generation"]["requested_area"] == area
        assert persisted["generation"]["outcome"] == "started"
        assert area in persisted["attempted_areas"]
        assert kwargs["max_output_tokens"] == 2048
        assert "no technology, finance or banking connection is required" in messages[-1]["content"]
        if area == "science":
            return "", {}
        if area == "environment":
            raise RuntimeError("offline provider failure")
        return f"FEED|https://example.com/{len(requested)}|{area}|Fixture", {}

    validate = AsyncMock(side_effect=ValueError("offline invalid feed"))
    monkeypatch.setattr("digest.llm.complete", generate)
    monkeypatch.setattr("digest.discovery_feed.validate_feed_url", validate)
    for index in range(12):
        monkeypatch.setenv("GITHUB_RUN_ID", f"run-{index}")
        assert await discover_sources("config.yaml", phase="prepare") == 0
        data = load_delivery(".cache")
        outcomes.append(data["generation"]["outcome"])
        assert data["area_offers"] == {}
        assert data["batch"]["bindings"] == []
    assert requested == config.discovery.exploration_areas * 2
    assert outcomes == [
        "no_valid_proposals", "empty", "no_valid_proposals", "no_valid_proposals", "failed", "no_valid_proposals",
    ] * 2
    assert validate.await_count == 8
    assert load_pending(".cache") == []
    assert [asdict(source) for source in config.sources] == original_sources
    assert Path("config.yaml").read_text() == "unchanged professional portfolio\n"


@pytest.mark.asyncio
@pytest.mark.parametrize("area", ["fintech/banking/architecture", "science"])
async def test_requested_area_can_refresh_professional_or_explore_cross_field(
    discovery_config: Config, monkeypatch: pytest.MonkeyPatch, area: str,
) -> None:
    discovery_config.discovery.exploration_areas = [area]
    model = AsyncMock(return_value=(f"FEED|https://example.com/new-feed|{area}|Fixture", {}))
    monkeypatch.setattr("digest.llm.complete", model)
    monkeypatch.setattr("digest.discovery_feed.validate_feed_url", AsyncMock(side_effect=lambda url: url))
    await discover_sources("config.yaml", phase="prepare")
    model.assert_awaited_once()
    messages = model.call_args.args[1]
    prompt = messages[-1]["content"]
    assert _requested_area(messages) == area
    assert "may refresh the owner's priority professional radar" in prompt
    assert "When exploring another discipline, no technology, finance or banking connection is required" in prompt
    assert "beyond their existing professional portfolio" not in prompt
    assert model.call_args.kwargs["max_output_tokens"] == 2048
    proposal = load_pending(".cache")[0]
    assert proposal.category == area
    assert load_delivery(".cache")["proposal_areas"][proposal_binding(proposal)] == area


@pytest.mark.asyncio
async def test_offers_unknown_rejection_and_empty_area_rotate_without_resends(
    discovery_config: Config, monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested = []
    sent_bindings = []

    async def generate(role: Any, messages: list[dict[str, str]], *args: Any, **kwargs: Any) -> tuple[str, dict]:
        area = _requested_area(messages)
        requested.append(area)
        return ("", {}) if area == "science" else (
            f"FEED|https://example.com/exploration-{len(requested)}|{area}|Fixture {len(requested)}", {},
        )

    async def send(source: PendingSource, *args: Any) -> ProposalDelivery:
        binding = proposal_binding(source)
        assert binding not in sent_bindings
        sent_bindings.append(binding)
        # The existing sender has saved uncertainty before entering the mocked POST.
        assert load_delivery(".cache")["deliveries"][binding]["status"] == "unknown"
        if source.category == "history/culture":
            return ProposalDelivery("unknown")
        if source.category == "environment":
            return ProposalDelivery("rejected")
        return ProposalDelivery("confirmed", len(sent_bindings))

    monkeypatch.setattr("digest.llm.complete", generate)
    monkeypatch.setattr("digest.discovery_feed.validate_feed_url", AsyncMock(side_effect=lambda url: url))
    monkeypatch.setattr("digest.adapters.telegram.discovery.send_source_approval_message", send)
    for index in range(12):
        monkeypatch.setenv("GITHUB_RUN_ID", f"run-{index}")
        await discover_sources("config.yaml", phase="prepare")
        await _send_prepared()
        if index == 2:
            proposal = next(source for source in load_pending(".cache") if source.category == "society/institutions")
            record_source_history(proposal, "rejected", ".cache")
            save_pending([source for source in load_pending(".cache") if source != proposal], ".cache", strict=True)
    assert requested == discovery_config.discovery.exploration_areas + [
        "science", "environment", "fintech/banking/architecture", "society/institutions", "history/culture", "design",
    ]
    data = load_delivery(".cache")
    assert data["area_offers"]["history/culture"]["status"] == "unknown"
    assert data["area_offers"]["society/institutions"]["status"] == "confirmed"
    assert "environment" not in data["area_offers"]
    assert "science" not in data["area_offers"]
    assert len(sent_bindings) == 10
    assert data["history"][0]["decision"] == "rejected"


@pytest.mark.asyncio
async def test_pending_failure_skips_one_distinct_run_and_preserves_identity(
    discovery_config: Config, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = PendingSource("Legacy", "https://example.com/legacy", "Tech", datetime.now(timezone.utc).isoformat())
    save_pending([source], ".cache")
    binding = proposal_binding(source)
    validate = AsyncMock(side_effect=[ValueError("unavailable"), ValueError("unavailable"), source.url])
    model = AsyncMock(return_value=("", {}))
    monkeypatch.setattr("digest.discovery_feed.validate_feed_url", validate)
    monkeypatch.setattr("digest.llm.complete", model)
    for run, attempt, calls, deferred in [
        ("A", "1", 1, 0), ("A", "2", 1, 1), ("B", "1", 1, 1), ("B", "2", 1, 1),
        ("C", "1", 2, 0), ("D", "1", 2, 1), ("E", "1", 3, 0),
    ]:
        monkeypatch.setenv("GITHUB_RUN_ID", run)
        monkeypatch.setenv("GITHUB_RUN_ATTEMPT", attempt)
        await discover_sources("config.yaml", phase="prepare")
        data = load_delivery(".cache")
        assert validate.await_count == calls
        assert data["prepare_counts"]["validation_deferred"] == deferred
        assert load_pending(".cache") == [source]
        assert proposal_binding(load_pending(".cache")[0]) == binding
        assert data["proposal_areas"] == {}  # Do not guess an area for the legacy identity.
        prompt = model.call_args.args[1][-1]["content"]
        assert prompt.startswith("Suggest up to 3" if deferred else "Suggest up to 2")
        if run == "B":
            assert data["validation_failures"][binding]["skipped_cycle"] == "github:B"
        if run != "E":
            before = data["validation_failures"]
            await _send_prepared()
            assert load_delivery(".cache")["validation_failures"] == before
    assert load_delivery(".cache")["validation_failures"] == {}
    assert load_delivery(".cache")["batch"]["bindings"] == [binding]


def test_latest_area_offers_survive_receipt_horizon_and_config_removal(tmp_path: Path) -> None:
    now = datetime.now(timezone.utc)
    data = load_delivery(str(tmp_path))
    for index, (area, status) in enumerate([("science", "confirmed"), ("history", "unknown"), ("design", "reserved")]):
        binding = str(index) * 64
        data["proposal_areas"][binding] = area
        data["deliveries"][binding] = {
            "status": status, "updated_at": (now - timedelta(days=31 + index)).isoformat(), "message_id": 1,
        }
    save_delivery(data, str(tmp_path))
    _, pruned, _ = prune_discovery_state(str(tmp_path), now, ["science", "history", "design"])
    assert pruned["deliveries"] == {}
    assert pruned["proposal_areas"] == {}
    assert set(pruned["area_offers"]) == {"science", "history"}
    assert [select_exploration_area(pruned, ["science", "history", "design"]) for _ in range(3)] == [
        "design", "history", "science",
    ]
    save_delivery(pruned, str(tmp_path))
    _, pruned, _ = prune_discovery_state(str(tmp_path), now, ["design"])
    assert pruned["area_offers"] == {}
    assert pruned["attempted_areas"] == ["design"]


def test_expiry_removes_retry_without_changing_expiry_or_editorial_decision(tmp_path: Path) -> None:
    now = datetime.now(timezone.utc)
    source = PendingSource("Expired", "https://example.com/expired", "Science", (now - timedelta(days=31)).isoformat())
    (tmp_path / PENDING_FILE).write_text(json.dumps({"pending": [asdict(source)]}))
    data = load_delivery(str(tmp_path))
    data["validation_failures"][proposal_binding(source)] = {
        "failed_at": (now - timedelta(days=2)).isoformat(), "failed_cycle": "github:A", "skipped_cycle": None,
    }
    save_delivery(data, str(tmp_path))
    pending, pruned, count = prune_discovery_state(str(tmp_path), now, ["science"])
    assert pending == [] and count == 1
    assert pruned["validation_failures"] == {}
    assert [item["decision"] for item in pruned["history"]] == ["expired"]


@pytest.mark.parametrize("key,value", [
    ("proposal_areas", {"bad-binding": "science"}), ("proposal_areas", {"a" * 64: ""}),
    ("area_offers", {"science": {"status": "reserved", "updated_at": "2026-01-01T00:00:00+00:00"}}),
    ("area_offers", {"science": {"status": "confirmed", "updated_at": "2026-01-01"}}),
    ("attempted_areas", ["science", "science"]), ("attempted_areas", [str(n) for n in range(17)]),
    ("validation_failures", {"a" * 64: {"failed_at": "2026-01-01T00:00:00+00:00", "failed_cycle": "A"}}),
    ("generation", {"requested_area": "science", "outcome": "covered"}),
])
def test_malformed_optional_metadata_fails_closed(tmp_path: Path, key: str, value: Any) -> None:
    data = {"schema_version": 1, "deliveries": {}, "history": [], "batch": None, key: value}
    (tmp_path / DELIVERY_FILE).write_text(json.dumps(data))
    with pytest.raises(ValueError):
        load_delivery(str(tmp_path))


def test_metadata_write_bound_preserves_existing_file(tmp_path: Path) -> None:
    data = load_delivery(str(tmp_path))
    save_delivery(data, str(tmp_path))
    original = (tmp_path / DELIVERY_FILE).read_bytes()
    data["oversized"] = "x" * 256000
    with pytest.raises(ValueError, match="storage bound"):
        save_delivery(data, str(tmp_path))
    assert (tmp_path / DELIVERY_FILE).read_bytes() == original


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["prepare", "all"])
@pytest.mark.parametrize("failure", [None, "pending", "reservation", "output"])
async def test_discovery_output_is_between_durable_reservation_and_send(
    discovery_config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], phase: str, failure: str | None,
) -> None:
    from digest.application.discovery import PreparedDiscovery
    from digest.cli import reporting

    monkeypatch.delenv("GITHUB_RUN_ID")
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    if failure == "output":
        output.mkdir()
    monkeypatch.setattr("digest.llm.complete", AsyncMock(return_value=(
        "FEED|https://example.com/barrier-feed|science|Barrier feed", {},
    )))
    monkeypatch.setattr("digest.discovery_feed.validate_feed_url", AsyncMock(side_effect=lambda url: url))
    events: list[str] = []
    prepared_outputs: list[PreparedDiscovery] = []

    def persist_pending(pending: list[PendingSource], cache_dir: str, *, strict: bool = False) -> None:
        assert strict
        if pending and failure == "pending":
            raise OSError("injected pending write failure")
        save_pending(pending, cache_dir, strict=strict)
        if pending:
            events.append("pending")

    def persist_delivery(data: dict[str, Any], cache_dir: str) -> None:
        if data["batch"] is not None and failure == "reservation":
            raise OSError("injected reservation write failure")
        save_delivery(data, cache_dir)
        if data["batch"] is not None:
            events.append("reservation")

    publish_outputs = reporting.publish_discovery_prepared

    def report(prepared: PreparedDiscovery) -> None:
        assert events == ["pending", "reservation"]
        assert prepared.pending_sha == hashlib.sha256((Path(".cache") / PENDING_FILE).read_bytes()).hexdigest()
        assert prepared.delivery_sha == hashlib.sha256((Path(".cache") / DELIVERY_FILE).read_bytes()).hexdigest()
        data = load_delivery(".cache")
        assert data["batch"]["owner"].startswith("local:")
        assert all(receipt["status"] == "reserved" for receipt in data["deliveries"].values())
        prepared_outputs.append(prepared)
        publish_outputs(prepared)
        events.append("output")

    async def send(proposal: PendingSource, token: str, chat: str, username: str) -> ProposalDelivery:
        prepared = prepared_outputs[0]
        assert output.read_text() == (f"discovery_pending_sha256={prepared.pending_sha}\n"
                                      f"discovery_delivery_sha256={prepared.delivery_sha}\n")
        assert events[:3] == ["pending", "reservation", "output"]
        data = load_delivery(".cache")
        receipt = data["deliveries"][proposal_binding(proposal)]
        assert receipt["status"] == "unknown"
        assert receipt["owner"] == data["batch"]["owner"]
        expected_target = hashlib.sha256(json.dumps([token, chat, username]).encode()).hexdigest()
        assert data["batch"]["target"] == expected_target
        events.append("send")
        return ProposalDelivery("confirmed", 42)

    transport = AsyncMock(side_effect=send)
    monkeypatch.setattr("digest.adapters.storage.pending_sources.save_pending", persist_pending)
    monkeypatch.setattr("digest.adapters.storage.discovery.save_delivery", persist_delivery)
    monkeypatch.setattr(reporting, "publish_discovery_prepared", report)
    monkeypatch.setattr("digest.adapters.telegram.discovery.send_source_approval_message", transport)
    if failure:
        with pytest.raises(OSError):
            await discover_sources("config.yaml", phase=phase)
        transport.assert_not_awaited()
        assert capsys.readouterr().out == ""
        if failure != "output":
            assert not output.exists()
        else:
            assert events == ["pending", "reservation"]
        return
    assert await discover_sources("config.yaml", phase=phase) == 0
    assert events[:3] == ["pending", "reservation", "output"]
    if phase == "all":
        transport.assert_awaited_once()
        assert json.loads(capsys.readouterr().out)["counts"]["confirmed"] == 1
    else:
        transport.assert_not_awaited()
        assert json.loads(capsys.readouterr().out)["stage"] == "prepare"
