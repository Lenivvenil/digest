"""Synthetic relevance overflow stays recoverable across bounded publication windows."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from digest.adapters.models.execution import ModelExecution
from digest.adapters.storage.candidate_progress import load_candidate_progress
from digest.application.analysis import analyze_articles as _analyze_articles
from digest.application.candidate_review import begin_packet, merge_candidates, plan_packet
from digest.application.preparation import CandidateWork, _review_candidates
from digest.application.review import run_evidence_review
from digest.application.review_request import build_evidence_bundle
from digest.config import Config, SourceConfig
from digest.delivery.edition import CLAIM_FILE, READY_FILE
from digest.domain.editorial.attempts import restore_review
from digest.domain.editorial.candidate_policy import pending_completed_report
from digest.domain.editorial.candidates import CandidateProgress
from digest.domain.editorial.reviews import _parse_live_review, _parse_review, validated_cached_selections
from digest.edition_runtime import delivery_phase
from digest.feedback import FeedbackStore, save_feedback
from digest.main import _run
from digest.presentation.review import primary_cards
from digest.radar.collector import Article, SourceCollectionOutcome, _capture_candidates, article_hash
from digest.review_checkpoint import load_review_checkpoint
from digest.review_resume import _reusable_slots
from scripts.review_fixture import fixture_config


def _inputs(now: datetime) -> tuple[Config, dict[str, list[Article]]]:
    config = fixture_config()
    config.review.max_detailed_selections = 8  # Exercise publication overflow independently of response detail.
    config.review.review_led_only = True
    config.sources = [SourceConfig("Source", "https://example.com/feed", "Tech", True, recency_hours=168)]
    articles = {
        "Tech": [
            Article(
                f"Useful item {index}",
                f"https://example.com/{index}",
                f"Distinct practical evidence {index}",
                "Source",
                "Tech",
                now,
            )
            for index in range(8)
        ]
    }
    return config, articles


def _response(items: list[dict[str, Any]]) -> str:
    return json.dumps(
        {
            "selections": [
                {
                    "evidence_id": item["evidence_id"],
                    "reason": "Useful synthetic evidence.",
                    "quote": item["title"],
                    "confidence": "high",
                }
                for item in items
            ],
            "limitations": ["Synthetic fixture; RSS metadata only."],
            "dispositions": [{"evidence_id": item["evidence_id"], "status": "selected"} for item in items],
        }
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("snapshot_failure", [False, True])
async def test_eight_useful_five_confirmed_three_next_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    snapshot_failure: bool,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    now = datetime.now(UTC)
    config, articles = _inputs(now)
    config.telegram.enabled = config.obsidian.enabled = True
    config.telegram.delivery_mode = "compact"
    config.telegram.bot_username = "test_digest_bot"
    config.obsidian.output_dir = "digests"
    save_feedback(FeedbackStore(), ".cache", strict=True)
    monkeypatch.setattr("digest.config.load_config", lambda _: config)
    monkeypatch.setattr("digest.application.run_state.collect_run_feedback",
          AsyncMock(return_value=(FeedbackStore(), True, 0)))
    monkeypatch.setattr("digest.application.run_state.apply_pending_approvals",
        lambda c, *args, **kwargs: (c, kwargs["execution"]))
    collection_calls = 0
    requests: list[list[str]] = []

    async def collect(c: Config, **kwargs: Any) -> tuple[dict[str, list[Article]], dict[str, str]]:
        nonlocal collection_calls
        collection_calls += 1
        observed = articles["Tech"] if collection_calls == 1 else []
        cache_path = Path(".cache/seen_articles.json")
        cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
        inventory = kwargs["inventory"]
        inventory.sources = [SourceCollectionOutcome("Source", "https://example.com/feed", "Tech", 3)]
        _capture_candidates(inventory, c.enabled_sources, [observed], cache, now, [], {})
        return ({"Tech": observed} if observed else {}), cache

    async def model(role: Any, messages: list[dict[str, str]], c: Config, **kwargs: Any) -> tuple[str, dict]:
        task = json.loads(messages[1]["content"])
        assert "max_selections" not in task
        assert kwargs["max_output_tokens"] == 4096
        evidence = task["evidence"]["items"]
        requests.append([item["evidence_id"] for item in evidence])
        return _response(evidence), {}

    monkeypatch.setattr("digest.radar.collect", collect)
    monkeypatch.setattr("digest.application.review.complete", model)
    with respx.mock(assert_all_mocked=True) as router:
        if snapshot_failure:
            with patch("digest.preparation.save_preparation", side_effect=OSError("snapshot failure")):
                with pytest.raises(OSError, match="snapshot failure"):
                    await _run("config.yaml", False, False, False, prepare_only=True)
            saved = load_candidate_progress()
            assert len(saved.packets[0].report.reviews[0].selections) == 8
            assert len(requests) == 1
        first = await _run("config.yaml", False, False, False, prepare_only=True)
        assert first.edition_status == "ready" and len(requests) == 1
        manifest = json.loads(Path(".cache", READY_FILE).read_text())
        cards = manifest["canonical_metadata"]["cards"]
        delivered = {article_hash(item["title"], item["link"]) for item in cards}
        assert len(cards) == 5 and [article_hash(item["title"], item["link"]) for item in cards] == requests[0][:5]
        state = load_candidate_progress()
        assert len(state.candidates) == 8 and {item.status for item in state.candidates.values()} == {"selected"}
        assert all(item.eligible and item.delivery_cache_observed_at is None for item in state.candidates.values())
        assert state.packets[0].max_selections == 5
        archive = next(Path("digests").glob("*.review.json"))
        original_bytes = archive.read_bytes()
        full_review = json.loads(original_bytes)["reviews"][0]
        assert [item["evidence_id"] for item in full_review["selections"]] == requests[0]
        assert full_review == asdict(state.packets[0].report.reviews[0])

        assert await delivery_phase("claim", "config.yaml", first.ready_sha256, None) == 0
        claim_sha = hashlib.sha256(Path(".cache", CLAIM_FILE).read_bytes()).hexdigest()
        delivery = router.post("https://api.telegram.org/bottest-token/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 70, "chat": {"id": 12345}}})
        )
        assert await delivery_phase("send", "config.yaml", first.ready_sha256, claim_sha) == 0
        assert delivery.call_count == 1
        assert set(json.loads(Path(".cache/seen_articles.json").read_text())) == delivered
        same_day = await _run("config.yaml", False, False, False, prepare_only=True)
        assert same_day.edition_status == "confirmed" and len(requests) == 1

        # Observe ordinary recovery before its next request consumes technical pending.
        async def next_model(role: Any, messages: list[dict[str, str]], c: Config, **kwargs: Any) -> tuple[str, dict]:
            pending = load_candidate_progress()
            assert set(pending.candidates) == set(requests[0]) - delivered
            assert {item.status for item in pending.candidates.values()} == {"technical_pending"}
            assert all(item.eligible for item in pending.candidates.values())
            return await model(role, messages, c, **kwargs)

        monkeypatch.setattr("digest.application.review.complete", next_model)
        tomorrow = await _run(
            "config.yaml", False, False, False, prepare_only=True, edition_date=now.date() + timedelta(days=1)
        )
        assert tomorrow.edition_status == "pending_window"
        assert len(requests) == 2 and set(requests[1]) == set(requests[0]) - delivered
        assert archive.read_bytes() == original_bytes
        next_cards = json.loads(Path(".cache", READY_FILE).read_text())["canonical_metadata"]["cards"]
        assert len(next_cards) == 3
        assert {article_hash(item["title"], item["link"]) for item in next_cards} == set(requests[1])
        assert {item.status for item in load_candidate_progress().candidates.values()} == {"selected"}


@pytest.mark.asyncio
@pytest.mark.parametrize("partial", [False, True])
async def test_full_or_partial_report_reuses_all_selections_after_publication_cap_changes(
    tmp_path: Path,
    partial: bool,
) -> None:
    execution = ModelExecution()
    config, articles = _inputs(datetime.now(UTC))
    bundle = build_evidence_bundle(articles, config.review)
    payload = json.loads(_response([asdict(item) for item in bundle.items]))
    if partial:
        payload["selections"][-1]["quote"] = "Not in the supplied evidence"
    raw = json.dumps(payload)
    with patch("digest.application.review.complete", AsyncMock(return_value=(raw, {}))) as complete:
        original = await run_evidence_review(bundle, config, execution=execution)
    assert complete.await_count == 2  # Only the existing independent primary and secondary slots.
    count = 7 if partial else 8
    assert all(len(review.selections) == count for review in original.reviews)
    path = tmp_path / "review.json"
    path.write_text(json.dumps(asdict(original)))
    original_bytes = path.read_bytes()
    config.review.max_selections = 2
    restored_bundle, cached = load_review_checkpoint(path, config)
    assert _reusable_slots(restored_bundle, cached, config) == {"primary", "secondary"}
    with patch(
        "digest.application.review.complete",
        AsyncMock(side_effect=AssertionError("No new relevance request"))) as complete:
        reused = await run_evidence_review(restored_bundle, config, cached, execution=execution)
    complete.assert_not_called()
    assert path.read_bytes() == original_bytes
    for before, after in zip(original.reviews, reused.reviews, strict=True):
        assert asdict(after) == {**asdict(before), "reused_from_checkpoint": True}
    before_cards = asdict(reused)
    assert len(primary_cards(restore_review(reused), articles, "en", max_cards=config.review.max_selections)) == 2
    assert asdict(reused) == before_cards
    if partial:
        assert cached[0].rejected_items[0].index == 7
        cached[0].rejected_items[0] = replace(cached[0].rejected_items[0], index=8)
        with pytest.raises(ValueError, match="partial-review provenance"):
            validated_cached_selections(cached[0], bundle)


@pytest.mark.asyncio
async def test_non_candidate_publication_also_caps_cards_after_full_relevance_review() -> None:
    execution = ModelExecution()
    config, articles = _inputs(datetime.now(UTC))
    bundle = build_evidence_bundle(articles, config.review)
    raw = _response([asdict(item) for item in bundle.items])
    with patch("digest.application.review.complete", AsyncMock(return_value=(raw, {}))) as complete:
        _, _, cards, report = await _analyze_articles(articles, config, execution=execution)
    complete.assert_awaited_once()
    assert len(cards) == 5 and len(report.reviews[0].selections) == 8


@pytest.mark.asyncio
async def test_fallback_preserves_duplicate_bound_to_overflow_selected_identity(tmp_path: Path) -> None:
    execution = ModelExecution()
    now = datetime.now(UTC)
    config, articles = _inputs(now)
    articles["Tech"].append(
        Article("Repeated item", "https://example.com/8", "Same evidence as item 7", "Source", "Tech", now)
    )
    progress = merge_candidates(CandidateProgress(), articles, config, {}, now=now)
    packet = plan_packet(progress, config, now)
    begin_packet(progress, packet, tmp_path)
    items = [asdict(item) for item in packet.evidence.items]
    payload = json.loads(_response(items[:8]))
    duplicate_id, retained_id = items[8]["evidence_id"], items[7]["evidence_id"]
    payload["dispositions"].append(
        {
            "evidence_id": duplicate_id,
            "status": "duplicate",
            "retained_id": retained_id,
            "reason": "Repeated synthetic evidence.",
        }
    )
    with patch(
        "digest.application.review.complete",
        AsyncMock(side_effect=[RuntimeError("unavailable"), (json.dumps(payload), {})])
    ) as complete:
        reviewed = await _review_candidates(
            CandidateWork(progress, packet), articles, config, str(tmp_path), execution=execution,
        )
        cards, report = reviewed.cards, reviewed.report
    assert complete.await_count == 2  # Existing primary/fallback budget, with no overflow repair call.
    assert report.reviews[0].status == "unavailable" and len(report.reviews[1].selections) == 8
    assert len(cards) == 5
    assert retained_id not in {article_hash(card.title, card.link) for card in cards}
    assert progress.candidates[duplicate_id].status == "duplicate"
    assert progress.candidates[duplicate_id].disposition.retained_id == retained_id
    assert progress.candidates[retained_id].status == "selected"
    restored = load_candidate_progress(tmp_path)
    assert restored.packets[0].max_selections == 5
    assert pending_completed_report(restored) == report
    assert restored.packets[0].disposition_attempts == packet.disposition_attempts


@pytest.mark.parametrize("failure", ["count", "response_budget"])
def test_packet_and_response_bounds_still_reject_invalid_envelopes(failure: str) -> None:
    config, articles = _inputs(datetime.now(UTC))
    bundle = build_evidence_bundle(articles, config.review)
    payload = json.loads(_response([asdict(item) for item in bundle.items]))
    if failure == "count":
        payload["selections"].append(payload["selections"][0])
    raw = json.dumps(payload)
    if failure == "response_budget":
        raw += " " * 32000
    for parser in (_parse_review, _parse_live_review):
        with pytest.raises(ValueError, match="invalid selection count|response exceeds review budget"):
            parser(raw, bundle)


@pytest.mark.asyncio
async def test_unicode_selections_reconcile_with_original_character_budget(tmp_path: Path) -> None:
    execution = ModelExecution()
    now = datetime.now(UTC)
    config, articles = _inputs(now)
    for article in articles["Tech"]:
        article.description = "Ж" * 200
    progress = merge_candidates(CandidateProgress(), articles, config, {}, now=now)
    packet = plan_packet(progress, config, now)
    begin_packet(progress, packet, tmp_path)
    payload = json.loads(_response([asdict(item) for item in packet.evidence.items]))
    for selection in payload["selections"]:
        selection.update(reason="Я" * 600, quote="Ж" * 200)
    raw = json.dumps(payload, ensure_ascii=False)
    assert len(raw) < 32000 < len(json.dumps(payload))
    with patch("digest.application.review.complete", AsyncMock(return_value=(raw, {}))) as complete:
        reviewed = await _review_candidates(
            CandidateWork(progress, packet), articles, config, str(tmp_path), execution=execution,
        )
        cards, report = reviewed.cards, reviewed.report
    complete.assert_awaited_once()
    assert len(cards) == 5 and len(report.reviews[0].selections) == 8
    restored = load_candidate_progress(tmp_path)
    assert pending_completed_report(restored) == report
    assert restored.packets[0].report.reviews[0].response_sha256 == hashlib.sha256(raw.encode()).hexdigest()
