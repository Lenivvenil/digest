"""Offline integration of optional closing with the current review and translation contracts."""
from __future__ import annotations

import copy
import json
import textwrap
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from digest.candidate_review import (
    CandidateProgress,
    begin_packet,
    load_candidate_progress,
    merge_candidates,
    plan_packet,
)
from digest.closing import ClosingCapture
from digest.config import Config, ReviewModelConfig, load_config
from digest.delivery.edition import READY_FILE
from digest.feedback import FeedbackStore
from digest.main import _analyze_candidate_articles, _preparation_closing, _run, main
from digest.preparation import load_preparation
from digest.radar.collector import Article, SourceCollectionOutcome, _capture_candidates, article_hash
from digest.review import _groq_review_format, build_evidence_bundle, build_review_messages, run_primary_review
from digest.translation import translate_publication_with_closing
from tests.test_closing import NOW, population, response
from tests.test_config import MINIMAL_CONFIG, _write_config
from tests.test_translation import config as translation_config


def six_articles() -> tuple[Config, dict[str, list[Article]]]:
    config, articles = population()
    articles["Society"].extend(replace(articles["Society"][0], title=f"Item {index}",
                                       link=f"https://example.com/{index}") for index in (4, 5))
    return config, articles


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_groq_wire_adds_only_enabled_closing_and_invalid_optional_keeps_main(
    monkeypatch: pytest.MonkeyPatch, enabled: bool,
) -> None:
    config, articles = population()
    config.review.primary = ReviewModelConfig("groq", "openai/gpt-oss-120b")
    config.closing = replace(config.closing, enabled=enabled)
    capture = ClosingCapture()
    complete = AsyncMock(side_effect=lambda role, messages, *args, **kwargs: (
        response(messages, {"schema_version": True, "evidence_id": None} if enabled else "missing"), {}))
    monkeypatch.setattr("digest.review.complete", complete)
    report = await run_primary_review(articles, config, closing_capture=capture)
    complete.assert_awaited_once()
    assert report.reviews[0].status == "ok" and len(report.reviews[0].selections) == 4
    assert capture.attempts[0].status == "incomplete"
    request = complete.call_args
    assert request.kwargs["reasoning_effort"] == "low"
    assert request.kwargs["max_output_tokens"] == 4096
    fmt = request.kwargs["response_format"]
    assert fmt == _groq_review_format(allow_closing=enabled)
    schema = copy.deepcopy(fmt["json_schema"]["schema"])
    if enabled:
        assert schema["properties"].pop("closing") == {
            "type": "object", "additionalProperties": False,
            "required": ["schema_version", "evidence_id"],
            "properties": {"schema_version": {"type": "integer", "enum": [1]},
                           "evidence_id": {"anyOf": [{"type": "string"}, {"type": "null"}]}},
        }
        schema["required"].remove("closing")
    else:
        bundle = build_evidence_bundle(articles, config.review)
        assert request.args[1] == build_review_messages(bundle, config.review, "en", sources=config.sources)
        assert "closing" not in json.dumps(request.args[1])
        assert fmt == _groq_review_format()
    assert schema == _groq_review_format()["json_schema"]["schema"]
    assert "closing" not in _groq_review_format()["json_schema"]["schema"]["properties"]


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_live_review_keeps_detail_bound_with_either_closing_flag(
    monkeypatch: pytest.MonkeyPatch, enabled: bool,
) -> None:
    config, articles = six_articles()
    config.closing = replace(config.closing, enabled=enabled)
    assert config.review.max_detailed_selections == 5
    complete = AsyncMock(side_effect=lambda role, messages, *args, **kwargs: (
        response(messages, "first" if enabled else "missing"), {}))
    monkeypatch.setattr("digest.review.complete", complete)
    report = await run_primary_review(articles, config)
    assert complete.await_count == 2
    assert all(review.status == "invalid" and review.error == "invalid selection count"
               and not review.selections for review in report.reviews)


@pytest.mark.asyncio
@pytest.mark.parametrize("limits", ["", "  max_detailed_selections: 5\n",
                                   "  max_detailed_selections: 6\n  max_evidence_articles: 5\n"])
async def test_enabled_closing_rejects_insufficient_capacity_before_external_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, limits: str,
) -> None:
    path = _write_config(tmp_path, textwrap.dedent(MINIMAL_CONFIG) + "\nreview:\n  enabled: true\n"
                         "  review_led_only: true\n" + limits + "telegram:\n  delivery_mode: compact\n"
                         "closing:\n  enabled: true\n  approved_sources:\n"
                         "    - {name: Test Feed, url: 'https://example.com/feed', category: Test}\n")
    collection = AsyncMock(side_effect=AssertionError("Collection must not start"))
    completion = AsyncMock(side_effect=AssertionError("Review must not start"))
    monkeypatch.setattr("digest.radar.collect", collection)
    monkeypatch.setattr("digest.review.complete", completion)
    with pytest.raises(ValueError, match="explicit detail and evidence limits"):
        await _run(path, False, False, False, prepare_only=True)
    collection.assert_not_awaited()
    completion.assert_not_awaited()
    text = Path(path).read_text().replace("closing:\n  enabled: true", "closing:\n  enabled: false")
    Path(path).write_text(text)
    disabled = load_config(path)
    assert not disabled.closing.enabled
    assert disabled.review.max_selections == 5 and disabled.review.max_output_tokens == 4096
    if not limits:
        assert disabled.review.max_detailed_selections == 5


