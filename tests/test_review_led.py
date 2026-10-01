"""Opt-in review-led delivery avoids spending quota on the legacy enrichment path."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from digest.config import ProviderConfig, ReviewConfig, _load_review, load_config
from digest.delivery import ArticleDeliveryResult
from digest.main import _analyze_articles, run
from digest.radar.summarizer import CategorySummary
from digest.review import build_evidence_bundle, build_review_messages
from scripts.review_fixture import fixture_articles, fixture_config, fixture_response, run_fixture
from tests.test_config import MINIMAL_CONFIG


def test_review_led_mode_is_opt_in() -> None:
    assert not ReviewConfig().review_led_only
    assert not _load_review({}).review_led_only
    assert not _load_review({"review": {"enabled": True}}).review_led_only
    assert _load_review({"review": {"enabled": True, "review_led_only": True}}).review_led_only
    assert not _load_review({"review": {"enabled": True, "review_led_only": False}}).review_led_only


@pytest.mark.parametrize("value", ["true", "false", 0, 1, None, [], {}])
def test_review_led_mode_rejects_non_boolean_values(value: object) -> None:
    with pytest.raises(ValueError, match=r"review.review_led_only must be a boolean"):
        _load_review({"review": {"review_led_only": value}})


def test_review_led_mode_loads_from_yaml(tmp_path: Path) -> None:
    import textwrap

    path = tmp_path / "config.yaml"
    path.write_text(textwrap.dedent(MINIMAL_CONFIG) + "\nreview:\n  enabled: true\n  review_led_only: true\n")
    assert load_config(path).review.review_led_only


def test_review_led_mode_does_not_change_evidence_or_review_prompt() -> None:
    normal = fixture_config()
    led = deepcopy(normal)
    led.review.review_led_only = True
    original = build_evidence_bundle(fixture_articles(), normal.review)
    assert build_evidence_bundle(fixture_articles(), led.review) == original
    assert build_review_messages(original, normal.review, normal.radar.language) == build_review_messages(
        original, led.review, led.radar.language,
    )


@pytest.mark.asyncio
async def test_review_led_analysis_never_calls_legacy_summary_or_picker() -> None:
    config = fixture_config()
    config.review.review_led_only = True
    with (
        patch("digest.review.complete", side_effect=fixture_response) as complete,
        patch("digest.radar.summarize_all", AsyncMock(side_effect=AssertionError("No category prose"))) as summarize,
        patch("digest.radar.pick_top_articles", AsyncMock(side_effect=AssertionError("No legacy picker"))) as picker,
    ):
        summaries, trends, cards, report = await _analyze_articles(fixture_articles(), config)
    summarize.assert_not_called()
    picker.assert_not_called()
    assert summaries == [] and trends is None
    assert len(cards) == 2 and report.status == "incomplete"
    assert complete.call_count == 1


@pytest.mark.asyncio
async def test_default_review_mode_preserves_legacy_category_analysis() -> None:
    config = fixture_config()
    original = await run_fixture()
    categories = [CategorySummary("AI", "Legacy category summary", 2)]
    with (
        patch("digest.review.run_blind_review", AsyncMock(return_value=original)),
        patch("digest.radar.summarize_all", AsyncMock(return_value=(categories, "Legacy trends"))) as summarize,
    ):
        summaries, trends, cards, report = await _analyze_articles(fixture_articles(), config)
    summarize.assert_awaited_once()
    assert summaries == categories and trends == "Legacy trends"
    assert report is original and cards


@pytest.mark.asyncio
async def test_review_led_mode_is_ignored_when_review_is_disabled() -> None:
    config = fixture_config()
    config.review.enabled = False
    config.review.review_led_only = True
    categories = [CategorySummary("AI", "Legacy summary", 2)]
    with (
        patch("digest.review.run_blind_review", AsyncMock(side_effect=AssertionError("Review is disabled"))),
        patch("digest.radar.summarize_all", AsyncMock(return_value=(categories, None))) as summarize,
        patch("digest.radar.pick_top_articles", AsyncMock(return_value=[])) as picker,
    ):
        summaries, _, _, report = await _analyze_articles(fixture_articles(), config)
    summarize.assert_awaited_once()
    picker.assert_awaited_once()
    assert summaries == categories and report is None


@pytest.mark.asyncio
async def test_review_led_pipeline_archives_and_delivers_cards_without_narrative_model_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    config = fixture_config()
    config.review.review_led_only = True
    config.telegram.enabled = config.telegram.required = config.obsidian.enabled = True
    config.obsidian.output_dir = str(tmp_path / "digests")
    config.llm.providers = [ProviderConfig("gemini", "gemini-3.8-flash", ["summarize"])]
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.get.side_effect = client.post.side_effect = AssertionError("No live HTTP")
    with (
        caplog.at_level("INFO"),
        patch("httpx.AsyncClient", return_value=client),
        patch("digest.config.load_config", return_value=config),
        patch("digest.radar.collect", AsyncMock(return_value=(fixture_articles(), {}))),
        patch("digest.review.complete", side_effect=fixture_response) as review_complete,
        patch("digest.radar.summarizer.complete", AsyncMock(side_effect=AssertionError("No summaries"))),
        patch("digest.irritator.narrative_extractor.complete", AsyncMock()) as narratives,
        patch("digest.irritator.query_generator.complete", AsyncMock()) as queries,
        patch("digest.irritator.ranker.complete", AsyncMock()) as ranking,
        patch("digest.delivery.send_article_cards", AsyncMock(
            return_value=ArticleDeliveryResult(attempted=2, sent=2),
        )) as delivery,
        patch("digest.delivery.send_counter_signals", AsyncMock()),
    ):
        result = await run("fixture.yaml", False, False, False)
    assert review_complete.call_count == 1
    narratives.assert_not_called()
    queries.assert_not_called()
    ranking.assert_not_called()
    client.get.assert_not_called()
    client.post.assert_not_called()
    assert result.telegram_sent and result.markdown_saved and not result.required_delivery_failed
    assert result.review_status == "incomplete"
    cards = delivery.call_args.kwargs["top_articles"]
    assert len(cards) == 2
    assert all("Model view" in card.summary for card in cards)
    archive = Path(result.markdown_path).with_suffix(".review.json")
    assert archive.exists()
    payload = json.loads(archive.read_text())
    evidence = asdict(build_evidence_bundle(fixture_articles(), config.review))
    assert payload["evidence"] == json.loads(json.dumps(evidence))
    assert payload["status"] == "incomplete"
    markdown = Path(result.markdown_path).read_text()
    assert "Independent Blind Review" in markdown
    assert all(card.title in markdown for card in cards)
    assert "skipping legacy category summaries, trends and counter-signal analysis" in caplog.text
