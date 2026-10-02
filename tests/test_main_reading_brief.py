"""Offline main-pipeline integration: full-source cards and durable delivery."""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from digest.config import Config, ProviderConfig, ReadingBriefConfig, TranslationConfig
from digest.delivery import ArticleDeliveryResult
from digest.delivery.telegram import IssueDeliveryResult
from digest.main import _analyze_publication, _setup_reading_budget, run
from digest.radar.collector import Article, article_hash
from digest.radar.summarizer import ArticleSummary
from digest.reading_brief import BriefRun, enrich_selected_cards
from digest.reading_brief_state import load_state, state_root
from scripts.review_fixture import fixture_articles, fixture_config, fixture_response
from tests.factories import make_article
from tests.test_reading_brief import fetched, response


def _config(tmp_path: Path) -> Config:
    config = fixture_config()
    config.review.review_led_only = True
    config.reading_brief = ReadingBriefConfig(True, "gemini", "gemini-3.8-flash")
    config.telegram.enabled = config.telegram.required = config.obsidian.enabled = True
    config.obsidian.output_dir = str(tmp_path / "digests")
    return config


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    with patch("httpx.AsyncClient", side_effect=AssertionError("No live HTTP")):
        yield


async def _selection_response(*args: Any, **kwargs: Any) -> Any:
    text, usage = await fixture_response(*args, **kwargs)
    return text, usage | {"finish_reason": "STOP"}


async def _generate(_role: Any, messages: list[dict[str, str]], *_args: Any, **_kwargs: Any) -> Any:
    return response(messages)


async def _seed(config: Config, article: Article) -> BriefRun:
    with (patch("digest.reading_brief.fetch_article",
                AsyncMock(return_value=fetched("Source wording stays verbatim."))),
          patch("digest.llm.count_gemini_tokens", AsyncMock(return_value=100)),
          patch("digest.llm.complete", side_effect=_generate)):
        return await enrich_selected_cards([article], config, Path(".cache"), time.monotonic() + 1000)


async def _confirmed(_articles: Any, _config: Any, *, top_articles: list[ArticleSummary]) -> ArticleDeliveryResult:
    hashes = {article_hash(card.title, card.link) for card in top_articles}
    return ArticleDeliveryResult(attempted=len(hashes), sent=len(hashes), delivered_hashes=hashes,
                                 article_source_map={article_hash(card.title, card.link)[:8]: card.source
                                                     for card in top_articles})


@pytest.mark.asyncio
async def test_selected_articles_only_become_full_source_cards_and_checkpoint(tmp_path: Path) -> None:
    config = _config(tmp_path)
    articles = fixture_articles()
    collected = {article_hash(a.title, a.link): "2026-10-02T12:00:00+00:00"
                 for group in articles.values() for a in group}
    with (patch("digest.config.load_config", return_value=config),
          patch("digest.radar.collect", AsyncMock(return_value=(articles, collected))),
          patch("digest.review.complete", side_effect=_selection_response),
          patch("digest.reading_brief.fetch_article", AsyncMock(return_value=fetched("Source wording stays verbatim.")))
          as fetch,
          patch("digest.llm.count_gemini_tokens", AsyncMock(return_value=100)),
          patch("digest.llm.complete", side_effect=_generate),
          patch("digest.delivery.send_article_cards", side_effect=_confirmed) as delivery):
        result = await run("fixture.yaml", False, False, False)
    cards = delivery.call_args.kwargs["top_articles"]
    assert len(cards) == fetch.await_count == 2 < len(collected)
    assert result.telegram_sent and result.markdown_saved
    assert "Reading brief model: gemini/gemini-3.8-flash" in Path(result.markdown_path).read_text()
    payload = json.loads(Path(result.review_checkpoint).read_text())
    assert payload["full_source_required"] is True and payload["status"] == "incomplete"
    assert payload["full_source_evidence"]["rss_bundle_id"] == payload["evidence"]["bundle_id"]
    assert all("Source wording stays verbatim." in card.summary for card in cards)
    assert {call.args[0] for call in fetch.call_args_list} == {card.link for card in cards}
    committed = json.loads(Path(".cache/seen_articles.json").read_text())
    assert set(committed) == {article_hash(card.title, card.link) for card in cards}
    assert all(load_state(Path(".cache"), identity).status == "delivered" for identity in committed)


