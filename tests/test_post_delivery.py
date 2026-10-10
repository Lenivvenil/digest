"""Durable post-delivery tests: immutable archives, one attempt, no primary replay."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from digest.adapters.models.execution import ModelExecution
from digest.application.review import run_blind_review
from digest.irritator.evidence_stage import EvidenceIrritatorResult, Outcome
from digest.post_delivery import _send_supplement, execute_post_delivery, prepare_post_delivery
from digest.review_checkpoint import load_review_checkpoint
from scripts.review_fixture import fixture_articles, fixture_config, fixture_response


async def confirmed_checkpoint(monkeypatch: pytest.MonkeyPatch, *, closing: bool = False) -> Path:
    """Real immutable publication/source records, with synthetic positive receipts."""
    from dataclasses import replace

    from digest.adapters.storage import edition as storage
    from digest.application.prepared_delivery import claim_edition
    from digest.domain.delivery.edition import ChunkReceipt, Receipts
    from digest.domain.editorial.summaries import ArticleSummary
    from digest.edition_runtime import present_preparation
    from digest.preparation import PreparationSnapshot, persist_accepted_preparation
    from tests.test_edition_runtime import bound_snapshot

    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    config = fixture_config()
    config.telegram.delivery_mode = "compact"
    config.obsidian.enabled = True
    config.review.review_led_only = True
    card = ArticleSummary("Delivered source", "https://example.com/origin", "Source", "Tech", "Raw evidence.")
    closer = (
        replace(
            card,
            title="Community source",
            link="https://example.com/community",
            source="NHS England",
            category="Health",
        )
        if closing
        else None
    )
    snapshot = bound_snapshot(PreparationSnapshot([card], [], "Notice", None, 1, 1, ["Source"]), closer)
    with patch(
        "digest.application.presentation.publication_presentation",
        AsyncMock(return_value=("Notice", [replace(card, summary="Delivered translated claim")], [])),
    ):
        accepted = persist_accepted_preparation(snapshot, cache_dir=".cache")
        result = await present_preparation(accepted, config, execution=ModelExecution())
    _, claim_sha = claim_edition(result.ready_sha256)
    data = json.loads(Path(".cache", storage.READY_FILE).read_text())
    receipts = Receipts(
        1,
        result.ready_sha256,
        claim_sha,
        "confirmed",
        len(data["payloads"]),
        [ChunkReceipt(index, index + 1, data["owner_sha256"]) for index in range(len(data["payloads"]))],
        True,
    )
    storage.write_record(Path(".cache", storage.RECEIPTS_FILE), asdict(receipts))
    return Path(result.review_checkpoint)


def accepted_result(
    bundle: Any,
    origin: Any,
    *,
    long: bool = False,
    source_url: str = "https://external.example/qualification",
) -> EvidenceIrritatorResult:
    from digest.domain.investigation.delivered import DeliveredQuote
    from digest.domain.investigation.queries import SearchQuery
    from digest.irritator.evidence_stage import (
        DeliveredNarrative,
        _admit_ranking,
        _parse_rankings,
        _ranking_signal_payload,
    )
    from tests.factories import make_signal

    card = origin.cards[0]
    item = next(item for item in bundle.items if item.evidence_id == card.card_id)
    narrative = DeliveredNarrative(
        "Source states a limited result.",
        item.category,
        [],
        "Check independent conditions.",
        [item.evidence_id],
        {item.evidence_id: item.excerpt},
        target_card_id=card.card_id,
        delivered_quote=DeliveredQuote("summary", card.canonical.summary),
    )
    signal = make_signal(title="Independent qualification", url=source_url)
    identity = hashlib.sha256(json.dumps(asdict(signal), ensure_ascii=True, sort_keys=True).encode()).hexdigest()
    admission = _admit_ranking([signal], 5, 3, {identity: {0}})
    ranking = _parse_rankings(
        json.dumps(
            {
                "rankings": [
                    {
                        "url": signal.url,
                        "score": 8,
                        "relation": "complicates",
                        "reasoning": ("Material condition " * 300 if long else "One material condition.")
                        + " LATE QUALIFIER",
                        "quote_id": _ranking_signal_payload(signal)["title"][0]["id"],
                    }
                ],
                "limitations": [],
            }
        ),
        admission,
        narrative,
    )
    return EvidenceIrritatorResult(
        1,
        bundle.bundle_id,
        "incomplete",
        narratives=[narrative],
        queries=[SearchQuery("Source result", "Find independent conditions.")],
        ranked_signals=ranking.ranked_signals,
        ranking_audit=ranking.audit,
        limitations=["One external source was unavailable."],
    )


async def pending_supplement(
    monkeypatch: pytest.MonkeyPatch,
    *,
    long: bool = False,
    source_url: str = "https://external.example/qualification",
) -> Any:
    from datetime import date, timedelta

    from digest.application.supplement import pending_fragment, prepare_fragment
    from digest.post_delivery import _delivered_input

    checkpoint = await confirmed_checkpoint(monkeypatch)
    config = fixture_config()
    config.telegram.delivery_mode = "compact"
    with patch("digest.post_delivery._config", return_value=config):
        marker = prepare_post_delivery(Path("config.yaml"), checkpoint)
    assert marker is not None
    record = json.loads(marker.read_text())
    bundle, _ = load_review_checkpoint(checkpoint, config)
    origin = _delivered_input(record, bundle)
    assert origin is not None
    result = accepted_result(bundle, origin, long=long, source_url=source_url)
    _result(checkpoint).write_text(json.dumps(asdict(result)))
    prepare_fragment(
        record,
        _result(checkpoint).resolve(),
        result,
        result,
        origin,
        language="en",
        notice="",
        bundle=bundle,
        source=None,
    )
    marker.write_text(json.dumps(record))
    pending, status = pending_fragment("digests", date.fromisoformat(origin.publication_day) + timedelta(days=1))
    assert pending is not None and status == "eligible"
    return pending


@pytest.mark.asyncio
async def test_future_destination_does_not_expire_a_fragment_before_its_actual_utc_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import datetime, timedelta, timezone

    from digest.application.supplement import pending_fragment

    pending = await pending_supplement(monkeypatch)
    day = datetime.fromisoformat(pending.fragment.origin.publication_day).replace(tzinfo=timezone.utc)
    original = Path(pending.attempt).read_bytes()
    assert pending_fragment("digests", (day + timedelta(days=4)).date(), now=day + timedelta(days=1)) == (
        pending,
        "ineligible_destination",
    )
    assert Path(pending.attempt).read_bytes() == original
    assert pending_fragment("digests", (day + timedelta(days=1)).date(), now=day + timedelta(days=1)) == (
        pending,
        "eligible",
    )


@pytest.mark.asyncio
async def test_projection_binds_checked_canonical_result_and_uses_faithful_localized_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dataclasses import replace

    from digest.application.supplement import prepare_fragment
    from digest.presentation.supplement import fragment_text

    pending = await pending_supplement(monkeypatch)
    bundle, _ = load_review_checkpoint(Path(pending.fragment.checkpoint), fixture_config())
    result = accepted_result(bundle, pending.fragment.origin)
    presented = replace(result, narratives=[replace(result.narratives[0], claim="Проверяемое утверждение")])
    text = fragment_text(
        result,
        presented,
        pending.fragment.origin,
        language="ru",
        notice="",
        investigated_at=pending.fragment.investigated_at,
    )
    assert "Проверяемое утверждение: Проверяемое утверждение" in text
    assert "Каноническое резюме: Raw evidence." in text
    assert "Внешнее свидетельство: Independent qualification" in text
    assert "Исходный выпуск:" in text and "Охват и ограничения" in text
    original = Path(pending.projection).read_bytes()
    Path(pending.fragment.result).write_text('{"status": "different stored result"}')
    with pytest.raises(ValueError, match="Stored canonical investigation differs"):
        prepare_fragment(
            json.loads(Path(pending.attempt).read_text()),
            Path(pending.fragment.result),
            result,
            presented,
            pending.fragment.origin,
            language="ru",
            notice="",
            bundle=bundle,
            source=None,
        )
    assert Path(pending.projection).read_bytes() == original


@pytest.mark.asyncio
async def test_compact_execution_retains_accepted_incomplete_fragment_and_occupied_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkpoint = await confirmed_checkpoint(monkeypatch)
    checkpoint_sha256 = hashlib.sha256(checkpoint.read_bytes()).hexdigest()
    config = fixture_config()
    config.telegram.delivery_mode = "compact"

    async def stage(bundle: Any, config: Any, client: Any, *, delivered: Any, **kwargs: Any) -> Any:
        assert len(delivered.cards) == 1
        assert delivered.checkpoint_sha256 == checkpoint_sha256
        assert delivered.canonical_sha256 != delivered.presentation_sha256
        assert delivered.cards[0].canonical.summary == "Raw evidence."
        assert delivered.cards[0].presentation.summary == "Delivered translated claim"
        return accepted_result(bundle, delivered)

    with (
        patch("digest.post_delivery._config", return_value=config),
        patch("httpx.AsyncClient", return_value=_client_context()),
        patch("digest.irritator.evidence_stage.run_evidence_irritator", side_effect=stage) as run,
    ):
        marker = prepare_post_delivery(Path("config.yaml"), checkpoint)
        assert marker is not None
        prepared = json.loads(marker.read_text())
        assert prepared["schema_version"] == 3
        assert len(prepared["delivered"]["cards"]) == 1
        assert await execute_post_delivery(Path("config.yaml"), checkpoint, execution=ModelExecution()) == 2
        occupied = checkpoint.with_name("next.review.json")
        occupied.write_bytes(checkpoint.read_bytes())
        assert prepare_post_delivery(Path("config.yaml"), occupied) is None
    run.assert_awaited_once()
    record = json.loads(_marker(checkpoint).read_text())
    assert record["supplement_status"] == "pending" and "status" not in record["fragment"]
    projection = json.loads(Path(record["fragment"]["projection"]).read_text())
    assert "LATE QUALIFIER" in projection["text"]
    assert "One external source was unavailable." in projection["text"]
    assert "published:" in projection["text"] and "Investigation:" in projection["text"]


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["non_counter", "not_returned", "wrong_quote", "self_source"])
async def test_fragment_admission_does_not_promote_invalid_rankings(
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    from dataclasses import replace

    from digest.irritator.evidence_stage import eligible_delivered_result
    from digest.post_delivery import _delivered_input

    checkpoint = await confirmed_checkpoint(monkeypatch)
    config = fixture_config()
    config.telegram.delivery_mode = "compact"
    with patch("digest.post_delivery._config", return_value=config):
        marker = prepare_post_delivery(Path("config.yaml"), checkpoint)
    assert marker is not None
    bundle, _ = load_review_checkpoint(checkpoint, config)
    origin = _delivered_input(json.loads(marker.read_text()), bundle)
    assert origin is not None
    result = accepted_result(bundle, origin)
    assert result.ranking_audit is not None
    candidate = result.ranking_audit.candidates[0]
    assert candidate.decision is not None
    if fault == "non_counter":
        candidate.decision = replace(candidate.decision, relation="supports")
    elif fault == "not_returned":
        candidate.disposition = "not_returned"
    elif fault == "wrong_quote":
        candidate.decision = replace(candidate.decision, quote="Unreturned quotation")
    else:
        result.ranked_signals[0].signal.url = origin.cards[0].canonical.link
    assert not eligible_delivered_result(result, origin, bundle, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("closing", [True])
async def test_compact_prepare_freezes_actual_delivered_origin_and_source_occurrences(
    monkeypatch: pytest.MonkeyPatch,
    closing: bool,
) -> None:
    from dataclasses import replace

    from digest.post_delivery import _delivered_input
    from digest.translation import translate_publication_with_closing

    async def presented_closing(*args: Any, **kwargs: Any) -> Any:
        text, cards, ranked, closer = await translate_publication_with_closing(*args, **kwargs)
        assert len(cards) == 1
        return text, [replace(cards[0], summary="Delivered translated claim")], ranked, closer

    with patch("digest.translation.translate_publication_with_closing", side_effect=presented_closing) as present:
        checkpoint = await confirmed_checkpoint(monkeypatch, closing=closing)
    present.assert_awaited_once()
    config = fixture_config()
    config.telegram.delivery_mode = "compact"
    with patch("digest.post_delivery._config", return_value=config):
        marker = prepare_post_delivery(Path("config.yaml"), checkpoint)
    assert marker is not None
    record = json.loads(marker.read_text())
    bundle, _ = load_review_checkpoint(checkpoint, config)
    origin = _delivered_input(record, bundle)
    assert record["schema_version"] == 3 and origin is not None
    assert len(origin.cards) == 1 + int(closing)
    assert origin.checkpoint_sha256 == hashlib.sha256(checkpoint.read_bytes()).hexdigest()
    assert origin.canonical_sha256 != origin.presentation_sha256
    assert origin.cards[0].canonical.summary == "Raw evidence."
    assert origin.cards[0].presentation.summary == "Delivered translated claim"
    if closing:
        assert origin.cards[-1].canonical.title == "Community source"
        assert "NHS England RSS feeds" in origin.cards[-1].presentation.summary


@pytest.mark.asyncio
async def test_omitted_closer_is_not_a_delivered_investigation_target(monkeypatch: pytest.MonkeyPatch) -> None:
    with patch("digest.closing.attribute_closing_card", return_value=None):
        checkpoint = await confirmed_checkpoint(monkeypatch, closing=True)
    config = fixture_config()
    config.telegram.delivery_mode = "compact"
    with patch("digest.post_delivery._config", return_value=config):
        marker = prepare_post_delivery(Path("config.yaml"), checkpoint)
    assert marker is not None
    origin = json.loads(marker.read_text())["delivered"]
    assert [card["canonical"]["title"] for card in origin["cards"]] == ["Delivered source"]


@pytest.mark.asyncio
async def test_origin_receipt_change_after_validation_cannot_freeze_unvalidated_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from digest.adapters.storage import edition as storage

    checkpoint = await confirmed_checkpoint(monkeypatch)
    config = fixture_config()
    config.telegram.delivery_mode = "compact"
    original_load = storage.load_receipts

    def changed_receipts(*args: Any, **kwargs: Any) -> Any:
        result = original_load(*args, **kwargs)
        path = Path(".cache", storage.RECEIPTS_FILE)
        changed = json.loads(path.read_text())
        changed["applied"] = False
        storage.write_record(path, changed)
        return result

    with (
        patch("digest.post_delivery._config", return_value=config),
        patch("digest.application.investigation_origin.storage.load_receipts", side_effect=changed_receipts),
    ):
        with pytest.raises(ValueError, match="Origin receipts changed"):
            prepare_post_delivery(Path("config.yaml"), checkpoint)
    assert not _marker(checkpoint).exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["unapplied", "unknown", "wrong_owner", "changed_checkpoint", "old_attempt"])
async def test_compact_origin_failures_stop_before_attempt_or_model_work(
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    from digest.adapters.storage import edition as storage

    checkpoint = await confirmed_checkpoint(monkeypatch)
    config = fixture_config()
    config.telegram.delivery_mode = "compact"
    receipts_path = Path(".cache", storage.RECEIPTS_FILE)
    receipts = json.loads(receipts_path.read_text())
    if fault == "unapplied":
        receipts["applied"] = False
    elif fault == "unknown":
        receipts["state"] = "unknown"
    elif fault == "wrong_owner":
        receipts["confirmed"][0]["owner_sha256"] = "f" * 64
    elif fault == "changed_checkpoint":
        checkpoint.write_bytes(checkpoint.read_bytes() + b"\n")
    else:
        with patch("digest.post_delivery._config", return_value=fixture_config()):
            prepare_post_delivery(Path("config.yaml"), checkpoint)
    storage.write_record(receipts_path, receipts)
    with (
        patch("digest.post_delivery._config", return_value=config),
        patch("digest.irritator.evidence_stage.run_evidence_irritator", AsyncMock()) as model,
    ):
        if fault == "old_attempt":
            original = _marker(checkpoint).read_bytes()
            with pytest.raises(ValueError, match="search policy"):
                await execute_post_delivery(Path("config.yaml"), checkpoint, execution=ModelExecution())
            assert _marker(checkpoint).read_bytes() == original
        else:
            with pytest.raises(ValueError):
                prepare_post_delivery(Path("config.yaml"), checkpoint)
            assert not _marker(checkpoint).exists()
    model.assert_not_called()


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
    execution = ModelExecution()
    with patch("digest.application.review.complete", side_effect=fixture_response):
        report = await run_blind_review(fixture_articles(), fixture_config(), execution=execution)
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
async def test_prepare_is_exclusive_and_never_replaces_an_existing_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkpoint = tmp_path / "digests/day.review.json"
    await _checkpoint(checkpoint)
    output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    assert not output.exists()
    assert prepare_post_delivery(Path("config.yaml"), checkpoint) == _marker(checkpoint)
    assert output.read_text() == "checkpoint=digests/day.review.json\nmarker=digests/day.post-attempt.json\n"
    first_marker = _marker(checkpoint).read_bytes()
    with patch("digest.irritator.evidence_stage.run_evidence_irritator", AsyncMock()) as run:
        assert prepare_post_delivery(Path("config.yaml"), checkpoint) is None
    run.assert_not_called()
    assert _marker(checkpoint).read_bytes() == first_marker


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["unstarted", "started", "completed"])
async def test_schema_one_attempts_and_archives_are_held_unchanged(state: str, tmp_path: Path) -> None:
    checkpoint = tmp_path / "digests/day.review.json"
    await _checkpoint(checkpoint)
    assert prepare_post_delivery(Path("config.yaml"), checkpoint) == _marker(checkpoint)
    record = json.loads(_marker(checkpoint).read_text())
    record["schema_version"] = 1
    del record["search_policy"]
    if state != "unstarted":
        record["execute_started"] = "2026-09-30T12:00:00+00:00"
    if state == "completed":
        _result(checkpoint).write_text('{"schema_version": 1, "status": "empty"}\n')
        _markdown(checkpoint).write_text("Historical archive\n")
    _marker(checkpoint).write_text(json.dumps(record) + "\n")
    originals = {
        path: path.read_bytes()
        for path in (checkpoint, _marker(checkpoint), _result(checkpoint), _markdown(checkpoint))
        if path.exists()
    }
    with (
        patch("digest.irritator.evidence_stage.run_evidence_irritator", AsyncMock()) as stage,
        patch("digest.post_delivery.storage.save_attempt") as save,
        patch("digest.post_delivery.datetime") as timestamp,
    ):
        assert prepare_post_delivery(Path("config.yaml"), checkpoint) is None
        with pytest.raises(ValueError, match="search policy|result already exists"):
            await execute_post_delivery(Path("config.yaml"), checkpoint, execution=ModelExecution())
    stage.assert_not_called()
    save.assert_not_called()
    timestamp.now.assert_not_called()
    assert {path: path.read_bytes() for path in originals} == originals
    assert _result(checkpoint).exists() is (state == "completed")
    assert _markdown(checkpoint).exists() is (state == "completed")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        "unknown_schema",
        "malformed_marker",
        "missing_policy",
        "unknown_policy",
        "reordered_sources",
        "duplicate_sources",
        "extra_field",
        "noninteger_cap",
        "configured_sources",
        "configured_cap",
    ],
)
async def test_policy_mismatch_is_held_before_attempt_effects(change: str, tmp_path: Path) -> None:
    config = fixture_config()
    config.irritator.sources = ["devto", "hackernews", "arxiv", "devto", "lobsters"]
    config.irritator.queries_per_narrative = 1
    checkpoint = tmp_path / "digests/day.review.json"
    await _checkpoint(checkpoint)
    with patch("digest.post_delivery.load_config", side_effect=lambda _: deepcopy(config)):
        assert prepare_post_delivery(Path("config.yaml"), checkpoint) == _marker(checkpoint)
        record = json.loads(_marker(checkpoint).read_text())
        if change == "unknown_schema":
            record["schema_version"] = 3
        elif change == "malformed_marker":
            record = []
        elif change == "missing_policy":
            del record["search_policy"]
        elif change == "unknown_policy":
            record["search_policy"]["id"] = "another-policy"
        elif change == "reordered_sources":
            record["search_policy"]["sources"].reverse()
        elif change == "duplicate_sources":
            record["search_policy"]["sources"].append("devto")
        elif change == "extra_field":
            record["search_policy"]["additional_source"] = "lobsters"
        elif change == "noninteger_cap":
            record["search_policy"]["max_queries"] = True
        elif change == "configured_sources":
            config.irritator.sources = ["hackernews", "arxiv"]
        else:
            config.irritator.queries_per_narrative = 2
        _marker(checkpoint).write_text(json.dumps(record) + "\n")
        original = _marker(checkpoint).read_bytes()
        with (
            patch("digest.irritator.evidence_stage.run_evidence_irritator", AsyncMock()) as stage,
            patch("digest.post_delivery.storage.save_attempt") as save,
            patch("digest.post_delivery.datetime") as timestamp,
            patch("digest.post_delivery._send_supplement", AsyncMock()) as send,
        ):
            with pytest.raises(ValueError, match="search policy.*Preserve existing attempt artifacts"):
                await execute_post_delivery(Path("config.yaml"), checkpoint, execution=ModelExecution())
        stage.assert_not_called()
        save.assert_not_called()
        send.assert_not_called()
        timestamp.now.assert_not_called()
        assert _marker(checkpoint).read_bytes() == original
        assert not _result(checkpoint).exists() and not _markdown(checkpoint).exists()


@pytest.mark.asyncio
async def test_initial_marker_is_fsynced_with_its_newline_before_workflow_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkpoint = tmp_path / "digests/day.review.json"
    payload = await _checkpoint(checkpoint)
    original = checkpoint.read_bytes()
    output = tmp_path / "github-output"
    output.write_text("previous=value\n", encoding="utf-8")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    fsync = os.fsync

    def sync_marker(descriptor: int) -> None:
        marker_bytes = _marker(checkpoint).read_bytes()
        record = json.loads(marker_bytes)
        assert marker_bytes == (json.dumps(record, indent=2) + "\n").encode("utf-8")
        assert os.fstat(descriptor).st_ino == _marker(checkpoint).stat().st_ino
        assert output.read_text() == "previous=value\n"
        fsync(descriptor)

    with (
        patch("digest.adapters.storage.post_delivery.os.fsync", side_effect=sync_marker) as sync,
        patch("digest.irritator.evidence_stage.run_evidence_irritator", AsyncMock()) as run,
    ):
        assert prepare_post_delivery(Path("config.yaml"), checkpoint) == _marker(checkpoint)
    sync.assert_called_once()
    run.assert_not_called()
    record = json.loads(_marker(checkpoint).read_text())
    assert record["checkpoint"] == "digests/day.review.json"
    assert record["checkpoint_sha256"] == hashlib.sha256(original).hexdigest()
    assert record["bundle_id"] == payload["evidence"]["bundle_id"]
    assert record["execute_started"] is None
    assert record["schema_version"] == 2
    assert record["search_policy"] == {
        "id": "bounded-hn-arxiv-devto-v1",
        "sources": ["hackernews", "arxiv", "devto"],
        "max_queries": 3,
    }
    assert checkpoint.read_bytes() == original
    assert not _result(checkpoint).exists()
    assert not _markdown(checkpoint).exists()
    assert output.read_text() == (
        "previous=value\ncheckpoint=digests/day.review.json\nmarker=digests/day.post-attempt.json\n"
    )


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
@pytest.mark.parametrize(
    "kind",
    [
        "missing",
        "changed_bytes",
        "changed_bundle",
        "changed_checkpoint",
        "already_started",
        "symlink",
    ],
)
async def test_execute_requires_matching_unused_marker_before_model_or_network_work(kind: str, tmp_path: Path) -> None:
    execution = ModelExecution()
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
            await execute_post_delivery(Path("config.yaml"), checkpoint, execution=execution)
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
    execution = ModelExecution()
    checkpoint = tmp_path / "digests/day.review.json"
    payload = await _checkpoint(checkpoint)
    original = checkpoint.read_bytes()
    assert prepare_post_delivery(Path("config.yaml"), checkpoint) == _marker(checkpoint)
    result = _stage_result(payload["evidence"]["bundle_id"])
    client = _client_context()
    equivalent_config = fixture_config()
    equivalent_config.irritator.sources = ["devto", "arxiv", "hackernews", "devto", "lobsters"]
    equivalent_config.irritator.queries_per_narrative = 20

    async def run(
        bundle: Any,
        config: Any,
        actual_client: Any,
        *,
        execution: ModelExecution,
    ) -> EvidenceIrritatorResult:
        assert asdict(bundle) == payload["evidence"]
        assert actual_client is client
        assert config.llm.max_retries == 0
        marker_bytes = _marker(checkpoint).read_bytes()
        record = json.loads(marker_bytes)
        assert record["execute_started"]
        assert record["schema_version"] == 2
        assert record["search_policy"] == {
            "id": "bounded-hn-arxiv-devto-v1",
            "sources": ["hackernews", "arxiv", "devto"],
            "max_queries": 3,
        }
        assert marker_bytes == json.dumps(record, indent=2).encode("utf-8")
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
        patch("digest.post_delivery.load_config", return_value=equivalent_config),
        patch("httpx.AsyncClient", return_value=client),
        patch("digest.irritator.evidence_stage.run_evidence_irritator", side_effect=run) as stage,
        patch("digest.post_delivery._send_supplement", side_effect=send) as sender,
    ):
        assert await execute_post_delivery(Path("config.yaml"), checkpoint, execution=execution) == 0
    stage.assert_awaited_once()
    sender.assert_awaited_once()
    record = json.loads(_marker(checkpoint).read_text())
    assert record["stage_status"] == "complete"
    assert record["supplement_status"] == "sent"
    assert record["finished_at"]
    assert _marker(checkpoint).read_bytes() == json.dumps(record, indent=2).encode("utf-8")
    assert _result(checkpoint).read_bytes() == json.dumps(asdict(result), indent=2).encode("utf-8")
    assert checkpoint.read_bytes() == original
    with (
        patch("digest.irritator.evidence_stage.run_evidence_irritator", AsyncMock()) as stage,
        patch("digest.post_delivery._send_supplement", AsyncMock()) as sender,
    ):
        with pytest.raises(ValueError):
            await execute_post_delivery(Path("config.yaml"), checkpoint, execution=execution)
    stage.assert_not_called()
    sender.assert_not_called()


@pytest.mark.asyncio
async def test_unexpected_model_error_leaves_durable_incomplete_result(tmp_path: Path) -> None:
    execution = ModelExecution()
    checkpoint = tmp_path / "digests/day.review.json"
    payload = await _checkpoint(checkpoint)
    original = checkpoint.read_bytes()
    assert prepare_post_delivery(Path("config.yaml"), checkpoint) == _marker(checkpoint)
    with (
        patch("httpx.AsyncClient", return_value=_client_context()),
        patch(
            "digest.irritator.evidence_stage.run_evidence_irritator",
            AsyncMock(side_effect=RuntimeError("private provider response")),
        ) as stage,
        patch("digest.post_delivery._send_supplement", AsyncMock()) as sender,
    ):
        assert await execute_post_delivery(Path("config.yaml"), checkpoint, execution=execution) == 2
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
    execution = ModelExecution()
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
        assert await execute_post_delivery(Path("config.yaml"), checkpoint, execution=execution) == 2
        with pytest.raises(ValueError):
            await execute_post_delivery(Path("config.yaml"), checkpoint, execution=execution)
    stage.assert_awaited_once()
    sender.assert_awaited_once()
    assert _result(checkpoint).read_bytes() == saved_result[0]
    marker = json.loads(_marker(checkpoint).read_text())
    assert marker["supplement_status"] == "unknown"
    assert marker["send_error"] == "TimeoutError"
    assert marker["finished_at"]
    assert checkpoint.read_bytes() == original


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,receipt,code",
    [
        ("empty", "sent", 0),
        ("incomplete", "sent", 2),
        ("error", "sent", 2),
        ("complete", "not_configured", 2),
    ],
)
async def test_stage_outcome_and_supplement_receipt_are_separate(
    status: Outcome,
    receipt: str,
    code: int,
    tmp_path: Path,
) -> None:
    execution = ModelExecution()
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
        assert await execute_post_delivery(Path("config.yaml"), checkpoint, execution=execution) == code
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
    missing: str,
    monkeypatch: pytest.MonkeyPatch,
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
@pytest.mark.parametrize("phase", ["prepare", "execute"])
async def test_checkpoint_change_during_validation_is_rejected(phase: str, tmp_path: Path) -> None:
    execution = ModelExecution()
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
                await execute_post_delivery(Path("config.yaml"), checkpoint, execution=execution)
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
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    execution = ModelExecution()
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
    monkeypatch.setattr("digest.adapters.telegram.delivery._POST_DELIVERY_DISPATCH_SECONDS", 0.02)
    with (
        patch("digest.post_delivery.load_config", return_value=config),
        patch("httpx.AsyncClient", return_value=client),
        patch("digest.irritator.evidence_stage.run_evidence_irritator", AsyncMock(return_value=result)) as stage,
    ):
        assert await execute_post_delivery(Path("config.yaml"), checkpoint, execution=execution) == 2
        with pytest.raises(ValueError):
            await execute_post_delivery(Path("config.yaml"), checkpoint, execution=execution)
    assert client.post.await_count == 2
    stage.assert_awaited_once()
    assert "LATE CONDITION" in _markdown(checkpoint).read_text()
    assert json.loads(_result(checkpoint).read_text()) == asdict(result)
    assert json.loads(_marker(checkpoint).read_text())["supplement_status"] == "unknown"


@pytest.mark.asyncio
async def test_optional_presentation_archives_canonical_before_translation_and_keeps_dispatch_reserve(
    tmp_path: Path,
) -> None:
    execution = ModelExecution()
    import time
    from dataclasses import replace

    from digest.config import TranslationConfig
    from digest.translation import TranslationResult

    checkpoint = tmp_path / "digests/day.review.json"
    payload = await _checkpoint(checkpoint)
    assert prepare_post_delivery(Path("config.yaml"), checkpoint) == _marker(checkpoint)
    canonical = _stage_result(payload["evidence"]["bundle_id"])
    canonical.limitations = ["```\n![diagnostic](https://example.invalid/pixel)\r\n```\u2028```\u2029```"]
    cfg = fixture_config()
    cfg.translation = TranslationConfig(enabled=True, provider="groq", model="test-model")
    presented = replace(canonical, status="empty")
    started = time.monotonic()

    async def translate(actual, actual_config, cache, *, execution, deadline):
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
        expected_diagnostics = asdict(canonical)
        expected_diagnostics.pop("ranking_audit")
        serialized = json.dumps(expected_diagnostics, ensure_ascii=False, indent=2)
        block = text.split("## Stage diagnostics\n\n", 1)[1]
        assert block == "```json\n" + serialized + "\n```\n"
        assert not any(line.strip().startswith("```") for line in serialized.split("\n"))
        assert json.loads(block.removeprefix("```json\n").removesuffix("\n```\n")) == expected_diagnostics
        return "sent"

    with (
        patch("digest.post_delivery._config", return_value=cfg),
        patch("httpx.AsyncClient", return_value=_client_context()),
        patch("digest.irritator.evidence_stage.run_evidence_irritator", AsyncMock(return_value=canonical)),
        patch("digest.translation.translate_supplement_presentation", side_effect=translate),
        patch("digest.post_delivery._send_supplement", side_effect=send),
    ):
        assert await execute_post_delivery(Path("config.yaml"), checkpoint, execution=execution) == 0
    assert json.loads(_result(checkpoint).read_text()) == asdict(canonical)
    assert json.loads(_marker(checkpoint).read_text())["translation_status"] == "translated"


@pytest.mark.asyncio
async def test_compact_mode_archives_actual_optional_outcome_without_telegram(monkeypatch: pytest.MonkeyPatch) -> None:
    execution = ModelExecution()
    checkpoint = await confirmed_checkpoint(monkeypatch)
    payload = json.loads(checkpoint.read_text())
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
        assert await execute_post_delivery(Path("config.yaml"), checkpoint, execution=execution) == 2
    process.assert_awaited_once()
    send.assert_not_called()
    assert json.loads(_result(checkpoint).read_text())["status"] == "incomplete"
    record = json.loads(_marker(checkpoint).read_text())
    assert record["supplement_status"] == "archive_only" and record["stage_status"] == "incomplete"
    assert _markdown(checkpoint).exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [False, True])
async def test_required_source_provenance_never_falls_back_to_rss(tmp_path: Path, invalid: bool) -> None:
    execution = ModelExecution()
    checkpoint = tmp_path / "digests/day.review.json"
    payload = await _checkpoint(checkpoint)
    payload["full_source_required"] = True
    if invalid:
        payload["full_source_evidence"] = {"invalid": "incomplete checkpoint"}
    checkpoint.write_text(json.dumps(payload))
    assert prepare_post_delivery(Path("config.yaml"), checkpoint) == _marker(checkpoint)
    with (
        patch("httpx.AsyncClient", return_value=_client_context()),
        patch("digest.llm.complete", AsyncMock(side_effect=AssertionError("No RSS fallback"))) as model,
        patch("digest.post_delivery._send_supplement", AsyncMock(return_value="sent")),
    ):
        assert await execute_post_delivery(Path("config.yaml"), checkpoint, execution=execution) == 2
    model.assert_not_called()
    result = json.loads(_result(checkpoint).read_text())
    assert result["status"] == "incomplete"
    assert result["diagnostics"][0]["error"] == "FullSourceEvidencePending"
    assert "selected literal full-source passages" in _markdown(checkpoint).read_text()
    marker = json.loads(_marker(checkpoint).read_text())
    assert marker["full_source_required"] is True
    if invalid:
        assert marker["full_source_error"] == "ValueError"


@pytest.mark.asyncio
async def test_private_audit_is_json_only_and_never_sent_or_replayed(tmp_path: Path) -> None:
    execution = ModelExecution()
    from digest.irritator.evidence_stage import _admit_ranking
    from tests.factories import make_signal

    checkpoint = tmp_path / "digests/day.review.json"
    payload = await _checkpoint(checkpoint)
    prepare_post_delivery(Path("config.yaml"), checkpoint)
    result = _stage_result(payload["evidence"]["bundle_id"], "incomplete")
    private = make_signal(title="PRIVATE AUDIT SENTINEL", snippet="Confined to the private JSON trace.")
    result.ranking_audit = _admit_ranking([private], 5, 3, {}).audit
    client = _client_context()

    async def send(actual: Any, config: Any) -> str:
        saved = json.loads(_result(checkpoint).read_text())
        assert saved == asdict(result) and actual is result
        markdown = _markdown(checkpoint).read_text()
        assert "PRIVATE AUDIT SENTINEL" not in markdown and '"ranking_audit"' not in markdown
        assert "1 validated candidates, 1 admitted" in markdown and ".irritator.json" in markdown
        return "sent"

    with (
        patch("httpx.AsyncClient", return_value=client),
        patch("digest.irritator.evidence_stage.run_evidence_irritator", AsyncMock(return_value=result)) as stage,
        patch("digest.post_delivery._send_supplement", side_effect=send),
    ):
        assert await execute_post_delivery(Path("config.yaml"), checkpoint, execution=execution) == 2
        with pytest.raises(ValueError):
            await execute_post_delivery(Path("config.yaml"), checkpoint, execution=execution)
    stage.assert_awaited_once()


@pytest.mark.asyncio
async def test_private_audit_does_not_enter_telegram(monkeypatch: pytest.MonkeyPatch) -> None:
    from digest.irritator.evidence_stage import RankingDecision, _admit_ranking
    from tests.factories import make_signal

    config = fixture_config()
    config.telegram.enabled = True
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "same-primary-chat")
    result = _stage_result("bundle", "incomplete")
    private = make_signal(title="PRIVATE TITLE SENTINEL", snippet="PRIVATE ABSTRACT SENTINEL")
    result.ranking_audit = _admit_ranking([private], 5, 3, {}).audit
    result.ranking_audit.candidates[0].decision = RankingDecision(
        "context",
        10,
        "PRIVATE REASON SENTINEL",
        "private-quote",
        "PRIVATE QUOTE SENTINEL",
    )
    client = _client_context()
    client.post = AsyncMock(
        return_value=httpx.Response(
            200,
            json={"ok": True},
            request=httpx.Request("POST", "https://example.com"),
        )
    )
    with patch("httpx.AsyncClient", return_value=client):
        assert await _send_supplement(result, config) == "sent"
    client.post.assert_awaited_once()
    assert client.post.call_args.args == ("https://api.telegram.org/bottest-token/sendMessage",)
    payload = client.post.call_args.kwargs["json"]
    assert payload["chat_id"] == "same-primary-chat"
    assert payload["disable_notification"] is True
    assert "incomplete" in payload["text"]
    assert "Limited coverage" in payload["text"]
    assert "PRIVATE" not in json.dumps([call.kwargs for call in client.post.await_args_list])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,body,error",
    [
        (200, {"ok": False}, ValueError),
        (200, [], ValueError),
        (400, {"ok": False}, httpx.HTTPStatusError),
        (500, {"ok": True}, httpx.HTTPStatusError),
    ],
)
async def test_supplement_rejection_never_retries_or_falls_back(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    body: Any,
    error: type[Exception],
) -> None:
    config = fixture_config()
    config.telegram.enabled = True
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "same-primary-chat")
    client = _client_context()
    client.post = AsyncMock(
        return_value=httpx.Response(
            status,
            json=body,
            request=httpx.Request("POST", "https://api.telegram.org/bottest-token/sendMessage"),
        )
    )
    with patch("httpx.AsyncClient", return_value=client), pytest.raises(error):
        await _send_supplement(_stage_result("bundle"), config)
    client.post.assert_awaited_once()
    assert client.post.call_args.kwargs["json"]["parse_mode"] == "MarkdownV2"
    assert client.post.call_args.kwargs["json"]["disable_notification"] is True
    assert client.post.call_args.kwargs["timeout"] == 30.0
