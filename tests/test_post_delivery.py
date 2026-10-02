"""Durable post-delivery tests: immutable archives, one attempt, no primary replay."""

from __future__ import annotations

import asyncio
import hashlib
import json
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from digest.irritator.evidence_stage import EvidenceIrritatorResult, Outcome
from digest.post_delivery import _send_supplement, execute_post_delivery, prepare_post_delivery
from digest.review import run_blind_review
from digest.review_checkpoint import load_review_checkpoint
from scripts.review_fixture import fixture_articles, fixture_config, fixture_response


@pytest.fixture(autouse=True)
def isolated_post_delivery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    config = fixture_config()
    with (
        patch("digest.post_delivery.load_config", side_effect=lambda _: deepcopy(config)),
        patch("httpx.AsyncClient", side_effect=AssertionError("No live HTTP")),
        patch("digest.delivery.send_article_cards", AsyncMock(side_effect=AssertionError("No primary delivery"))),
        patch("digest.radar.collect", AsyncMock(side_effect=AssertionError("No feed collection"))),
        patch("digest.radar.save_dedup_cache", side_effect=AssertionError("No dedup writes")),
        patch("digest.irritator.run_irritator", AsyncMock(side_effect=AssertionError("No summary-derived replay"))),
    ):
        yield


async def _checkpoint(path: Path) -> dict[str, Any]:
    with patch("digest.review.complete", side_effect=fixture_response):
        report = await run_blind_review(fixture_articles(), fixture_config())
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = asdict(report)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return payload


def _marker(checkpoint: Path) -> Path:
    return checkpoint.with_name(checkpoint.name.removesuffix(".review.json") + ".post-attempt.json")


def _result(checkpoint: Path) -> Path:
    return checkpoint.with_name(checkpoint.name.removesuffix(".review.json") + ".irritator.json")


def _markdown(checkpoint: Path) -> Path:
    return checkpoint.with_name(checkpoint.name.removesuffix(".review.json") + ".irritator.md")


@pytest.mark.asyncio
async def test_prepare_persists_checkpoint_identity_without_live_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkpoint = tmp_path / "digests/day.review.json"
    payload = await _checkpoint(checkpoint)
    original = checkpoint.read_bytes()
    output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    with patch("digest.irritator.evidence_stage.run_evidence_irritator", AsyncMock()) as run:
        assert prepare_post_delivery(Path("config.yaml"), checkpoint) == _marker(checkpoint)
    run.assert_not_called()
    record = json.loads(_marker(checkpoint).read_text())
    assert record["checkpoint"] == "digests/day.review.json"
    assert record["checkpoint_sha256"] == hashlib.sha256(original).hexdigest()
    assert record["bundle_id"] == payload["evidence"]["bundle_id"]
    assert record["execute_started"] is None
    assert dict(line.split("=", 1) for line in output.read_text().splitlines()) == {
        "checkpoint": "digests/day.review.json",
        "marker": "digests/day.post-attempt.json",
    }
    assert checkpoint.read_bytes() == original
    assert not _result(checkpoint).exists()
    assert not _markdown(checkpoint).exists()


@pytest.mark.asyncio
async def test_prepare_is_exclusive_and_never_replaces_an_existing_marker(tmp_path: Path) -> None:
    checkpoint = tmp_path / "digests/day.review.json"
    await _checkpoint(checkpoint)
    assert prepare_post_delivery(Path("config.yaml"), checkpoint) == _marker(checkpoint)
    first_marker = _marker(checkpoint).read_bytes()
    with patch("digest.irritator.evidence_stage.run_evidence_irritator", AsyncMock()) as run:
        assert prepare_post_delivery(Path("config.yaml"), checkpoint) is None
    run.assert_not_called()
    assert _marker(checkpoint).read_bytes() == first_marker


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["outside", "newline", "symlink", "wrong_suffix"])
async def test_prepare_rejects_unsafe_checkpoint_paths(kind: str, tmp_path: Path) -> None:
    checkpoint = tmp_path / "digests/day.review.json"
    await _checkpoint(checkpoint)
    if kind == "outside":
        candidate = tmp_path.parent / "outside.review.json"
    elif kind == "newline":
        candidate = tmp_path / "digests/day\nmarker=injected.review.json"
        candidate.write_bytes(checkpoint.read_bytes())
    elif kind == "symlink":
        candidate = tmp_path / "digests/alias.review.json"
        candidate.symlink_to(checkpoint)
    else:
        candidate = tmp_path / "digests/day.json"
        candidate.write_bytes(checkpoint.read_bytes())
    with patch("digest.irritator.evidence_stage.run_evidence_irritator", AsyncMock()) as run:
        with pytest.raises(ValueError):
            prepare_post_delivery(Path("config.yaml"), candidate)
    run.assert_not_called()
    assert not _marker(checkpoint).exists()


