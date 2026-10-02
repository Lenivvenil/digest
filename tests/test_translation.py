"""Presentation translation preserves identity and fails back without hidden calls."""
from __future__ import annotations

import json
import textwrap
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from digest.config import ProviderConfig, TranslationConfig, load_config
from digest.main import _primary_presentation
from digest.radar.collector import article_hash
from digest.radar.summarizer import ArticleSummary
from digest.translation import _parse, translate_fields, translate_primary_presentation
from scripts.review_fixture import fixture_config
from tests.test_config import MINIMAL_CONFIG


def config():
    result = fixture_config()
    result.llm.providers = [ProviderConfig("groq", "configured-model", ["summarize"])]
    result.llm.max_retries = 3
    result.translation = TranslationConfig(enabled=True, provider="groq", model="configured-model")
    return result


def write_config(tmp_path: Path, suffix: str):
    path = tmp_path / "config.yaml"
    path.write_text(textwrap.dedent(MINIMAL_CONFIG) + suffix)
    return load_config(path)


def test_configuration_opt_in_and_legacy_language_compatibility(tmp_path: Path) -> None:
    legacy = write_config(tmp_path, "")
    assert legacy.radar.language == "ru" and not legacy.translation.enabled
    explicit = write_config(tmp_path, "\nradar: {language: ru}\ntranslation: {enabled: false}\n")
    assert explicit.radar.language == "ru" and not explicit.translation.enabled
    new = write_config(tmp_path, "\ntranslation: {enabled: false}\n")
    assert new.radar.language == "en"
    enabled = write_config(tmp_path, "\ntranslation:\n  enabled: true\n  provider: groq\n"
                           "  model: llama-3.3-70b-versatile\n  target_language: ru\n")
    assert enabled.radar.language == "en" and enabled.translation.enabled
    assert enabled.translation.timeout_seconds == 90
    explicit_timeout = write_config(tmp_path, "\ntranslation: {enabled: false, timeout_seconds: 30}\n")
    assert explicit_timeout.translation.timeout_seconds == 30
    with pytest.raises(ValueError, match="canonical"):
        write_config(tmp_path, "\nradar: {language: ru}\ntranslation: {enabled: true}\n")
    with pytest.raises(ValueError, match="already present"):
        write_config(tmp_path, "\ntranslation: {enabled: true, provider: other, model: other}\n")
    with pytest.raises(ValueError, match="boolean"):
        write_config(tmp_path, '\ntranslation: {enabled: "true"}\n')


@pytest.mark.asyncio
async def test_absent_translation_does_not_touch_models_or_cache(tmp_path: Path) -> None:
    cfg = fixture_config()
    fields = {"a": "English original."}
    with patch("digest.translation.complete", AsyncMock(side_effect=AssertionError("No request"))) as call:
        result = await translate_fields(fields, cfg, tmp_path / "absent")
    assert result.fields == fields and result.status == "disabled" and not (tmp_path / "absent").exists()
    call.assert_not_called()


@pytest.mark.asyncio
async def test_success_is_pinned_cached_and_preserves_original_config(tmp_path: Path) -> None:
    cfg = config()
    raw = json.dumps({"translations": [{"id": "a", "text": 'Только 20 клиентов; "preview". https://example.com/a'}]})
    fields = {"a": 'Only 20 clients; "preview". https://example.com/a'}
    with patch("digest.translation.complete", AsyncMock(return_value=(raw, {"finish_reason": "stop"}))) as call:
        first = await translate_fields(fields, cfg, tmp_path / "cache")
        second = await translate_fields(fields, cfg, tmp_path / "cache")
    assert first.status == second.status == "translated"
    assert second.fields == first.fields and second.cache_hits == 1
    call.assert_awaited_once()
    assert call.call_args.kwargs["provider_override"] == ProviderConfig("groq", "configured-model")
    assert call.call_args.args[2].llm.max_retries == 0 and cfg.llm.max_retries == 3
    record = json.loads(next((tmp_path / "cache").glob("*.json")).read_text())
    assert record["canonical"] == fields and record["status"] == "translated"


