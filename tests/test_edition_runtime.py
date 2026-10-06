"""Offline end-to-end preparation/sender boundary and failure persistence."""

from __future__ import annotations

import json
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from digest.delivery.edition import CLAIM_FILE, READY_FILE, inspect_edition
from digest.edition_runtime import delivery_phase, finish_preparation, resume_preparation
from digest.feedback import FeedbackStore, load_feedback, save_feedback
from digest.preparation import PreparationSnapshot, load_preparation, save_preparation
from digest.radar.summarizer import ArticleSummary


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
    config, snapshot = setup
    save_preparation(snapshot)
    presentation = AsyncMock(side_effect=RuntimeError("presentation failed"))
    monkeypatch.setattr("digest.main._publication_presentation", presentation)
    with pytest.raises(RuntimeError):
        await resume_preparation(config, 0, verbose=False)
    assert asdict(load_preparation()) == asdict(snapshot)
    assert not Path(".cache", CLAIM_FILE).exists()
    assert not Path(".cache/seen_articles.json").exists()
    presentation.side_effect = None
    presentation.return_value = ("Rendered notice", snapshot.top_articles, [])
    stats = await resume_preparation(config, 0, verbose=False)
    assert stats.edition_status == "ready"
    frozen = json.loads(Path(".cache", READY_FILE).read_text())
    assert not Path(".cache", CLAIM_FILE).exists()
    assert load_preparation() is None

    # Later preparation/model settings cannot change the frozen transport body.
    config.review.enabled = False
    config.radar.language = "ru"
    monkeypatch.setattr("digest.main._publication_presentation", AsyncMock(side_effect=AssertionError("rerender")))
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
    with respx.mock(assert_all_called=False) as router:
        assert await delivery_phase("send", "config.yaml", stats.ready_sha256, claim_sha) == 0
        assert not router.calls


@pytest.mark.asyncio
async def test_state_save_failure_after_accepted_post_holds_instead_of_replaying(
    setup: tuple[SimpleNamespace, PreparationSnapshot], monkeypatch: pytest.MonkeyPatch
) -> None:
    config, snapshot = setup
    stats = await finish_preparation(snapshot, config)
    assert await delivery_phase("claim", "config.yaml", stats.ready_sha256, None) == 0
    import hashlib

    claim_sha = hashlib.sha256(Path(".cache", CLAIM_FILE).read_bytes()).hexdigest()
    monkeypatch.setattr("digest.edition_runtime._merge_delivery", lambda *_: (_ for _ in ()).throw(OSError("disk")))
    with respx.mock() as router:
        router.post("https://api.telegram.org/bottest-token/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 52, "chat": {"id": 12345}}}),
        )
        with pytest.raises(OSError):
            await delivery_phase("send", "config.yaml", stats.ready_sha256, claim_sha)
    assert inspect_edition()[2] == "held"
    with respx.mock(assert_all_called=False) as router:
        with pytest.raises(ValueError):
            await delivery_phase("send", "config.yaml", stats.ready_sha256, claim_sha)
        assert not router.calls


@pytest.mark.asyncio
async def test_empty_or_corrupt_attribution_never_sends(setup: tuple[SimpleNamespace, PreparationSnapshot]) -> None:
    config, snapshot = setup
    snapshot.top_articles.clear()
    assert (await finish_preparation(snapshot, config)).edition_status == "no_ready"
    assert not Path(".cache", READY_FILE).exists()
    snapshot.top_articles.append(ArticleSummary("Title", "https://example.com/x", "Source", "Tech", "Claim"))
    stats = await finish_preparation(snapshot, config)
    Path(".cache/feedback.json").write_text("corrupt")
    with pytest.raises(ValueError):
        await delivery_phase("claim", "config.yaml", stats.ready_sha256, None)
    assert not Path(".cache", CLAIM_FILE).exists()