@pytest.mark.asyncio
async def test_prepare_rejects_changed_evidence_before_claiming_attempt(tmp_path: Path) -> None:
    checkpoint = tmp_path / "digests/day.review.json"
    payload = await _checkpoint(checkpoint)
    payload["evidence"]["items"][0]["excerpt"] = "Evidence changed after its bundle hash was calculated."
    checkpoint.write_text(json.dumps(payload), encoding="utf-8")
    with patch("digest.irritator.evidence_stage.run_evidence_irritator", AsyncMock()) as run:
        with pytest.raises(ValueError):
            prepare_post_delivery(Path("config.yaml"), checkpoint)
    run.assert_not_called()
    assert not _marker(checkpoint).exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [
    "missing", "changed_bytes", "changed_bundle", "changed_checkpoint", "already_started", "symlink",
])
async def test_execute_requires_matching_unused_marker_before_model_or_network_work(kind: str, tmp_path: Path) -> None:
    checkpoint = tmp_path / "digests/day.review.json"
    await _checkpoint(checkpoint)
    if kind != "missing":
        assert prepare_post_delivery(Path("config.yaml"), checkpoint) == _marker(checkpoint)
        marker = json.loads(_marker(checkpoint).read_text())
        if kind == "changed_bytes":
            checkpoint.write_text(checkpoint.read_text() + "\n", encoding="utf-8")
        elif kind == "changed_bundle":
            marker["bundle_id"] = "different-bundle"
        elif kind == "changed_checkpoint":
            marker["checkpoint"] = "digests/other.review.json"
        elif kind == "already_started":
            marker["execute_started"] = "2026-09-30T12:00:00+00:00"
        if kind == "symlink":
            target = tmp_path / "marker-target.json"
            _marker(checkpoint).rename(target)
            _marker(checkpoint).symlink_to(target)
        else:
            _marker(checkpoint).write_text(json.dumps(marker), encoding="utf-8")
    original = checkpoint.read_bytes()
    with (
        patch("digest.irritator.evidence_stage.run_evidence_irritator", AsyncMock()) as run,
        patch("digest.post_delivery._send_supplement", AsyncMock()) as send,
    ):
        with pytest.raises(ValueError):
            await execute_post_delivery(Path("config.yaml"), checkpoint)
    run.assert_not_called()
    send.assert_not_called()
    assert checkpoint.read_bytes() == original
    assert not _result(checkpoint).exists()
    assert not _markdown(checkpoint).exists()


def _stage_result(bundle_id: str, status: Outcome = "complete") -> EvidenceIrritatorResult:
    return EvidenceIrritatorResult(schema_version=1, bundle_id=bundle_id, status=status)


def _client_context() -> MagicMock:
    """An inert client: entering a context is allowed, HTTP requests are not."""
    client = MagicMock()
    client.get = AsyncMock(side_effect=AssertionError("No live HTTP GET"))
    client.post = AsyncMock(side_effect=AssertionError("No live HTTP POST"))
    client.request = AsyncMock(side_effect=AssertionError("No live HTTP request"))
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