@pytest.mark.asyncio
async def test_pending_only_never_sends_notice_archive_or_consumes_dedup(tmp_path: Path) -> None:
    config = _config(tmp_path)
    old = {"a" * 32: "2026-10-01T12:00:00+00:00"}
    Path(".cache").mkdir()
    Path(".cache/seen_articles.json").write_text(json.dumps(old))
    before = Path(".cache/seen_articles.json").read_bytes()
    articles = fixture_articles()
    provisional = old | {article_hash(a.title, a.link): "2026-10-02T12:00:00+00:00"
                         for group in articles.values() for a in group}
    with (patch("digest.config.load_config", return_value=config),
          patch("digest.radar.collect", AsyncMock(return_value=(articles, provisional))),
          patch("digest.review.complete", side_effect=_selection_response),
          patch("digest.reading_brief.fetch_article", AsyncMock(side_effect=OSError("unavailable"))),
          patch("digest.delivery.send_article_cards", AsyncMock()) as cards,
          patch("digest.main._send_status_message", AsyncMock()) as notice,
          patch("digest.delivery.write_digest") as archive):
        result = await run("fixture.yaml", False, False, False)
    assert result.reading_pending == 2 and result.reading_abstained == 0 and result.reading_oldest_pending
    assert not result.telegram_sent and not result.markdown_saved and result.digest_length == 0
    cards.assert_not_called()
    notice.assert_not_called()
    archive.assert_not_called()
    assert Path(".cache/seen_articles.json").read_bytes() == before
    assert len(list(state_root(Path(".cache")).glob("*.json"))) == 2


@pytest.mark.asyncio
async def test_empty_rss_resumes_ready_brief_and_quotes_bypass_translation(tmp_path: Path) -> None:
    config = _config(tmp_path)
    article = make_article()
    seeded = await _seed(config, article)
    quote = seeded.quotations[article_hash(article.title, article.link)]
    config.translation = TranslationConfig(enabled=True, provider="groq", model="openai/gpt-oss-120b")

    async def translate(summary: str, cards: list[ArticleSummary], ranked: list[Any], *_args: Any) -> Any:
        assert "Source wording stays verbatim." not in summary
        assert all("Source wording stays verbatim." not in card.summary for card in cards)
        return summary, [replace(card, summary="Переведённый ракурс чтения.") for card in cards], ranked

    with (patch("digest.config.load_config", return_value=config),
          patch("digest.radar.collect", AsyncMock(return_value=({}, {}))),
          patch("digest.main._analyze_articles",
                AsyncMock(side_effect=AssertionError("No fresh RSS analysis"))) as analysis,
          patch("digest.main._publication_presentation", side_effect=translate),
          patch("digest.reading_brief.fetch_article", AsyncMock(side_effect=AssertionError("Already read"))) as fetch,
          patch("digest.llm.complete", AsyncMock(side_effect=AssertionError("Already ready"))) as model,
          patch("digest.delivery.send_article_cards", side_effect=_confirmed) as delivery):
        result = await run("fixture.yaml", False, False, False)
    analysis.assert_not_called()
    fetch.assert_not_called()
    model.assert_not_called()
    assert result.new_articles == 0 and result.telegram_sent and result.markdown_saved
    card = delivery.call_args.kwargs["top_articles"][0]
    assert card.summary == "Переведённый ракурс чтения.\n\n" + quote
    markdown = Path(result.markdown_path).read_text()
    assert all("> " + line in markdown for line in card.summary.split("\n"))
    payload = json.loads(Path(result.review_checkpoint).read_text())
    assert payload["reviews"] == [] and payload["status"] == "incomplete"
    assert payload["evidence"]["items"][0]["evidence_id"] == article_hash(article.title, article.link)
    assert payload["full_source_evidence"]["items"][0]["article_id"] == article_hash(article.title, article.link)
    assert "Model view" not in markdown


@pytest.mark.asyncio
async def test_unexpected_stage_failure_preserves_saved_progress_and_has_no_rss_fallback(tmp_path: Path) -> None:
    config = _config(tmp_path)
    article = make_article()
    await _seed(config, article)
    identity = article_hash(article.title, article.link)
    before = (state_root(Path(".cache")) / f"{identity}.json").read_bytes()
    with (patch("digest.config.load_config", return_value=config),
          patch("digest.radar.collect", AsyncMock(return_value=(fixture_articles(), {}))),
          patch("digest.review.complete", side_effect=_selection_response),
          patch("digest.reading_brief.enrich_selected_cards", AsyncMock(side_effect=OSError("disk unavailable"))),
          patch("digest.delivery.send_article_cards", AsyncMock()) as delivery):
        result = await run("fixture.yaml", False, False, False)
    assert not result.markdown_saved and not result.telegram_sent
    assert result.reading_state == "unavailable" and result.reading_pending is result.reading_abstained is None
    delivery.assert_not_called()
    assert (state_root(Path(".cache")) / f"{identity}.json").read_bytes() == before
    assert not Path(".cache/seen_articles.json").exists()


