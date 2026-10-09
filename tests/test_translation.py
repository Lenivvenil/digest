"""Presentation translation preserves identity and fails back without hidden calls."""

from __future__ import annotations

import json
import textwrap
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from digest.adapters.models.execution import ModelExecution
from digest.application.presentation import primary_presentation as _primary_presentation
from digest.config import ProviderConfig, TranslationConfig, load_config
from digest.radar.collector import article_hash
from digest.radar.summarizer import ArticleSummary
from digest.translation import TranslationResult, _parse, translate_fields, translate_primary_presentation
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
    enabled = write_config(
        tmp_path,
        "\ntranslation:\n  enabled: true\n  provider: groq\n  model: llama-3.3-70b-versatile\n  target_language: ru\n",
    )
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
    execution = ModelExecution()
    cfg = fixture_config()
    fields = {"a": "English original."}
    with patch("digest.translation.complete", AsyncMock(side_effect=AssertionError("No request"))) as call:
        result = await translate_fields(fields, cfg, tmp_path / "absent", execution=execution)
    assert result.fields == fields and result.status == "disabled" and not (tmp_path / "absent").exists()
    call.assert_not_called()
    assert execution._state is None


def test_structural_contract_rejects_changed_numbers_urls_quotes_and_missing_ids() -> None:
    originals = {"a": 'Only 20 clients; "preview". https://example.com/a'}
    for bad in (
        'Только 21 клиентов; "preview". https://example.com/a',
        'Только 20 клиентов; "preview". https://example.com/b',
        'Только 20 клиентов; "GA". https://example.com/a',
    ):
        with pytest.raises(ValueError, match="protected"):
            _parse(json.dumps({"translations": [{"id": "a", "text": bad}]}), originals)
    with pytest.raises(ValueError, match="coverage"):
        _parse('{"translations": []}', originals)
    # This layer deliberately cannot prove that a qualifier's meaning survived.
    assert (
        _parse('{"translations":[{"id":"a","text":"Все клиенты."}]}', {"a": "Only participating clients."})["a"]
        == "Все клиенты."
    )


@pytest.mark.asyncio
async def test_incomplete_completion_falls_back_and_is_not_automatically_retried(tmp_path: Path) -> None:
    execution = ModelExecution()
    cfg = config()
    fields = {"a": "Only participating clients."}
    raw = '{"translations":[{"id":"a","text":"Только участвующие клиенты."}]}'
    with patch("digest.translation.complete", AsyncMock(return_value=(raw, {"finish_reason": "length"}))) as call:
        first = await translate_fields(fields, cfg, tmp_path, execution=execution)
        repeated = await translate_fields(fields, cfg, tmp_path, execution=execution)
    assert first.fields == repeated.fields == fields
    assert first.status == repeated.status == "fallback"
    call.assert_awaited_once()
    record = json.loads(next(tmp_path.glob("*.json")).read_text())
    assert record["error_kind"] == "completion_incomplete"


@pytest.mark.asyncio
async def test_cache_unavailable_and_reserved_attempt_never_make_a_request(tmp_path: Path) -> None:
    execution = ModelExecution()
    cfg = config()
    fields = {"a": "Only participating clients."}
    raw = '{"translations":[{"id":"a","text":"Только участвующие клиенты."}]}'
    with patch("digest.translation.complete", AsyncMock(return_value=(raw, {"finish_reason": "stop"}))):
        await translate_fields(fields, cfg, tmp_path / "cache", execution=execution)
    record_path = next((tmp_path / "cache").glob("*.json"))
    record = json.loads(record_path.read_text())
    record["status"] = "reserved"
    record_path.write_text(json.dumps(record))
    blocked = tmp_path / "not-directory"
    blocked.write_text("existing")
    with patch("digest.translation.complete", AsyncMock(side_effect=AssertionError("No request"))) as call:
        result = await translate_fields(fields, cfg, tmp_path / "cache", execution=execution)
        failed = await translate_fields(fields, cfg, blocked, execution=execution)
    assert result.status == failed.status == "fallback"
    call.assert_not_called()


@pytest.mark.asyncio
async def test_budget_resumes_cached_progress_without_mixed_language_output(tmp_path: Path) -> None:
    execution = ModelExecution()
    cfg = config()
    cfg.translation = replace(cfg.translation, max_input_chars=256)
    fields = {"a": "Only clients. " * 10, "b": "Only partners. " * 10}

    async def answer(_role, messages, _config, **_kwargs):
        supplied = json.loads(messages[1]["content"])["fields"]
        return json.dumps({"translations": [{"id": x["id"], "text": "Только участники."} for x in supplied]}), {
            "finish_reason": "stop"
        }

    with patch("digest.translation.complete", side_effect=answer) as call:
        first = await translate_fields(fields, cfg, tmp_path, execution=execution)
        second = await translate_fields(fields, cfg, tmp_path, execution=execution)
    assert first.status == "fallback" and first.fields == fields
    assert second.status == "translated" and call.call_count == 2 and second.cache_hits == 1