def test_unavailable_primary_is_not_a_reusable_empty_preparation(
    setup: tuple[SimpleNamespace, PreparationSnapshot],
) -> None:
    from digest.edition_runtime import save_accepted_preparation
    from tests.test_preparation import _snapshot

    snapshot = _snapshot()
    snapshot.top_articles.clear()
    snapshot.review_report.reviews[0].status = "unavailable"
    snapshot.review_report.reviews[0].selections.clear()
    save_accepted_preparation(snapshot, cache_dir=".cache")
    assert load_preparation() is None
    # A later accepted response is persisted, rather than stuck behind failed work.
    accepted = _snapshot()
    save_accepted_preparation(accepted, cache_dir=".cache")
    assert load_preparation().top_articles == accepted.top_articles


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
    monkeypatch.setattr("digest.main._analyze_articles", analysis)
    presentation = AsyncMock(side_effect=RuntimeError("late presentation failure"))
    monkeypatch.setattr("digest.main._publication_presentation", presentation)
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
    from datetime import datetime, timedelta, timezone

    from digest.delivery.telegram import IssueDeliveryResult
    from digest.edition_runtime import _merge_delivery
    from digest.radar.collector import article_hash
    from digest.source_scorer import DailySnapshot, SourceStats, load_stats, save_stats

    config, snapshot = setup
    today = datetime.now(timezone.utc).date()
    target = today + timedelta(days=1)
    result = await finish_preparation(snapshot, config, publication_date=target)
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
async def test_selected_closing_is_final_identical_card_in_archive_and_frozen_edition(
    setup: tuple[SimpleNamespace, PreparationSnapshot],
) -> None:
    from digest.closing import ClosingDecision
    from digest.radar.collector import article_hash

    config, original = setup
    card = ArticleSummary("Closing title", "https://example.com/good", "Closing source", "World", "Volunteers helped.")
    decision = ClosingDecision("selected", "same_response_designation", card)
    snapshot = replace(original, closing=decision)
    stats = await finish_preparation(snapshot, config)
    frozen = json.loads(Path(".cache", READY_FILE).read_text())
    published = [asdict(original.top_articles[0]), asdict(card)]
    assert frozen["presentation_metadata"]["cards"] == published
    assert [item["card"] for item in frozen["articles"]] == published
    assert frozen["canonical_metadata"]["cards"] == [asdict(original.top_articles[0])]
    assert frozen["canonical_metadata"]["closing"] == asdict(decision)
    assert frozen["presentation_metadata"]["closing"]["status"] == "presented"
    markdown = Path(stats.markdown_path).read_text()
    assert markdown.index(original.top_articles[0].title) < markdown.index(card.title)
    assert card.summary in markdown and card.link in markdown
    assert len(frozen["checkpoint_refs"]) == 1
    closing = frozen["articles"][-1]
    assert closing["source"] == card.source and closing["full_hash"] == article_hash(card.title, card.link)
    assert set(closing["card"]) == {"title", "link", "source", "category", "summary"}
    assert closing["covering_chunks"]
    buttons = frozen["payloads"][closing["covering_chunks"][-1]]["reply_markup"]["inline_keyboard"]
    assert buttons[-1][0]["url"].endswith(f"vote_g_{closing['full_hash'][:8]}")
    ready_bytes = Path(".cache", READY_FILE).read_bytes()
    config.translation.enabled = True
    config.radar.language = "ru"
    config.review.enabled = False
    assert (await resume_preparation(config, 0, verbose=False)).ready_sha256 == stats.ready_sha256
    assert Path(".cache", READY_FILE).read_bytes() == ready_bytes


@pytest.mark.asyncio
async def test_unrenderable_closing_is_omitted_before_archive_and_freeze(
    setup: tuple[SimpleNamespace, PreparationSnapshot],
) -> None:
    from digest.closing import ClosingDecision

    config, snapshot = setup
    card = ArticleSummary("Unsafe optional", "https://example.com/" + "a" * 5000, "Source", "World", "Claim.")
    snapshot = replace(snapshot, closing=ClosingDecision("selected", "same_response_designation", card))
    stats = await finish_preparation(snapshot, config)
    frozen = json.loads(Path(".cache", READY_FILE).read_text())
    assert len(frozen["articles"]) == 1 and len(frozen["presentation_metadata"]["cards"]) == 1
    optional = frozen["presentation_metadata"]["closing"]
    assert optional["status"] == "incomplete" and optional["reason"] == "rendering_failed"
    assert optional["card"] is None
    assert card.title not in Path(stats.markdown_path).read_text()
    assert all(card.title not in payload["text"] for payload in frozen["payloads"])
    assert frozen["canonical_metadata"]["closing"]["card"] == asdict(card)