@pytest.mark.asyncio
async def test_explicit_six_details_yield_five_main_and_same_response_closing_in_one_v3_translation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, articles = six_articles()
    config.review.max_selections = 5
    config.review.max_detailed_selections = 6
    config.translation = translation_config().translation
    assert config.translation.max_calls == 1
    progress = merge_candidates(CandidateProgress(), articles, config, {}, now=NOW)
    packet = plan_packet(progress, config, NOW)
    assert packet is not None
    begin_packet(progress, packet, tmp_path)
    completion = AsyncMock(side_effect=lambda role, messages, *args, **kwargs: (response(messages), {}))
    monkeypatch.setattr("digest.review.complete", completion)
    _, _, cards, report = await _analyze_candidate_articles(articles, config, progress, packet, None, str(tmp_path))
    assert report is not None and len(report.reviews[0].selections) == 6
    main_cards, closing = _preparation_closing(cards, report, articles, config, str(tmp_path))
    assert len(main_cards) == 5
    assert closing is not None and closing.status == "selected" and closing.card is not None
    assert closing.provenance is not None
    assert closing.provenance.response_sha256 == report.reviews[0].response_sha256
    assert closing.card.link not in {card.link for card in main_cards}
    completion.assert_awaited_once()
    assert completion.call_args.kwargs["max_output_tokens"] == 4096
    assert json.loads(completion.call_args.args[1][1]["content"])["max_detailed_selections"] == 6
    canonical = (asdict(report), [asdict(card) for card in main_cards], asdict(closing))

    async def translate(role: Any, messages: list[dict[str, str]], *args: Any, **kwargs: Any) -> tuple[str, dict]:
        fields = json.loads(messages[1]["content"])["fields"]
        return json.dumps({"translations": [{"id": item["id"], "text": "Перевод: " + item["text"]}
                                             for item in fields]}), {"finish_reason": "stop"}

    translation = AsyncMock(side_effect=translate)
    monkeypatch.setattr("digest.translation.complete", translation)
    cache = tmp_path / "translations"
    first = await translate_publication_with_closing(
        "Overview.", main_cards, [], closing.card, config, cache, selection_binding=asdict(closing))
    replay = await translate_publication_with_closing(
        "Overview.", main_cards, [], closing.card, config, cache, selection_binding=asdict(closing))
    translation.assert_awaited_once()
    assert first[:3] == replay[:3] and first[3].card == replay[3].card
    assert all(card.summary.startswith("Перевод: ") for card in first[1])
    assert first[3].card is not None and first[3].card.summary.startswith("Перевод: ")
    assert replay[3].translation is not None and replay[3].translation.cache_hits == 1
    records = list(cache.glob("*.json"))
    assert len(records) == 1
    record = json.loads(records[0].read_text())
    assert record["prompt_version"] == "presentation-translation-v3"
    assert record["schema_version"] == 2 and record["status"] == "translated"
    assert set(record["canonical"]) == {
        "category_digest", "closing.summary",
        *[f"article:{article_hash(card.title, card.link)}" for card in main_cards],
    }
    assert record["optional"]["status"] == "translated"
    assert canonical == (asdict(report), [asdict(card) for card in main_cards], asdict(closing))


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["truncated", "deferred", "abstained"])
async def test_enabled_closing_preserves_technical_empty_status_and_complete_abstention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    config, articles = population()
    now = datetime.now(UTC)
    observed = [replace(articles["Society"][0], pub_date=now)]
    monkeypatch.setattr("digest.config.load_config", lambda _: config)
    monkeypatch.setattr("digest.main._collect_run_feedback", AsyncMock(return_value=(FeedbackStore(), True, 0)))
    monkeypatch.setattr("digest.main._apply_pending_approvals", lambda c, *args, **kwargs: c)

    async def collect(c: Any, **kwargs: Any) -> tuple[dict, dict]:
        inventory = kwargs["inventory"]
        inventory.sources = [SourceCollectionOutcome("Community", "https://example.com/feed", "Society", 3)]
        _capture_candidates(inventory, c.enabled_sources, [observed], {}, now, [], {})
        return {"Society": observed}, {}

    async def model(role: Any, messages: list[dict[str, str]], *args: Any, **kwargs: Any) -> tuple[str, dict]:
        if outcome == "truncated":
            return '{"selections":[', {"finish_reason": "length"}
        raw = json.loads(response(messages, {"schema_version": 1, "evidence_id": None}))
        raw["selections"] = []
        raw["dispositions"] = [{**item, "status": "deferred" if outcome == "deferred" else "not_selected",
                                "reason": "Offline fixture explanation"} for item in raw["dispositions"]]
        return json.dumps(raw), {"finish_reason": "stop"}

    monkeypatch.setattr("digest.radar.collect", collect)
    completion = AsyncMock(side_effect=model)
    monkeypatch.setattr("digest.review.complete", completion)
    stats = await _run("config.yaml", False, False, False, prepare_only=True)
    incomplete = outcome != "abstained"
    assert stats.edition_status == ("selection_incomplete" if incomplete else "no_ready")
    assert not Path(".cache", READY_FILE).exists()
    snapshot = load_preparation()
    if incomplete:
        assert snapshot is None
        progress = load_candidate_progress()
        assert progress.candidates and all(item.status == "technical_pending" for item in progress.candidates.values())
        assert not progress.packets[0].handed_to_preparation
    else:
        assert snapshot is not None and snapshot.closing is not None
        assert snapshot.closing.status == "unavailable" and snapshot.closing.reason == "no_suitable_item_in_packet"
    monkeypatch.setattr("digest.main.run", AsyncMock(return_value=stats))
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "outputs"))
    assert await main(["--config", "config.yaml", "--prepare-edition"]) == int(incomplete)
    assert f"edition_status={stats.edition_status}" in (tmp_path / "outputs").read_text()