@pytest.mark.asyncio
async def test_saved_delivery_ledger_reconciles_ready_brief_before_new_collection_entries(tmp_path: Path) -> None:
    config = _config(tmp_path)
    article = make_article()
    await _seed(config, article)
    identity = article_hash(article.title, article.link)
    Path(".cache/seen_articles.json").write_text(json.dumps({identity: "2026-10-01T12:00:00+00:00"}))
    with (patch("digest.config.load_config", return_value=config),
          patch("digest.radar.collect", AsyncMock(return_value=({}, {identity: "2026-10-01T12:00:00+00:00"}))),
          patch("digest.delivery.send_article_cards", AsyncMock()) as delivery):
        result = await run("fixture.yaml", False, False, False)
    delivery.assert_not_called()
    assert not result.markdown_saved and load_state(Path(".cache"), identity).status == "delivered"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["none", "feedback", "ack"])
async def test_compact_ack_after_durable_saves_before_confirmation(tmp_path: Path, failure: str) -> None:
    config = _config(tmp_path)
    config.telegram.delivery_mode = "compact"
    article = make_article()
    await _seed(config, article)
    identity = article_hash(article.title, article.link)
    guard = MagicMock(state="reserved")
    events: list[str] = []

    async def deliver(*_args: Any) -> IssueDeliveryResult:
        guard.state = "sending"
        events.append("sent")
        return IssueDeliveryResult(attempted=1, sent=1, delivered_hashes={identity},
                                   article_source_map={identity[:8]: article.source}, outcome="sent",
                                   total_chunks=1, confirmed_chunks=1, attempted_chunks=1)

    def finish(outcome: str, **_kwargs: Any) -> None:
        events.append(outcome)
        guard.state = outcome

    from digest.feedback import save_feedback
    from digest.reading_brief import mark_briefs_delivered

    def feedback(*args: Any, **kwargs: Any) -> None:
        assert Path(".cache/seen_articles.json").exists()
        assert kwargs["strict"] is True
        events.append("feedback")
        if failure == "feedback":
            raise OSError("feedback persistence failed")
        save_feedback(*args, **kwargs)

    def ack(*args: Any) -> None:
        assert events == ["sent", "feedback"] and Path(".cache/feedback.json").exists()
        events.append("ack")
        if failure == "ack":
            raise OSError("brief acknowledgement failed")
        mark_briefs_delivered(*args)

    guard.finish.side_effect = finish
    with (patch("digest.config.load_config", return_value=config),
          patch("digest.radar.collect", AsyncMock(return_value=({}, {}))),
          patch("digest.main._deliver_compact", side_effect=deliver),
          patch("digest.feedback.save_feedback", side_effect=feedback),
          patch("digest.reading_brief.mark_briefs_delivered", side_effect=ack)):
        if failure == "none":
            await run("fixture.yaml", False, False, False, issue_guard=guard)
        else:
            with pytest.raises(OSError):
                await run("fixture.yaml", False, False, False, issue_guard=guard)
    assert events == (["sent", "feedback", "ack", "confirmed"] if failure == "none"
                      else ["sent", "feedback", "unknown"] if failure == "feedback"
                      else ["sent", "feedback", "ack", "unknown"])
    assert load_state(Path(".cache"), identity).status == ("delivered" if failure == "none" else "ready")