@pytest.mark.asyncio
async def test_execute_marks_started_before_work_and_persists_result_before_supplement(tmp_path: Path) -> None:
    checkpoint = tmp_path / "digests/day.review.json"
    payload = await _checkpoint(checkpoint)
    original = checkpoint.read_bytes()
    assert prepare_post_delivery(Path("config.yaml"), checkpoint) == _marker(checkpoint)
    result = _stage_result(payload["evidence"]["bundle_id"])
    client = _client_context()

    async def run(bundle: Any, config: Any, actual_client: Any) -> EvidenceIrritatorResult:
        assert asdict(bundle) == payload["evidence"]
        assert actual_client is client
        assert config.llm.max_retries == 0
        assert json.loads(_marker(checkpoint).read_text())["execute_started"]
        assert not _result(checkpoint).exists()
        return result

    async def send(actual: Any, config: Any) -> str:
        assert actual is result
        assert json.loads(_result(checkpoint).read_text()) == asdict(result)
        assert result.bundle_id in _markdown(checkpoint).read_text()
        marker = json.loads(_marker(checkpoint).read_text())
        assert marker["execute_started"]
        assert marker["supplement_status"] == "dispatching"
        assert checkpoint.read_bytes() == original
        return "sent"

    with (
        patch("httpx.AsyncClient", return_value=client),
        patch("digest.irritator.evidence_stage.run_evidence_irritator", side_effect=run) as stage,
        patch("digest.post_delivery._send_supplement", side_effect=send) as sender,
    ):
        assert await execute_post_delivery(Path("config.yaml"), checkpoint) == 0
    stage.assert_awaited_once()
    sender.assert_awaited_once()
    record = json.loads(_marker(checkpoint).read_text())
    assert record["stage_status"] == "complete"
    assert record["supplement_status"] == "sent"
    assert record["finished_at"]
    assert checkpoint.read_bytes() == original
    with (
        patch("digest.irritator.evidence_stage.run_evidence_irritator", AsyncMock()) as stage,
        patch("digest.post_delivery._send_supplement", AsyncMock()) as sender,
    ):
        with pytest.raises(ValueError):
            await execute_post_delivery(Path("config.yaml"), checkpoint)
    stage.assert_not_called()
    sender.assert_not_called()


@pytest.mark.asyncio
async def test_unexpected_model_error_leaves_durable_incomplete_result(tmp_path: Path) -> None:
    checkpoint = tmp_path / "digests/day.review.json"
    payload = await _checkpoint(checkpoint)
    original = checkpoint.read_bytes()
    assert prepare_post_delivery(Path("config.yaml"), checkpoint) == _marker(checkpoint)
    with (
        patch("httpx.AsyncClient", return_value=_client_context()),
        patch("digest.irritator.evidence_stage.run_evidence_irritator",
              AsyncMock(side_effect=RuntimeError("private provider response"))) as stage,
        patch("digest.post_delivery._send_supplement", AsyncMock()) as sender,
    ):
        assert await execute_post_delivery(Path("config.yaml"), checkpoint) == 2
    stage.assert_awaited_once()
    sender.assert_not_called()
    result = json.loads(_result(checkpoint).read_text())
    assert result["status"] == "error"
    assert result["bundle_id"] == payload["evidence"]["bundle_id"]
    assert result["stage"] == "unexpected_failure"
    assert result["error"] == "RuntimeError"
    assert "incomplete" in _markdown(checkpoint).read_text().lower()
    assert "private provider response" not in _result(checkpoint).read_text()
    assert json.loads(_marker(checkpoint).read_text())["execute_started"]
    assert checkpoint.read_bytes() == original


@pytest.mark.asyncio
async def test_send_error_preserves_result_and_records_unknown_without_retry(tmp_path: Path) -> None:
    checkpoint = tmp_path / "digests/day.review.json"
    payload = await _checkpoint(checkpoint)
    original = checkpoint.read_bytes()
    assert prepare_post_delivery(Path("config.yaml"), checkpoint) == _marker(checkpoint)
    result = _stage_result(payload["evidence"]["bundle_id"])
    saved_result: list[bytes] = []

    async def failed_send(actual: Any, config: Any) -> str:
        saved_result.append(_result(checkpoint).read_bytes())
        assert json.loads(saved_result[0]) == asdict(result)
        assert _markdown(checkpoint).is_file()
        raise TimeoutError("Delivery may already have succeeded")

    with (
        patch("httpx.AsyncClient", return_value=_client_context()),
        patch("digest.irritator.evidence_stage.run_evidence_irritator", AsyncMock(return_value=result)) as stage,
        patch("digest.post_delivery._send_supplement", side_effect=failed_send) as sender,
    ):
        assert await execute_post_delivery(Path("config.yaml"), checkpoint) == 2
        with pytest.raises(ValueError):
            await execute_post_delivery(Path("config.yaml"), checkpoint)
    stage.assert_awaited_once()
    sender.assert_awaited_once()
    assert _result(checkpoint).read_bytes() == saved_result[0]
    marker = json.loads(_marker(checkpoint).read_text())
    assert marker["supplement_status"] == "unknown"
    assert marker["send_error"] == "TimeoutError"
    assert marker["finished_at"]
    assert checkpoint.read_bytes() == original


