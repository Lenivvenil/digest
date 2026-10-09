"""Offline end-to-end preparation/sender boundary and failure persistence."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, replace
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
from digest.edition_runtime import delivery_phase, finish_preparation, resume_preparation
from digest.feedback import FeedbackStore, load_feedback, save_feedback
from digest.preparation import PreparationSnapshot, _canonical, load_preparation, save_preparation
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
            Article(card.title, card.link, "Raw evidence.", card.source, card.category, NOW)
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
        prepared = await finish_preparation(
            snapshot, config, execution=ModelExecution(), publication_date=next_day.date()
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
        result = await finish_preparation(snapshot, config, execution=ModelExecution(), publication_date=day)
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
    )
    card = ArticleSummary("Source title", "https://example.com/article", "Source", "Tech", "Canonical claim")
    snapshot = PreparationSnapshot([card], [], "Canonical notice", None, 1, 1, ["Source"])
    save_feedback(FeedbackStore(), ".cache", strict=True)
    monkeypatch.setattr("digest.config.load_config", lambda _: config)
    return config, snapshot


@pytest.mark.asyncio
async def test_accepted_analysis_survives_presentation_failure_and_sender_uses_frozen_bytes(
    setup: tuple[SimpleNamespace, PreparationSnapshot], monkeypatch: pytest.MonkeyPatch
) -> None:
    execution = ModelExecution()
    config, snapshot = setup
    save_preparation(snapshot)
    presentation = AsyncMock(side_effect=RuntimeError("presentation failed"))
    monkeypatch.setattr("digest.application.presentation.publication_presentation", presentation)
    with pytest.raises(RuntimeError):
        await resume_preparation(config, 0, verbose=False, execution=execution)
    assert asdict(load_preparation()) == asdict(snapshot)
    assert not Path(".cache", CLAIM_FILE).exists()
    assert not Path(".cache/seen_articles.json").exists()
    presentation.side_effect = None
    presentation.return_value = ("Rendered notice", snapshot.top_articles, [])
    stats = await resume_preparation(config, 0, verbose=False, execution=execution)
    assert stats.edition_status == "ready"
    frozen = json.loads(Path(".cache", READY_FILE).read_text())
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
    assert (await resume_preparation(config, 0, verbose=False, execution=execution)).ready_sha256 == stats.ready_sha256
    assert await delivery_phase("claim", "config.yaml", stats.ready_sha256, None) == 0
    import hashlib

    claim_sha = hashlib.sha256(Path(".cache", CLAIM_FILE).read_bytes()).hexdigest()
    current = load_feedback(".cache", strict=True)
    current.last_update_id = 999
    save_feedback(current, ".cache", strict=True)
    with respx.mock(assert_all_called=True) as router:
        route = router.post("https://api.telegram.org/bottest-token/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 52, "chat": {"id": 12345}}}),
        )
        assert await delivery_phase("send", "config.yaml", stats.ready_sha256, claim_sha) == 0
        assert json.loads(route.calls[0].request.content) == frozen["payloads"][0]
    assert load_feedback(".cache", strict=True).last_update_id == 999
    assert load_feedback(".cache", strict=True).article_source_map
    assert json.loads(Path(".cache/seen_articles.json").read_text())
    assert inspect_edition()[2] == "confirmed"
    assert (await resume_preparation(config, 0, verbose=False, execution=execution)).edition_status == "confirmed"
    with respx.mock(assert_all_called=False) as router:
        assert await delivery_phase("send", "config.yaml", stats.ready_sha256, claim_sha) == 0
        assert not router.calls


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
    stats = await finish_preparation(snapshot, config, execution=execution)
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
    assert (await finish_preparation(snapshot, config, execution=execution)).edition_status == "no_ready"
    assert not Path(".cache", READY_FILE).exists()
    snapshot.top_articles.append(ArticleSummary("Title", "https://example.com/x", "Source", "Tech", "Claim"))
    stats = await finish_preparation(snapshot, config, execution=execution)
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
    stats = await resume_preparation(config, 0, verbose=False, execution=execution)
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

    from digest.delivery.telegram import IssueDeliveryResult
    from digest.edition_runtime import _merge_delivery
    from digest.radar.collector import article_hash
    from digest.source_scorer import DailySnapshot, SourceStats, load_stats, save_stats

    config, snapshot = setup
    today = datetime.now(timezone.utc).date()
    target = today + timedelta(days=1)
    result = await finish_preparation(snapshot, config, publication_date=target, execution=execution)
    assert result.edition_status == "pending_window"
    archive = Path("digests", f"{target.isoformat()}.md")
    assert archive.exists() and f"date: {target.isoformat()}" in archive.read_text()
    assert inspect_edition()[2] == "pending_window"
    with pytest.raises(ValueError):
        await delivery_phase("claim", "config.yaml", result.ready_sha256, None)
    assert not Path(".cache", CLAIM_FILE).exists()

    # Delivery-day inclusion does not invent an additional fetch observation.
    source = SourceStats(
        "Source", total_fetches=2, successful_fetches=2, history=[DailySnapshot(today.isoformat(), 3, 0, True)]
    )
    save_stats({"Source": source}, ".cache")
    card = snapshot.top_articles[0]
    identity = article_hash(card.title, card.link)
    receipt = IssueDeliveryResult(
        sent=1,
        outcome="sent",
        total_chunks=1,
        confirmed_chunks=1,
        delivered_hashes={identity},
        article_source_map={identity[:8]: "Source"},
    )
    manifest = json.loads(Path(".cache", READY_FILE).read_text())
    _merge_delivery(receipt, manifest, config)
    _merge_delivery(receipt, manifest, config)
    persisted = load_stats(".cache")["Source"]
    assert persisted.total_fetches == persisted.successful_fetches == 2
    assert persisted.articles_included_in_digest == 1
    assert persisted.history[-1].date == target.isoformat()
    assert persisted.history[-1].articles_included == 1


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
    snapshot = bound_snapshot(original, card, main_feed=EA_FEED)
    decision = snapshot.closing
    canonical = _canonical(asdict(snapshot))

    async def translate(role: Any, messages: list[dict[str, str]], *args: Any, **kwargs: Any) -> tuple[str, dict]:
        fields = json.loads(messages[1]["content"])["fields"]
        assert next(item["text"] for item in fields if item["id"] == "closing.summary") == card.summary
        assert NHS_CREDIT not in json.dumps(fields)
        assert EA_CREDIT not in json.dumps(fields)
        return json.dumps(
            {"translations": [{"id": item["id"], "text": "Перевод: " + item["text"]} for item in fields]}
        ), {"finish_reason": "stop"}

    completion = AsyncMock(side_effect=translate if translation == "translated" else RuntimeError("offline fallback"))
    monkeypatch.setattr("digest.translation.complete", completion)
    if translation != "canonical":
        settings = translation_config()
        config.translation, config.llm = settings.translation, settings.llm
    stats = await finish_preparation(snapshot, config, execution=execution)
    frozen = json.loads(Path(".cache", READY_FILE).read_text())
    prefix = "Перевод: " if translation == "translated" else ""
    presented = replace(card, summary=f"{prefix}{card.summary} {NHS_CREDIT}")
    published = [
        asdict(replace(original.top_articles[0], summary=f"{prefix}{original.top_articles[0].summary} {EA_CREDIT}")),
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
    markdown = Path(stats.markdown_path).read_text()
    assert markdown.index(original.top_articles[0].title) < markdown.index(card.title)
    assert presented.summary in markdown and card.link in markdown
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
    assert (await resume_preparation(config, 0, verbose=False, execution=execution)).ready_sha256 == stats.ready_sha256
    assert Path(".cache", READY_FILE).read_bytes() == ready_bytes


@pytest.mark.asyncio
async def test_unrenderable_closing_is_omitted_before_archive_and_freeze(
    setup: tuple[SimpleNamespace, PreparationSnapshot],
) -> None:
    execution = ModelExecution()
    config, snapshot = setup
    card = ArticleSummary("Unsafe optional", "https://example.com/" + "a" * 5000, "Closing source", "World", "Claim.")
    snapshot = bound_snapshot(snapshot, card)
    stats = await finish_preparation(snapshot, config, execution=execution)
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
    with pytest.raises(ValueError, match="URL exceeds"):
        await finish_preparation(unsafe_snapshot, config, execution=execution)
    assert not Path("digests").exists() and not Path(".cache", READY_FILE).exists()
    monkeypatch.setattr("digest.delivery.write_digest", lambda *_args, **_kwargs: None)
    snapshot = bound_snapshot(original, closing)
    with pytest.raises(ValueError, match="archive persistence failed"):
        await finish_preparation(snapshot, config, execution=execution)
    assert not Path(".cache", READY_FILE).exists()


@pytest.mark.asyncio
async def test_omitted_closing_decision_is_frozen_without_changing_main_output(
    setup: tuple[SimpleNamespace, PreparationSnapshot],
) -> None:
    execution = ModelExecution()
    config, original = setup
    decision = ClosingDecision("unavailable", "no_suitable_item_in_packet")
    snapshot = bound_snapshot(replace(original, closing=decision))
    await finish_preparation(snapshot, config, execution=execution)
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
    decision = ClosingDecision("selected", "same_response_designation", original.top_articles[0])
    stats = await finish_preparation(replace(original, top_articles=[], closing=decision), config, execution=execution)
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
    stats = await finish_preparation(snapshot, config, execution=execution)
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
    stats = await finish_preparation(snapshot, config, execution=execution)
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
    accepted = path.read_bytes()
    translation = AsyncMock(side_effect=AssertionError("Attribution must resolve before translation"))
    monkeypatch.setattr("digest.translation.complete", translation)
    with pytest.raises(ValueError, match="attribution|candidate evidence|storage hash mismatch"):
        await finish_preparation(snapshot, config, execution=execution)
    translation.assert_not_awaited()
    assert path.read_bytes() == accepted and load_preparation() == snapshot
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
    stats = await resume_preparation(config, 0, verbose=False, execution=execution)
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
    accepted = path.read_bytes()
    translation = AsyncMock(side_effect=AssertionError("Translation is disabled"))
    monkeypatch.setattr("digest.translation.complete", translation)
    with pytest.raises(ValueError, match="Main source attribution spans delivery chunks"):
        await finish_preparation(snapshot, config, execution=execution)
    translation.assert_not_awaited()
    assert path.read_bytes() == accepted and load_preparation() == snapshot
    assert not Path("digests").exists() and not Path(".cache", READY_FILE).exists()
    assert not Path(".cache", CLAIM_FILE).exists() and not Path(".cache/seen_articles.json").exists()