def test_structural_contract_rejects_changed_numbers_urls_quotes_and_missing_ids() -> None:
    originals = {"a": 'Only 20 clients; "preview". https://example.com/a'}
    for bad in ('Только 21 клиентов; "preview". https://example.com/a',
                'Только 20 клиентов; "preview". https://example.com/b',
                'Только 20 клиентов; "GA". https://example.com/a'):
        with pytest.raises(ValueError, match="protected"):
            _parse(json.dumps({"translations": [{"id": "a", "text": bad}]}), originals)
    with pytest.raises(ValueError, match="coverage"):
        _parse('{"translations": []}', originals)
    # This layer deliberately cannot prove that a qualifier's meaning survived.
    assert _parse('{"translations":[{"id":"a","text":"Все клиенты."}]}',
                  {"a": "Only participating clients."})["a"] == "Все клиенты."


@pytest.mark.asyncio
async def test_incomplete_completion_falls_back_and_is_not_automatically_retried(tmp_path: Path) -> None:
    cfg = config()
    fields = {"a": "Only participating clients."}
    raw = '{"translations":[{"id":"a","text":"Только участвующие клиенты."}]}'
    with patch("digest.translation.complete", AsyncMock(return_value=(raw, {"finish_reason": "length"}))) as call:
        first = await translate_fields(fields, cfg, tmp_path)
        repeated = await translate_fields(fields, cfg, tmp_path)
    assert first.fields == repeated.fields == fields
    assert first.status == repeated.status == "fallback"
    call.assert_awaited_once()
    record = json.loads(next(tmp_path.glob("*.json")).read_text())
    assert record["error_kind"] == "completion_incomplete"


@pytest.mark.asyncio
async def test_cache_unavailable_and_reserved_attempt_never_make_a_request(tmp_path: Path) -> None:
    cfg = config()
    fields = {"a": "Only participating clients."}
    raw = '{"translations":[{"id":"a","text":"Только участвующие клиенты."}]}'
    with patch("digest.translation.complete", AsyncMock(return_value=(raw, {"finish_reason": "stop"}))):
        await translate_fields(fields, cfg, tmp_path / "cache")
    record_path = next((tmp_path / "cache").glob("*.json"))
    record = json.loads(record_path.read_text())
    record["status"] = "reserved"
    record_path.write_text(json.dumps(record))
    blocked = tmp_path / "not-directory"
    blocked.write_text("existing")
    with patch("digest.translation.complete", AsyncMock(side_effect=AssertionError("No request"))) as call:
        result = await translate_fields(fields, cfg, tmp_path / "cache")
        failed = await translate_fields(fields, cfg, blocked)
    assert result.status == failed.status == "fallback"
    call.assert_not_called()


@pytest.mark.asyncio
async def test_budget_resumes_cached_progress_without_mixed_language_output(tmp_path: Path) -> None:
    cfg = config()
    cfg.translation = replace(cfg.translation, max_input_chars=256)
    fields = {"a": "Only clients. " * 10, "b": "Only partners. " * 10}

    async def answer(_role, messages, _config, **_kwargs):
        supplied = json.loads(messages[1]["content"])["fields"]
        return json.dumps({"translations": [{"id": x["id"], "text": "Только участники."}
                                             for x in supplied]}), {"finish_reason": "stop"}

    with patch("digest.translation.complete", side_effect=answer) as call:
        first = await translate_fields(fields, cfg, tmp_path)
        second = await translate_fields(fields, cfg, tmp_path)
    assert first.status == "fallback" and first.fields == fields
    assert second.status == "translated" and call.call_count == 2 and second.cache_hits == 1


@pytest.mark.asyncio
async def test_primary_view_keeps_identity_and_dry_run_uses_only_temporary_cache(tmp_path: Path) -> None:
    cfg = config()
    card = ArticleSummary("Original title", "https://example.com/a", "Source", "Category", "Only clients.")
    original = asdict(card)

    async def answer(_role, messages, _config, **_kwargs):
        supplied = json.loads(messages[1]["content"])["fields"]
        return json.dumps({"translations": [{"id": x["id"], "text": "Только участники."}
                                             for x in supplied]}), {"finish_reason": "stop"}

    with patch("digest.translation.complete", side_effect=answer):
        summary, cards = await translate_primary_presentation("Only clients.", [card], cfg, tmp_path / "cache")
        preview, preview_cards = await _primary_presentation("Only clients.", [card], cfg, tmp_path / "unused", True)
    assert asdict(card) == original
    assert cards[0].title == card.title and cards[0].link == card.link
    assert article_hash(cards[0].title, cards[0].link) == article_hash(card.title, card.link)
    assert cards[0].summary == preview_cards[0].summary and summary == preview
    assert "not independently verified" in summary
    assert not (tmp_path / "unused").exists()