@pytest.mark.asyncio
async def test_application_deadline_reserves_translation_and_dispatch_without_resetting(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.translation = TranslationConfig(enabled=True, provider="groq", model="openai/gpt-oss-120b",
                                           timeout_seconds=30)
    started = time.monotonic() - 20
    with (patch("digest.main._analyze_articles", AsyncMock(return_value=([], None, [], None))),
          patch("digest.main._enrich_reading_briefs",
                AsyncMock(return_value=BriefRun([], {}, [], 0, 0, None))) as enrich):
        await _analyze_publication(fixture_articles(), config, tmp_path, started, False, set())
    assert enrich.call_args.args[3] == started + 360 - 30 - 45

    async def too_slow(*_args: Any) -> Any:
        await asyncio.sleep(1)
        raise AssertionError("Primary should have timed out")

    with (patch("digest.main._analyze_articles", side_effect=too_slow),
          patch("digest.main._enrich_reading_briefs",
                AsyncMock(return_value=BriefRun([], {}, [], 1, 0, "old"))) as enrich):
        _, _, cards, report, stage = await _analyze_publication(
            fixture_articles(), config, tmp_path, time.monotonic() - 400, False, set(),
        )
    assert not cards and report is None and stage.pending == 1
    assert enrich.call_args.args[0] == []


@pytest.mark.asyncio
async def test_shared_request_cap_counts_primary_fallback_preflight_reading_and_translation(tmp_path: Path) -> None:
    from digest import llm
    from digest.review import run_primary_review
    from digest.translation import translate_fields

    config = _config(tmp_path)
    config.reading_brief = replace(config.reading_brief, max_requests_per_run=5)
    config.translation = TranslationConfig(enabled=True, provider="groq", model="openai/gpt-oss-120b")
    _setup_reading_budget(config, tmp_path)
    requests: list[str] = []
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.post.return_value = httpx.Response(200, json={"totalTokens": 100},
                                            request=httpx.Request("POST", "https://example.com/countTokens"))

    async def provider(_client: Any, route: Any, messages: list[dict[str, str]], *_args: Any, **_kwargs: Any) -> Any:
        payload = json.loads(messages[1]["content"])
        requests.append("primary" if "evidence" in payload else "reading" if "spans" in payload else "translation")
        if len(requests) == 1:
            raise RuntimeError("primary unavailable")
        if "evidence" in payload:
            return await _selection_response(None, messages, config, provider_override=route)
        if "spans" in payload:
            return response(messages)
        return json.dumps({"translations": [{"id": field["id"], "text": "Перевод"}
                                            for field in payload["fields"]]}), {"finish_reason": "stop"}

    with (patch("httpx.AsyncClient", return_value=client),
          patch("digest.llm._call_provider", side_effect=provider),
          patch.dict("os.environ", {"GEMINI_API_KEY": "fake-key"}),
          patch("digest.reading_brief.fetch_article",
                AsyncMock(return_value=fetched("Source wording stays verbatim.")))):
        report = await run_primary_review(fixture_articles(), config)
        assert report.reviews[0].status == "unavailable" and report.reviews[1].status == "ok"
        ready = await enrich_selected_cards([make_article()], config, tmp_path, time.monotonic() + 1000)
        assert len(ready.cards) == 1
        translated = await translate_fields({"angle": "Reading angle"}, config, tmp_path / "translations")
        assert translated.status == "translated"
        assert llm.request_budget_remaining(config) == 0
        with pytest.raises(RuntimeError):
            await llm.complete(llm.LLMRole.SUMMARIZE, [], config,
                               provider_override=ProviderConfig("gemini", "gemini-3.8-flash"))
    assert requests == ["primary", "primary", "reading", "translation"]
    assert client.post.await_count == 1


@pytest.mark.asyncio
async def test_failed_feeds_still_deliver_ready_backlog_without_fresh_selection(tmp_path: Path) -> None:
    from digest.radar import AllFeedsFailedError

    config = _config(tmp_path)
    article = make_article()
    await _seed(config, article)
    old = {"a" * 32: "2026-10-01T12:00:00+00:00"}
    Path(".cache/seen_articles.json").write_text(json.dumps(old))
    with (patch("digest.config.load_config", return_value=config),
          patch("digest.radar.collect", AsyncMock(side_effect=AllFeedsFailedError("All feeds unavailable"))),
          patch("digest.main._analyze_articles", AsyncMock(side_effect=AssertionError("No fresh RSS"))) as selection,
          patch("digest.delivery.send_article_cards", side_effect=_confirmed)):
        result = await run("fixture.yaml", False, False, False)
    selection.assert_not_called()
    assert result.new_articles == 0 and result.telegram_sent and result.markdown_saved
    assert result.reading_state == "available"
    payload = json.loads(Path(result.review_checkpoint).read_text())
    assert payload["reviews"] == [] and payload["reading_brief_status"]["state"] == "available"
    committed = json.loads(Path(".cache/seen_articles.json").read_text())
    assert committed["a" * 32] == old["a" * 32]
    assert set(committed) == {"a" * 32, article_hash(article.title, article.link)}