@pytest.mark.asyncio
async def test_invalid_main_rendering_and_required_archive_errors_are_not_optional_failures(
    setup: tuple[SimpleNamespace, PreparationSnapshot], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from digest.closing import ClosingDecision

    config, original = setup
    closing = ArticleSummary("Closing", "https://example.com/good", "Source", "World", "Claim.")
    snapshot = replace(original, closing=ClosingDecision("selected", "same_response_designation", closing))
    unsafe = replace(snapshot.top_articles[0], link="https://example.com/" + "a" * 5000)
    with pytest.raises(ValueError, match="URL exceeds"):
        await finish_preparation(replace(snapshot, top_articles=[unsafe]), config)
    assert not Path("digests").exists() and not Path(".cache", READY_FILE).exists()
    monkeypatch.setattr("digest.delivery.write_digest", lambda *_args, **_kwargs: None)
    with pytest.raises(ValueError, match="archive persistence failed"):
        await finish_preparation(snapshot, config)
    assert not Path(".cache", READY_FILE).exists()


@pytest.mark.asyncio
async def test_omitted_closing_decision_is_frozen_without_changing_main_output(
    setup: tuple[SimpleNamespace, PreparationSnapshot],
) -> None:
    from digest.closing import ClosingDecision

    config, original = setup
    decision = ClosingDecision("unavailable", "no_suitable_item_in_packet")
    snapshot = replace(original, closing=decision)
    await finish_preparation(snapshot, config)
    frozen = json.loads(Path(".cache", READY_FILE).read_text())
    assert len(frozen["articles"]) == 1
    assert frozen["canonical_metadata"]["closing"] == asdict(decision)
    assert frozen["presentation_metadata"]["closing"]["status"] == "unavailable"
    assert frozen["presentation_metadata"]["closing"]["reason"] == decision.reason


@pytest.mark.asyncio
async def test_closing_alone_does_not_create_a_filler_edition(
    setup: tuple[SimpleNamespace, PreparationSnapshot],
) -> None:
    from digest.closing import ClosingDecision

    config, original = setup
    decision = ClosingDecision("selected", "same_response_designation", original.top_articles[0])
    stats = await finish_preparation(replace(original, top_articles=[], closing=decision), config)
    assert stats.edition_status == "no_ready"
    assert not Path(".cache", READY_FILE).exists() and not Path("digests").exists()


@pytest.mark.asyncio
async def test_multichunk_closing_is_seen_only_after_complete_article_coverage(
    setup: tuple[SimpleNamespace, PreparationSnapshot],
) -> None:
    import hashlib

    from digest.closing import ClosingDecision
    from digest.radar.collector import article_hash

    config, original = setup
    card = ArticleSummary("Closing", "https://example.com/good", "Closing source", "World", "More evidence. " * 1000)
    snapshot = replace(original, closing=ClosingDecision("selected", "same_response_designation", card))
    stats = await finish_preparation(snapshot, config)
    manifest = json.loads(Path(".cache", READY_FILE).read_text())
    assert len(manifest["articles"][-1]["covering_chunks"]) > 1
    assert await delivery_phase("claim", "config.yaml", stats.ready_sha256, None) == 0
    claim_sha = hashlib.sha256(Path(".cache", CLAIM_FILE).read_bytes()).hexdigest()
    with respx.mock(assert_all_called=True) as router:
        router.post("https://api.telegram.org/bottest-token/sendMessage").mock(side_effect=[
            httpx.Response(200, json={"ok": True, "result": {"message_id": 52, "chat": {"id": 12345}}}),
            httpx.Response(500, json={"ok": False}),
        ])
        assert await delivery_phase("send", "config.yaml", stats.ready_sha256, claim_sha) == 1
    seen = json.loads(Path(".cache/seen_articles.json").read_text())
    main = original.top_articles[0]
    assert article_hash(main.title, main.link) in seen
    assert article_hash(card.title, card.link) not in seen
    feedback = load_feedback(".cache", strict=True)
    assert article_hash(card.title, card.link)[:8] not in feedback.article_source_map