@pytest.mark.asyncio
async def test_pipeline_uses_one_presentation_for_both_outputs_and_keeps_raw_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from digest.delivery import ArticleDeliveryResult
    from digest.main import run
    from scripts.review_fixture import fixture_articles, fixture_response

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    cfg = config()
    cfg.review.review_led_only = True
    cfg.telegram.enabled = cfg.telegram.required = cfg.obsidian.enabled = True
    cfg.obsidian.output_dir = str(tmp_path / "digests")

    async def translate(_role, messages, _config, **_kwargs):
        supplied = json.loads(messages[1]["content"])["fields"]
        return json.dumps({"translations": [{"id": x["id"], "text": "Перевод: " + x["text"]}
                                             for x in supplied]}), {"finish_reason": "stop"}

    with (
        patch("digest.config.load_config", return_value=cfg),
        patch("digest.radar.collect", AsyncMock(return_value=(fixture_articles(), {}))),
        patch("digest.review.complete", side_effect=fixture_response),
        patch("digest.translation.complete", side_effect=translate) as translation,
        patch("digest.delivery.send_article_cards", AsyncMock(
            return_value=ArticleDeliveryResult(attempted=2, sent=2),
        )) as delivery,
        patch("digest.delivery.send_counter_signals", AsyncMock(side_effect=AssertionError("Separate stage"))),
    ):
        result = await run("fixture.yaml", False, False, False)
    translation.assert_awaited_once()
    cards = delivery.call_args.kwargs["top_articles"]
    assert all(card.summary.startswith("Перевод: ") for card in cards)
    markdown = Path(result.markdown_path).read_text()
    assert all(card.summary in markdown for card in cards)
    raw = Path(result.markdown_path).with_suffix(".review.json").read_text()
    assert "Перевод: " not in raw
    assert result.telegram_sent and not result.required_delivery_failed


@pytest.mark.asyncio
async def test_pacing_over_budget_is_not_reserved_and_can_run_when_eligible(tmp_path: Path) -> None:
    import time

    from digest.llm import _request_state

    cfg = config()
    cfg.llm.min_request_interval_seconds = 65
    cfg.translation = replace(cfg.translation, timeout_seconds=30)
    state = _request_state(cfg)
    state.next_request_at = time.monotonic() + 65
    fields = {"a": "Only participating clients."}
    raw = '{"translations":[{"id":"a","text":"Только участвующие клиенты."}]}'
    with patch("digest.translation.complete", AsyncMock(return_value=(raw, {"finish_reason": "stop"}))) as call:
        held = await translate_fields(fields, cfg, tmp_path)
        assert held.status == "fallback" and held.calls == 0
        assert "provider_wait_exceeds_time_allowance" in held.reasons
        assert not list(tmp_path.glob("*.json"))
        state.next_request_at = 0  # Later eligible pass; no real waiting or HTTP.
        resumed = await translate_fields(fields, cfg, tmp_path)
    assert resumed.status == "translated"
    call.assert_awaited_once()
    declared = write_config(tmp_path, "\ntranslation: {enabled: false, timeout_seconds: 90}\n")
    assert declared.translation.timeout_seconds == 90


@pytest.mark.asyncio
async def test_anthropic_normal_completion_is_distinct_from_budget_and_tool_stop() -> None:
    import httpx

    from digest.llm import _anthropic_call

    for reason, expected in [("end_turn", "stop"), ("max_tokens", "max_tokens"), ("tool_use", "tool_use")]:
        client = AsyncMock()
        client.post.return_value = httpx.Response(200, request=httpx.Request("POST", "https://example.com"), json={
            "content": [{"text": "complete visible text"}], "stop_reason": reason,
        })
        _, usage = await _anthropic_call(client, "synthetic-key", "synthetic-model", [], 0)
        assert usage["finish_reason"] == expected