@pytest.mark.asyncio
@pytest.mark.parametrize("status,receipt,code", [
    ("empty", "sent", 0), ("incomplete", "sent", 2), ("error", "sent", 2),
    ("complete", "not_configured", 2),
])
async def test_stage_outcome_and_supplement_receipt_are_separate(
    status: Outcome, receipt: str, code: int, tmp_path: Path,
) -> None:
    checkpoint = tmp_path / "digests/day.review.json"
    payload = await _checkpoint(checkpoint)
    original = checkpoint.read_bytes()
    assert prepare_post_delivery(Path("config.yaml"), checkpoint) == _marker(checkpoint)
    result = _stage_result(payload["evidence"]["bundle_id"], status=status)
    with (
        patch("httpx.AsyncClient", return_value=_client_context()),
        patch("digest.irritator.evidence_stage.run_evidence_irritator", AsyncMock(return_value=result)),
        patch("digest.post_delivery._send_supplement", AsyncMock(return_value=receipt)) as sender,
    ):
        assert await execute_post_delivery(Path("config.yaml"), checkpoint) == code
    sender.assert_awaited_once()
    marker = json.loads(_marker(checkpoint).read_text())
    assert marker["stage_status"] == status
    assert marker["supplement_status"] == receipt
    assert json.loads(_result(checkpoint).read_text())["status"] == status
    assert checkpoint.read_bytes() == original


@pytest.mark.asyncio
@pytest.mark.parametrize("existing", ["json", "markdown"])
async def test_prepare_does_not_replace_an_orphaned_result(existing: str, tmp_path: Path) -> None:
    checkpoint = tmp_path / "digests/day.review.json"
    await _checkpoint(checkpoint)
    output = _result(checkpoint) if existing == "json" else _markdown(checkpoint)
    output.write_text("Existing result must not be overwritten", encoding="utf-8")
    assert prepare_post_delivery(Path("config.yaml"), checkpoint) is None
    assert output.read_text() == "Existing result must not be overwritten"
    assert not _marker(checkpoint).exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["disabled", "token", "chat"])
async def test_supplement_without_destination_never_contacts_telegram(
    missing: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = fixture_config()
    config.telegram.enabled = missing != "disabled"
    if missing != "token":
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    if missing != "chat":
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "same-primary-chat")
    with patch("httpx.AsyncClient", side_effect=AssertionError("No destination, no client")) as client:
        assert await _send_supplement(_stage_result("bundle"), config) == "not_configured"
    client.assert_not_called()


@pytest.mark.asyncio
async def test_supplement_uses_existing_primary_telegram_target_once(monkeypatch: pytest.MonkeyPatch) -> None:
    config = fixture_config()
    config.telegram.enabled = True
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "same-primary-chat")
    client = _client_context()
    response = httpx.Response(
        200, json={"ok": True, "result": {"message_id": 99}},
        request=httpx.Request("POST", "https://api.telegram.org/bottest-token/sendMessage"),
    )
    client.post = AsyncMock(return_value=response)
    with patch("httpx.AsyncClient", return_value=client):
        assert await _send_supplement(_stage_result("bundle", status="incomplete"), config) == "sent"
    client.post.assert_awaited_once()
    assert client.post.call_args.args == ("https://api.telegram.org/bottest-token/sendMessage",)
    payload = client.post.call_args.kwargs["json"]
    assert payload["chat_id"] == "same-primary-chat"
    assert payload["disable_notification"] is True
    assert "incomplete" in payload["text"]
    assert "Limited coverage" in payload["text"]


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["prepare", "execute"])
async def test_checkpoint_change_during_validation_is_rejected(phase: str, tmp_path: Path) -> None:
    checkpoint = tmp_path / "digests/day.review.json"
    await _checkpoint(checkpoint)
    if phase == "execute":
        assert prepare_post_delivery(Path("config.yaml"), checkpoint) == _marker(checkpoint)
    original = checkpoint.read_bytes()

    def changing_checkpoint(path: Path, config: Any) -> Any:
        loaded = load_review_checkpoint(path, config)
        path.write_bytes(original + b"\n")
        return loaded

    with (
        patch("digest.post_delivery.load_review_checkpoint", side_effect=changing_checkpoint),
        patch("digest.irritator.evidence_stage.run_evidence_irritator", AsyncMock()) as stage,
        patch("digest.post_delivery._send_supplement", AsyncMock()) as sender,
    ):
        with pytest.raises(ValueError, match="changed"):
            if phase == "prepare":
                prepare_post_delivery(Path("config.yaml"), checkpoint)
            else:
                await execute_post_delivery(Path("config.yaml"), checkpoint)
    stage.assert_not_called()
    sender.assert_not_called()
    assert not _result(checkpoint).exists()
    assert not _markdown(checkpoint).exists()
    if phase == "prepare":
        assert not _marker(checkpoint).exists()
    else:
        assert json.loads(_marker(checkpoint).read_text())["execute_started"] is None