@pytest.mark.asyncio
async def test_primary_view_keeps_identity_and_dry_run_uses_only_temporary_cache(tmp_path: Path) -> None:
    execution = ModelExecution()
    cfg = config()
    card = ArticleSummary("Original title", "https://example.com/a", "Source", "Category", "Only clients.")
    original = asdict(card)

    async def answer(_role, messages, _config, **_kwargs):
        supplied = json.loads(messages[1]["content"])["fields"]
        return json.dumps({"translations": [{"id": x["id"], "text": "Только участники."} for x in supplied]}), {
            "finish_reason": "stop"
        }

    with patch("digest.translation.complete", side_effect=answer):
        summary, cards = await translate_primary_presentation(
            "Only clients.", [card], cfg, tmp_path / "cache", execution=execution
        )
        preview, preview_cards = await _primary_presentation(
            "Only clients.", [card], cfg, tmp_path / "unused", True, execution=execution
        )
    assert asdict(card) == original
    assert cards[0].title == card.title and cards[0].link == card.link
    assert article_hash(cards[0].title, cards[0].link) == article_hash(card.title, card.link)
    assert cards[0].summary == preview_cards[0].summary and summary == preview
    assert "not independently verified" in summary
    assert not (tmp_path / "unused").exists()


@pytest.mark.asyncio
async def test_primary_preview_cleans_temporary_cache_when_presentation_raises(tmp_path: Path) -> None:
    execution = ModelExecution()
    temporary_cache: Path | None = None
    failure = ValueError("Invalid presentation cache")

    async def fail(_summary, _cards, signals, _config, cache, *, execution):
        nonlocal temporary_cache
        assert signals == [] and cache.is_dir()
        temporary_cache = cache
        (cache / "partial.json").write_text("{}")
        raise failure

    with patch("digest.translation.translate_publication_presentation", side_effect=fail):
        with pytest.raises(ValueError) as raised:
            await _primary_presentation("Canonical", [], config(), tmp_path / "persistent", True, execution=execution)
    assert raised.value is failure
    assert temporary_cache is not None and not temporary_cache.exists()
    assert not (tmp_path / "persistent").exists()