@pytest.mark.asyncio
async def test_radar_only_preview_translates_without_running_irritator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    from digest.main import run
    from digest.radar.summarizer import CategorySummary
    from scripts.review_fixture import fixture_articles

    monkeypatch.chdir(tmp_path)
    cfg = config()
    cfg.review.enabled = False
    card = ArticleSummary("Original", "https://example.com/a", "Source", "Category", "Only clients.")

    async def translate(_role, messages, _config, **_kwargs):
        fields = json.loads(messages[1]["content"])["fields"]
        return json.dumps({"translations": [{"id": x["id"], "text": "Перевод: " + x["text"]}
                                             for x in fields]}), {"finish_reason": "stop"}

    with (
        patch("digest.config.load_config", return_value=cfg),
        patch("digest.radar.collect", AsyncMock(return_value=(fixture_articles(), {}))),
        patch("digest.main._analyze_articles", AsyncMock(return_value=(
            [CategorySummary("Category", "Only clients.", 1)], None, [card], None,
        ))),
        patch("digest.translation.complete", side_effect=translate) as translation,
        patch("digest.main._run_irritator", AsyncMock(side_effect=AssertionError("No supplementary stage"))) as stage,
    ):
        result = await run("fixture.yaml", True, True, False)
    assert "Перевод: " in capsys.readouterr().out
    translation.assert_awaited_once()
    stage.assert_not_called()
    assert not result.telegram_sent and not result.markdown_saved
    assert not (tmp_path / ".cache/translations").exists()


@pytest.mark.asyncio
async def test_conflicting_article_identity_keeps_each_original_before_any_model_call(tmp_path: Path) -> None:
    cfg = config()
    first = ArticleSummary("Same title", "https://example.com/a", "Source", "Category", "First condition.")
    second = replace(first, summary="Different material condition.")
    with patch("digest.translation.complete", AsyncMock(side_effect=AssertionError("No call"))) as call:
        _, presented = await translate_primary_presentation("Canonical summary.", [first, second], cfg, tmp_path)
    assert presented[0].summary.startswith(first.summary)
    assert presented[1].summary.startswith(second.summary)
    assert all("canonical English text retained" in item.summary for item in presented)
    assert not list(tmp_path.glob("*.json"))
    call.assert_not_called()
    cfg.translation = replace(cfg.translation, enabled=False)
    original_summary, untouched = await translate_primary_presentation(
        "Canonical summary.", [first, second], cfg, tmp_path,
    )
    assert original_summary == "Canonical summary." and untouched == [first, second]


@pytest.mark.asyncio
async def test_supplement_copies_prose_and_preserves_evidence_with_shared_deadline(tmp_path: Path) -> None:
    import time

    from digest.irritator.evidence_stage import EvidenceIrritatorResult, EvidenceNarrative, EvidenceRankedSignal
    from digest.irritator.sources import Signal
    from digest.llm import _request_state
    from digest.translation import translate_supplement_presentation

    cfg = config()
    source = Signal("https://example.com/source", "Literal title", "Unchanged excerpt.", "hn", "", 0)
    narrative = EvidenceNarrative("Only enrolled clients.", "Category", [], "Reason.", ["S1"],
                                  {"S1": "Literal evidence."})
    ranked = EvidenceRankedSignal(source, 7, "Only the preview was measured.", "Only enrolled clients.",
                                  "complicates", "Literal evidence.")
    canonical = EvidenceIrritatorResult(1, "bundle", "complete", narratives=[narrative], ranked_signals=[ranked])
    original = asdict(canonical)

    async def answer(_role, messages, _config, **_kwargs):
        fields = json.loads(messages[1]["content"])["fields"]
        return json.dumps({"translations": [{"id": x["id"], "text": "Перевод: " + x["text"]}
                                             for x in fields]}), {"finish_reason": "stop"}

    state = _request_state(cfg)
    state.next_request_at = time.monotonic() + 65
    with patch("digest.translation.complete", side_effect=answer) as call:
        held, result = await translate_supplement_presentation(
            canonical, cfg, tmp_path / "held", deadline=time.monotonic() + 45,
        )
        assert result.status == "fallback" and asdict(held) == original
        assert result.reasons == ["provider_wait_exceeds_time_allowance"]
        assert not list((tmp_path / "held").glob("*.json"))
        call.assert_not_called()
        state.next_request_at = 0
        presented, result = await translate_supplement_presentation(
            canonical, cfg, tmp_path / "ready", deadline=time.monotonic() + 90,
        )
    assert result.status == "translated" and asdict(canonical) == original
    assert presented.narratives[0].claim.startswith("Перевод:")
    assert presented.narratives[0].quotes == narrative.quotes
    assert presented.ranked_signals[0].signal is source
    assert presented.ranked_signals[0].quote == ranked.quote
    assert presented.ranked_signals[0].reasoning.startswith("Перевод:")
    call.assert_awaited_once()