@pytest.mark.asyncio
async def test_prepare_losing_marker_creation_race_preserves_winning_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkpoint = tmp_path / "digests/day.review.json"
    await _checkpoint(checkpoint)
    marker = _marker(checkpoint)
    output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    original_open = Path.open
    winning_record = '{"winning_attempt": true}\n'

    def competing_open(path: Path, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if path == marker and mode == "x":
            with original_open(path, "w", encoding="utf-8") as handle:
                handle.write(winning_record)
            raise FileExistsError("Another prepare process claimed the attempt")
        return original_open(path, mode, *args, **kwargs)

    with patch.object(Path, "open", new=competing_open):
        assert prepare_post_delivery(Path("config.yaml"), checkpoint) is None
    assert marker.read_text() == winning_record
    assert not output.exists()


@pytest.mark.asyncio
async def test_telegram_timeout_is_never_retried_within_supplement_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = fixture_config()
    config.telegram.enabled = True
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "same-primary-chat")
    client = _client_context()
    client.post = AsyncMock(side_effect=httpx.ReadTimeout("Server may already have accepted this message"))
    with (
        patch("httpx.AsyncClient", return_value=client),
        patch("asyncio.sleep", AsyncMock()),
    ):
        with pytest.raises(httpx.ReadTimeout):
            await _send_supplement(_stage_result("bundle"), config)
    client.post.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["http_timeout", "overall_deadline"])
async def test_second_chunk_timeout_preserves_archive_and_blocks_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    from tests.factories import make_ranked_signal

    checkpoint = tmp_path / "digests/day.review.json"
    payload = await _checkpoint(checkpoint)
    prepare_post_delivery(Path("config.yaml"), checkpoint)
    result = _stage_result(payload["evidence"]["bundle_id"])
    result.ranked_signals = [make_ranked_signal(reasoning="Long evidence " * 800 + "LATE CONDITION")]
    config = fixture_config()
    config.telegram.enabled = True
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "same-primary-chat")
    client = _client_context()
    calls = 0

    async def post(*args: Any, **kwargs: Any) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(200, json={"ok": True}, request=httpx.Request("POST", "https://example.com"))
        if failure == "overall_deadline":
            await asyncio.Event().wait()
        raise httpx.ReadTimeout("Uncertain second chunk")

    client.post = AsyncMock(side_effect=post)
    monkeypatch.setattr("digest.post_delivery._SUPPLEMENT_DISPATCH_SECONDS", 0.02)
    with (
        patch("digest.post_delivery.load_config", return_value=config),
        patch("httpx.AsyncClient", return_value=client),
        patch("digest.irritator.evidence_stage.run_evidence_irritator", AsyncMock(return_value=result)) as stage,
    ):
        assert await execute_post_delivery(Path("config.yaml"), checkpoint) == 2
        with pytest.raises(ValueError):
            await execute_post_delivery(Path("config.yaml"), checkpoint)
    assert client.post.await_count == 2
    stage.assert_awaited_once()
    assert "LATE CONDITION" in _markdown(checkpoint).read_text()
    assert json.loads(_result(checkpoint).read_text()) == asdict(result)
    assert json.loads(_marker(checkpoint).read_text())["supplement_status"] == "unknown"