@pytest.mark.asyncio
async def test_pipeline_uses_one_presentation_for_both_outputs_and_keeps_raw_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
        return json.dumps({"translations": [{"id": x["id"], "text": "Перевод: " + x["text"]} for x in supplied]}), {
            "finish_reason": "stop"
        }

    with (
        patch("digest.config.load_config", return_value=cfg),
        patch("digest.radar.collect", AsyncMock(return_value=(fixture_articles(), {}))),
        patch("digest.application.review.complete", side_effect=fixture_response),
        patch("digest.translation.complete", side_effect=translate) as translation,
        patch(
            "digest.delivery.send_article_cards",
            AsyncMock(
                return_value=ArticleDeliveryResult(attempted=2, sent=2),
            ),
        ) as delivery,
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
    execution = ModelExecution()
    import time

    from digest.llm import _request_state

    cfg = config()
    cfg.llm.min_request_interval_seconds = 65
    cfg.translation = replace(cfg.translation, timeout_seconds=30)
    state = _request_state(cfg, execution)
    state.next_request_at = time.monotonic() + 65
    fields = {"a": "Only participating clients."}
    raw = '{"translations":[{"id":"a","text":"Только участвующие клиенты."}]}'
    with patch("digest.translation.complete", AsyncMock(return_value=(raw, {"finish_reason": "stop"}))) as call:
        held = await translate_fields(fields, cfg, tmp_path, execution=execution)
        assert held.status == "fallback" and held.calls == 0
        assert "provider_wait_exceeds_time_allowance" in held.reasons
        assert not list(tmp_path.glob("*.json"))
        state.next_request_at = 0  # Later eligible pass; no real waiting or HTTP.
        resumed = await translate_fields(fields, cfg, tmp_path, execution=execution)
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
        client.post.return_value = httpx.Response(
            200,
            request=httpx.Request("POST", "https://example.com"),
            json={
                "content": [{"text": "complete visible text"}],
                "stop_reason": reason,
            },
        )
        _, usage = await _anthropic_call(client, "synthetic-key", "synthetic-model", [], 0)
        assert usage["finish_reason"] == expected


@pytest.mark.asyncio
async def test_radar_only_preview_translates_without_running_irritator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
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
        return json.dumps({"translations": [{"id": x["id"], "text": "Перевод: " + x["text"]} for x in fields]}), {
            "finish_reason": "stop"
        }

    with (
        patch("digest.config.load_config", return_value=cfg),
        patch("digest.radar.collect", AsyncMock(return_value=(fixture_articles(), {}))),
        patch(
            "digest.application.analysis.analyze_articles",
            AsyncMock(
                return_value=(
                    [CategorySummary("Category", "Only clients.", 1)],
                    None,
                    [card],
                    None,
                )
            ),
        ),
        patch("digest.translation.complete", side_effect=translate) as translation,
        patch(
            "digest.application.investigation.run_irritator",
            AsyncMock(side_effect=AssertionError("No supplementary stage")),
        ) as stage,
    ):
        result = await run("fixture.yaml", True, True, False)
    assert "Перевод: " in capsys.readouterr().out
    translation.assert_awaited_once()
    stage.assert_not_called()
    assert not result.telegram_sent and not result.markdown_saved
    assert not (tmp_path / ".cache/translations").exists()


@pytest.mark.asyncio
async def test_conflicting_article_identity_keeps_each_original_before_any_model_call(tmp_path: Path) -> None:
    execution = ModelExecution()
    cfg = config()
    first = ArticleSummary("Same title", "https://example.com/a", "Source", "Category", "First condition.")
    second = replace(first, summary="Different material condition.")
    with patch("digest.translation.complete", AsyncMock(side_effect=AssertionError("No call"))) as call:
        _, presented = await translate_primary_presentation(
            "Canonical summary.", [first, second], cfg, tmp_path, execution=execution
        )
    assert presented[0].summary.startswith(first.summary)
    assert presented[1].summary.startswith(second.summary)
    assert all("canonical English text retained" in item.summary for item in presented)
    assert not list(tmp_path.glob("*.json"))
    call.assert_not_called()
    cfg.translation = replace(cfg.translation, enabled=False)
    original_summary, untouched = await translate_primary_presentation(
        "Canonical summary.",
        [first, second],
        cfg,
        tmp_path,
        execution=execution,
    )
    assert original_summary == "Canonical summary." and untouched == [first, second]


@pytest.mark.asyncio
async def test_supplement_copies_prose_and_preserves_evidence_with_shared_deadline(tmp_path: Path) -> None:
    execution = ModelExecution()
    import time

    from digest.irritator.evidence_stage import (
        EvidenceIrritatorResult,
        EvidenceNarrative,
        EvidenceRankedSignal,
        _admit_ranking,
    )
    from digest.irritator.sources import Signal
    from digest.llm import _request_state
    from digest.translation import translate_supplement_presentation

    cfg = config()
    source = Signal("https://example.com/source", "Literal title", "Unchanged excerpt.", "hn", "", 0)
    narrative = EvidenceNarrative(
        "Only enrolled clients.", "Category", [], "Reason.", ["S1"], {"S1": "Literal evidence."}
    )
    ranked = EvidenceRankedSignal(
        source, 7, "Only the preview was measured.", "Only enrolled clients.", "complicates", "Literal evidence."
    )
    canonical = EvidenceIrritatorResult(1, "bundle", "complete", narratives=[narrative], ranked_signals=[ranked])
    private = replace(source, title="PRIVATE AUDIT SENTINEL", snippet="Private diagnostic evidence.")
    canonical.ranking_audit = _admit_ranking([private], 5, 3, {}).audit
    original = asdict(canonical)

    async def answer(_role, messages, _config, **_kwargs):
        assert "PRIVATE AUDIT SENTINEL" not in json.dumps(messages)
        fields = json.loads(messages[1]["content"])["fields"]
        return json.dumps({"translations": [{"id": x["id"], "text": "Перевод: " + x["text"]} for x in fields]}), {
            "finish_reason": "stop"
        }

    state = _request_state(cfg, execution)
    state.next_request_at = time.monotonic() + 65
    with patch("digest.translation.complete", side_effect=answer) as call:
        held, result = await translate_supplement_presentation(
            canonical,
            cfg,
            tmp_path / "held",
            deadline=time.monotonic() + 45,
            execution=execution,
        )
        assert result.status == "fallback" and asdict(held) == original
        assert result.reasons == ["provider_wait_exceeds_time_allowance"]
        assert not list((tmp_path / "held").glob("*.json"))
        call.assert_not_called()
        state.next_request_at = 0
        presented, result = await translate_supplement_presentation(
            canonical,
            cfg,
            tmp_path / "ready",
            deadline=time.monotonic() + 90,
            execution=execution,
        )
    assert result.status == "translated" and asdict(canonical) == original
    assert presented.narratives[0].claim.startswith("Перевод:")
    assert presented.narratives[0].quotes == narrative.quotes
    assert presented.ranked_signals[0].signal is source
    assert presented.ranked_signals[0].quote == ranked.quote
    assert presented.ranked_signals[0].reasoning.startswith("Перевод:")
    assert presented.ranking_audit is canonical.ranking_audit
    call.assert_awaited_once()


@pytest.mark.asyncio
async def test_legacy_primary_and_supplement_use_one_translation_budget(tmp_path: Path) -> None:
    execution = ModelExecution()
    from digest.irritator.ranker import RankedSignal
    from digest.irritator.sources import Signal
    from digest.translation import translate_publication_presentation

    cfg = config()
    item = RankedSignal(
        Signal("https://example.com/source", "Title", "Excerpt", "hn", "", 0),
        7,
        "Only a preview.",
        "Only enrolled clients.",
    )

    async def answer(_role, messages, _config, **_kwargs):
        fields = json.loads(messages[1]["content"])["fields"]
        assert {field["id"] for field in fields} == {
            "category_digest",
            "signal:0:narrative_claim",
            "signal:0:reasoning",
        }
        return json.dumps({"translations": [{"id": x["id"], "text": "Перевод: " + x["text"]} for x in fields]}), {
            "finish_reason": "stop"
        }

    with patch("digest.translation.complete", side_effect=answer) as call:
        summary, _, ranked = await translate_publication_presentation(
            "Only a preview.", [], [item], cfg, tmp_path, execution=execution
        )
    assert summary.startswith("Перевод:") and ranked[0].reasoning.startswith("Перевод:")
    assert ranked[0].signal is item.signal and item.reasoning == "Only a preview."
    call.assert_awaited_once()


@pytest.mark.asyncio
async def test_explicit_review_translation_route_preserves_ordinary_roles_and_cache_identity(tmp_path: Path) -> None:
    execution = ModelExecution()
    import yaml

    data = yaml.safe_load(textwrap.dedent(MINIMAL_CONFIG))
    data["review"] = {"tie_breaker": {"provider": "groq", "model": "configured-presentation-model"}}
    data["translation"] = {"enabled": True, "provider": "groq", "model": "configured-presentation-model"}
    path = tmp_path / "route.yaml"
    path.write_text(yaml.safe_dump(data))
    cfg = load_config(path)
    cfg.llm.max_retries = 3
    assert [(p.name, p.model, p.role) for p in cfg.llm.providers] == [
        ("groq", "llama-3.3-70b-versatile", ["summarize"]),
    ]
    fields = {"a": 'Only 20 clients; "preview". https://example.com/a'}
    translated = {"a": 'Только 20 клиентов; "preview". https://example.com/a'}
    response = json.dumps({"translations": [{"id": "a", "text": translated["a"]}]})
    with patch("digest.translation.complete", AsyncMock(return_value=(response, {"finish_reason": "stop"}))) as call:
        result = await translate_fields(fields, cfg, tmp_path / "cache", execution=execution)
    assert result.status == "translated" and result.fields == translated
    call.assert_awaited_once()
    assert call.call_args.kwargs["provider_override"] == ProviderConfig("groq", "configured-presentation-model")
    assert call.call_args.args[2].llm.max_retries == 0 and cfg.llm.max_retries == 3
    derived = call.call_args.kwargs["execution"]
    assert derived is not execution and execution._state is not None
    assert derived.request_state(call.call_args.args[2].llm) is execution.request_state(cfg.llm)
    assert execution.request_state(cfg.llm).semaphore._value == cfg.llm.max_concurrent_requests
    record = json.loads(next((tmp_path / "cache").glob("*.json")).read_text())
    assert record["provider"] == "groq" and record["model"] == "configured-presentation-model"
    assert record["canonical"] == fields and record["status"] == "translated"
    # Neither an arbitrary model nor an implicit default review slot authorizes reuse.
    for provider, model in (("groq", "unconfigured-model"), (cfg.review.primary.provider, cfg.review.primary.model)):
        data["translation"]["provider"] = provider
        data["translation"]["model"] = model
        path.write_text(yaml.safe_dump(data))
        with pytest.raises(ValueError, match="already present"):
            load_config(path)


@pytest.mark.asyncio
async def test_generated_card_conditions_and_conflict_share_translation_field(tmp_path: Path) -> None:
    execution = ModelExecution()
    cfg = config()
    cfg.telegram.delivery_mode = "compact"
    brief = (
        "Reading brief: The proxy is the sole database boundary. Only pilot clients can use it. "
        "The source contradicts itself about payload logging; this remains unresolved."
    )
    card = ArticleSummary("Title", "https://example.com/a", "Source", "Category", brief)
    identity = article_hash(card.title, card.link)
    translated = "Прокси ограничивает доступ к базе. Только пилотные клиенты. Противоречие о логировании не разрешено."
    supplied_fields = []

    async def complete(_role, messages, *_args, **_kwargs):
        supplied_fields.extend(json.loads(messages[1]["content"])["fields"])
        assert supplied_fields == [{"id": f"article:{identity}", "text": brief}]
        return json.dumps({"translations": [{"id": f"article:{identity}", "text": translated}]}), {
            "finish_reason": "stop",
        }

    with (
        patch("digest.translation.complete", side_effect=complete),
        patch("digest.translation.translate_fields", wraps=translate_fields) as translate,
    ):
        summary, cards = await translate_primary_presentation("", [card], cfg, tmp_path, execution=execution)
    notice = TranslationResult({}, "translated").notice
    assert summary.count(notice) == 1 and notice not in cards[0].summary
    assert (cards[0].title, cards[0].link, cards[0].source) == (card.title, card.link, card.source)
    translate.assert_awaited_once()
    assert cards[0].summary == translated
    assert card.summary == brief


@pytest.mark.asyncio
@pytest.mark.parametrize("target_language", ["de"])
async def test_closing_uses_one_existing_request_and_replays_exact_combined_cache(
    tmp_path: Path,
    target_language: str,
) -> None:
    execution = ModelExecution()
    from digest.llm import _request_state
    from digest.translation import _request_hash, translate_publication_with_closing

    cfg = config()
    cfg.telegram.delivery_mode = "compact"
    cfg.translation = replace(cfg.translation, max_calls=1, target_language=target_language)
    main = ArticleSummary("Main", "https://example.com/main", "Main source", "Tech", "Only 20 pilot clients.")
    closing = ArticleSummary("Closing", "https://example.com/good", "Closing source", "World", "Volunteers helped.")
    state = _request_state(cfg, execution)
    calls: list[list[dict[str, str]]] = []

    async def answer(_role, messages, routed, **kwargs):
        assert kwargs["execution"] is not execution
        assert _request_state(routed, kwargs["execution"]) is state and routed.llm.max_retries == 0
        assert kwargs["max_output_tokens"] == cfg.translation.max_output_tokens
        request = json.loads(messages[1]["content"])
        assert request["target_language"] == target_language == "de"
        fields = request["fields"]
        calls.append(fields)
        return json.dumps(
            {"translations": [{"id": item["id"], "text": "Перевод: " + item["text"]} for item in fields]}
        ), {"finish_reason": "stop"}

    with (
        patch("digest.translation.complete", side_effect=answer) as complete,
        patch("digest.translation.translate_fields", wraps=translate_fields) as translate,
    ):
        first = await translate_publication_with_closing(
            "Overview.",
            [main],
            [],
            closing,
            cfg,
            tmp_path,
            selection_binding="accepted-response",
            execution=execution,
        )
        replay = await translate_publication_with_closing(
            "Overview.",
            [main],
            [],
            closing,
            cfg,
            tmp_path,
            selection_binding="accepted-response",
            execution=execution,
        )
    assert first[:3] == replay[:3] and first[3].card == replay[3].card
    notice = TranslationResult({}, "translated").notice
    assert first[0].count(notice) == 1 and notice not in first[1][0].summary
    assert (first[1][0].title, first[1][0].link, first[1][0].source) == (main.title, main.link, main.source)
    assert first[1][0].summary == "Перевод: Only 20 pilot clients."
    assert first[3].card.summary == "Перевод: Volunteers helped."
    assert (first[3].card.title, first[3].card.link, first[3].card.source) == (
        closing.title,
        closing.link,
        closing.source,
    )
    assert {item["id"] for item in calls[0]} == {
        "category_digest",
        f"article:{article_hash(main.title, main.link)}",
        "closing.summary",
    }
    assert replay[3].translation.cache_hits == 1
    assert translate.await_count == 2
    complete.assert_awaited_once()
    record = json.loads(next(tmp_path.glob("*.json")).read_text())
    assert record["schema_version"] == 2 and record["status"] == "translated"
    assert record["target_language"] == "de"
    assert len(record["response_sha256"]) == 64 and "response" not in record
    assert "closing.summary" not in record["required_fields"]
    assert record["canonical"]["closing.summary"] == closing.summary
    assert record["optional"]["status"] == "translated"
    assert record["selection_sha256"]
    assert record["request_sha256"] == _request_hash(record["canonical"], cfg.translation)
    assert record["max_output_tokens"] == cfg.translation.max_output_tokens and record["temperature"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("defect", ["missing", "number", "duplicate", "wrong_id", "invalid_id"])
async def test_invalid_closing_preserves_valid_main_and_terminal_cache_omission(
    tmp_path: Path,
    defect: str,
) -> None:
    execution = ModelExecution()
    from digest.translation import translate_publication_with_closing

    cfg = config()
    cfg.telegram.delivery_mode = "compact"
    main = ArticleSummary("Main", "https://example.com/main", "Source", "Tech", "Only 20 clients.")
    closing = replace(main, title="Closing", link="https://example.com/good", summary="Helped 10 people.")

    async def answer(_role, messages, _config, **_kwargs):
        fields = json.loads(messages[1]["content"])["fields"]
        entries = [
            {"id": item["id"], "text": "Перевод: " + item["text"]} for item in fields if item["id"] != "closing.summary"
        ]
        optional = {"id": "closing.summary", "text": "Помогли 10 людям."}
        if defect == "number":
            optional["text"] = "Помогли 99 людям."
        elif defect == "wrong_id":
            optional["id"] = "unexpected.optional"
        elif defect == "invalid_id":
            optional["id"] = ["closing.summary"]
        if defect != "missing":
            entries.append(optional)
        if defect == "duplicate":
            entries.append(optional)
        return json.dumps({"translations": entries}), {"finish_reason": "stop"}

    with patch("digest.translation.complete", side_effect=answer) as complete:
        first = await translate_publication_with_closing(
            "Overview.", [main], [], closing, cfg, tmp_path, execution=execution
        )
        replay = await translate_publication_with_closing(
            "Overview.", [main], [], closing, cfg, tmp_path, execution=execution
        )
    assert first[:3] == replay[:3]
    assert first[1][0].summary == "Перевод: Only 20 clients."
    assert first[0].startswith("Перевод: Overview.")
    assert first[3].card is replay[3].card is None
    assert first[3].translation.reasons == replay[3].translation.reasons == ["closing_contract_invalid"]
    complete.assert_awaited_once()
    record = json.loads(next(tmp_path.glob("*.json")).read_text())
    assert record["status"] == "translated" and record["optional"]["status"] == "fallback"


@pytest.mark.asyncio
@pytest.mark.parametrize("defect", ["missing_main", "changed_main_number", "invalid_json", "truncated"])
async def test_invalid_main_keeps_existing_canonical_fallback_without_closing_repair(
    tmp_path: Path,
    defect: str,
) -> None:
    execution = ModelExecution()
    from digest.translation import translate_publication_with_closing

    cfg = config()
    cfg.telegram.delivery_mode = "compact"
    main = ArticleSummary("Main", "https://example.com/main", "Source", "Tech", "Only 20 clients.")
    closing = replace(main, title="Closing", link="https://example.com/good", summary="Volunteers helped.")

    async def answer(_role, messages, _config, **_kwargs):
        fields = json.loads(messages[1]["content"])["fields"]
        entries = [{"id": item["id"], "text": "Перевод: " + item["text"]} for item in fields]
        if defect == "missing_main":
            entries = [item for item in entries if item["id"] == "closing.summary"]
        elif defect == "changed_main_number":
            entries[0]["text"] = "Only 99 clients."
        response = "{" if defect == "invalid_json" else json.dumps({"translations": entries})
        return response, {"finish_reason": "length" if defect == "truncated" else "stop"}

    with patch("digest.translation.complete", side_effect=answer) as complete:
        first = await translate_publication_with_closing("", [main], [], closing, cfg, tmp_path, execution=execution)
        replay = await translate_publication_with_closing("", [main], [], closing, cfg, tmp_path, execution=execution)
    assert first[1] == replay[1] == [main]
    assert "canonical English text retained" in first[0]
    assert first[3].card == replay[3].card == closing
    assert first[3].reason == replay[3].reason == "main_canonical_fallback"
    complete.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("oversized", ["a" * 20000, "я" * 8000])
async def test_oversized_closing_keeps_exact_main_request_cache_bytes_and_output(
    tmp_path: Path,
    oversized: str,
) -> None:
    execution = ModelExecution()
    from datetime import UTC, datetime

    from digest.translation import translate_publication_presentation, translate_publication_with_closing

    cfg = config()
    cfg.telegram.delivery_mode = "compact"
    cfg.translation = replace(cfg.translation, max_input_chars=16000)
    main = ArticleSummary("Main", "https://example.com/main", "Source", "Tech", "Main claim.")
    closing = replace(main, title="Closing", link="https://example.com/good", summary=oversized)
    requests = []

    async def answer(_role, messages, _config, **_kwargs):
        requests.append(messages)
        fields = json.loads(messages[1]["content"])["fields"]
        return json.dumps(
            {"translations": [{"id": item["id"], "text": "Перевод: " + item["text"]} for item in fields]}
        ), {"finish_reason": "stop"}

    with patch("digest.translation.complete", side_effect=answer), patch("digest.translation.datetime") as clock:
        clock.now.return_value = datetime(2026, 1, 1, tzinfo=UTC)
        baseline = await translate_publication_presentation(
            "Overview.", [main], [], cfg, tmp_path / "legacy", execution=execution
        )
        actual = await translate_publication_with_closing(
            "Overview.", [main], [], closing, cfg, tmp_path / "optional", execution=execution
        )
    assert actual[:3] == baseline and actual[3].card is None
    assert requests[0] == requests[1]
    original = next((tmp_path / "legacy").glob("*.json"))
    optional = next((tmp_path / "optional").glob("*.json"))
    assert original.name == optional.name and original.read_bytes() == optional.read_bytes()
    assert json.loads(optional.read_text())["schema_version"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field",
    [
        "response_sha256",
        "optional",
        "required_fields",
        "selection_sha256",
        "schema_version",
    ],
)
async def test_corrupt_combined_cache_cannot_repair_or_publish_unbound_prose(
    tmp_path: Path,
    field: str,
) -> None:
    execution = ModelExecution()
    from digest.translation import translate_publication_with_closing

    cfg = config()
    cfg.telegram.delivery_mode = "compact"
    main = ArticleSummary("Main", "https://example.com/main", "Source", "Tech", "Main claim.")
    closing = replace(main, title="Closing", link="https://example.com/good")

    async def answer(_role, messages, _config, **_kwargs):
        fields = json.loads(messages[1]["content"])["fields"]
        return json.dumps(
            {"translations": [{"id": item["id"], "text": "Перевод: " + item["text"]} for item in fields]}
        ), {"finish_reason": "stop"}

    with patch("digest.translation.complete", side_effect=answer):
        await translate_publication_with_closing("", [main], [], closing, cfg, tmp_path, execution=execution)
    path = next(tmp_path.glob("*.json"))
    record = json.loads(path.read_text())
    record[field] = 2.0 if field == "schema_version" else "tampered"
    path.write_text(json.dumps(record))
    with patch("digest.translation.complete", AsyncMock(side_effect=AssertionError("No repair"))) as complete:
        result = await translate_publication_with_closing("", [main], [], closing, cfg, tmp_path, execution=execution)
    assert result[1] == [main] and result[3].card == closing
    assert result[3].reason == "main_canonical_fallback"
    complete.assert_not_called()


@pytest.mark.asyncio
async def test_combined_cache_is_bound_to_accepted_selection(tmp_path: Path) -> None:
    execution = ModelExecution()
    from digest.translation import translate_publication_with_closing

    cfg = config()
    main = ArticleSummary("Main", "https://example.com/main", "Source", "Tech", "Main claim.")
    closing = replace(main, title="Closing", link="https://example.com/good")

    async def answer(_role, messages, _config, **_kwargs):
        fields = json.loads(messages[1]["content"])["fields"]
        return json.dumps(
            {"translations": [{"id": item["id"], "text": "Перевод: " + item["text"]} for item in fields]}
        ), {"finish_reason": "stop"}

    with patch("digest.translation.complete", side_effect=answer) as complete:
        for selection in ("accepted-one", "accepted-two"):
            result = await translate_publication_with_closing(
                "",
                [main],
                [],
                closing,
                cfg,
                tmp_path,
                selection_binding=selection,
                execution=execution,
            )
            assert result[3].status == "presented"
    assert complete.await_count == 2
    records = [json.loads(path.read_text()) for path in tmp_path.glob("*.json")]
    assert len({record["selection_sha256"] for record in records}) == 2
    assert len({record["binding"] for record in records}) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("malformed", [False, True])
async def test_oversized_optional_output_keeps_main_with_bounded_terminal_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    malformed: bool,
) -> None:
    execution = ModelExecution()
    from digest.translation import translate_publication_with_closing

    cfg = config()
    cfg.telegram.delivery_mode = "compact"
    main = ArticleSummary("Main", "https://example.com/main", "Source", "Tech", "Main claim.")
    closing = replace(main, title="Closing", link="https://example.com/good")
    monkeypatch.setattr("digest.translation.MAX_CACHE_BYTES", 4096)

    async def answer(_role, messages, _config, **_kwargs):
        fields = json.loads(messages[1]["content"])["fields"]
        entries = [
            {"id": item["id"], "text": "Перевод: " + item["text"]} for item in fields if item["id"] != "closing.summary"
        ]
        optional = {"id": "closing.summary", "text": "я" * 3000}
        if malformed:
            optional["unexpected"] = "я" * 3000
        entries.append(optional)
        return json.dumps({"translations": entries}), {"finish_reason": "stop"}

    with patch("digest.translation.complete", side_effect=answer) as complete:
        first = await translate_publication_with_closing("", [main], [], closing, cfg, tmp_path, execution=execution)
        replay = await translate_publication_with_closing("", [main], [], closing, cfg, tmp_path, execution=execution)
    assert first[:3] == replay[:3] and first[1][0].summary == "Перевод: Main claim."
    assert first[3].card is replay[3].card is None
    reason = "closing_contract_invalid" if malformed else "closing_output_allowance"
    assert first[3].translation.reasons == replay[3].translation.reasons == [reason]
    path = next(tmp_path.glob("*.json"))
    assert path.stat().st_size <= 4096 and "response" not in json.loads(path.read_text())
    complete.assert_awaited_once()


@pytest.mark.asyncio
async def test_optional_reservation_overflow_keeps_legacy_main_request_cache_and_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution = ModelExecution()
    from datetime import UTC, datetime

    cfg = config()
    identity = "article:" + "a" * 32
    fields = {identity: "This is the accepted main claim."}
    requests = []
    monkeypatch.setattr("digest.translation.MAX_CACHE_BYTES", 730)

    async def answer(_role, messages, _config, **_kwargs):
        requests.append(messages)
        return json.dumps({"translations": [{"id": identity, "text": "Translated."}]}), {"finish_reason": "stop"}

    with (
        patch("digest.translation.complete", side_effect=answer) as complete,
        patch("digest.translation.datetime") as clock,
    ):
        clock.now.return_value = datetime(2026, 1, 1, tzinfo=UTC)
        baseline = await translate_fields(fields, cfg, tmp_path / "legacy", execution=execution)
        actual = await translate_fields(
            fields, cfg, tmp_path / "optional", closing_summary="Kind.", execution=execution
        )
        replay = await translate_fields(
            fields, cfg, tmp_path / "optional", closing_summary="Kind.", execution=execution
        )
    assert baseline.status == actual.status == replay.status == "translated"
    assert baseline.fields == actual.fields == replay.fields == {identity: "Translated."}
    assert actual.optional.status == replay.optional.status == "fallback"
    assert actual.calls == 1 and replay.calls == 0 and replay.cache_hits == 1
    assert complete.await_count == 2 and requests[0] == requests[1]
    original = next((tmp_path / "legacy").glob("*.json"))
    optional = next((tmp_path / "optional").glob("*.json"))
    assert original.stat().st_size < 730
    assert original.name == optional.name and original.read_bytes() == optional.read_bytes()
    assert json.loads(optional.read_text())["schema_version"] == 1


@pytest.mark.asyncio
async def test_combined_cache_write_failure_preserves_main_without_fabricating_legacy_cache(
    tmp_path: Path,
) -> None:
    execution = ModelExecution()
    from digest import translation

    cfg = config()
    fields = {"main": "Claim."}
    real_write = translation.atomic_json_write

    async def answer(_role, messages, _config, **_kwargs):
        supplied = json.loads(messages[1]["content"])["fields"]
        return json.dumps({"translations": [{"id": item["id"], "text": "Translated."} for item in supplied]}), {
            "finish_reason": "stop"
        }

    def fail_combined_write(path, record):
        if record["schema_version"] == 2:
            raise OSError("Optional combined metadata cannot be persisted")
        real_write(path, record)

    with (
        patch("digest.translation.complete", side_effect=answer) as complete,
        patch("digest.translation.atomic_json_write", side_effect=fail_combined_write),
    ):
        first = await translate_fields(fields, cfg, tmp_path / "combined", closing_summary="Kind.", execution=execution)
        replay = await translate_fields(
            fields, cfg, tmp_path / "combined", closing_summary="Kind.", execution=execution
        )
    assert first.status == "translated" and first.fields == {"main": "Translated."}
    assert first.optional.status == "fallback" and first.optional.reasons == ["closing_cache_unavailable"]
    assert replay.status == "fallback" and replay.fields == fields and replay.calls == 0
    assert "previous_attempt_incomplete" in replay.reasons
    complete.assert_awaited_once()
    records = [json.loads(path.read_text()) for path in (tmp_path / "combined").glob("*.json")]
    assert len(records) == 1 and records[0]["schema_version"] == 2 and records[0]["status"] == "reserved"
    assert "translated" not in records[0]
    with (
        patch("digest.translation.complete", side_effect=answer),
        patch("digest.translation.atomic_json_write", side_effect=OSError("Required main cache failed")),
    ):
        required = await translate_fields(fields, cfg, tmp_path / "main-only", execution=execution)
    assert required.status == "fallback" and required.fields == fields
