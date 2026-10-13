"""Offline end-to-end preparation/sender boundary and failure persistence."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import respx

from digest.adapters.models.execution import ModelExecution
from digest.adapters.storage.candidate_objects import freeze_packet
from digest.application.candidate_review import merge_candidates, plan_packet
from digest.closing import ClosingDecision, capture_closing, decide_closing
from digest.config import ClosingConfig, ClosingSourceBinding, SourceConfig
from digest.delivery.edition import CLAIM_FILE, READY_FILE, inspect_edition
from digest.domain.editorial.attempts import ReviewAttempt, resolve_review
from digest.domain.editorial.candidates import CandidateProgress
from digest.domain.editorial.dispositions import capture_review_dispositions
from digest.domain.editorial.reviews import BlindReviewReport, EvidenceSelection, ModelReview
from digest.edition_runtime import (
    ExistingEdition,
    delivery_phase,
    preparation_stats,
    present_preparation,
    recover_preparation,
)
from digest.feedback import FeedbackStore, load_feedback, save_feedback
from digest.preparation import (
    AcceptedPreparation,
    PreparationSnapshot,
    _canonical,
    load_accepted_preparation,
    load_preparation,
    persist_accepted_preparation,
    save_preparation,
)
from digest.radar.collector import Article, article_hash
from digest.radar.summarizer import ArticleSummary
from scripts.review_fixture import fixture_config
from tests.test_closing import EA_CREDIT, EA_FEED, NHS_CREDIT, NHS_FEED, NOW


def bound_snapshot(
    original: PreparationSnapshot,
    closing_card: ArticleSummary | None = None,
    *,
    closing_feed: str = NHS_FEED,
    main_feed: str = "https://example.com/feed",
    published: datetime = NOW,
) -> PreparationSnapshot:
    """Persist actual accepted report/occurrences; presentation tests make no model calls."""
    config = fixture_config()
    cards = [*original.top_articles, *([closing_card] if closing_card is not None else [])]
    config.sources = [
        SourceConfig(
            card.source,
            closing_feed if card is closing_card else main_feed,
            card.category,
            True,
        )
        for card in cards
    ]
    if closing_card is not None:
        config.closing = ClosingConfig(
            True, (ClosingSourceBinding(closing_card.source, closing_feed, closing_card.category),)
        )
    articles: dict[str, list[Article]] = {}
    for card in cards:
        articles.setdefault(card.category, []).append(
            Article(card.title, card.link, "Raw evidence.", card.source, card.category, published)
        )
    packet = plan_packet(merge_candidates(CandidateProgress(), articles, config, {}, now=NOW), config, NOW)
    assert packet is not None and len(packet.articles) == len(cards)
    by_id = {article_hash(card.title, card.link): card for card in cards}
    selections = [
        EvidenceSelection(item.evidence_id, by_id[item.evidence_id].summary, item.excerpt, "high")
        for item in packet.evidence.items
    ]
    raw = json.dumps(
        {
            "selections": [
                {key: value for key, value in asdict(item).items() if key != "typography_normalized"}
                for item in selections
            ],
            "limitations": [],
            "closing": {
                "schema_version": 1,
                "evidence_id": article_hash(closing_card.title, closing_card.link)
                if closing_card is not None
                else None,
            },
        }
    )
    review = ModelReview(
        "primary",
        "test",
        "test",
        packet.evidence.bundle_id,
        packet.prompt_hash,
        "ok",
        selections=selections,
        response_sha256=hashlib.sha256(raw.encode()).hexdigest(),
    )
    report = BlindReviewReport(1, packet.evidence, [review], "incomplete", None, [], "Fixture review")
    packet.report = report
    freeze_packet(packet, {}, ".cache")
    decision = original.closing
    if closing_card is not None:
        result = resolve_review(
            report,
            (
                ReviewAttempt(
                    review,
                    capture_review_dispositions(report.evidence, review, raw),
                    capture_closing(review, raw, "stop"),
                ),
            ),
        )
        decision = decide_closing(result, packet, config.closing, config.sources)
        assert decision.status == "selected" and decision.card == closing_card
    return replace(original, review_report=report, closing=decision)


@pytest.mark.asyncio
async def test_supplement_delivery_outputs_current_review_and_applies_real_state_before_next_investigation(
    setup: tuple[SimpleNamespace, PreparationSnapshot],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from datetime import datetime, timedelta, timezone
    from unittest.mock import patch

    from digest import post_delivery
    from digest.application import prepared_delivery
    from tests.test_post_delivery import pending_supplement

    config, snapshot = setup
    config.obsidian.output_dir = str(Path("digests").resolve())
    pending = await pending_supplement(monkeypatch)
    next_day = datetime.fromisoformat(pending.fragment.origin.publication_day).replace(tzinfo=timezone.utc) + timedelta(
        days=1, hours=12
    )
    monkeypatch.setattr(prepared_delivery, "_instant", lambda now: now or next_day)
    snapshot = bound_snapshot(snapshot)
    output = tmp_path / "workflow-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    with patch(
        "digest.application.presentation.publication_presentation",
        AsyncMock(return_value=("Current notice", snapshot.top_articles, [])),
    ):
        accepted = persist_accepted_preparation(snapshot, cache_dir=".cache", publication_date=next_day.date())
        prepared = preparation_stats(
            await present_preparation(accepted, config, execution=ModelExecution(), publication_date=next_day.date())
        )
    manifest = json.loads(Path(".cache", READY_FILE).read_text())
    assert manifest["current_review_checkpoint"] == prepared.review_checkpoint != pending.fragment.checkpoint
    assert pending.fragment.checkpoint in manifest["checkpoint_refs"]
    assert prepared.review_checkpoint in manifest["checkpoint_refs"]
    assert await delivery_phase("claim", "config.yaml", prepared.ready_sha256, None) == 0
    claim_sha = hashlib.sha256(Path(".cache", CLAIM_FILE).read_bytes()).hexdigest()
    with respx.mock(assert_all_called=True) as router:
        router.post("https://api.telegram.org/bottest-token/sendMessage").mock(
            return_value=httpx.Response(
                200,
                json={"ok": True, "result": {"message_id": 88, "chat": {"id": 12345}}},
            )
        )
        assert await delivery_phase("send", "config.yaml", prepared.ready_sha256, claim_sha) == 0
    outputs = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert outputs["review_checkpoint"] == prepared.review_checkpoint
    assert json.loads(Path(pending.attempt).read_text())["supplement_status"] == "consumed"
    identity = article_hash(snapshot.top_articles[0].title, snapshot.top_articles[0].link)
    assert identity in json.loads(Path(".cache/seen_articles.json").read_text())
    assert load_feedback(".cache", strict=True).article_source_map == {identity: "Source"}
    assert pending.fragment.fragment_id not in Path(".cache/source_stats.json").read_text()
    post_config = fixture_config()
    post_config.telegram.delivery_mode = "compact"
    with patch("digest.post_delivery._config", return_value=post_config):
        marker = post_delivery.prepare_post_delivery(Path("config.yaml"), Path(outputs["review_checkpoint"]))
    assert marker is not None
    next_origin = json.loads(marker.read_text())["delivered"]
    assert next_origin["ready_sha256"] == prepared.ready_sha256
    assert [card["card_id"] for card in next_origin["cards"]] == [identity]


@pytest.mark.asyncio
@pytest.mark.parametrize("omission", ["corrupt_projection", "render_capacity", "split_closer"])
async def test_optional_fragment_failure_before_freeze_preserves_main_and_closer(
    setup: tuple[SimpleNamespace, PreparationSnapshot],
    monkeypatch: pytest.MonkeyPatch,
    omission: str,
) -> None:
    from datetime import datetime, timedelta
    from unittest.mock import patch

    from tests.test_post_delivery import pending_supplement

    config, snapshot = setup
    url = "https://external.example/" + "_" * 2000 if omission == "render_capacity" else "https://external.example/ok"
    if omission == "split_closer":
        with patch("digest.application.supplement.fragment_text", return_value="x" * 3607):
            pending = await pending_supplement(monkeypatch)
    else:
        pending = await pending_supplement(monkeypatch, source_url=url)
    if omission == "corrupt_projection":
        Path(pending.projection).write_text("corrupt optional file")
    closer = ArticleSummary("Human story", "https://example.com/human", "NHS England", "Health", "Raw evidence.")
    snapshot = bound_snapshot(snapshot, closer)
    day = datetime.fromisoformat(pending.fragment.origin.publication_day).date() + timedelta(days=1)
    with patch(
        "digest.application.presentation.publication_presentation",
        AsyncMock(return_value=("Current notice", snapshot.top_articles, [])),
    ):
        accepted = persist_accepted_preparation(snapshot, cache_dir=".cache", publication_date=day)
        result = preparation_stats(
            await present_preparation(accepted, config, execution=ModelExecution(), publication_date=day)
        )
    manifest = json.loads(Path(".cache", READY_FILE).read_text())
    assert manifest["schema"] == 2 and len(manifest["articles"]) == 2
    assert [item["card"]["title"] for item in manifest["articles"]] == [snapshot.top_articles[0].title, closer.title]
    assert result.markdown_saved
    if omission != "corrupt_projection":
        record = json.loads(Path(pending.attempt).read_text())
        assert record["supplement_status"] == "archive_only"
        assert record["supplement_reason"] == "supplement_rendering_failed"
        if omission == "split_closer":
            from digest.presentation.telegram import SupplementPlacement, render_compact_publication

            cards = [ArticleSummary(**item["card"]) for item in manifest["articles"]]
            plain = render_compact_publication(cards, config, "Current notice")
            shifted = render_compact_publication(
                cards,
                config,
                "Current notice",
                SupplementPlacement(
                    pending.fragment.fragment_id,
                    pending.fragment.text,
                    1,
                ),
            )
            assert plain.articles[-1].covering_chunks == (0,)
            assert shifted.articles[-1].covering_chunks == (0, 1)
    else:
        assert manifest["presentation_metadata"]["supplement"]["status"] == "unverified_pending_fragment"


@pytest.fixture
def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[SimpleNamespace, PreparationSnapshot]:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    config = SimpleNamespace(
        review=SimpleNamespace(enabled=True, review_led_only=True),
        radar=SimpleNamespace(language="en"),
        telegram=SimpleNamespace(enabled=True, delivery_mode="compact", bot_username="test_digest_bot"),
        obsidian=SimpleNamespace(enabled=True, output_dir="digests"),
        translation=SimpleNamespace(enabled=False),
        adaptive=SimpleNamespace(enabled=False), enabled_sources=[],
    )
    card = ArticleSummary("Source title", "https://example.com/article", "Source", "Tech", "Canonical claim")
    snapshot = PreparationSnapshot([card], [], "Canonical notice", None, 1, 1, ["Source"])
    save_feedback(FeedbackStore(), ".cache", strict=True)
    monkeypatch.setattr("digest.config.load_config", lambda _: config)
    return config, snapshot


@pytest.mark.asyncio
async def test_accepted_analysis_survives_presentation_failure_and_sender_uses_frozen_bytes(
    setup: tuple[SimpleNamespace, PreparationSnapshot], monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    execution = ModelExecution()
    config, snapshot = setup
    today = datetime.now(timezone.utc).date()
    published = datetime.combine(today + timedelta(days=1), datetime.min.time(), timezone.utc)
    snapshot = bound_snapshot(snapshot, published=published)
    save_preparation(snapshot)
    presentation = AsyncMock(side_effect=RuntimeError("presentation failed"))
    monkeypatch.setattr("digest.application.presentation.publication_presentation", presentation)
    recovered = recover_preparation()
    assert isinstance(recovered, AcceptedPreparation)
    with pytest.raises(RuntimeError):
        await present_preparation(recovered, config, execution=execution)
    assert asdict(load_preparation()) == asdict(snapshot)
    assert not Path(".cache", CLAIM_FILE).exists()
    assert not Path(".cache/seen_articles.json").exists()
    presentation.side_effect = None
    presentation.return_value = ("Rendered notice", snapshot.top_articles, [])
    recovered = recover_preparation()
    assert isinstance(recovered, AcceptedPreparation)
    stats = preparation_stats(await present_preparation(recovered, config, execution=execution))
    assert stats.edition_status == "ready"
    frozen = json.loads(Path(".cache", READY_FILE).read_text())
    notice = (
        f"Source feed timestamp (UTC): {published.isoformat()}; later than this edition’s date ({today.isoformat()})."
    )
    assert frozen["articles"][0]["card"]["summary"] == f"{notice} {snapshot.top_articles[0].summary}"
    assert frozen["canonical_metadata"]["cards"] == [asdict(card) for card in snapshot.top_articles]
    assert notice in Path(stats.markdown_path).read_text()
    assert all(call.args[1] == snapshot.top_articles for call in presentation.await_args_list)
    assert not Path(".cache", CLAIM_FILE).exists()
    assert load_preparation() is None

    # Recovery must not initialize a request state or inspect model-budget settings.
    monkeypatch.setattr(ModelExecution, "request_state", MagicMock(side_effect=AssertionError("No model state")))
    monkeypatch.setenv("DIGEST_MODEL_BUDGET_REQUIRED", "invalid")
    # Later preparation/model settings cannot change the frozen transport body.
    config.review.enabled = False
    config.radar.language = "ru"
    config.sources = [SourceConfig("NHS England", NHS_FEED, "Health", True)]
    monkeypatch.setattr(
        "digest.application.presentation.publication_presentation", AsyncMock(side_effect=AssertionError("rerender"))
    )
    recovered = recover_preparation()
    assert isinstance(recovered, ExistingEdition)
    assert preparation_stats(recovered).ready_sha256 == stats.ready_sha256
    assert await delivery_phase("claim", "config.yaml", stats.ready_sha256, None) == 0
    import hashlib

    claim_sha = hashlib.sha256(Path(".cache", CLAIM_FILE).read_bytes()).hexdigest()
    current = load_feedback(".cache", strict=True)
    current.last_update_id = 999
    save_feedback(current, ".cache", strict=True)
    caplog.clear()
    with respx.mock(assert_all_called=True) as router:
        route = router.post("https://api.telegram.org/bottest-token/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 52, "chat": {"id": 12345}}}),
        )
        assert await delivery_phase("send", "config.yaml", stats.ready_sha256, claim_sha) == 0
        assert json.loads(route.calls[0].request.content) == frozen["payloads"][0]
    assert "Prepared edition held" not in caplog.text
    assert load_feedback(".cache", strict=True).last_update_id == 999
    assert load_feedback(".cache", strict=True).article_source_map
    assert json.loads(Path(".cache/seen_articles.json").read_text())
    assert inspect_edition()[2] == "confirmed"
    recovered = recover_preparation()
    assert isinstance(recovered, ExistingEdition)
    assert preparation_stats(recovered).edition_status == "confirmed"
    with respx.mock(assert_all_called=False) as router:
        assert await delivery_phase("send", "config.yaml", stats.ready_sha256, claim_sha) == 0
        assert not router.calls
    assert "Prepared edition held" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failed_file",
    [
        "feedback.json",
        "source_stats.json",
        "source_state.json",
        "seen_articles.json",
    ],
)
async def test_state_save_failure_after_accepted_post_holds_instead_of_replaying(
    setup: tuple[SimpleNamespace, PreparationSnapshot],
    monkeypatch: pytest.MonkeyPatch,
    failed_file: str,
) -> None:
    execution = ModelExecution()
    from digest._util import atomic_json_write
    from digest.delivery.edition import RECEIPTS_FILE
    from digest.source_scorer import SourceStats, load_stats, save_stats

    config, snapshot = setup
    config.adaptive = SimpleNamespace(enabled=True)
    config.enabled_sources = [SourceConfig("Source", "https://example.com/feed", "Tech", True, trial=True)]
    accepted = persist_accepted_preparation(snapshot, cache_dir=".cache")
    stats = preparation_stats(await present_preparation(accepted, config, execution=execution))
    assert await delivery_phase("claim", "config.yaml", stats.ready_sha256, None) == 0
    claim_sha = hashlib.sha256(Path(".cache", CLAIM_FILE).read_bytes()).hexdigest()
    current = FeedbackStore(last_update_id=999, article_source_map={"old": "Previous source"})
    save_feedback(current, ".cache", strict=True)
    save_stats({"Source": SourceStats("Source", total_fetches=2, successful_fetches=2)}, ".cache")
    atomic_json_write(Path(".cache/seen_articles.json"), {"old": "previous timestamp"})
    write_order = ["feedback.json", "source_stats.json", "source_state.json", "seen_articles.json"]
    writes: list[str] = []

    def fail_write(path: Path, data: Any) -> None:
        writes.append(path.name)
        if path.name == failed_file:
            raise OSError("synthetic application write failure")
        atomic_json_write(path, data)

    monkeypatch.setattr("digest.adapters.storage.feedback.atomic_json_write", fail_write)
    monkeypatch.setattr("digest.adapters.storage.delivery_state.atomic_json_write", fail_write)
    with respx.mock() as router:
        route = router.post("https://api.telegram.org/bottest-token/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 52, "chat": {"id": 12345}}}),
        )
        with pytest.raises(OSError, match="application write failure"):
            await delivery_phase("send", "config.yaml", stats.ready_sha256, claim_sha)
        assert route.call_count == 1
    assert writes == write_order[: write_order.index(failed_file) + 1]
    receipts = json.loads(Path(".cache", RECEIPTS_FILE).read_text())
    assert receipts["state"] == "confirmed" and receipts["applied"] is False
    assert receipts["confirmed"][0]["message_id"] == 52
    persisted = load_feedback(".cache", strict=True)
    assert persisted.last_update_id == 999
    card = snapshot.top_articles[0]
    identity = article_hash(card.title, card.link)
    assert (identity in persisted.article_source_map) is (failed_file != "feedback.json")
    source = load_stats(".cache")["Source"]
    assert source.articles_included_in_digest == int(failed_file in {"source_state.json", "seen_articles.json"})
    assert source.total_fetches == source.successful_fetches == 2
    assert Path(".cache/source_state.json").exists() is (failed_file == "seen_articles.json")
    assert json.loads(Path(".cache/seen_articles.json").read_text()) == {"old": "previous timestamp"}
    assert inspect_edition()[2] == "held"
    retained = {path: path.read_bytes() for path in Path(".cache").glob("*.json")}
    with respx.mock(assert_all_called=False) as router:
        with pytest.raises(ValueError, match="held"):
            await delivery_phase("send", "config.yaml", stats.ready_sha256, claim_sha)
        assert not router.calls
    assert writes == write_order[: write_order.index(failed_file) + 1]
    assert {path: path.read_bytes() for path in retained} == retained


@pytest.mark.asyncio
async def test_empty_or_corrupt_attribution_never_sends(setup: tuple[SimpleNamespace, PreparationSnapshot]) -> None:
    execution = ModelExecution()
    config, snapshot = setup
    snapshot.top_articles.clear()
    accepted = persist_accepted_preparation(snapshot, cache_dir=".cache")
    empty = preparation_stats(await present_preparation(accepted, config, execution=execution))
    assert empty.edition_status == "no_ready"
    assert not Path(".cache", READY_FILE).exists()
    snapshot.top_articles.append(ArticleSummary("Title", "https://example.com/x", "Source", "Tech", "Claim"))
    accepted = persist_accepted_preparation(snapshot, cache_dir=".cache")
    stats = preparation_stats(await present_preparation(accepted, config, execution=execution))
    Path(".cache/feedback.json").write_text("corrupt")
    with pytest.raises(ValueError):
        await delivery_phase("claim", "config.yaml", stats.ready_sha256, None)
    assert not Path(".cache", CLAIM_FILE).exists()


@pytest.mark.asyncio
async def test_unavailable_primary_is_not_a_reusable_empty_preparation(
    setup: tuple[SimpleNamespace, PreparationSnapshot],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from digest.application.preparation import (
        CategoryAnalysis,
        CollectedArticles,
        PreparationRun,
        _prepare_category_edition,
    )
    from digest.domain.catalog.sources import SourceStateStore
    from digest.irritator import IrritatorStatus
    from scripts.review_fixture import fixture_articles
    from tests.test_preparation import _snapshot

    execution = ModelExecution()
    config = fixture_config()
    config.telegram.delivery_mode = "compact"
    snapshot = _snapshot()
    assert snapshot.review_report is not None
    snapshot.review_report.reviews[0].status = "unavailable"
    snapshot.review_report.reviews[0].selections.clear()
    work = CategoryAnalysis([], None, [], snapshot.review_report)
    collected = CollectedArticles(config, fixture_articles(), {}, False, snapshot.article_count)
    run = PreparationRun(config, execution, SourceStateStore(), {}, FeedbackStore(), True, 0)
    monkeypatch.setattr(
        "digest.application.investigation.run_irritator",
        AsyncMock(return_value=([], [], IrritatorStatus("Offline fixture", "empty"))),
    )
    presentation = AsyncMock(side_effect=RuntimeError("late presentation failure"))
    monkeypatch.setattr("digest.application.presentation.publication_presentation", presentation)

    stats = await _prepare_category_edition(work, collected, run, verbose=False, publication_date=None)
    assert stats.edition_status == "selection_incomplete"
    assert load_preparation() is None
    assert not Path(".cache", READY_FILE).exists()
    presentation.assert_not_awaited()

    # Category projections remain resumable even when their optional review failed.
    work.cards = snapshot.top_articles
    with pytest.raises(RuntimeError, match="late presentation failure"):
        await _prepare_category_edition(work, collected, run, verbose=False, publication_date=None)
    accepted = load_preparation()
    assert accepted is not None and accepted.top_articles == work.cards
    assert accepted.review_report == snapshot.review_report
    presentation.side_effect = None
    presentation.return_value = ("Notice", work.cards, [])
    recovered = recover_preparation()
    assert isinstance(recovered, AcceptedPreparation)
    stats = preparation_stats(await present_preparation(recovered, config, execution=execution))
    assert stats.edition_status == "ready"
    manifest = json.loads(Path(".cache", READY_FILE).read_text())
    assert manifest["canonical_metadata"]["cards"] == [asdict(card) for card in work.cards]
    assert load_preparation() is None


@pytest.mark.asyncio
async def test_main_prepare_resume_does_not_refetch_or_reanalyze(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    from digest.main import run
    from scripts.review_fixture import fixture_articles, fixture_config

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    config = fixture_config()
    config = replace(config, telegram=replace(config.telegram, delivery_mode="compact", bot_username="test_bot"))
    monkeypatch.setattr("digest.config.load_config", lambda _: config)
    articles = fixture_articles()
    first = next(iter(articles.values()))[0]
    cards = [ArticleSummary(first.title, first.link, first.source, first.category, "Accepted canonical")]
    collection = AsyncMock(return_value=(articles, {}))
    analysis = AsyncMock(return_value=([], None, cards, None))
    monkeypatch.setattr("digest.radar.collect", collection)
    monkeypatch.setattr("digest.application.analysis.analyze_articles", analysis)
    presentation = AsyncMock(side_effect=RuntimeError("late presentation failure"))
    monkeypatch.setattr("digest.application.presentation.publication_presentation", presentation)
    with pytest.raises(RuntimeError):
        await run("config.yaml", False, False, False, feedback_precollected=True, prepare_only=True)
    assert load_preparation() is not None
    assert not Path(".cache", CLAIM_FILE).exists()
    assert not Path(".cache/seen_articles.json").exists()
    presentation.side_effect = None
    presentation.return_value = ("Notice", cards, [])
    result = await run("config.yaml", False, False, False, feedback_precollected=True, prepare_only=True)
    assert result.edition_status == "ready"
    assert result.feeds_fetched == result.new_articles == 0
    assert result.digest_length == len("Notice")
    assert collection.await_count == analysis.await_count == 1


@pytest.mark.asyncio
async def test_future_edition_archive_and_delivery_day_accounting(
    setup: tuple[SimpleNamespace, PreparationSnapshot],
) -> None:
    execution = ModelExecution()
    from datetime import datetime, timedelta, timezone

    from digest.application.prepared_delivery import claim_edition, send_prepared_edition
    from digest.radar.collector import article_hash
    from digest.source_scorer import DailySnapshot, SourceStats, load_stats, save_stats

    config, snapshot = setup
    old_card = replace(snapshot.top_articles[0], title="Already delivered",
                       link="https://example.com/already", source="Retained")
    snapshot = replace(snapshot, top_articles=[*snapshot.top_articles, old_card],
                       contributing_sources=["Source", "Retained"])
    today = datetime.now(timezone.utc).date()
    target = today + timedelta(days=1)
    snapshot = bound_snapshot(snapshot, published=datetime.combine(target, datetime.min.time(), timezone.utc))
    accepted = persist_accepted_preparation(snapshot, cache_dir=".cache", publication_date=target)
    result = preparation_stats(
        await present_preparation(accepted, config, publication_date=target, execution=execution)
    )
    assert result.edition_status == "pending_window"
    archive = Path("digests", f"{target.isoformat()}.md")
    assert archive.exists() and f"date: {target.isoformat()}" in archive.read_text()
    manifest = json.loads(Path(".cache", READY_FILE).read_text())
    assert manifest["presentation_metadata"]["cards"] == [asdict(card) for card in snapshot.top_articles]
    assert "Source feed timestamp" not in archive.read_text()
    assert inspect_edition()[2] == "pending_window"
    with pytest.raises(ValueError):
        await delivery_phase("claim", "config.yaml", result.ready_sha256, None)
    assert not Path(".cache", CLAIM_FILE).exists()

    # Delivery-day inclusion does not invent an additional fetch observation.
    source = SourceStats(
        "Source", total_fetches=2, successful_fetches=2, history=[DailySnapshot(today.isoformat(), 3, 0, True)]
    )
    retained = SourceStats("Retained", articles_included_in_digest=3)
    save_stats({"Source": source, "Retained": retained}, ".cache")
    old_identity = article_hash(old_card.title, old_card.link)
    old_timestamp = "2026-01-01T00:00:00+00:00"
    Path(".cache/seen_articles.json").write_text(json.dumps({old_identity: old_timestamp}))
    card = snapshot.top_articles[0]
    identity = article_hash(card.title, card.link)
    dispatch_time = datetime.combine(target, datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=12)
    _, claim = claim_edition(result.ready_sha256, now=dispatch_time)
    with respx.mock() as router:
        route = router.post("https://api.telegram.org/bottest-token/sendMessage").mock(return_value=httpx.Response(
            200, json={"ok": True, "result": {"message_id": 17, "chat": {"id": 12345}}},
        ))
        delivered = await send_prepared_edition(result.ready_sha256, claim, config=config, now=dispatch_time)
        assert delivered.complete and delivered.delivered_hashes == {identity, old_identity}
        saved_cache = Path(".cache/seen_articles.json").read_bytes()
        repeated = await send_prepared_edition(result.ready_sha256, claim, config=config, now=dispatch_time)
        assert repeated == delivered and route.call_count == 1
        assert Path(".cache/seen_articles.json").read_bytes() == saved_cache
    persisted = load_stats(".cache")["Source"]
    assert persisted.total_fetches == persisted.successful_fetches == 2
    assert persisted.articles_included_in_digest == 1
    assert persisted.history[-1].date == target.isoformat()
    assert persisted.history[-1].articles_included == 1
    assert load_stats(".cache")["Retained"] == retained
    assert json.loads(Path(".cache/seen_articles.json").read_text())[old_identity] == old_timestamp


@pytest.mark.asyncio
async def test_implicit_day_crossing_midnight_retains_translation_archive_and_accepted_work_without_freezing(
    setup: tuple[SimpleNamespace, PreparationSnapshot],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from digest.application.supplement import pending_fragment
    from tests.test_translation import config as translation_config

    config, snapshot = setup
    start = datetime(2026, 10, 11, 23, 59, 59, tzinfo=timezone.utc)
    instant = start
    monkeypatch.setattr(
        "digest.edition_runtime.datetime",
        SimpleNamespace(now=lambda _zone: instant, combine=datetime.combine, min=datetime.min),
    )
    monkeypatch.setattr("digest.preparation._instant", lambda now: now or instant)
    monkeypatch.setattr("digest.application.prepared_delivery._instant", lambda now: now or instant)
    snapshot = bound_snapshot(snapshot, published=start + timedelta(days=1))
    settings = translation_config()
    config.translation, config.llm = settings.translation, settings.llm
    accepted = persist_accepted_preparation(snapshot, cache_dir=".cache")
    accepted_bytes = accepted.path.read_bytes()

    async def translate(role: Any, messages: list[dict[str, str]], *args: Any, **kwargs: Any) -> tuple[str, dict]:
        nonlocal instant
        fields = json.loads(messages[1]["content"])["fields"]
        instant = start + timedelta(seconds=2)
        return json.dumps(
            {"translations": [{"id": item["id"], "text": "Перевод: " + item["text"]} for item in fields]}
        ), {"finish_reason": "stop"}

    completion = AsyncMock(side_effect=translate)
    fragment_lookup = MagicMock(wraps=pending_fragment)
    monkeypatch.setattr("digest.translation.complete", completion)
    monkeypatch.setattr("digest.application.supplement.pending_fragment", fragment_lookup)
    with pytest.raises(ValueError, match=r"^Publication date must be today or a future UTC date\.$"):
        await present_preparation(accepted, config, execution=ModelExecution())
    completion.assert_awaited_once()
    fragment_lookup.assert_called_once_with(config.obsidian.output_dir, start.date())
    translation_path, = Path(".cache/translations").glob("*.json")
    cached = json.loads(translation_path.read_text())
    card = snapshot.top_articles[0]
    assert cached["status"] == "translated"
    assert cached["canonical"] == {
        "category_digest": snapshot.combined,
        f"article:{article_hash(card.title, card.link)}": card.summary,
    }
    assert cached["translated"] == {key: "Перевод: " + value for key, value in cached["canonical"].items()}
    archive = Path("digests/2026-10-11.md")
    assert archive.exists() and "date: 2026-10-11" in archive.read_text()
    assert (
        "Дата и время в ленте источника (UTC): 2026-10-12T23:59:59+00:00; "
        "позже даты этого выпуска (2026-10-11). Перевод: Canonical claim"
    ) in archive.read_text()
    assert json.loads(archive.with_suffix(".review.json").read_text()) == json.loads(
        _canonical(asdict(snapshot.review_report))
    )
    assert not Path("digests/2026-10-12.md").exists()
    assert accepted.path.read_bytes() == accepted_bytes
    assert not Path(".cache", READY_FILE).exists() and not Path(".cache", CLAIM_FILE).exists()
    assert not Path(".cache/seen_articles.json").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("translation", ["canonical", "translated", "main_canonical_fallback"])
async def test_selected_closing_is_final_identical_card_in_archive_and_frozen_edition(
    setup: tuple[SimpleNamespace, PreparationSnapshot],
    monkeypatch: pytest.MonkeyPatch,
    translation: str,
) -> None:
    execution = ModelExecution()
    from digest.delivery.telegram import escape_markdownv2
    from digest.radar.collector import article_hash
    from tests.test_translation import config as translation_config

    config, original = setup
    card = ArticleSummary("Closing title", "https://example.com/good", "Closing source", "World", "Volunteers helped.")
    today = datetime.now(timezone.utc).date()
    published_at = datetime.combine(today + timedelta(days=1), datetime.min.time(), timezone.utc)
    snapshot = bound_snapshot(original, card, main_feed=EA_FEED, published=published_at)
    decision = snapshot.closing
    canonical = _canonical(asdict(snapshot))

    async def translate(role: Any, messages: list[dict[str, str]], *args: Any, **kwargs: Any) -> tuple[str, dict]:
        request = json.loads(messages[1]["content"])
        assert request["target_language"] == "ru"
        fields = request["fields"]
        assert next(item["text"] for item in fields if item["id"] == "closing.summary") == card.summary
        assert next(item["text"] for item in fields if item["id"].startswith("article:")) == (
            original.top_articles[0].summary
        )
        assert NHS_CREDIT not in json.dumps(fields)
        assert EA_CREDIT not in json.dumps(fields)
        assert "Source feed timestamp" not in json.dumps(fields)
        assert "Дата и время в ленте источника" not in json.dumps(fields, ensure_ascii=False)
        return json.dumps(
            {"translations": [{"id": item["id"], "text": "Перевод: " + item["text"]} for item in fields]}
        ), {"finish_reason": "stop"}

    completion = AsyncMock(side_effect=translate if translation == "translated" else RuntimeError("offline fallback"))
    monkeypatch.setattr("digest.translation.complete", completion)
    if translation != "canonical":
        settings = translation_config()
        config.translation, config.llm = settings.translation, settings.llm
    else:
        config.radar.language = "ru"
    accepted = persist_accepted_preparation(snapshot, cache_dir=".cache")
    stats = preparation_stats(await present_preparation(accepted, config, execution=execution))
    frozen = json.loads(Path(".cache", READY_FILE).read_text())
    prefix = "Перевод: " if translation == "translated" else ""
    notice = (
        f"Дата и время в ленте источника (UTC): {published_at.isoformat()}; "
        f"позже даты этого выпуска ({today.isoformat()})."
    )
    presented = replace(card, summary=f"{prefix}{card.summary} {NHS_CREDIT}")
    published = [
        asdict(replace(
            original.top_articles[0], summary=f"{notice} {prefix}{original.top_articles[0].summary} {EA_CREDIT}"
        )),
        asdict(presented),
    ]
    assert frozen["presentation_metadata"]["cards"] == published
    assert [item["card"] for item in frozen["articles"]] == published
    assert frozen["canonical_metadata"]["cards"] == [asdict(original.top_articles[0])]
    assert frozen["canonical_metadata"]["closing"] == asdict(decision)
    assert frozen["presentation_metadata"]["closing"]["status"] == "presented"
    assert frozen["presentation_metadata"]["closing"]["reason"] == translation
    assert frozen["presentation_metadata"]["closing"]["card"] == asdict(presented)
    assert _canonical(asdict(snapshot)) == canonical
    assert completion.await_count == (0 if translation == "canonical" else 1)
    if translation == "main_canonical_fallback":
        assert "Translation incomplete or unavailable; canonical English text retained." in (
            frozen["presentation_metadata"]["combined"]
        )
    markdown = Path(stats.markdown_path).read_text()
    assert markdown.index(original.top_articles[0].title) < markdown.index(card.title)
    assert presented.summary in markdown and card.link in markdown
    assert published[0]["summary"] in markdown
    assert markdown.count(NHS_CREDIT) == 1
    assert markdown.count(EA_CREDIT) == 1
    assert _canonical(json.loads(Path(stats.review_checkpoint).read_text())) == _canonical(
        asdict(snapshot.review_report)
    )
    assert len(frozen["checkpoint_refs"]) == 5
    assert all(
        hashlib.sha256(Path(path).read_bytes()).hexdigest() == sha for path, sha in frozen["checkpoint_refs"].items()
    )
    closing = frozen["articles"][-1]
    assert closing["source"] == card.source and closing["full_hash"] == article_hash(card.title, card.link)
    assert set(closing["card"]) == {"title", "link", "source", "category", "summary"}
    assert len(closing["covering_chunks"]) == 1
    chunk = frozen["payloads"][closing["covering_chunks"][0]]["text"]
    assert escape_markdownv2(presented.summary) in chunk and escape_markdownv2(card.link) in chunk
    buttons = frozen["payloads"][closing["covering_chunks"][-1]]["reply_markup"]["inline_keyboard"]
    assert buttons[-1][0]["url"].endswith(f"vote_g_{closing['full_hash']}")
    ready_bytes = Path(".cache", READY_FILE).read_bytes()
    config.translation = SimpleNamespace(enabled=True)
    config.radar.language = "ru"
    config.review.enabled = False
    recovered = recover_preparation()
    assert isinstance(recovered, ExistingEdition)
    assert preparation_stats(recovered).ready_sha256 == stats.ready_sha256
    assert Path(".cache", READY_FILE).read_bytes() == ready_bytes


@pytest.mark.asyncio
async def test_unrenderable_closing_is_omitted_before_archive_and_freeze(
    setup: tuple[SimpleNamespace, PreparationSnapshot],
) -> None:
    execution = ModelExecution()
    config, snapshot = setup
    card = ArticleSummary("Unsafe optional", "https://example.com/" + "a" * 5000, "Closing source", "World", "Claim.")
    snapshot = bound_snapshot(snapshot, card)
    accepted = persist_accepted_preparation(snapshot, cache_dir=".cache")
    stats = preparation_stats(await present_preparation(accepted, config, execution=execution))
    frozen = json.loads(Path(".cache", READY_FILE).read_text())
    assert len(frozen["articles"]) == 1 and len(frozen["presentation_metadata"]["cards"]) == 1
    optional = frozen["presentation_metadata"]["closing"]
    assert optional["status"] == "incomplete" and optional["reason"] == "rendering_failed"
    assert optional["card"] is None
    publication = Path(stats.markdown_path).read_text().split("## Independent Blind Review")[0]
    assert card.title not in publication
    assert all(card.title not in payload["text"] for payload in frozen["payloads"])
    assert frozen["canonical_metadata"]["closing"]["card"] == asdict(card)


@pytest.mark.asyncio
async def test_invalid_main_rendering_and_required_archive_errors_are_not_optional_failures(
    setup: tuple[SimpleNamespace, PreparationSnapshot],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution = ModelExecution()
    config, original = setup
    closing = ArticleSummary("Closing", "https://example.com/good", "Closing source", "World", "Claim.")
    unsafe = replace(original.top_articles[0], link="https://example.com/" + "a" * 5000)
    unsafe_snapshot = bound_snapshot(replace(original, top_articles=[unsafe]), closing)
    accepted = persist_accepted_preparation(unsafe_snapshot, cache_dir=".cache")
    with pytest.raises(ValueError, match="URL exceeds"):
        await present_preparation(accepted, config, execution=execution)
    assert not Path("digests").exists() and not Path(".cache", READY_FILE).exists()
    monkeypatch.setattr("digest.delivery.write_digest", lambda *_args, **_kwargs: None)
    snapshot = bound_snapshot(original, closing)
    accepted = persist_accepted_preparation(snapshot, cache_dir=".cache")
    with pytest.raises(ValueError, match="archive persistence failed"):
        await present_preparation(accepted, config, execution=execution)
    assert not Path(".cache", READY_FILE).exists()


@pytest.mark.asyncio
async def test_omitted_closing_decision_is_frozen_without_changing_main_output(
    setup: tuple[SimpleNamespace, PreparationSnapshot],
) -> None:
    execution = ModelExecution()
    config, original = setup
    decision = ClosingDecision("unavailable", "no_suitable_item_in_packet")
    snapshot = bound_snapshot(replace(original, closing=decision))
    accepted = persist_accepted_preparation(snapshot, cache_dir=".cache")
    await present_preparation(accepted, config, execution=execution)
    frozen = json.loads(Path(".cache", READY_FILE).read_text())
    assert len(frozen["articles"]) == 1
    assert frozen["canonical_metadata"]["closing"] == asdict(decision)
    assert frozen["presentation_metadata"]["closing"]["status"] == "unavailable"
    assert frozen["presentation_metadata"]["closing"]["reason"] == decision.reason


@pytest.mark.asyncio
async def test_closing_alone_does_not_create_a_filler_edition(
    setup: tuple[SimpleNamespace, PreparationSnapshot],
) -> None:
    execution = ModelExecution()
    config, original = setup
    closer = replace(
        original.top_articles[0],
        title="Closing title",
        link="https://example.com/closing",
        source="NHS England",
        category="Health",
    )
    snapshot = replace(bound_snapshot(original, closer), top_articles=[])
    assert snapshot.closing is not None and snapshot.closing.card == closer
    accepted = persist_accepted_preparation(snapshot, cache_dir=".cache")
    stats = preparation_stats(await present_preparation(accepted, config, execution=execution))
    assert stats.edition_status == "no_ready"
    assert not Path(".cache", READY_FILE).exists() and not Path("digests").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["unknown_feed", "long_closing", "credit_crosses_boundary"])
async def test_unattributable_closing_is_omitted_and_only_unchanged_main_is_delivered(
    setup: tuple[SimpleNamespace, PreparationSnapshot],
    failure: str,
) -> None:
    execution = ModelExecution()
    from digest.closing import attribute_closing_card
    from digest.delivery.telegram import _render_compact_issue
    from digest.radar.collector import article_hash

    config, original = setup
    title = "Closing " + "long " * 900 if failure == "long_closing" else "Closing"
    card = ArticleSummary(title, "https://www.england.nhs.uk/good", "Closing source", "World", "Volunteers helped.")
    feed = "https://www.england.nhs.uk/unknown-feed/" if failure == "unknown_feed" else NHS_FEED
    if failure == "credit_crosses_boundary":
        original = replace(original, top_articles=[replace(original.top_articles[0], title="Evidence " * 398)])
    snapshot = bound_snapshot(original, card, closing_feed=feed)
    decision = snapshot.closing
    assert decision is not None
    if failure != "unknown_feed":
        attributed = attribute_closing_card(decision, card)
        assert attributed is not None
        _, ranges = _render_compact_issue([*original.top_articles, attributed], config, original.combined)
        assert len(ranges[-1].covering_chunks) > 1
        if failure == "credit_crosses_boundary":
            _, uncredited = _render_compact_issue([*original.top_articles, card], config, original.combined)
            assert len(uncredited[-1].covering_chunks) == 1
    expected_chunks, _ = _render_compact_issue(original.top_articles, config, original.combined)
    accepted = persist_accepted_preparation(snapshot, cache_dir=".cache")
    stats = preparation_stats(await present_preparation(accepted, config, execution=execution))
    manifest = json.loads(Path(".cache", READY_FILE).read_text())
    assert manifest["presentation_metadata"]["cards"] == [asdict(item) for item in original.top_articles]
    assert [item["card"] for item in manifest["articles"]] == [asdict(item) for item in original.top_articles]
    assert [item["text"] for item in manifest["payloads"]] == [chunk.text for chunk in expected_chunks]
    assert manifest["canonical_metadata"]["closing"] == asdict(decision)
    optional = manifest["presentation_metadata"]["closing"]
    assert optional["status"] == "incomplete" and optional["card"] is None
    assert optional["reason"] == ("attribution_unavailable" if failure == "unknown_feed" else "attribution_split")
    markdown = Path(stats.markdown_path).read_text().split("## Independent Blind Review")[0]
    assert card.title not in markdown and NHS_CREDIT not in markdown
    assert original.top_articles[0].summary in markdown
    assert await delivery_phase("claim", "config.yaml", stats.ready_sha256, None) == 0
    claim_sha = hashlib.sha256(Path(".cache", CLAIM_FILE).read_bytes()).hexdigest()
    with respx.mock(assert_all_called=True) as router:
        route = router.post("https://api.telegram.org/bottest-token/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 52, "chat": {"id": 12345}}}),
        )
        assert await delivery_phase("send", "config.yaml", stats.ready_sha256, claim_sha) == 0
        assert [json.loads(call.request.content) for call in route.calls] == manifest["payloads"]
    seen = json.loads(Path(".cache/seen_articles.json").read_text())
    main = original.top_articles[0]
    assert article_hash(main.title, main.link) in seen
    assert article_hash(card.title, card.link) not in seen
    feedback = load_feedback(".cache", strict=True)
    assert article_hash(card.title, card.link) not in feedback.article_source_map


@pytest.mark.asyncio
@pytest.mark.parametrize("feed,credit", [(NHS_FEED, NHS_CREDIT), (EA_FEED, EA_CREDIT)])
async def test_main_credit_uses_immutable_packet_even_after_source_settings_change(
    setup: tuple[SimpleNamespace, PreparationSnapshot],
    feed: str,
    credit: str,
) -> None:
    execution = ModelExecution()
    config, original = setup
    snapshot = bound_snapshot(original, main_feed=feed)
    evidence = {
        path: path.read_bytes()
        for directory in ("candidate_reports", "candidate_sources")
        for path in Path(".cache", directory).glob("*.json")
    }
    config.sources = [SourceConfig("Source", "https://changed.example/feed", "Tech", False)]
    accepted = persist_accepted_preparation(snapshot, cache_dir=".cache")
    stats = preparation_stats(await present_preparation(accepted, config, execution=execution))
    frozen = json.loads(Path(".cache", READY_FILE).read_text())
    card = original.top_articles[0]
    expected = asdict(replace(card, summary=f"{card.summary} {credit}"))
    assert frozen["presentation_metadata"]["cards"] == [expected]
    assert frozen["articles"][0]["card"] == expected
    assert frozen["articles"][0]["full_hash"] == article_hash(card.title, card.link)
    assert frozen["canonical_metadata"]["cards"] == [asdict(card)]
    assert "closing" not in frozen["canonical_metadata"]
    assert expected["summary"] in Path(stats.markdown_path).read_text()
    assert evidence == {path: path.read_bytes() for path in evidence}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", ["no_report", "missing_packet", "corrupt_packet", "missing_source", "changed_identity"]
)
async def test_missing_main_attribution_proof_retains_accepted_work_before_any_translation(
    setup: tuple[SimpleNamespace, PreparationSnapshot],
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    execution = ModelExecution()
    from tests.test_translation import config as translation_config

    config, original = setup
    snapshot = bound_snapshot(original, main_feed=NHS_FEED)
    config.sources = [SourceConfig("Source", NHS_FEED, "Tech", True)]
    settings = translation_config()
    config.translation, config.llm = settings.translation, settings.llm
    if failure == "no_report":
        snapshot = replace(snapshot, review_report=None)
    elif failure == "changed_identity":
        snapshot = replace(snapshot, top_articles=[replace(snapshot.top_articles[0], source="Changed source")])
    elif failure == "corrupt_packet":
        next(Path(".cache/candidate_reports").glob("*.json")).write_text("{}")
    else:
        directory = "candidate_reports" if failure == "missing_packet" else "candidate_sources"
        next(Path(".cache", directory).glob("*.json")).unlink()
    path = save_preparation(snapshot)
    accepted_bytes = path.read_bytes()
    accepted = load_accepted_preparation()
    assert accepted is not None
    translation = AsyncMock(side_effect=AssertionError("Attribution must resolve before translation"))
    monkeypatch.setattr("digest.translation.complete", translation)
    with pytest.raises(ValueError, match="attribution|candidate evidence|storage hash mismatch"):
        await present_preparation(accepted, config, execution=execution)
    translation.assert_not_awaited()
    assert path.read_bytes() == accepted_bytes and load_preparation() == snapshot
    assert not Path("digests").exists() and not Path(".cache", READY_FILE).exists()
    assert not Path(".cache", CLAIM_FILE).exists() and not Path(".cache/seen_articles.json").exists()


@pytest.mark.asyncio
async def test_corrupt_optional_packet_preserves_legacy_recovery_with_archive_disabled(
    setup: tuple[SimpleNamespace, PreparationSnapshot],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    execution = ModelExecution()
    config, original = setup
    config.obsidian.enabled = False
    config.sources = [SourceConfig("Source", "https://example.com/feed", "Tech", True)]
    snapshot = bound_snapshot(original)
    assert snapshot.closing is None
    save_preparation(snapshot)
    report_path = next(Path(".cache/candidate_reports").glob("*.json"))
    report_path.write_text("{}")
    translation = AsyncMock(side_effect=AssertionError("Legacy recovery needs no model call"))
    monkeypatch.setattr("digest.translation.complete", translation)
    recovered = recover_preparation()
    assert isinstance(recovered, AcceptedPreparation)
    stats = preparation_stats(await present_preparation(recovered, config, execution=execution))
    assert stats is not None and stats.edition_status == "ready"
    frozen = json.loads(Path(".cache", READY_FILE).read_text())
    expected = [asdict(card) for card in original.top_articles]
    assert frozen["canonical_metadata"]["cards"] == frozen["presentation_metadata"]["cards"] == expected
    assert [article["card"] for article in frozen["articles"]] == expected
    assert frozen["checkpoint_refs"] == {} and report_path.read_text() == "{}"
    assert "Legacy attribution audit unavailable" in caplog.text
    translation.assert_not_awaited()
    assert load_preparation() is None and not Path("digests").exists()
    assert not Path(".cache", CLAIM_FILE).exists() and not Path(".cache/seen_articles.json").exists()


@pytest.mark.asyncio
async def test_main_credit_crossing_chunk_boundary_holds_accepted_work_without_discarding_it(
    setup: tuple[SimpleNamespace, PreparationSnapshot],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution = ModelExecution()
    from digest.delivery.telegram import _render_compact_issue

    config, original = setup
    card = replace(original.top_articles[0], title="Evidence " * 406)
    snapshot = bound_snapshot(replace(original, top_articles=[card]), main_feed=NHS_FEED)
    _, uncredited = _render_compact_issue([card], config, original.combined)
    _, credited = _render_compact_issue(
        [replace(card, summary=f"{card.summary} {NHS_CREDIT}")], config, original.combined
    )
    assert len(uncredited[0].covering_chunks) == 1 and len(credited[0].covering_chunks) > 1
    path = save_preparation(snapshot)
    accepted_bytes = path.read_bytes()
    accepted = load_accepted_preparation()
    assert accepted is not None
    translation = AsyncMock(side_effect=AssertionError("Translation is disabled"))
    monkeypatch.setattr("digest.translation.complete", translation)
    with pytest.raises(ValueError, match="Main source attribution spans delivery chunks"):
        await present_preparation(accepted, config, execution=execution)
    translation.assert_not_awaited()
    assert path.read_bytes() == accepted_bytes and load_preparation() == snapshot
    assert not Path("digests").exists() and not Path(".cache", READY_FILE).exists()
    assert not Path(".cache", CLAIM_FILE).exists() and not Path(".cache/seen_articles.json").exists()


@pytest.mark.parametrize(("ordinary", "closing"), [(True, False), (True, True), (False, False)])
async def test_prepared_publication_owns_retry_opt_in_for_both_ordinary_branches(
    setup: tuple[SimpleNamespace, PreparationSnapshot], monkeypatch: pytest.MonkeyPatch,
    ordinary: bool, closing: bool,
) -> None:
    from digest.application.publication import assemble_publication
    from digest.irritator import IrritatorStatus
    from digest.translation import TranslationResult
    from tests.test_translation import config as translation_config

    config, original = setup
    settings = translation_config()
    config.translation, config.llm = settings.translation, settings.llm
    config.review.enabled = config.review.review_led_only = ordinary
    closer = ArticleSummary("Closing title", "https://example.com/good", "Closing source", "World", "Kindness.")
    snapshot = bound_snapshot(original, closer if closing else None)
    received: list[bool] = []

    async def translate(fields: dict[str, str], *args: Any, **kwargs: Any) -> TranslationResult:
        received.append(kwargs["retry_explicit_429"])
        optional = TranslationResult({"closing.summary": kwargs["closing_summary"]}, "translated") if closing else None
        return TranslationResult(fields, "translated", optional=optional)

    monkeypatch.setattr("digest.translation.translate_fields", translate)
    monkeypatch.setattr(
        "digest.application.investigation.run_irritator",
        AsyncMock(return_value=([], [], IrritatorStatus("Offline", "empty"))),
    )
    assembled = await assemble_publication(
        snapshot, config, publication_date=datetime.now(timezone.utc).date(), execution=ModelExecution(),
    )
    assert received == [ordinary]
    assert assembled.main_cards and (assembled.closing is not None) == closing


# Optional synthesis witnesses are synthetic contract fixtures, not news or model-quality evidence.
_SYNTHESIS_EXCERPTS = (
    "Queue A accepts payment requests during settlement outages and completes settlement only after recovery.",
    "Gateway B accepts invoice requests during ledger outages and replays them only after recovery.",
)


def _synthesis_accepted(
    monkeypatch: pytest.MonkeyPatch, *, language: str = "en", publication_date: date | None = None,
) -> tuple[Any, AcceptedPreparation]:
    import time

    from digest.config import ObsidianConfig, SynthesisConfig, TelegramConfig, TranslationConfig

    config = fixture_config()
    config.review.review_led_only = True
    config.review.tie_breaker = None
    config.synthesis = SynthesisConfig(True)
    config.telegram = TelegramConfig(enabled=True, delivery_mode="compact", bot_username="test_digest_bot")
    config.obsidian = ObsidianConfig(enabled=True, output_dir="digests")
    if language == "ru":
        config.translation = TranslationConfig(True, "ru", "gemini", "gemini-3.8-flash")
    config.sources = [
        SourceConfig("Synthetic source A", "https://example.com/rss", "Synthetic systems", True),
        SourceConfig("Synthetic source B", "https://example.org/rss", "Synthetic systems", True),
    ]
    cards = [
        ArticleSummary("Synthetic Queue A", "https://example.com/a", "Synthetic source A", "Synthetic systems",
                       "A toy queue accepts requests during an outage."),
        ArticleSummary("Synthetic Gateway B", "https://example.org/b", "Synthetic source B", "Synthetic systems",
                       "A toy gateway accepts requests during an outage."),
    ]
    instant = datetime.now(timezone.utc)
    articles = {"Synthetic systems": [
        Article(card.title, card.link, excerpt, card.source, card.category, instant)
        for card, excerpt in zip(cards, _SYNTHESIS_EXCERPTS, strict=True)
    ]}
    packet = plan_packet(merge_candidates(CandidateProgress(), articles, config, {}, now=instant), config, instant)
    assert packet is not None
    selections = [EvidenceSelection(item.evidence_id, "Synthetic reason", item.excerpt, "high")
                  for item in packet.evidence.items]
    review = ModelReview("primary", "test", "test", packet.evidence.bundle_id, packet.prompt_hash,
                         "ok", selections=selections)
    packet.report = BlindReviewReport(1, packet.evidence, [review], "incomplete", None, [], "Synthetic review")
    freeze_packet(packet, {}, ".cache")
    snapshot = PreparationSnapshot(cards, [], "Canonical notice", packet.report, 2, 2,
                                   ["Synthetic source A", "Synthetic source B"])
    accepted = persist_accepted_preparation(snapshot, cache_dir=".cache", publication_date=publication_date)
    monkeypatch.setenv("PREPARATION_DEADLINE", str(int(time.time()) + 440))
    return config, accepted


def _synthesis_response(messages: list[dict[str, str]], *, abstained: bool = False) -> str:
    task = json.loads(messages[1]["content"])
    language = task["publication_language"]
    comparison = {
        "relationship_type": "shared_mechanism",
        "supports": [
            {"evidence_id": item["evidence"]["evidence_id"], "occurrence_sha256": item["occurrence_sha256"],
             "field": "excerpt", "quote": item["evidence"]["excerpt"]}
            for item in task["selected_items"][:2]
        ],
        "canonical": {
            "relationship": "Both examples separate request acceptance from downstream work: requests are accepted "
                            "during an outage, while the dependent operation waits for recovery.",
            "qualification": "The excerpts describe behavior; neither supplies production-scale recovery "
                             "measurements or confirms end-to-end completion.",
        },
        "presentation": {
            "relationship": "Оба примера разделяют приём запросов и последующую обработку: запросы принимаются "
                            "во время сбоя, а зависимая операция откладывается до восстановления.",
            "qualification": "В отрывках описано поведение; ни один не содержит измерений восстановления "
                             "в промышленном масштабе или подтверждения сквозного завершения.",
        } if language == "ru" else None,
    }
    return json.dumps({"schema_version": 1, "publication_language": language,
                       "status": "abstained" if abstained else "comparison",
                       "abstention_reason": "no_supported_relationship" if abstained else None,
                       "comparison": None if abstained else comparison}, ensure_ascii=False)


async def _synthesis_completion(role: Any, messages: list[dict[str, str]], config: Any, **kwargs: Any) -> Any:
    from digest.llm import LLMRole

    assert role is LLMRole.SYNTHESIZE_EVIDENCE
    assert (kwargs["provider_override"].name, kwargs["provider_override"].model) == ("groq", "openai/gpt-oss-120b")
    assert config.llm.max_retries == 0
    assert kwargs["temperature"] == 0 and kwargs["max_output_tokens"] == 4096
    assert 0 < kwargs["request_timeout_seconds"] <= 60
    assert kwargs["reasoning_effort"] == "low"
    assert kwargs["response_format"]["json_schema"]["strict"] is True
    return _synthesis_response(messages), {"finish_reason": "stop", "resolved_model": "openai/gpt-oss-120b",
                                           "prompt_tokens": 800, "completion_tokens": 400}


@pytest.mark.asyncio
async def test_synthesis_is_bound_archived_and_frozen_without_sender_dependency(
    setup: tuple[SimpleNamespace, PreparationSnapshot], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from digest.presentation.telegram import render_compact_publication

    config, accepted = _synthesis_accepted(monkeypatch)
    complete = AsyncMock(side_effect=_synthesis_completion)
    monkeypatch.setattr("digest.application.synthesis.complete", complete)
    baseline = render_compact_publication(accepted.snapshot.top_articles, config, accepted.snapshot.combined)
    frozen = await present_preparation(accepted, config, execution=ModelExecution(), allow_new_synthesis=True)
    complete.assert_awaited_once()
    record = json.loads(Path(".cache/synthesis", accepted.sha256 + ".json").read_text())
    assert record["status"] == "valid"
    ready = json.loads(Path(".cache", READY_FILE).read_text())
    shown = ready["presentation_metadata"]["synthesis"]
    assert shown["status"] == "included"
    assert shown["text"] in frozen.archive.read_text()
    assert len(ready["payloads"]) == len(baseline.chunks) == 1
    assert ready["payloads"][0]["reply_markup"] == baseline.chunks[0].reply_markup
    assert [item["card"] for item in ready["articles"]] == [asdict(card) for card in accepted.snapshot.top_articles]
    assert all(item["covering_chunks"] == [0] for item in ready["articles"])
    assert not any("synthesis" in reference for reference in ready["checkpoint_refs"])
    task = json.loads(complete.call_args.args[1][1]["content"])
    assert task["ordered_selected_ids"] == [article_hash(card.title, card.link)
                                            for card in accepted.snapshot.top_articles]
    assert [item["evidence"]["excerpt"] for item in task["selected_items"]] == list(_SYNTHESIS_EXCERPTS)
    assert all("Synthetic reason" not in json.dumps(item) for item in task["selected_items"])
    # Sender owns frozen bytes; losing this preparation-only sidecar cannot reopen or block it.
    Path(".cache/synthesis", accepted.sha256 + ".json").unlink()
    assert await delivery_phase("claim", "config.yaml", frozen.ready_sha256, None) == 0
    claim_sha = hashlib.sha256(Path(".cache", CLAIM_FILE).read_bytes()).hexdigest()
    with respx.mock(assert_all_called=True) as router:
        route = router.post("https://api.telegram.org/bottest-token/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 70, "chat": {"id": 12345}}}),
        )
        assert await delivery_phase("send", "config.yaml", frozen.ready_sha256, claim_sha) == 0
        assert json.loads(route.calls[0].request.content) == ready["payloads"][0]
    complete.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["en", "ru"])
async def test_synthesis_archive_failure_reuses_exact_terminal_and_preserves_main_translation(
    setup: tuple[SimpleNamespace, PreparationSnapshot], monkeypatch: pytest.MonkeyPatch, language: str,
) -> None:
    from digest.delivery.markdown import write_digest

    config, accepted = _synthesis_accepted(monkeypatch, language=language)
    complete = AsyncMock(side_effect=_synthesis_completion)
    monkeypatch.setattr("digest.application.synthesis.complete", complete)
    presentation = AsyncMock(return_value=("Existing main fallback notice", accepted.snapshot.top_articles, []))
    monkeypatch.setattr("digest.application.presentation.publication_presentation", presentation)
    monkeypatch.setattr("digest.delivery.write_digest", lambda *args, **kwargs: None)
    with pytest.raises(ValueError, match="archive persistence"):
        await present_preparation(accepted, config, execution=ModelExecution(), allow_new_synthesis=True)
    assert load_accepted_preparation() == accepted
    monkeypatch.setattr("digest.delivery.write_digest", write_digest)
    await present_preparation(accepted, config, execution=ModelExecution())
    complete.assert_awaited_once()
    ready = json.loads(Path(".cache", READY_FILE).read_text())
    assert ready["presentation_metadata"]["combined"] == "Existing main fallback notice"
    assert ready["presentation_metadata"]["synthesis"]["reason"] == "reused"
    assert all(call.args[0] == accepted.snapshot.combined for call in presentation.await_args_list)
    assert all(call.args[1] == accepted.snapshot.top_articles for call in presentation.await_args_list)
    assert ready["presentation_metadata"]["synthesis"]["result"]["publication_language"] == language


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["recovery", "disabled", "missing_deadline", "bad_deadline", "expired_deadline",
                                   "wrong_route", "reading", "category", "russian_canonical", "no_packet",
                                   "budget", "held", "pace"])
async def test_synthesis_omission_never_calls_or_reopens(
    setup: tuple[SimpleNamespace, PreparationSnapshot], monkeypatch: pytest.MonkeyPatch, case: str,
) -> None:
    import time

    from digest.application.synthesis import prepare_synthesis
    from digest.config import ReadingBriefConfig, ReviewModelConfig, SynthesisConfig

    config, accepted = _synthesis_accepted(monkeypatch)
    execution = ModelExecution()
    allow_new = True
    if case == "recovery":
        allow_new = False
    elif case == "disabled":
        config.synthesis = SynthesisConfig(False)
    elif case == "missing_deadline":
        monkeypatch.delenv("PREPARATION_DEADLINE")
    elif case == "bad_deadline":
        monkeypatch.setenv("PREPARATION_DEADLINE", "1e20")
    elif case == "expired_deadline":
        monkeypatch.setenv("PREPARATION_DEADLINE", str(int(time.time()) + 40))
    elif case == "wrong_route":
        config.review.secondary = ReviewModelConfig("gemini", "gemini-3.8-flash")
    elif case == "reading":
        config.reading_brief = ReadingBriefConfig(True)
    elif case == "category":
        config.review.review_led_only = False
    elif case == "russian_canonical":
        config.radar.language = "ru"
    elif case == "no_packet":
        next(Path(".cache/candidate_reports").glob("*.json")).unlink()
    elif case == "budget":
        execution.request_state(config.llm).request_limit = 0
    elif case == "held":
        execution.request_state(config.llm).unavailable_until[("groq", "openai/gpt-oss-120b")] = time.monotonic() + 1
    elif case == "pace":
        execution.request_state(config.llm).next_request_at = time.monotonic() + 61
    complete = AsyncMock(side_effect=AssertionError("No synthesis dispatch"))
    monkeypatch.setattr("digest.application.synthesis.complete", complete)
    outcome = await prepare_synthesis(accepted, config, publication_date=datetime.now(timezone.utc).date(),
                                     execution=execution, allow_new=allow_new)
    assert outcome.result is None
    complete.assert_not_awaited()
    if case == "disabled":
        assert not Path(".cache/synthesis").exists()
    else:
        sidecar = Path(".cache/synthesis", accepted.sha256 + ".json")
        assert json.loads(sidecar.read_text())["status"] == "skipped"
        before = sidecar.read_bytes()
        # Retaining a terminal disposition never creates a second attempt.
        await prepare_synthesis(accepted, config, publication_date=datetime.now(timezone.utc).date(),
                                execution=execution, allow_new=True)
        assert sidecar.read_bytes() == before
        complete.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["abstained", "invalid", "unknown_finish", "provider_failed", "timeout", "cancelled",
                                   "reserve_failed", "terminal_failed", "reserved", "malformed", "oversized"])
async def test_synthesis_failure_boundaries_retain_terminal_attempt(
    setup: tuple[SimpleNamespace, PreparationSnapshot], monkeypatch: pytest.MonkeyPatch, case: str,
) -> None:
    import asyncio

    from digest.adapters.storage import synthesis as storage
    from digest.application.synthesis import _request, prepare_synthesis

    config, accepted = _synthesis_accepted(monkeypatch)
    day = datetime.now(timezone.utc).date()
    if case in {"reserved", "malformed"}:
        record, _ = _request(accepted, day, config, ".cache")
        storage.reserve_attempt(record, ".cache")
        if case == "malformed":
            Path(".cache/synthesis", accepted.sha256 + ".json").write_text('{"schema_version":false}')
    if case == "reserve_failed":
        monkeypatch.setattr(storage, "reserve_attempt", MagicMock(side_effect=OSError("fixture")))
    if case == "terminal_failed":
        monkeypatch.setattr(storage, "finish_attempt", MagicMock(side_effect=OSError("fixture")))

    async def response(role: Any, messages: list[dict[str, str]], config: Any, **kwargs: Any) -> Any:
        if case == "provider_failed":
            raise RuntimeError("Do not retain arbitrary provider exception text")
        if case == "timeout":
            raise TimeoutError
        if case == "cancelled":
            raise asyncio.CancelledError
        text = "{" if case == "invalid" else _synthesis_response(messages, abstained=case == "abstained")
        if case == "oversized":
            text = "x" * 16001
        return text, {"finish_reason": None if case == "unknown_finish" else "stop"}

    complete = AsyncMock(side_effect=response)
    monkeypatch.setattr("digest.application.synthesis.complete", complete)
    if case == "cancelled":
        with pytest.raises(asyncio.CancelledError):
            await prepare_synthesis(accepted, config, publication_date=day, execution=ModelExecution(), allow_new=True)
    else:
        outcome = await prepare_synthesis(accepted, config, publication_date=day,
                                         execution=ModelExecution(), allow_new=True)
        assert outcome.result is None
    assert complete.await_count == (0 if case in {"reserve_failed", "reserved", "malformed"} else 1)
    sidecar = Path(".cache/synthesis", accepted.sha256 + ".json")
    if sidecar.exists():
        before = sidecar.read_bytes()
        assert b"Do not retain arbitrary" not in before
        complete.reset_mock()
        await prepare_synthesis(accepted, config, publication_date=day, execution=ModelExecution(), allow_new=True)
        assert sidecar.read_bytes() == before
        complete.assert_not_awaited()


@pytest.mark.asyncio
async def test_disabled_synthesis_ignores_valid_capture_and_preserves_exact_baseline(
    setup: tuple[SimpleNamespace, PreparationSnapshot], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from digest.application.synthesis import prepare_synthesis
    from digest.config import SynthesisConfig
    from digest.presentation.telegram import render_compact_publication

    config, accepted = _synthesis_accepted(monkeypatch)
    complete = AsyncMock(side_effect=_synthesis_completion)
    monkeypatch.setattr("digest.application.synthesis.complete", complete)
    await prepare_synthesis(accepted, config, publication_date=datetime.now(timezone.utc).date(),
                            execution=ModelExecution(), allow_new=True)
    config.synthesis = SynthesisConfig(False)
    baseline = render_compact_publication(accepted.snapshot.top_articles, config, accepted.snapshot.combined)
    before = Path(".cache/synthesis", accepted.sha256 + ".json").read_bytes()
    monkeypatch.setattr("digest.application.synthesis.storage.load_attempt", MagicMock(side_effect=AssertionError))
    await present_preparation(accepted, config, execution=ModelExecution(), allow_new_synthesis=True)
    ready = json.loads(Path(".cache", READY_FILE).read_text())
    assert "synthesis" not in ready["presentation_metadata"]
    assert [payload["text"] for payload in ready["payloads"]] == [chunk.text for chunk in baseline.chunks]
    assert Path(".cache/synthesis", accepted.sha256 + ".json").read_bytes() == before
    complete.assert_awaited_once()


@pytest.mark.parametrize("raw, expected", [(None, None), ("", None), ("-1", None), ("1e3", None),
                                           ("1" * 21, None), ("1045", None), ("1050", 105.0),
                                           ("1105", 160.0), ("99999999999999999999", 160.0)])
def test_synthesis_fixed_deadline_has_independent_45_second_tail(
    monkeypatch: pytest.MonkeyPatch, raw: str | None, expected: float | None,
) -> None:
    from digest.application import synthesis

    monkeypatch.setattr(synthesis.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(synthesis.time, "time", lambda: 1000.0)
    if raw is None:
        monkeypatch.delenv("PREPARATION_DEADLINE", raising=False)
    else:
        monkeypatch.setenv("PREPARATION_DEADLINE", raw)
    assert synthesis.optional_deadline() == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("extend_cooldown", [False, True])
async def test_synthesis_real_complete_shares_spent_cap_pacing_and_cooldown(
    setup: tuple[SimpleNamespace, PreparationSnapshot], monkeypatch: pytest.MonkeyPatch, extend_cooldown: bool,
) -> None:
    from digest import llm
    from digest.application import synthesis

    config, accepted = _synthesis_accepted(monkeypatch)
    config.llm.max_retries = 5
    config.llm.min_request_interval_seconds = 5
    execution = ModelExecution()
    state = execution.request_state(config.llm)
    state.requests_attempted = 4  # Primary fallback 2 + main explicit-429 recovery 2 already spent.
    state.request_limit = 9
    state.next_request_at = 125.0
    state.last_request_at = 120.0
    now = [100.0]
    waits = []
    monkeypatch.setattr(synthesis.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(synthesis.time, "time", lambda: 1000.0 + now[0] - 100.0)
    monkeypatch.setenv("PREPARATION_DEADLINE", "1100")
    monkeypatch.setattr(llm, "set_request_limit", MagicMock(side_effect=AssertionError("No cap reset")))
    monkeypatch.setattr(llm, "count_gemini_tokens", AsyncMock(side_effect=AssertionError("No token count")))

    async def sleep(seconds: float) -> None:
        waits.append(seconds)
        now[0] += seconds
        if extend_cooldown:
            state.unavailable_until[("groq", "openai/gpt-oss-120b")] = now[0] + 1

    async def provider(client: Any, selected: Any, messages: Any, temperature: Any,
                       max_output_tokens: Any, **kwargs: Any) -> Any:
        assert now[0] == 125 and state.requests_attempted == 5
        assert kwargs["request_timeout_seconds"] <= 30
        now[0] += 1
        return _synthesis_response(messages), {"finish_reason": "stop", "resolved_model": selected.model}

    monkeypatch.setattr(synthesis.asyncio, "sleep", sleep)
    physical = AsyncMock(side_effect=provider)
    monkeypatch.setattr(llm, "_call_provider", physical)
    outcome = await synthesis.prepare_synthesis(accepted, config, publication_date=datetime.now(timezone.utc).date(),
                                                execution=execution, allow_new=True)
    assert waits == [25.0]
    assert state is execution.request_state(config.llm)
    assert state.request_limit == 9 and config.llm.max_retries == 5
    assert state.requests_attempted == (4 if extend_cooldown else 5)
    assert physical.await_count == (0 if extend_cooldown else 1)
    assert (outcome.result is not None) is not extend_cooldown


@pytest.mark.asyncio
async def test_synthesis_async_window_covers_provider_wait(
    setup: tuple[SimpleNamespace, PreparationSnapshot], monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    import time

    from digest.application import synthesis

    config, accepted = _synthesis_accepted(monkeypatch)
    monkeypatch.setattr(synthesis, "optional_deadline", lambda: time.monotonic() + 0.05)
    completed = []

    async def slow(*args: Any, **kwargs: Any) -> Any:
        await asyncio.sleep(0.2)
        completed.append(True)
        raise AssertionError("Optional provider wait escaped its timeout")

    monkeypatch.setattr(synthesis, "complete", slow)
    outcome = await synthesis.prepare_synthesis(accepted, config, publication_date=datetime.now(timezone.utc).date(),
                                                execution=ModelExecution(), allow_new=True)
    assert outcome.reason == "timed_out" and not completed
    assert json.loads(Path(".cache/synthesis", accepted.sha256 + ".json").read_text())["status"] == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["request", "reservation", "response", "terminal"])
async def test_synthesis_sync_boundary_overrun_omits_without_renewing_window(
    setup: tuple[SimpleNamespace, PreparationSnapshot], monkeypatch: pytest.MonkeyPatch, boundary: str,
) -> None:
    from digest.application import synthesis

    config, accepted = _synthesis_accepted(monkeypatch)
    now = [100.0]
    monkeypatch.setattr(synthesis.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(synthesis.time, "time", lambda: 1000.0 + now[0] - 100.0)
    monkeypatch.setenv("PREPARATION_DEADLINE", "1105")
    target, name = {
        "request": (synthesis, "_request"),
        "reservation": (synthesis.storage, "reserve_attempt"),
        "terminal": (synthesis.storage, "finish_attempt"),
    }.get(boundary, (None, None))
    if target is not None:
        original = getattr(target, name)

        def slow_local(*args: Any, **kwargs: Any) -> Any:
            value = original(*args, **kwargs)
            now[0] = 160.0
            return value

        monkeypatch.setattr(target, name, slow_local)

    async def provider(*args: Any, **kwargs: Any) -> Any:
        result = await _synthesis_completion(*args, **kwargs)
        if boundary == "response":
            now[0] = 160.0
        return result

    complete = AsyncMock(side_effect=provider)
    monkeypatch.setattr(synthesis, "complete", complete)
    outcome = await synthesis.prepare_synthesis(accepted, config, publication_date=datetime.now(timezone.utc).date(),
                                                execution=ModelExecution(), allow_new=True)
    assert outcome.result is None and outcome.reason == "deadline_exhausted"
    assert complete.await_count == (0 if boundary in {"request", "reservation"} else 1)
    assert Path(".cache/synthesis", accepted.sha256 + ".json").exists()


@pytest.mark.parametrize("change", ["extra_chunk", "article_identity", "article_source", "article_coverage",
                                    "supplement", "keyboard", "split_synthesis", "missing_synthesis"])
def test_synthesis_full_gate_rejects_each_changed_delivery_obligation(change: str) -> None:
    from copy import deepcopy

    from digest.config import TelegramConfig
    from digest.edition_runtime import _same_synthesis_layout
    from digest.presentation.telegram import SynthesisPlacement, render_compact_publication

    config = fixture_config()
    config.telegram = TelegramConfig(bot_username="test_bot", delivery_mode="compact")
    cards = [ArticleSummary("A", "https://example.com/a", "A", "Tech", "Plain description")]
    baseline = render_compact_publication(cards, config, "Notice")
    candidate = render_compact_publication(cards, config, "Notice", synthesis=SynthesisPlacement("Comparison", 1))
    assert _same_synthesis_layout(baseline, candidate)
    if change == "extra_chunk":
        candidate.chunks.append(deepcopy(candidate.chunks[0]))
    elif change == "article_identity":
        candidate.articles[0].full_hash = "0" * 32
    elif change == "article_source":
        candidate.articles[0].source = "Changed"
    elif change == "article_coverage":
        candidate.articles[0].covering_chunks = (1,)
    elif change == "supplement":
        from digest.domain.delivery.supplement import SupplementCoverage

        candidate = replace(candidate, supplement=SupplementCoverage("changed", (0,)))
    elif change == "keyboard":
        candidate.chunks[0].reply_markup = None
    else:
        candidate = replace(candidate, synthesis=(0, 1) if change == "split_synthesis" else None)
    assert not _same_synthesis_layout(baseline, candidate)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["language", "model", "prompt"])
async def test_synthesis_changed_request_never_reopens_retained_attempt(
    setup: tuple[SimpleNamespace, PreparationSnapshot], monkeypatch: pytest.MonkeyPatch, change: str,
) -> None:
    from digest.application import synthesis
    from digest.config import ReviewModelConfig, TranslationConfig
    from digest.domain.editorial import synthesis as contract

    config, accepted = _synthesis_accepted(monkeypatch)
    complete = AsyncMock(side_effect=_synthesis_completion)
    monkeypatch.setattr(synthesis, "complete", complete)
    day = datetime.now(timezone.utc).date()
    first = await synthesis.prepare_synthesis(accepted, config, publication_date=day,
                                              execution=ModelExecution(), allow_new=True)
    assert first.result is not None
    path = Path(".cache/synthesis", accepted.sha256 + ".json")
    before = path.read_bytes()
    if change == "language":
        config.translation = TranslationConfig(True, "ru", "gemini", "gemini-3.8-flash")
    elif change == "model":
        config.review.secondary = ReviewModelConfig("groq", "other")
    else:
        monkeypatch.setattr(contract, "SYSTEM", contract.SYSTEM + " Changed prompt.")
    second = await synthesis.prepare_synthesis(accepted, config, publication_date=day,
                                               execution=ModelExecution(), allow_new=True)
    assert second.result is None and path.read_bytes() == before
    complete.assert_awaited_once()


@pytest.mark.asyncio
async def test_synthesis_real_chunk_overflow_uses_exact_baseline_payloads(
    setup: tuple[SimpleNamespace, PreparationSnapshot], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from digest.presentation.telegram import render_compact_publication

    config, accepted = _synthesis_accepted(monkeypatch)
    cards = [replace(accepted.snapshot.top_articles[0], summary="x" * 3000), accepted.snapshot.top_articles[1]]
    accepted = persist_accepted_preparation(replace(accepted.snapshot, top_articles=cards), cache_dir=".cache")
    baseline = render_compact_publication(cards, config, accepted.snapshot.combined)
    complete = AsyncMock(side_effect=_synthesis_completion)
    monkeypatch.setattr("digest.application.synthesis.complete", complete)
    await present_preparation(accepted, config, execution=ModelExecution(), allow_new_synthesis=True)
    ready = json.loads(Path(".cache", READY_FILE).read_text())
    assert ready["presentation_metadata"]["synthesis"]["reason"] == "layout_rejected"
    assert [payload["text"] for payload in ready["payloads"]] == [chunk.text for chunk in baseline.chunks]
    assert [payload.get("reply_markup") for payload in ready["payloads"]] == [
        chunk.reply_markup for chunk in baseline.chunks
    ]
    complete.assert_awaited_once()


@pytest.mark.asyncio
async def test_baseline_required_failure_precedes_any_optional_attempt(
    setup: tuple[SimpleNamespace, PreparationSnapshot], monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, accepted = _synthesis_accepted(monkeypatch)
    cards = [replace(accepted.snapshot.top_articles[0], summary="https://example.com/" + "x" * 4000),
             accepted.snapshot.top_articles[1]]
    accepted = persist_accepted_preparation(replace(accepted.snapshot, top_articles=cards), cache_dir=".cache")
    complete = AsyncMock(side_effect=AssertionError("Required baseline must finish first"))
    monkeypatch.setattr("digest.application.synthesis.complete", complete)
    with pytest.raises(ValueError):
        await present_preparation(accepted, config, execution=ModelExecution(), allow_new_synthesis=True)
    complete.assert_not_awaited()
    assert load_accepted_preparation() == accepted
    assert not Path(".cache/synthesis").exists() and not Path("digests").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("with_closer", [False, True])
async def test_synthesis_full_pending_supplement_preflight_and_freeze_agree(
    setup: tuple[SimpleNamespace, PreparationSnapshot], monkeypatch: pytest.MonkeyPatch, with_closer: bool,
) -> None:
    from digest.application.publication import PublicationAssembly
    from digest.irritator import IrritatorStatus
    from digest.presentation.telegram import SupplementPlacement, render_compact_publication
    from digest.translation import ClosingPresentation
    from tests.test_post_delivery import pending_supplement

    # The supplement is a real immutable bound projection. Its prior confirmed
    # edition is retained, and the next day's accepted work remains separate.
    pending = await pending_supplement(monkeypatch)
    day = datetime.fromisoformat(pending.fragment.origin.publication_day).date() + timedelta(days=1)
    config, accepted = _synthesis_accepted(monkeypatch, publication_date=day)
    closer = ArticleSummary("Synthetic closer", "https://example.com/closer", "Closer source", "Kindness",
                            "Synthetic optional closing card.") if with_closer else None
    # Assembly is the already-tested required attribution/translation boundary;
    # this test isolates the new coordinator's complete baseline composition.
    publication_snapshot = replace(
        accepted.snapshot, closing=ClosingDecision("selected", "selected", closer),
    ) if closer is not None else accepted.snapshot
    publication = PublicationAssembly(
        publication_snapshot, accepted.snapshot.combined, tuple(accepted.snapshot.top_articles), (),
        IrritatorStatus("Deferred", "deferred"),
        ClosingPresentation("selected", "selected", closer) if closer is not None else None,
    )
    monkeypatch.setattr("digest.application.publication.assemble_publication", AsyncMock(return_value=publication))
    complete = AsyncMock(side_effect=_synthesis_completion)
    monkeypatch.setattr("digest.application.synthesis.complete", complete)
    placement = SupplementPlacement(pending.fragment.fragment_id, pending.fragment.text, 2)
    baseline = render_compact_publication(publication.cards, config, publication.combined, placement)
    preflights = []
    original = render_compact_publication

    def capture(*args: Any, **kwargs: Any) -> Any:
        rendered = original(*args, **kwargs)
        if kwargs.get("synthesis") is not None:
            preflights.append(rendered)
        return rendered

    monkeypatch.setattr("digest.presentation.telegram.render_compact_publication", capture)
    monkeypatch.setattr("digest.application.prepared_delivery.render_compact_publication", capture)
    outcome = await present_preparation(accepted, config, execution=ModelExecution(),
                                        publication_date=day, allow_new_synthesis=True)
    ready = json.loads(Path(".cache", READY_FILE).read_text())
    shown = ready["presentation_metadata"]["synthesis"]
    assert shown["status"] == "included"
    assert len(preflights) == 2
    assert preflights[0] == preflights[1]
    assert len(ready["payloads"]) == len(baseline.chunks)
    assert [item["covering_chunks"] for item in ready["articles"]] == [list(item.covering_chunks)
                                                                        for item in baseline.articles]
    assert ready["supplement"]["coverage"] == asdict(baseline.supplement) | {"covering_chunks": [0]}
    archive = outcome.archive.read_text()
    assert archive.index(shown["text"]) < archive.index(pending.fragment.text)
    if closer is not None:
        assert archive.index(pending.fragment.text) < archive.index(closer.title)
        assert ready["articles"][-1]["card"]["title"] == closer.title
    complete.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("envelope", [
    [], "wrong", {"choices": {"wrong": []}}, {"choices": [None]}, {"choices": [{"message": None}]},
    {"choices": [{"message": {"content": ["not prose"]}, "finish_reason": "stop"}], "usage": {}},
    {"choices": [{"message": {"content": 123}, "finish_reason": "stop"}], "usage": {}},
])
async def test_synthesis_malformed_http_success_is_one_failed_attempt_and_baseline_freezes(
    setup: tuple[SimpleNamespace, PreparationSnapshot], monkeypatch: pytest.MonkeyPatch, envelope: Any,
) -> None:
    from digest.application.synthesis import prepare_synthesis
    from digest.presentation.telegram import render_compact_publication

    config, accepted = _synthesis_accepted(monkeypatch)
    config.llm.max_retries = 5  # The optional settings copy still permits only one physical attempt.
    monkeypatch.setenv("GROQ_API_KEY", "synthetic-test-key")
    baseline = render_compact_publication(accepted.snapshot.top_articles, config, accepted.snapshot.combined)
    execution = ModelExecution()
    with respx.mock(assert_all_called=True) as router:
        route = router.post("https://api.groq.com/openai/v1/chat/completions").mock(
            return_value=httpx.Response(200, json=envelope),
        )
        await present_preparation(accepted, config, execution=execution, allow_new_synthesis=True)
        assert route.call_count == 1 and execution.request_state(config.llm).requests_attempted == 1
        # The same retained attempt cannot acquire another physical request.
        outcome = await prepare_synthesis(accepted, config, publication_date=datetime.now(timezone.utc).date(),
                                         execution=execution, allow_new=True)
        assert outcome.result is None and route.call_count == 1
    ready = json.loads(Path(".cache", READY_FILE).read_text())
    assert ready["presentation_metadata"]["synthesis"]["status"] == "failed"
    assert [payload["text"] for payload in ready["payloads"]] == [chunk.text for chunk in baseline.chunks]
    record = json.loads(Path(".cache/synthesis", accepted.sha256 + ".json").read_text())
    assert record["status"] == "failed" and record["response_text"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("completion", [(123, {}), ("text", []), (None, None)])
async def test_synthesis_rejects_wrong_completion_tuple_shapes_before_capture(
    setup: tuple[SimpleNamespace, PreparationSnapshot], monkeypatch: pytest.MonkeyPatch, completion: Any,
) -> None:
    from digest.application.synthesis import prepare_synthesis

    config, accepted = _synthesis_accepted(monkeypatch)
    complete = AsyncMock(return_value=completion)
    monkeypatch.setattr("digest.application.synthesis.complete", complete)
    result = await prepare_synthesis(accepted, config, publication_date=datetime.now(timezone.utc).date(),
                                     execution=ModelExecution(), allow_new=True)
    assert result.status == "failed" and result.reason == "response_invalid"
    complete.assert_awaited_once()
    assert json.loads(Path(".cache/synthesis", accepted.sha256 + ".json").read_text())["status"] == "failed"
