"""Synthetic-only native prefill integration through real discovery preparation."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from digest._serialization import canonical_json_bytes
from digest.adapters.storage import discovery as storage
from digest.adapters.storage.pending_sources import load_pending, save_pending
from digest.application import discovery as application
from digest.application import native_discovery as native
from digest.config import (
    Config,
    FiltersConfig,
    IrritatorConfig,
    LLMConfig,
    ObsidianConfig,
    ProviderConfig,
    RadarConfig,
    SourceConfig,
    TelegramConfig,
)
from digest.domain.catalog.exploration import ProposalDelivery
from digest.domain.catalog.proposals import PendingSource

NOW = datetime.now(timezone.utc).replace(second=0, microsecond=0)
OWNER = "actual-controller-fixture"
WORKER = "actual-terminal-worker-fixture"


@pytest.fixture
def session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> application.DiscoverySession:
    monkeypatch.chdir(tmp_path)
    clock = Mock(wraps=datetime)
    clock.now.return_value = NOW
    monkeypatch.setattr(application, "datetime", clock)
    monkeypatch.setattr("digest.llm.complete", AsyncMock(side_effect=AssertionError("No provider API permitted")))
    monkeypatch.setattr("digest.llm._resolve_routed_providers", Mock(side_effect=AssertionError("No provider route")))
    config = Config(LLMConfig([ProviderConfig("groq", "not-called")]), RadarConfig(), IrritatorConfig(),
                    [SourceConfig("Configured", "https://configured.example/rss", "Science", True)],
                    FiltersConfig(), TelegramConfig(bot_username="fixture_bot"), ObsidianConfig())
    return application.DiscoverySession(config, "123:1", "github:123", "target", "fixture-token", "fixture-chat")


def _proposal(number: int = 1) -> dict[str, str]:
    return {"url": f"https://example.org/{number}.rss", "category": "Science", "name": f"Source {number}"}


def _pending(number: int = 0) -> PendingSource:
    return PendingSource(f"Pending {number}", f"https://pending.example/{number}", "Science",
                         (NOW - timedelta(hours=2)).isoformat())


def _prefill(session: application.DiscoverySession, proposals: list[dict[str, str]] | None = None,
             *, reserve: bool = True) -> native.NativeDiscoveryInput:
    request = native.export_request(session.config, cache=Path(session.cache_dir), slot=NOW,
                                    controller_ref=OWNER, now=NOW - timedelta(minutes=5))
    response = canonical_json_bytes({"schema": "native-discovery-response/v1", "request_sha256": request.sha256,
                                     "execution_ref": WORKER, "proposals": proposals if proposals is not None
                                     else [_proposal()]})
    Path("response.json").write_bytes(response)
    prefill = native.bind_prefill(Path(request.path), Path("response.json"), execution_ref=WORKER,
                                 controller_ref=OWNER, cache=Path(session.cache_dir), now=NOW - timedelta(minutes=1))
    inputs = native.NativeDiscoveryInput(Path(prefill.path), prefill.sha256, OWNER)
    if reserve:
        inputs = replace(inputs, reservation_sha256=native.reserve_prefill(session, inputs, now=NOW))
    return inputs


def _feed(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    feed = AsyncMock(side_effect=lambda url: url)
    monkeypatch.setattr("digest.discovery_feed.validate_feed_url", feed)
    return feed


def _slot(session: application.DiscoverySession) -> Path:
    return Path(session.cache_dir) / "native_discovery" / NOW.strftime("%Y-%m-%dT%H-%M-%SZ")


async def test_native_prepare_actual_pending_pair_and_existing_send(
    session: application.DiscoverySession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = _prefill(session)
    feed = _feed(monkeypatch)
    send = AsyncMock(return_value=ProposalDelivery("confirmed", 73))
    monkeypatch.setattr("digest.adapters.telegram.discovery.send_source_approval_message", send)
    prepared = await application.prepare_and_reserve(session, native=inputs)
    assert prepared.counts["prepared"] == 1 and prepared.counts["generation_failed"] == 0
    feed.assert_awaited_once_with(_proposal()["url"])
    pending = load_pending(session.cache_dir, strict=True)
    assert len(pending) == 1 and pending[0].discovered_at == NOW.isoformat()
    assert storage.pending_sha256(session.cache_dir) == prepared.pending_sha
    assert storage.delivery_sha256(session.cache_dir) == prepared.delivery_sha
    assert len(session.config.sources) == 1  # Suggestions are not source activation.
    result = await application.send_reserved(session, prepared.pending_sha, prepared.delivery_sha)
    assert result.counts["confirmed"] == 1
    send.assert_awaited_once()
    with pytest.raises(ValueError):
        await application.send_reserved(session, prepared.pending_sha, storage.delivery_sha256(session.cache_dir))
    assert send.await_count == 1


async def test_old_pending_checks_have_priority_and_native_only_uses_remaining_prefix(
    session: application.DiscoverySession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = [_pending(0), _pending(1)]
    save_pending(old, session.cache_dir, strict=True)
    inputs = _prefill(session, [_proposal(1), _proposal(2), _proposal(3)])
    feed = _feed(monkeypatch)
    prepared = await application.prepare_and_reserve(session, native=inputs)
    assert [call.args[0] for call in feed.await_args_list] == [old[0].url, old[1].url, _proposal(1)["url"]]
    assert prepared.counts["prepared"] == 3
    started = json.loads((_slot(session) / "started.json").read_bytes())
    assert started["admitted_count"] == 1 and started["deferred"] == [_proposal(2), _proposal(3)]
    assert json.loads(json.loads(inputs.prefill.read_bytes())["response_raw"])["proposals"] == [
        _proposal(1), _proposal(2), _proposal(3)]


async def test_failed_old_check_consumes_budget_and_full_pending_skips_native(
    session: application.DiscoverySession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = [_pending(n) for n in range(3)]
    save_pending(old, session.cache_dir, strict=True)
    inputs = _prefill(session)
    feed = AsyncMock(side_effect=ValueError("unavailable"))
    monkeypatch.setattr("digest.discovery_feed.validate_feed_url", feed)
    prepared = await application.prepare_and_reserve(session, native=inputs)
    assert feed.await_count == 3 and prepared.counts["invalid_feed"] == 3
    assert not (_slot(session) / "started.json").exists()
    assert prepared.counts["generation_failed"] == 0


@pytest.mark.parametrize("change", ["missing", "hash", "controller", "reservation", "owner", "cycle", "source_url",
                                     "source_category", "source_enabled", "policy", "history", "expired", "early"])
async def test_unavailable_native_never_calls_api_and_keeps_old_pending_sendable(
    session: application.DiscoverySession, monkeypatch: pytest.MonkeyPatch, change: str,
) -> None:
    old = _pending()
    save_pending([old], session.cache_dir, strict=True)
    inputs = _prefill(session)
    if change == "missing":
        inputs = replace(inputs, prefill=Path("absent.json"))
    elif change == "hash":
        inputs = replace(inputs, prefill_sha256="0" * 64)
    elif change == "controller":
        inputs = replace(inputs, controller_ref="wrong")
    elif change == "reservation":
        inputs = replace(inputs, reservation_sha256="")
    elif change in {"owner", "cycle"}:
        session = replace(session, **{change: "different"})
    elif change.startswith("source_"):
        key = change.removeprefix("source_")
        setattr(session.config.sources[0], key, False if key == "enabled" else "changed")
    elif change == "policy":
        session.config.discovery.exploration_areas = ["Changed policy"]
    elif change == "history":
        data = storage.load_delivery(session.cache_dir)
        data["history"].append({"binding": "changed", "url": "https://other.example/rss", "name": "Other",
                                "category": "Science", "decision": "rejected", "recorded_at": NOW.isoformat()})
        storage.save_delivery(data, session.cache_dir)
    else:
        application.datetime.now.return_value = (
            (NOW + timedelta(days=1)).replace(hour=0, minute=0) if change == "expired"
            else NOW - timedelta(minutes=1)
        )
    feed = _feed(monkeypatch)
    prepared = await application.prepare_and_reserve(session, native=inputs)
    assert prepared.counts["generation_failed"] == 1
    assert prepared.counts["prepared"] == 1
    assert load_pending(session.cache_dir, strict=True) == [old]
    feed.assert_awaited_once_with(old.url)
    assert not (_slot(session) / "started.json").exists()


async def test_empty_native_is_distinct_from_unavailable_and_consumed_once(
    session: application.DiscoverySession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = _prefill(session, [])
    feed = _feed(monkeypatch)
    prepared = await application.prepare_and_reserve(session, native=inputs)
    assert prepared.counts["generation_failed"] == 0
    assert storage.load_delivery(session.cache_dir)["generation"]["outcome"] == "empty"
    # Direct admission with the originally reviewed snapshot cannot replay even in the same owner.
    snapshot = json.loads(json.loads(inputs.prefill.read_bytes())["request_raw"])["context"]
    with pytest.raises(native.NativeDiscoveryUnavailable, match="already attempted"):
        native.consume_prefill(session, inputs, snapshot, NOW, 3)
    feed.assert_not_awaited()


def test_export_preview_preserves_bytes_and_rotation_and_is_immutable(session: application.DiscoverySession) -> None:
    old = _pending()
    save_pending([old], session.cache_dir, strict=True)
    data = storage.load_delivery(session.cache_dir)
    data["attempted_areas"] = [session.config.discovery.exploration_areas[0]]
    storage.save_delivery(data, session.cache_dir)
    before = {name: (Path(session.cache_dir) / name).read_bytes()
              for name in ("pending_sources.json", "discovery_delivery.json")}
    first = native.export_request(session.config, cache=Path(session.cache_dir), slot=NOW,
                                  controller_ref=OWNER, now=NOW - timedelta(minutes=5))
    second = native.export_request(session.config, cache=Path(session.cache_dir), slot=NOW,
                                   controller_ref=OWNER, now=NOW - timedelta(minutes=4))
    assert first == second
    assert before == {name: (Path(session.cache_dir) / name).read_bytes() for name in before}
    request = json.loads(Path(first.path).read_bytes())
    assert request["context"]["proposal_context"]["requested_exploration_area"] != data["attempted_areas"][0]
    session.config.sources[0].name = "Changed"
    with pytest.raises(ValueError, match="different immutable request"):
        native.export_request(session.config, cache=Path(session.cache_dir), slot=NOW,
                              controller_ref=OWNER, now=NOW - timedelta(minutes=3))


@pytest.mark.parametrize("bad", ["duplicate_key", "unknown", "newline", "delimiter", "long", "overfull", "wrong_worker",
                                  "wrong_request", "empty_field", "not_list", "non_string"])
def test_native_response_schema_is_strict(session: application.DiscoverySession, bad: str) -> None:
    request = native.export_request(session.config, cache=Path(session.cache_dir), slot=NOW,
                                    controller_ref=OWNER, now=NOW - timedelta(minutes=5))
    value = {"schema": "native-discovery-response/v1", "request_sha256": request.sha256,
             "execution_ref": WORKER, "proposals": [_proposal()]}
    if bad == "unknown":
        value["arbitrary_code"] = "never execute"
    elif bad == "wrong_worker":
        value["execution_ref"] = "self-asserted-other-worker"
    elif bad == "wrong_request":
        value["request_sha256"] = "0" * 64
    elif bad == "overfull":
        value["proposals"] = [_proposal(n) for n in range(4)]
    elif bad == "not_list":
        value["proposals"] = {}
    elif bad != "duplicate_key":
        value["proposals"][0]["name"] = {"newline": "A\nFEED|https://bad|X|X", "delimiter": "A|B", "long": "a" * 161,
                                        "empty_field": "", "non_string": None}[bad]
    raw = canonical_json_bytes(value)
    if bad == "duplicate_key":
        raw = raw.replace(b'"schema":', b'"schema":"x","schema":')
    Path("response.json").write_bytes(raw)
    with pytest.raises(ValueError):
        native.bind_prefill(Path(request.path), Path("response.json"), execution_ref=WORKER,
                            controller_ref=OWNER, cache=Path(session.cache_dir), now=NOW - timedelta(minutes=1))
    assert not (_slot(session) / "prefill.json").exists()


def test_bind_rejects_late_and_different_actual_worker_and_rebinding(session: application.DiscoverySession) -> None:
    inputs = _prefill(session, reserve=False)
    request_path = _slot(session) / "request.json"
    for worker, instant in [("wrong-worker", NOW), (WORKER, (NOW + timedelta(days=1)).replace(hour=0, minute=0))]:
        with pytest.raises(ValueError):
            native.bind_prefill(request_path, Path("response.json"), execution_ref=worker,
                                controller_ref=OWNER, cache=Path(session.cache_dir), now=instant)
    before = inputs.prefill.read_bytes()
    with pytest.raises(FileExistsError):
        native.bind_prefill(request_path, Path("response.json"), execution_ref=WORKER,
                            controller_ref=OWNER, cache=Path(session.cache_dir), now=NOW)
    assert inputs.prefill.read_bytes() == before


def test_reservation_is_exclusive_and_missing_remote_hash_is_not_admissible(
    session: application.DiscoverySession,
) -> None:
    inputs = _prefill(session)
    with pytest.raises(native.NativeDiscoveryUnavailable, match="already reserved"):
        native.reserve_prefill(session, inputs, now=NOW)
    snapshot = native.preview_context(session.config, Path(session.cache_dir), NOW)
    with pytest.raises(native.NativeDiscoveryUnavailable, match="reservation"):
        native.consume_prefill(session, replace(inputs, reservation_sha256=""), snapshot, NOW, 3)
    assert not (_slot(session) / "started.json").exists()


async def test_new_attempt_cannot_reuse_remote_reserved_artifact(
    session: application.DiscoverySession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = _prefill(session)
    changed_owner = replace(session, owner="123:2")
    feed = _feed(monkeypatch)
    prepared = await application.prepare_and_reserve(changed_owner, native=inputs)
    assert prepared.counts["generation_failed"] == 1
    feed.assert_not_awaited()
    assert not (_slot(session) / "started.json").exists()


async def test_cancelled_consumption_preserves_validated_pending_prefix_without_regeneration(
    session: application.DiscoverySession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = _prefill(session, [_proposal(1), _proposal(2)])
    feed = AsyncMock(side_effect=[_proposal(1)["url"], asyncio.CancelledError()])
    monkeypatch.setattr("digest.discovery_feed.validate_feed_url", feed)
    with pytest.raises(asyncio.CancelledError):
        await application.prepare_and_reserve(session, native=inputs)
    assert (_slot(session) / "started.json").exists()
    assert [source.url for source in load_pending(session.cache_dir, strict=True)] == [_proposal(1)["url"]]
    feed = _feed(monkeypatch)
    result = await application.prepare_and_reserve(replace(session, owner="123:2"), native=inputs)
    assert result.counts["prepared"] == 1 and result.counts["generation_failed"] == 1
    feed.assert_awaited_once_with(_proposal(1)["url"])


async def test_real_validator_rejects_private_literal_before_any_socket(
    session: application.DiscoverySession,
) -> None:
    inputs = _prefill(session, [{**_proposal(), "url": "http://127.0.0.1/private"}])
    prepared = await application.prepare_and_reserve(session, native=inputs)
    assert prepared.counts["invalid_feed"] == 1 and prepared.counts["prepared"] == 0
    assert load_pending(session.cache_dir, strict=True) == []


async def test_safe_redirect_dedup_and_rejected_history_use_existing_validation_body(
    session: application.DiscoverySession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    proposals = [_proposal(1), _proposal(1), _proposal(2)]
    inputs = _prefill(session, proposals)
    feed = AsyncMock(return_value=session.config.sources[0].url)
    monkeypatch.setattr("digest.discovery_feed.validate_feed_url", feed)
    prepared = await application.prepare_and_reserve(session, native=inputs)
    assert prepared.counts["duplicates"] == 3 and prepared.counts["prepared"] == 0
    assert feed.await_count == 2


async def test_unknown_send_is_held_without_replay(session: application.DiscoverySession,
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    inputs = _prefill(session)
    _feed(monkeypatch)
    prepared = await application.prepare_and_reserve(session, native=inputs)
    send = AsyncMock(return_value=ProposalDelivery("unknown"))
    monkeypatch.setattr("digest.adapters.telegram.discovery.send_source_approval_message", send)
    await application.send_reserved(session, prepared.pending_sha, prepared.delivery_sha)
    with pytest.raises(ValueError):
        await application.send_reserved(session, prepared.pending_sha, storage.delivery_sha256(session.cache_dir))
    assert send.await_count == 1


def test_provider_prompt_context_is_exact_legacy_serialization(session: application.DiscoverySession) -> None:
    pending = [_pending()]
    data = storage.load_delivery(session.cache_dir)
    area = session.config.discovery.exploration_areas[0]
    expected = {"requested_exploration_area": area,
                "exploration_areas": session.config.discovery.exploration_areas,
                "categories": {"Science": ["Configured"]}, "configured_urls": [session.config.sources[0].url],
                "pending_urls": [pending[0].url], "history": []}
    assert json.dumps(native.proposal_context(session.config, pending, data, area), ensure_ascii=False) == json.dumps(
        expected, ensure_ascii=False)
    before = copy.deepcopy(data)
    native.context(session.config, pending, data)
    assert data == before


def test_no_legacy_retained_shapes_or_sidecars_changed_by_provider_default(
    session: application.DiscoverySession,
) -> None:
    pending = _pending()
    assert set(asdict(pending)) == {"name", "url", "category", "discovered_at", "source_hash"}
    data = storage.load_delivery(session.cache_dir)
    storage.save_delivery(data, session.cache_dir)
    before = (Path(session.cache_dir) / storage.DELIVERY_FILE).read_bytes()
    storage.save_delivery(storage.load_delivery(session.cache_dir), session.cache_dir)
    assert (Path(session.cache_dir) / storage.DELIVERY_FILE).read_bytes() == before
    assert hashlib.sha256(before).hexdigest() == storage.delivery_sha256(session.cache_dir)
    assert not (Path(session.cache_dir) / "native_discovery").exists()


def test_delayed_same_day_runner_is_not_confused_with_ten_minute_job_timeout(
    session: application.DiscoverySession,
) -> None:
    slot = NOW.replace(hour=0, minute=0)
    request = native.export_request(session.config, cache=Path(session.cache_dir), slot=slot,
                                    controller_ref=OWNER, now=slot - timedelta(minutes=5))
    Path("response.json").write_bytes(canonical_json_bytes({
        "schema": "native-discovery-response/v1", "request_sha256": request.sha256,
        "execution_ref": WORKER, "proposals": [_proposal()],
    }))
    prefill = native.bind_prefill(Path(request.path), Path("response.json"), execution_ref=WORKER,
                                 controller_ref=OWNER, cache=Path(session.cache_dir), now=slot - timedelta(minutes=1))
    inputs = native.NativeDiscoveryInput(Path(prefill.path), prefill.sha256, OWNER)
    # Real cron delays can be hours; the runner's ten-minute budget is not an arrival SLA.
    observed = slot + timedelta(hours=12)
    inputs = replace(inputs, reservation_sha256=native.reserve_prefill(session, inputs, now=observed))
    snapshot = native.preview_context(session.config, Path(session.cache_dir), observed)
    assert native.consume_prefill(session, inputs, snapshot, observed, 3).startswith("FEED|")


async def test_explicit_blank_native_cli_flag_never_enters_provider_mode(
    session: application.DiscoverySession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from digest.main import main

    monkeypatch.setattr(application, "start_session", Mock(return_value=session))
    _feed(monkeypatch)
    assert await main(["--discover", "--discovery-phase", "prepare", "--discovery-native-prefill", ""]) == 0
    assert storage.load_delivery(session.cache_dir)["prepare_counts"]["generation_failed"] == 1


async def test_native_positive_uses_real_feed_parser_with_stubbed_acquisition(
    session: application.DiscoverySession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    from digest.adapters.http.public_fetch import PublicResponse

    inputs = _prefill(session)
    response = PublicResponse(_proposal()["url"], 200, httpx.Headers({"Content-Type": "application/rss+xml"}),
                              b'<rss version="2.0"><channel><title>Synthetic</title></channel></rss>', "utf-8")
    fetch = AsyncMock(return_value=response)
    monkeypatch.setattr("digest.discovery_feed.fetch_public", fetch)
    prepared = await application.prepare_and_reserve(session, native=inputs)
    assert prepared.counts["prepared"] == 1
    fetch.assert_awaited_once_with(_proposal()["url"], timeout=15.0, max_bytes=2 * 1024 * 1024, max_redirects=3)


@pytest.mark.parametrize("field", ["request_raw", "response_raw", "execution_ref", "bound_at", "schema"])
async def test_tampered_prefill_is_unavailable_even_with_new_outer_hash(
    session: application.DiscoverySession, monkeypatch: pytest.MonkeyPatch, field: str,
) -> None:
    inputs = _prefill(session)
    value = json.loads(inputs.prefill.read_bytes())
    value[field] = "changed"
    raw = canonical_json_bytes(value)
    inputs.prefill.write_bytes(raw)
    inputs = replace(inputs, prefill_sha256=hashlib.sha256(raw).hexdigest())
    feed = _feed(monkeypatch)
    result = await application.prepare_and_reserve(session, native=inputs)
    assert result.counts["generation_failed"] == 1
    feed.assert_not_awaited()


async def test_consumption_storage_failure_stops_before_new_feed_validation(
    session: application.DiscoverySession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = _prefill(session)
    feed = _feed(monkeypatch)
    monkeypatch.setattr(native, "_exclusive", Mock(side_effect=OSError("disk failure")))
    with pytest.raises(OSError, match="disk failure"):
        await application.prepare_and_reserve(session, native=inputs)
    feed.assert_not_awaited()
    assert load_pending(session.cache_dir, strict=True) == []


def test_reserve_cli_missing_input_reports_unavailable_without_failing_weekly_pending_stage(
    session: application.DiscoverySession, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    from digest import discovery_native

    monkeypatch.setattr(discovery_native, "start_session", Mock(return_value=session))
    discovery_native.main(["reserve", "--config", "unused", "--prefill", "absent.json", "--prefill-sha", "",
                           "--controller", OWNER])
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "unavailable" and output["reservation_sha256"] == ""


def test_reservation_unexpected_storage_error_is_not_downgraded_to_unavailable(
    session: application.DiscoverySession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = _prefill(session, reserve=False)
    monkeypatch.setattr(native, "_exclusive", Mock(side_effect=OSError("disk failure")))
    with pytest.raises(OSError, match="disk failure"):
        native.reserve_prefill(session, inputs, now=NOW)


async def test_send_only_current_pair_is_independent_of_prefill_availability(
    session: application.DiscoverySession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from digest.main import main

    inputs = _prefill(session)
    _feed(monkeypatch)
    prepared = await application.prepare_and_reserve(session, native=inputs)
    inputs.prefill.unlink()
    monkeypatch.setattr(application, "start_session", Mock(return_value=session))
    send = AsyncMock(return_value=ProposalDelivery("confirmed", 92))
    monkeypatch.setattr("digest.adapters.telegram.discovery.send_source_approval_message", send)
    assert await main(["--discover", "--discovery-phase", "send", "--discovery-pending-sha", prepared.pending_sha,
                       "--discovery-delivery-sha", prepared.delivery_sha]) == 0
    send.assert_awaited_once()


def test_outside_repository_output_rejected_before_any_directory_creation(
    session: application.DiscoverySession, tmp_path: Path,
) -> None:
    outside = tmp_path.parent / (tmp_path.name + "-outside-native-output")
    assert not outside.exists()
    with pytest.raises(ValueError):
        native.export_request(session.config, cache=outside / "nested", slot=NOW,
                              controller_ref=OWNER, now=NOW - timedelta(minutes=5))
    assert not outside.exists()


async def test_already_expired_pending_preview_has_stable_semantics_and_real_prepare_prunes_once(
    session: application.DiscoverySession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    expired = replace(_pending(), discovered_at=(NOW - timedelta(days=31)).isoformat())
    cache = Path(session.cache_dir)
    cache.mkdir()
    pending_path = cache / "pending_sources.json"
    raw = json.dumps({"pending": [asdict(expired)]}).encode()
    pending_path.write_bytes(raw)  # Retained legacy bytes; save_pending would prune first.
    inputs = _prefill(session)
    assert pending_path.read_bytes() == raw  # Export/reserve never mutate operational state.
    _feed(monkeypatch)
    prepared = await application.prepare_and_reserve(session, native=inputs)
    assert prepared.counts["expired"] == 1 and prepared.counts["prepared"] == 1
    assert prepared.counts["generation_failed"] == 0
    history = storage.load_delivery(session.cache_dir)["history"]
    assert len(history) == 1 and history[0]["recorded_at"] == NOW.isoformat()
    assert history[0]["decision"] == "expired"


def test_pending_expiring_between_export_and_reservation_still_changes_context(
    session: application.DiscoverySession,
) -> None:
    boundary = replace(_pending(), discovered_at=(NOW - timedelta(days=30, minutes=2)).isoformat())
    cache = Path(session.cache_dir)
    cache.mkdir()
    (cache / "pending_sources.json").write_text(json.dumps({"pending": [asdict(boundary)]}))
    inputs = _prefill(session, reserve=False)  # Export at NOW-5min: not expired yet.
    with pytest.raises(native.NativeDiscoveryUnavailable, match="context changed"):
        native.reserve_prefill(session, inputs, now=NOW)


@pytest.mark.parametrize(
    "flag", ["--discovery-native-sha", "--discovery-native-owner", "--discovery-native-reservation-sha"],
)
async def test_explicit_blank_auxiliary_native_flag_without_mode_fails_before_dispatch(
    monkeypatch: pytest.MonkeyPatch, flag: str,
) -> None:
    from digest import main as entry

    dispatch = AsyncMock(side_effect=AssertionError("Must not enter provider default"))
    monkeypatch.setattr(entry, "discover_sources", dispatch)
    assert await entry.main(["--discover", "--discovery-phase", "prepare", flag, ""]) == 1
    dispatch.assert_not_awaited()


@pytest.mark.parametrize("offset", [None, timezone(timedelta(hours=3))])
def test_noncanonical_input_clock_is_rejected_before_sidecar_writes(
    session: application.DiscoverySession, offset: timezone | None,
) -> None:
    bad = NOW.replace(tzinfo=offset)
    with pytest.raises(ValueError, match="canonical UTC"):
        native.export_request(session.config, cache=Path(session.cache_dir), slot=NOW,
                              controller_ref=OWNER, now=bad)
    assert not Path(session.cache_dir).exists()
    inputs = _prefill(session, reserve=False)
    before = {path: path.read_bytes() for path in Path(session.cache_dir).rglob("*") if path.is_file()}
    with pytest.raises(ValueError, match="canonical UTC"):
        native.bind_prefill(_slot(session) / "request.json", Path("response.json"), execution_ref=WORKER,
                            controller_ref=OWNER, cache=Path(session.cache_dir), now=bad)
    with pytest.raises(ValueError, match="canonical UTC"):
        native.reserve_prefill(session, inputs, now=bad)
    after = {path: path.read_bytes() for path in Path(session.cache_dir).rglob("*") if path.is_file()}
    assert after == before