@pytest.mark.asyncio
async def test_optional_presentation_archives_canonical_before_translation_and_keeps_dispatch_reserve(
    tmp_path: Path,
) -> None:
    import time
    from dataclasses import replace

    from digest.config import TranslationConfig
    from digest.translation import TranslationResult

    checkpoint = tmp_path / "digests/day.review.json"
    payload = await _checkpoint(checkpoint)
    assert prepare_post_delivery(Path("config.yaml"), checkpoint) == _marker(checkpoint)
    canonical = _stage_result(payload["evidence"]["bundle_id"])
    cfg = fixture_config()
    cfg.translation = TranslationConfig(enabled=True, provider="groq", model="test-model")
    presented = replace(canonical, status="empty")
    started = time.monotonic()

    async def translate(actual, actual_config, cache, *, deadline):
        assert actual is canonical
        assert json.loads(_result(checkpoint).read_text()) == asdict(canonical)
        assert actual_config.translation.timeout_seconds == 90
        assert started + 224 <= deadline <= time.monotonic() + 225
        return presented, TranslationResult({}, "translated")

    async def send(actual, actual_config, *, notice):
        assert actual is presented and actual_config.radar.language == "ru"
        assert "not independently verified" in notice
        text = _markdown(checkpoint).read_text()
        assert "Status: empty" in text and notice in text
        assert json.loads(text.split("## Stage diagnostics\n", 1)[1]) == asdict(canonical)
        return "sent"

    with (
        patch("digest.post_delivery._config", return_value=cfg),
        patch("httpx.AsyncClient", return_value=_client_context()),
        patch("digest.irritator.evidence_stage.run_evidence_irritator", AsyncMock(return_value=canonical)),
        patch("digest.translation.translate_supplement_presentation", side_effect=translate),
        patch("digest.post_delivery._send_supplement", side_effect=send),
    ):
        assert await execute_post_delivery(Path("config.yaml"), checkpoint) == 0
    assert json.loads(_result(checkpoint).read_text()) == asdict(canonical)
    assert json.loads(_marker(checkpoint).read_text())["translation_status"] == "translated"

@pytest.mark.asyncio
async def test_compact_mode_archives_actual_optional_outcome_without_telegram(tmp_path: Path) -> None:
    checkpoint = tmp_path / "digests/day.review.json"
    payload = await _checkpoint(checkpoint)
    config = fixture_config()
    config.telegram.delivery_mode = "compact"
    stage = _stage_result(payload["evidence"]["bundle_id"], status="incomplete")
    with (
        patch("digest.post_delivery._config", return_value=config),
        patch("httpx.AsyncClient", return_value=_client_context()),
        patch("digest.irritator.evidence_stage.run_evidence_irritator", AsyncMock(return_value=stage)) as process,
        patch("digest.post_delivery._send_supplement", AsyncMock()) as send,
    ):
        prepare_post_delivery(Path("config.yaml"), checkpoint)
        assert await execute_post_delivery(Path("config.yaml"), checkpoint) == 2
    process.assert_awaited_once()
    send.assert_not_called()
    assert json.loads(_result(checkpoint).read_text())["status"] == "incomplete"
    record = json.loads(_marker(checkpoint).read_text())
    assert record["supplement_status"] == "archive_only" and record["stage_status"] == "incomplete"
    assert _markdown(checkpoint).exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [False, True])
async def test_required_source_provenance_never_falls_back_to_rss(tmp_path: Path, invalid: bool) -> None:
    checkpoint = tmp_path / "digests/day.review.json"
    payload = await _checkpoint(checkpoint)
    payload["full_source_required"] = True
    if invalid:
        payload["full_source_evidence"] = {"invalid": "incomplete checkpoint"}
    checkpoint.write_text(json.dumps(payload))
    assert prepare_post_delivery(Path("config.yaml"), checkpoint) == _marker(checkpoint)
    with (patch("httpx.AsyncClient", return_value=_client_context()),
          patch("digest.llm.complete", AsyncMock(side_effect=AssertionError("No RSS fallback"))) as model,
          patch("digest.post_delivery._send_supplement", AsyncMock(return_value="sent"))):
        assert await execute_post_delivery(Path("config.yaml"), checkpoint) == 2
    model.assert_not_called()
    result = json.loads(_result(checkpoint).read_text())
    assert result["status"] == "incomplete"
    assert result["diagnostics"][0]["error"] == "FullSourceEvidencePending"
    assert "selected literal full-source passages" in _markdown(checkpoint).read_text()
    marker = json.loads(_marker(checkpoint).read_text())
    assert marker["full_source_required"] is True
    if invalid:
        assert marker["full_source_error"] == "ValueError"