@pytest.mark.asyncio
async def test_legacy_primary_and_supplement_use_one_translation_budget(tmp_path: Path) -> None:
    from digest.irritator.ranker import RankedSignal
    from digest.irritator.sources import Signal
    from digest.translation import translate_publication_presentation

    cfg = config()
    item = RankedSignal(Signal("https://example.com/source", "Title", "Excerpt", "hn", "", 0),
                        7, "Only a preview.", "Only enrolled clients.")

    async def answer(_role, messages, _config, **_kwargs):
        fields = json.loads(messages[1]["content"])["fields"]
        assert {field["id"] for field in fields} == {
            "category_digest", "signal:0:narrative_claim", "signal:0:reasoning",
        }
        return json.dumps({"translations": [{"id": x["id"], "text": "Перевод: " + x["text"]}
                                             for x in fields]}), {"finish_reason": "stop"}

    with patch("digest.translation.complete", side_effect=answer) as call:
        summary, _, ranked = await translate_publication_presentation("Only a preview.", [], [item], cfg, tmp_path)
    assert summary.startswith("Перевод:") and ranked[0].reasoning.startswith("Перевод:")
    assert ranked[0].signal is item.signal and item.reasoning == "Only a preview."
    call.assert_awaited_once()


@pytest.mark.asyncio
async def test_explicit_review_translation_route_preserves_ordinary_roles_and_cache_identity(tmp_path: Path) -> None:
    import yaml

    data = yaml.safe_load(textwrap.dedent(MINIMAL_CONFIG))
    data["review"] = {"tie_breaker": {"provider": "groq", "model": "configured-presentation-model"}}
    data["translation"] = {"enabled": True, "provider": "groq", "model": "configured-presentation-model"}
    path = tmp_path / "route.yaml"
    path.write_text(yaml.safe_dump(data))
    cfg = load_config(path)
    assert [(p.name, p.model, p.role) for p in cfg.llm.providers] == [
        ("groq", "llama-3.3-70b-versatile", ["summarize"]),
    ]
    response = '{"translations":[{"id":"a","text":"Только участники."}]}'
    with patch("digest.translation.complete", AsyncMock(return_value=(response, {"finish_reason": "stop"}))) as call:
        result = await translate_fields({"a": "Only participants."}, cfg, tmp_path / "cache")
    assert result.status == "translated"
    assert call.call_args.kwargs["provider_override"] == ProviderConfig("groq", "configured-presentation-model")
    record = json.loads(next((tmp_path / "cache").glob("*.json")).read_text())
    assert record["provider"] == "groq" and record["model"] == "configured-presentation-model"
    # Neither an arbitrary model nor an implicit default review slot authorizes reuse.
    for provider, model in (("groq", "unconfigured-model"),
                            (cfg.review.primary.provider, cfg.review.primary.model)):
        data["translation"]["provider"] = provider
        data["translation"]["model"] = model
        path.write_text(yaml.safe_dump(data))
        with pytest.raises(ValueError, match="already present"):
            load_config(path)

@pytest.mark.asyncio
async def test_compact_translation_has_one_global_notice_and_keeps_article_identity(tmp_path: Path) -> None:
    from digest.translation import TranslationResult

    cfg = config()
    cfg.telegram.delivery_mode = "compact"
    card = ArticleSummary("Title", "https://example.com/a", "Source", "Category", "Only participating clients.")
    identity = f"article:{article_hash(card.title, card.link)}"
    result = TranslationResult(
        {"category_digest": "Общий обзор.", identity: "Только участвующие клиенты."}, "translated",
    )
    with patch("digest.translation.translate_fields", AsyncMock(return_value=result)) as translate:
        summary, cards = await translate_primary_presentation("Overview.", [card], cfg, tmp_path)
    assert summary.count(result.notice) == 1 and result.notice not in cards[0].summary
    assert cards[0].summary == result.fields[identity]
    assert (cards[0].title, cards[0].link, cards[0].source) == (card.title, card.link, card.source)
    translate.assert_awaited_once()
