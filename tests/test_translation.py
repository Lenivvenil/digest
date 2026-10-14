"""Presentation translation preserves identity and fails back without hidden calls."""

from __future__ import annotations

import hashlib
import json
import os
import textwrap
from copy import deepcopy
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from digest.adapters.models.execution import ModelExecution
from digest.application.presentation import primary_presentation as _primary_presentation
from digest.application.review import run_primary_review
from digest.config import ProviderConfig, TranslationConfig, load_config
from digest.radar.collector import article_hash
from digest.radar.summarizer import ArticleSummary
from digest.translation import TranslationResult, _parse, translate_fields, translate_primary_presentation
from scripts.review_fixture import fixture_articles, fixture_config, fixture_response
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


@pytest.mark.asyncio
async def test_primary_review_pacing_carries_into_translation_without_reading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in tuple(os.environ):
        if name.startswith("DIGEST_MODEL_BUDGET_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("GROQ_API_KEY", "synthetic-offline-key")
    execution = ModelExecution()
    cfg = config()
    assert not cfg.reading_brief.enabled
    cfg.review.primary = cfg.review.secondary
    cfg.translation = replace(cfg.translation, model=cfg.review.primary.model)
    cfg.llm.min_request_interval_seconds = 20
    clock = [100.0]
    waits: list[float] = []
    requests: list[tuple[float, httpx.Request]] = []
    client_class = httpx.AsyncClient

    async def sleep(delay: float) -> None:
        waits.append(delay)
        clock[0] += delay

    async def respond(request: httpx.Request) -> httpx.Response:
        requests.append((clock[0], request))
        payload = json.loads(request.content)
        if len(requests) == 1:
            text, _ = await fixture_response(
                None,
                payload["messages"],
                cfg,
                provider_override=ProviderConfig(cfg.review.primary.provider, cfg.review.primary.model),
            )
            clock[0] += 7.5
        else:
            text = '{"translations":[{"id":"a","text":"Только участвующие клиенты."}]}'
        return httpx.Response(200, json={"choices": [{"message": {"content": text}, "finish_reason": "stop"}]})

    with (
        patch("digest.llm.time.monotonic", side_effect=lambda: clock[0]),
        patch("digest.llm.asyncio.sleep", side_effect=sleep),
        patch(
            "digest.llm.httpx.AsyncClient",
            side_effect=lambda: client_class(
                transport=httpx.MockTransport(respond),
                trust_env=False,
            ),
        ),
    ):
        review = await run_primary_review(fixture_articles(), cfg, execution=execution)
        assert review.report.reviews[0].status == "ok" and clock[0] == 107.5
        translated = await translate_fields(
            {"a": "Only participating clients."},
            cfg,
            tmp_path,
            execution=execution,
        )

    assert translated.status == "translated" and translated.calls == 1
    assert translated.fields == {"a": "Только участвующие клиенты."}
    assert [started for started, _ in requests] == [100.0, 120.0]
    assert waits == [12.5]
    assert execution.request_state(cfg.llm).requests_attempted == 2
    assert [str(request.url) for _, request in requests] == [
        "https://api.groq.com/openai/v1/chat/completions",
    ] * 2
    payloads = [json.loads(request.content) for _, request in requests]
    assert [payload["model"] for payload in payloads] == [cfg.review.primary.model, cfg.translation.model]
    assert [payload["temperature"] for payload in payloads] == [0.2, 0]
    assert [payload["max_completion_tokens"] for payload in payloads] == [
        cfg.review.max_output_tokens,
        cfg.translation.max_output_tokens,
    ]
    assert payloads[0]["reasoning_effort"] == "low" and payloads[0]["response_format"]["type"] == "json_schema"
    assert "reasoning_effort" not in payloads[1] and "response_format" not in payloads[1]


def test_structural_contract_rejects_changed_numbers_urls_quotes_and_missing_ids() -> None:
    originals = {"a": 'Only 20 clients; "preview". https://example.com/a'}
    for bad, changed in (
        ('Только 21 клиентов; "preview". https://example.com/a', ("numeric_literal",)),
        ('Только 20 клиентов; "preview". https://example.com/b', ("url",)),
        ('Только 20 клиентов; "GA". https://example.com/a', ("quotation",)),
        ('Только 21 клиентов; "GA". https://example.com/b', ("url", "numeric_literal", "quotation")),
    ):
        with pytest.raises(ValueError, match="protected") as caught:
            _parse(json.dumps({"translations": [{"id": "a", "text": bad}]}), originals)
        assert caught.value.field_id == "a" and caught.value.changed == changed
    with pytest.raises(ValueError, match="coverage"):
        _parse('{"translations": []}', originals)
    # This layer deliberately cannot prove that a qualifier's meaning survived.
    assert (
        _parse('{"translations":[{"id":"a","text":"Все клиенты."}]}', {"a": "Only participating clients."})["a"]
        == "Все клиенты."
    )


@pytest.mark.parametrize(("original", "translated", "accepted"), [
    ("20ms", "20 мс", True),
    ("1.5ms", "1.5 мс", True),
    ("100MB/s", "100 МБ/с", True),
    ("+20ms -1,5ms 20-30ms 10:20ms 1/2ms 20%load", "+20 мс -1,5 мс 20-30 мс 10:20 мс 1/2 мс 20% загрузки", True),
    ("model20", "model30", True),  # The leading word boundary is unchanged.
    ("20ms", "30ms", False),
    ("1.5ms", "1.9ms", False),
    ("100MB/s", "200MB/s", False),
    ("1.5ms", "1,5 мс", False),  # Literal spelling, not numeric equivalence.
    ("+20ms", "20ms", False),
    ("-20ms", "20ms", False),
    ("20%load", "20load", False),
    ("model.20ms", "model.30ms", False),  # Punctuation can start a recognized digit run.
    ("20ms 20ms", "20ms", False),
])
def test_numeric_contract_preserves_full_attached_literals(
    original: str, translated: str, accepted: bool,
) -> None:
    raw = json.dumps({"translations": [{"id": "a", "text": translated}]})
    if accepted:
        assert _parse(raw, {"a": original}) == {"a": translated}
    else:
        with pytest.raises(ValueError, match="protected") as caught:
            _parse(raw, {"a": original})
        assert caught.value.field_id == "a" and caught.value.changed == ("numeric_literal",)


@pytest.mark.asyncio
@pytest.mark.parametrize(("combined", "changed_field"), [(False, "a"), (True, "a"), (True, "closing.summary")])
async def test_saved_attached_numbers_revalidate_without_rewrite_or_retry(
    tmp_path: Path, combined: bool, changed_field: str,
) -> None:
    from digest.translation import _digest

    cfg, execution = config(), ModelExecution()
    fields = {"a": "Latency 20ms."}
    closing = "Help arrived in 20ms." if combined else None
    translated = {"a": "Задержка 20ms."}
    if combined:
        translated["closing.summary"] = "Помощь за 20ms."
    raw = json.dumps({"translations": [{"id": key, "text": value} for key, value in translated.items()]})
    with patch("digest.translation.complete", AsyncMock(return_value=(raw, {"finish_reason": "stop"}))) as call:
        first = await translate_fields(fields, cfg, tmp_path, execution=execution, closing_summary=closing)
    call.assert_awaited_once()
    assert first.status == "translated"
    path = next(tmp_path.glob("*.json"))
    saved = path.read_bytes()
    with patch("digest.translation.complete", AsyncMock(side_effect=AssertionError("No request"))) as call:
        reused = await translate_fields(fields, cfg, tmp_path, execution=execution, closing_summary=closing)
        assert reused.status == "translated" and reused.fields == first.fields
        assert reused.calls == 0 and reused.cache_hits == 1 and path.read_bytes() == saved
        if combined:
            assert reused.optional.status == "translated" and reused.optional.cache_hits == 1
            assert reused.optional.fields == {"closing.summary": translated["closing.summary"]}

        # Rehash a formerly accepted changed value, so rejection must come from the numeric contract.
        record = json.loads(saved)
        translated[changed_field] = translated[changed_field].replace("20ms", "30ms")
        if changed_field == "a":
            record["translated"]["a"] = translated["a"]
            record["translated_sha256"] = _digest(record["translated"])
        else:
            record["optional"]["fields"][changed_field] = translated[changed_field]
            record["optional_sha256"] = _digest(record["optional"])
        if combined:
            changed_raw = json.dumps({
                "translations": [{"id": key, "text": value} for key, value in translated.items()],
            })
            record["response_sha256"] = hashlib.sha256(changed_raw.encode()).hexdigest()
        path.write_text(json.dumps(record, indent=2))
        changed_bytes = path.read_bytes()
        rejected = await translate_fields(fields, cfg, tmp_path, execution=execution, closing_summary=closing)
    call.assert_not_called()
    assert rejected.status == "fallback" and rejected.fields == fields
    assert rejected.reasons == ["cache_unavailable_or_invalid"]
    assert rejected.calls == rejected.cache_hits == 0 and path.read_bytes() == changed_bytes
    assert list(tmp_path.glob("*.json")) == [path]
    if combined:
        # Cached optional rejection retains the existing whole-batch fallback, unlike fresh parsing.
        assert rejected.optional.status == "fallback" and rejected.optional.fields == {"closing.summary": closing}


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
    assert "точность смысла независимо не проверена" in summary
    assert not (tmp_path / "unused").exists()


@pytest.mark.asyncio
async def test_primary_preview_cleans_temporary_cache_when_presentation_raises(tmp_path: Path) -> None:
    execution = ModelExecution()
    temporary_cache: Path | None = None
    failure = ValueError("Invalid presentation cache")

    async def fail(_summary, _cards, signals, _config, cache, *, execution, retry_explicit_429=False):
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
    assert all("\n".join("> " + line for line in card.summary.split("\n")) in markdown for card in cards)
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
    assert all("сохранён исходный английский текст" in item.summary for item in presented)
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
    cfg.translation = replace(cfg.translation, timeout_seconds=150)
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
    notice = TranslationResult({}, "translated").notice_for(cfg.translation.target_language)
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
        path = next(tmp_path.glob("*.json"))
        saved = path.read_bytes()
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
        assert path.read_bytes() == saved
    assert first[:3] == replay[:3] and first[3].card == replay[3].card
    notice = TranslationResult({}, "translated").notice_for(cfg.translation.target_language)
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
    assert "http_rejection" not in record
    assert "validation_reason" not in record
    assert "validation_detail" not in record
    assert record["target_language"] == "de"
    assert len(record["response_sha256"]) == 64 and "response" not in record
    assert "closing.summary" not in record["required_fields"]
    assert record["canonical"]["closing.summary"] == closing.summary
    assert record["optional"]["status"] == "translated"
    assert record["selection_sha256"]
    assert record["request_sha256"] == _request_hash(record["canonical"], cfg.translation)
    assert record["max_output_tokens"] == cfg.translation.max_output_tokens and record["temperature"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(("defect", "reason", "changed"), [
    ("missing", "coverage", []),
    ("field_shape", "field_shape", []),
    ("number", "protected_content", ["numeric_literal"]),
    ("attached_number", "protected_content", ["numeric_literal"]),
    ("protected_all", "protected_content", ["url", "numeric_literal", "quotation"]),
    ("duplicate", "identity_or_text", []),
    ("wrong_id", "identity_or_text", []),
    ("invalid_id", "identity_or_text", []),
    ("generic_value", None, []),
    ("generic_type", None, []),
])
async def test_invalid_closing_preserves_valid_main_and_terminal_cache_omission(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, defect: str, reason: str | None, changed: list[str],
) -> None:
    execution = ModelExecution()
    from digest.translation import _messages, atomic_json_write, translate_publication_with_closing

    cfg = config()
    cfg.telegram.delivery_mode = "compact"
    main = ArticleSummary("Main", "https://example.com/main", "Source", "Tech", "Only 20 clients.")
    closing = replace(main, title="Closing", link="https://example.com/good", summary="Helped 10 people.")
    if defect == "attached_number":
        closing = replace(closing, summary="Help arrived in 20ms.")
    elif defect == "protected_all":
        closing = replace(closing, summary='Helped 10 people; "preview". https://example.com/original')

    def parse_with_generic_failure(text, originals):
        if originals == {"closing.summary": closing.summary}:
            if defect == "generic_value":
                raise ValueError("SENTINEL_REJECTED_EXCEPTION")
            if defect == "generic_type":
                raise TypeError("SENTINEL_REJECTED_EXCEPTION")
        return _parse(text, originals)

    async def answer(_role, messages, _config, **_kwargs):
        fields = json.loads(messages[1]["content"])["fields"]
        assert messages == _messages({item["id"]: item["text"] for item in fields}, cfg.translation.target_language)
        entries = [
            {"id": item["id"], "text": "Перевод: " + item["text"]} for item in fields if item["id"] != "closing.summary"
        ]
        optional = {"id": "closing.summary", "text": "SENTINEL_REJECTED_CONTENT Помогли 10 людям."}
        if defect == "number":
            optional["text"] = "SENTINEL_REJECTED_CONTENT Помогли 99 людям."
        elif defect == "attached_number":
            optional["text"] = "SENTINEL_REJECTED_CONTENT Помощь за 30ms."
        elif defect == "protected_all":
            optional["text"] = 'SENTINEL_REJECTED_CONTENT 99 "SENTINEL_QUOTE" https://example.com/SENTINEL_URL'
        elif defect == "field_shape":
            optional["SENTINEL_REJECTED_KEY"] = "SENTINEL_REJECTED_VALUE"
        elif defect == "wrong_id":
            optional["id"] = "SENTINEL_REJECTED_ID"
        elif defect == "invalid_id":
            optional["id"] = ["SENTINEL_REJECTED_ID"]
        if defect != "missing":
            entries.append(optional)
        if defect == "duplicate":
            entries.append(optional)
        return json.dumps({"translations": entries}), {"finish_reason": "stop"}

    with (
        patch("digest.translation.complete", side_effect=answer) as complete,
        patch("digest.translation._parse", side_effect=parse_with_generic_failure),
        patch("digest.translation.atomic_json_write", wraps=atomic_json_write) as write,
    ):
        first = await translate_publication_with_closing(
            "Overview.", [main], [], closing, cfg, tmp_path, execution=execution
        )
        path = next(tmp_path.glob("*.json"))
        saved = path.read_bytes()
        record = json.loads(saved)
        replay = await translate_publication_with_closing(
            "Overview.", [main], [], closing, cfg, tmp_path, execution=execution
        )
        assert path.read_bytes() == saved
        historical = {key: value for key, value in record.items()
                      if key not in {"validation_reason", "validation_detail"}}
        historical_bytes = json.dumps(historical, indent=2).encode()
        path.write_bytes(historical_bytes)
        legacy = await translate_publication_with_closing(
            "Overview.", [main], [], closing, cfg, tmp_path, execution=execution
        )
        assert path.read_bytes() == historical_bytes
    assert first[:3] == replay[:3] == legacy[:3]
    assert first[1][0].summary == "Перевод: Only 20 clients."
    assert first[0].startswith("Перевод: Overview.")
    assert first[3].card is replay[3].card is legacy[3].card is None
    assert (first[3].translation.reasons == replay[3].translation.reasons == legacy[3].translation.reasons
            == ["closing_contract_invalid"])
    complete.assert_awaited_once()
    write.assert_called_once()
    assert record["status"] == "translated" and record["optional"]["status"] == "fallback"
    assert set(record["optional"]) == {"fields", "status", "calls", "cache_hits", "reasons", "optional"}
    if reason is None:
        assert "validation_reason" not in record
    else:
        assert record["validation_reason"] == reason
    if changed:
        assert record["validation_detail"] == {"field_id": "closing.summary", "changed": changed}
    else:
        assert "validation_detail" not in record
    assert "http_rejection" not in record
    assert "SENTINEL_" not in saved.decode() + caplog.text


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
    assert "сохранён исходный английский текст" in first[0]
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
    from digest import translation
    from digest.translation import translate_publication_with_closing

    cfg = config()
    cfg.telegram.delivery_mode = "compact"
    main = ArticleSummary("Main", "https://example.com/main", "Source", "Tech", "Main claim.")
    closing = replace(main, title="Closing", link="https://example.com/good")
    monkeypatch.setattr("digest.translation.MAX_CACHE_BYTES", 4096)
    persist = translation._persist_batch_translation

    def with_stale_validation(path, record, optional, original, validation):
        if not malformed:
            assert validation is None
            # Synthetic stale transient data: real sizing must gate on its final outcome.
            validation = translation._TranslationValidationError("coverage", "SENTINEL_REJECTED_EXCEPTION")
        return persist(path, record, optional, original, validation)

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

    with (
        patch("digest.translation.complete", side_effect=answer) as complete,
        patch("digest.translation._persist_batch_translation", side_effect=with_stale_validation),
        patch("digest.translation.atomic_json_write", wraps=translation.atomic_json_write) as write,
    ):
        first = await translate_publication_with_closing("", [main], [], closing, cfg, tmp_path, execution=execution)
        path = next(tmp_path.glob("*.json"))
        saved = path.read_bytes()
        replay = await translate_publication_with_closing("", [main], [], closing, cfg, tmp_path, execution=execution)
        assert path.read_bytes() == saved
    assert first[:3] == replay[:3] and first[1][0].summary == "Перевод: Main claim."
    assert first[3].card is replay[3].card is None
    reason = "closing_contract_invalid" if malformed else "closing_output_allowance"
    assert first[3].translation.reasons == replay[3].translation.reasons == [reason]
    record = json.loads(saved)
    assert len(saved) <= 4096 and "response" not in record
    if malformed:
        assert record["validation_reason"] == "field_shape"
    else:
        assert "validation_reason" not in record
    assert "validation_detail" not in record
    complete.assert_awaited_once()
    write.assert_called_once()


@pytest.mark.parametrize("projection", ["byte_limit", "serialization_failure", "business_overflow"])
async def test_optional_diagnostics_preserve_exact_business_bytes_and_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, projection: str,
) -> None:
    from datetime import UTC, datetime

    from digest import translation

    cfg = config()
    fields = {"main": "Only 20 clients."}
    response = json.dumps({"translations": [
        {"id": "main", "text": "Только 20 клиентов."},
        {"id": "closing.summary", "text": "SENTINEL_REJECTED_CONTENT 99."},
    ]})
    dumps = json.dumps

    def reject_diagnostic_encoding(value, *args, **kwargs):
        if isinstance(value, dict) and "validation_reason" in value:
            raise TypeError("SENTINEL_REJECTED_EXCEPTION")
        return dumps(value, *args, **kwargs)

    with (
        patch("digest.translation.complete", AsyncMock(return_value=(response, {"finish_reason": "stop"}))) as call,
        patch("digest.translation.datetime") as clock,
        patch("digest.translation.atomic_json_write", wraps=translation.atomic_json_write) as write,
    ):
        clock.now.return_value = datetime(2026, 1, 1, tzinfo=UTC)
        first = await translate_fields(fields, cfg, tmp_path / "first", closing_summary="Helped 10 people.",
                                       execution=ModelExecution())
        record = json.loads(next((tmp_path / "first").glob("*.json")).read_text())
        assert record.pop("validation_reason") == "protected_content"
        assert record.pop("validation_detail") == {"field_id": "closing.summary", "changed": ["numeric_literal"]}
        business_bytes = dumps(record, indent=2).encode()
        if projection in {"byte_limit", "business_overflow"}:
            limit = len(business_bytes) - (projection == "business_overflow")
            monkeypatch.setattr(translation, "MAX_CACHE_BYTES", limit)
        else:
            monkeypatch.setattr(translation.json, "dumps", reject_diagnostic_encoding)
        bounded = await translate_fields(fields, cfg, tmp_path / "bounded", closing_summary="Helped 10 people.",
                                         execution=ModelExecution())
        path = next((tmp_path / "bounded").glob("*.json"))
        saved = path.read_bytes()
        if projection == "business_overflow":
            assert json.loads(saved)["status"] == "reserved"
            assert "translated" not in json.loads(saved)
            assert "validation_reason" not in json.loads(saved) and "validation_detail" not in json.loads(saved)
        else:
            assert saved == business_bytes
        replay = await translate_fields(fields, cfg, tmp_path / "bounded", closing_summary="Helped 10 people.",
                                        execution=ModelExecution())
        assert path.read_bytes() == saved
    assert first.status == bounded.status == "translated"
    assert first.fields == bounded.fields == {"main": "Только 20 клиентов."}
    assert first.calls == bounded.calls == 1 and replay.calls == 0
    if projection == "business_overflow":
        assert bounded.optional.reasons == ["closing_cache_unavailable"]
        assert replay.status == "fallback" and replay.fields == fields
        assert replay.reasons == ["previous_attempt_incomplete"] and replay.cache_hits == 0
        write.assert_called_once()  # The oversized second business result never reaches atomic replacement.
    else:
        assert first == bounded and replay.status == "translated" and replay.fields == first.fields
        assert first.optional.reasons == replay.optional.reasons == ["closing_contract_invalid"]
        assert replay.cache_hits == 1 and write.call_count == 2
    assert call.await_count == 2
    assert call.await_args_list[0].args[1] == call.await_args_list[1].args[1]
    assert "SENTINEL_" not in business_bytes.decode()


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
@pytest.mark.parametrize("malformed", [False, True])
async def test_combined_cache_write_failure_preserves_main_without_fabricating_legacy_cache(
    tmp_path: Path, malformed: bool,
) -> None:
    execution = ModelExecution()
    from digest import translation

    cfg = config()
    fields = {"main": "Claim."}
    real_write = translation.atomic_json_write

    async def answer(_role, messages, _config, **_kwargs):
        supplied = json.loads(messages[1]["content"])["fields"]
        if malformed:
            supplied = [item for item in supplied if item["id"] != "closing.summary"]
        return json.dumps({"translations": [{"id": item["id"], "text": "Translated."} for item in supplied]}), {
            "finish_reason": "stop"
        }

    def fail_combined_write(path, record):
        if record["schema_version"] == 2:
            if malformed:
                assert record["validation_reason"] == "coverage"
            else:
                assert "validation_reason" not in record
            raise OSError("Optional combined metadata cannot be persisted")
        real_write(path, record)

    with (
        patch("digest.translation.complete", side_effect=answer) as complete,
        patch("digest.translation.atomic_json_write", side_effect=fail_combined_write) as write,
    ):
        first = await translate_fields(fields, cfg, tmp_path / "combined", closing_summary="Kind.", execution=execution)
        path = next((tmp_path / "combined").glob("*.json"))
        saved = path.read_bytes()
        replay = await translate_fields(
            fields, cfg, tmp_path / "combined", closing_summary="Kind.", execution=execution
        )
        assert path.read_bytes() == saved
    assert first.status == "translated" and first.fields == {"main": "Translated."}
    assert first.optional.status == "fallback" and first.optional.reasons == ["closing_cache_unavailable"]
    assert replay.status == "fallback" and replay.fields == fields and replay.calls == 0
    assert "previous_attempt_incomplete" in replay.reasons
    complete.assert_awaited_once()
    write.assert_called_once()
    records = [json.loads(path.read_text()) for path in (tmp_path / "combined").glob("*.json")]
    assert len(records) == 1 and records[0]["schema_version"] == 2 and records[0]["status"] == "reserved"
    assert "translated" not in records[0]
    assert "validation_reason" not in records[0] and "validation_detail" not in records[0]
    with (
        patch("digest.translation.complete", side_effect=answer),
        patch("digest.translation.atomic_json_write", side_effect=OSError("Required main cache failed")),
    ):
        required = await translate_fields(fields, cfg, tmp_path / "main-only", execution=execution)
    assert required.status == "fallback" and required.fields == fields


@respx.mock
@pytest.mark.parametrize("combined", [False, True])
async def test_http_rejection_is_safe_failure_only_metadata_and_old_records_still_replay(
    tmp_path: Path, combined: bool,
) -> None:
    cfg, execution = config(), ModelExecution()
    route = respx.post("https://api.groq.com/openai/v1/chat/completions").respond(429, json={
        "error": {"code": "rate_limit_exceeded", "message": "HOSTILE_BODY"}, "usage": {"prompt_tokens": 7},
    }, headers={"retry-after": "30", "x-ratelimit-limit-requests": "100",
                "x-ratelimit-remaining-requests": "0", "x-ratelimit-limit-tokens": "8000",
                "x-ratelimit-remaining-tokens": "42", "x-request-id": "HOSTILE_HEADER"})
    fields = {"a": "English original."}
    with patch.dict("os.environ", {"GROQ_API_KEY": "synthetic-only-key"}):
        result = await translate_fields(fields, cfg, tmp_path, execution=execution,
                                        closing_summary="Kind." if combined else None)
    path = next(tmp_path.glob("*.json"))
    record = json.loads(path.read_text())
    assert result.fields == fields and result.status == "fallback" and result.reasons == ["translation_failed"]
    assert result.calls == execution.request_state(cfg.llm).requests_attempted == route.call_count == 1
    assert record["schema_version"] == (2 if combined else 1)
    assert (record["status"], record["error"], record["error_kind"]) == (
        "incomplete", "RuntimeError", "provider_failure")
    assert record["http_rejection"] == {
        "http_status": 429, "provider_code": "rate_limit_exceeded", "retry_after_seconds": 30.0,
        "rate_limit_limit_requests": 100, "rate_limit_remaining_requests": 0,
        "rate_limit_limit_tokens": 8000, "rate_limit_remaining_tokens": 42,
    }
    assert "HOSTILE" not in path.read_text() and "synthetic-only-key" not in path.read_text()
    assert json.loads(route.calls.last.request.content)["temperature"] == 0
    for legacy in (False, True):
        if legacy:
            del record["http_rejection"]
            path.write_text(json.dumps(record, indent=2))
        before = path.read_bytes()
        replay = await translate_fields(fields, cfg, tmp_path, execution=execution,
                                        closing_summary="Kind." if combined else None)
        assert replay.calls == 0 and replay.reasons == ["previous_attempt_incomplete"]
        assert path.read_bytes() == before and route.call_count == 1


@respx.mock
async def test_rejection_overflow_preserves_exact_combined_failure_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import UTC, datetime

    from digest import translation

    route = respx.post("https://api.groq.com/openai/v1/chat/completions").respond(
        429, json={"error": {"code": "rate_limit_exceeded"}},
    )
    cfg = config()
    with patch.dict("os.environ", {"GROQ_API_KEY": "synthetic"}), patch("digest.translation.datetime") as clock:
        clock.now.return_value = datetime(2026, 1, 1, tzinfo=UTC)
        first = await translate_fields({"a": "English."}, cfg, tmp_path / "first", execution=ModelExecution(),
                                       closing_summary="Kind.")
        record = json.loads(next((tmp_path / "first").glob("*.json")).read_text())
        assert record["schema_version"] == 2 and "http_rejection" in record
        del record["http_rejection"]
        business_bytes = json.dumps(record, indent=2).encode("utf-8")
        monkeypatch.setattr(translation, "MAX_CACHE_BYTES", len(business_bytes))
        bounded = await translate_fields({"a": "English."}, cfg, tmp_path / "bounded", execution=ModelExecution(),
                                         closing_summary="Kind.")
    assert first == bounded and first.reasons == ["translation_failed"] and route.call_count == 2
    assert next((tmp_path / "bounded").glob("*.json")).read_bytes() == business_bytes


def test_unavailable_failure_projection_is_omitted_without_mutating_record(monkeypatch: pytest.MonkeyPatch) -> None:
    from digest.llm import HTTPRejection
    from digest.translation import _with_failure_diagnostics

    record = {"schema_version": 1, "status": "incomplete"}
    assert _with_failure_diagnostics(record, []) is record
    assert _with_failure_diagnostics(record, [HTTPRejection(), HTTPRejection()]) is record
    assert _with_failure_diagnostics(record, [HTTPRejection(provider_code="a" * 1024)]) is record
    with patch("digest.translation.asdict", side_effect=TypeError("projection failed")):
        assert _with_failure_diagnostics(record, [HTTPRejection(http_status=429)]) is record
    assert "http_rejection" not in record
    assert _with_failure_diagnostics(record, [], "coverage") == {**record, "validation_reason": "coverage"}
    assert "validation_reason" not in record
    monkeypatch.setattr("digest.translation.MAX_CACHE_BYTES", len(json.dumps(record, indent=2).encode("utf-8")))
    assert _with_failure_diagnostics(record, [], "coverage") is record


@pytest.mark.parametrize(("raw", "reason"), [
    ("SENTINEL_PRIVATE_RESPONSE", "extraction"),
    ('{"other": []}', "envelope"),
    ('{"translations": [{"id": "a"}]}', "field_shape"),
    ('{"translations": [{"id": "unknown", "text": "20 clients."}]}', "identity_or_text"),
    ('{"translations": [{"id": "a", "text": "21 clients."}]}', "protected_content"),
    ('{"translations": [{"id": "a", "text": "20 clients."}, {"id": "a", "text": "21 clients."}]}',
     "identity_or_text"),
    ('{"translations": []}', "coverage"),
])
async def test_validation_reason_preserves_safe_failure_and_terminal_cache(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, raw: str, reason: str,
) -> None:
    fields = {"a": "20 clients."}
    cfg, execution = config(), ModelExecution()
    with patch("digest.translation.complete", AsyncMock(return_value=(raw, {"finish_reason": "stop"}))) as call:
        first = await translate_fields(fields, cfg, tmp_path, execution=execution)
        path = next(tmp_path.glob("*.json"))
        record = json.loads(path.read_text())
        assert (record["error"], record["error_kind"], record["validation_reason"]) == (
            "ValueError", "contract_invalid", reason)
        if reason == "protected_content":
            assert record["validation_detail"] == {"field_id": "a", "changed": ["numeric_literal"]}
        else:
            assert "validation_detail" not in record
        before = path.read_bytes()
        repeated = await translate_fields(fields, cfg, tmp_path, execution=execution)
        assert path.read_bytes() == before
        del record["validation_reason"]  # Historical records lack these optional diagnostics.
        record.pop("validation_detail", None)
        path.write_text(json.dumps(record, indent=2))
        legacy_bytes = path.read_bytes()
        legacy = await translate_fields(fields, cfg, tmp_path, execution=execution)
        assert path.read_bytes() == legacy_bytes
    assert first.fields == repeated.fields == legacy.fields == fields
    assert first.status == repeated.status == legacy.status == "fallback"
    assert repeated.reasons == legacy.reasons == ["previous_attempt_incomplete"]
    call.assert_awaited_once()
    assert "SENTINEL_PRIVATE_RESPONSE" not in before.decode() + caplog.text
    assert "_TranslationValidationError" not in before.decode() + caplog.text
    assert "validation_detail" not in caplog.text and "field_id" not in caplog.text


async def test_validation_reason_is_not_projected_outside_contract_phase(tmp_path: Path) -> None:
    from digest.translation import _TranslationValidationError

    error = _TranslationValidationError("protected_content", "safe", field_id="a", changed=("quotation",))
    with patch("digest.translation.complete", AsyncMock(side_effect=error)):
        result = await translate_fields({"a": "Original."}, config(), tmp_path, execution=ModelExecution())
    record = json.loads(next(tmp_path.glob("*.json")).read_text())
    assert result.status == "fallback"
    assert record["error"] == "ValueError" and record["error_kind"] == "provider_failure"
    assert "validation_reason" not in record and "validation_detail" not in record


def test_protected_detail_omission_preserves_base_diagnostics(monkeypatch: pytest.MonkeyPatch) -> None:
    from digest.llm import HTTPRejection
    from digest.translation import _TranslationValidationError, _with_failure_diagnostics

    record = {"schema_version": 1, "status": "incomplete", "canonical": {"a": "Original."}}
    rejection = [HTTPRejection(http_status=429)]
    base = _with_failure_diagnostics(record, rejection, "protected_content")
    error = _TranslationValidationError("protected_content", "safe", field_id="a", changed=("quotation",))
    enriched = _with_failure_diagnostics(record, rejection, "protected_content", error)
    assert enriched == {**base, "validation_detail": {"field_id": "a", "changed": ["quotation"]}}
    assert "validation_detail" not in record
    error.field_id = "unknown"
    assert _with_failure_diagnostics(record, rejection, "protected_content", error) == base
    error.field_id = "a"
    error.changed = ("SENTINEL_PRIVATE_RESPONSE",)
    assert _with_failure_diagnostics(record, rejection, "protected_content", error) == base
    error.changed = ("quotation",)
    bounded = json.dumps(base, indent=2).encode("utf-8")
    monkeypatch.setattr("digest.translation.MAX_CACHE_BYTES", len(bounded))
    omitted = _with_failure_diagnostics(record, rejection, "protected_content", error)
    assert json.dumps(omitted, indent=2).encode() == bounded


def test_oversized_canonical_field_detail_is_omitted() -> None:
    from digest.translation import _TranslationValidationError, _with_failure_diagnostics

    identity = "я" * 129
    record = {"schema_version": 1, "canonical": {identity: "Original."}}
    error = _TranslationValidationError("protected_content", "safe", field_id=identity, changed=("quotation",))
    assert _with_failure_diagnostics(record, [], "protected_content", error) == {
        **record, "validation_reason": "protected_content",
    }


@pytest.fixture
def retry_clock(monkeypatch: pytest.MonkeyPatch) -> tuple[list[float], list[float]]:
    """Exercise real request owners without real waiting or provider credentials."""
    clock, waits = [100.0], []
    for name in tuple(os.environ):
        if name.startswith("DIGEST_MODEL_BUDGET_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("GROQ_API_KEY", "synthetic-offline-key")

    async def sleep(delay: float) -> None:
        waits.append(delay)
        clock[0] += delay

    monkeypatch.setattr("digest.llm.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("digest.llm.asyncio.sleep", sleep)
    return clock, waits


def translated_response(request: httpx.Request) -> httpx.Response:
    fields = json.loads(json.loads(request.content)["messages"][1]["content"])["fields"]
    text = json.dumps({"translations": [{"id": item["id"], "text": "Перевод: " + item["text"]} for item in fields]})
    return httpx.Response(200, json={"choices": [{"message": {"content": text}, "finish_reason": "stop"}]})


@respx.mock
@pytest.mark.parametrize(("retry_after", "combined", "wait"), [
    (None, False, 60), ("invalid", True, 60), ("30", False, 30), ("1e2", True, 100),
])
async def test_opted_in_retry_reuses_raw_cooldown_and_exact_reserved_request(
    tmp_path: Path, retry_clock: tuple[list[float], list[float]],
    retry_after: str | None, combined: bool, wait: float,
) -> None:
    cfg, execution = config(), ModelExecution()
    cfg.translation = replace(cfg.translation, timeout_seconds=150)
    cfg.llm.min_request_interval_seconds = 20
    clock, waits = retry_clock
    starts: list[float] = []

    def respond(request: httpx.Request) -> httpx.Response:
        starts.append(clock[0])
        if len(starts) == 1:
            return httpx.Response(429, json={"error": {"code": "rate_limit_exceeded", "message": "PRIVATE"}},
                                  headers={"retry-after": retry_after} if retry_after is not None else {})
        return translated_response(request)

    route = respx.post("https://api.groq.com/openai/v1/chat/completions").mock(side_effect=respond)
    fields = {"a": "Only 20 participating clients."}
    result = await translate_fields(fields, cfg, tmp_path, execution=execution, retry_explicit_429=True,
                                    closing_summary="Help arrived." if combined else None)
    assert result.status == "translated" and result.calls == 2
    assert execution.request_state(cfg.llm).requests_attempted == route.call_count == 2
    assert starts == [100, 100 + wait] and waits == [wait]
    assert route.calls[0].request.content == route.calls[1].request.content
    assert json.loads(route.calls[1].request.content)["max_completion_tokens"] == cfg.translation.max_output_tokens
    path, = tmp_path.glob("*.json")
    record, saved = json.loads(path.read_text()), path.read_bytes()
    assert record["schema_version"] == (2 if combined else 1) and record["status"] == "translated"
    assert "http_rejection" not in record and "PRIVATE" not in saved.decode()
    assert result.optional is None or result.optional.status == "translated"
    replay = await translate_fields(fields, cfg, tmp_path, execution=ModelExecution(), retry_explicit_429=True,
                                    closing_summary="Help arrived." if combined else None)
    assert replay.cache_hits == 1 and replay.calls == 0 and replay.fields == result.fields
    assert path.read_bytes() == saved and route.call_count == 2


@respx.mock
@pytest.mark.parametrize("kind", ["deadline", "long_raw", "extended", "pacing", "earlier_deadline"])
async def test_retry_waits_cannot_cross_original_or_earlier_deadline(
    tmp_path: Path, retry_clock: tuple[list[float], list[float]], monkeypatch: pytest.MonkeyPatch, kind: str,
) -> None:
    cfg, execution = config(), ModelExecution()
    cfg.translation = replace(cfg.translation, timeout_seconds=150)
    clock, waits = retry_clock
    state = execution.request_state(cfg.llm)

    def respond(request: httpx.Request) -> httpx.Response:
        clock[0] += 13
        if kind == "pacing":
            state.next_request_at = 250
        return httpx.Response(429, headers={"retry-after": {"deadline": "137", "long_raw": "86401"}.get(kind, "60")})

    async def extend(delay: float) -> None:
        waits.append(delay)
        clock[0] += delay
        state.unavailable_until[(cfg.translation.provider, cfg.translation.model)] = 250

    if kind == "extended":
        monkeypatch.setattr("digest.translation.asyncio.sleep", extend)
    route = respx.post("https://api.groq.com/openai/v1/chat/completions").mock(side_effect=respond)
    result = await translate_fields({"a": "Original."}, cfg, tmp_path, execution=execution,
                                    deadline=170 if kind == "earlier_deadline" else None, retry_explicit_429=True)
    assert result.status == "fallback" and result.calls == route.call_count == state.requests_attempted == 1
    assert waits == ([60] if kind == "extended" else [])
    record = json.loads(next(tmp_path.glob("*.json")).read_text())
    assert record["status"] == "incomplete" and record["http_rejection"]["http_status"] == 429
    if kind == "long_raw":
        assert record["http_rejection"]["retry_after_seconds"] is None
        assert state.unavailable_until[(cfg.translation.provider, cfg.translation.model)] == 113 + 86401


@respx.mock
@pytest.mark.parametrize("kind", ["default", "server", "transport", "completion", "validation", "repeated"])
async def test_retry_is_exclusive_to_one_explicit_429(
    tmp_path: Path, retry_clock: tuple[list[float], list[float]], kind: str,
) -> None:
    cfg, execution = config(), ModelExecution()
    cfg.translation = replace(cfg.translation, timeout_seconds=150)

    def respond(request: httpx.Request) -> httpx.Response:
        if kind == "transport":
            raise httpx.ReadTimeout("Unknown completion", request=request)
        if kind == "completion":
            return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}, "finish_reason": "length"}]})
        if kind == "validation":
            return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]})
        return httpx.Response(503 if kind == "server" else 429, headers={"retry-after": "0"})

    route = respx.post("https://api.groq.com/openai/v1/chat/completions").mock(side_effect=respond)
    fields = {"a": "Original."}
    result = await translate_fields(fields, cfg, tmp_path, execution=execution, retry_explicit_429=kind != "default")
    expected = 2 if kind == "repeated" else 1
    assert result.fields == fields and result.status == "fallback"
    assert result.calls == execution.request_state(cfg.llm).requests_attempted == route.call_count == expected
    path, = tmp_path.glob("*.json")
    saved = path.read_bytes()
    record = json.loads(saved)
    assert record["error_kind"] == {"completion": "completion_incomplete", "validation": "contract_invalid"}.get(
        kind, "provider_failure",
    )
    if kind == "repeated":
        assert record["http_rejection"]["http_status"] == 429
    replay = await translate_fields(fields, cfg, tmp_path, execution=ModelExecution(), retry_explicit_429=True)
    assert replay.calls == 0 and replay.reasons == ["previous_attempt_incomplete"]
    assert path.read_bytes() == saved and route.call_count == expected


@respx.mock
@pytest.mark.parametrize("max_calls", [1, 2])
async def test_only_one_retry_beyond_base_batch_allowance(
    tmp_path: Path, retry_clock: tuple[list[float], list[float]], max_calls: int,
) -> None:
    cfg, execution = config(), ModelExecution()
    cfg.translation = replace(cfg.translation, max_calls=max_calls, max_input_chars=256, timeout_seconds=150)
    fields = {"a": "Only clients. " * 10, "b": "Only partners. " * 10}
    attempts: dict[str, int] = {}

    def respond(request: httpx.Request) -> httpx.Response:
        identity = json.loads(json.loads(request.content)["messages"][1]["content"])["fields"][0]["id"]
        attempts[identity] = attempts.get(identity, 0) + 1
        if attempts[identity] == 1:
            return httpx.Response(429, headers={"retry-after": "0"})
        return translated_response(request)

    route = respx.post("https://api.groq.com/openai/v1/chat/completions").mock(side_effect=respond)
    result = await translate_fields(fields, cfg, tmp_path, execution=execution, retry_explicit_429=True)
    assert result.calls == route.call_count == execution.request_state(cfg.llm).requests_attempted == max_calls + 1
    assert attempts == ({"a": 2} if max_calls == 1 else {"a": 2, "b": 1})
    assert result.fields == fields and result.status == "fallback"
    assert ("request_allowance" in result.reasons) == (max_calls == 1)
    if max_calls == 1:
        resumed = await translate_fields(fields, cfg, tmp_path, execution=ModelExecution(), retry_explicit_429=True)
        assert resumed.status == "translated" and resumed.cache_hits == 1 and resumed.calls == 2
        assert attempts == {"a": 2, "b": 2} and route.call_count == 4


@respx.mock
@pytest.mark.parametrize("cancel", [False, True])
async def test_retry_reservation_stays_exclusive_during_wait_and_interruption(
    tmp_path: Path, retry_clock: tuple[list[float], list[float]], monkeypatch: pytest.MonkeyPatch, cancel: bool,
) -> None:
    import asyncio

    cfg, execution = config(), ModelExecution()
    cfg.translation = replace(cfg.translation, timeout_seconds=150)
    clock, waits = retry_clock
    fields = {"a": "Original."}
    observed: list[bytes] = []

    async def observe(delay: float) -> None:
        path, = tmp_path.glob("*.json")
        saved = path.read_bytes()
        assert json.loads(saved)["status"] == "reserved"
        replay = await translate_fields(fields, cfg, tmp_path, execution=ModelExecution(), retry_explicit_429=True)
        assert replay.calls == 0 and replay.reasons == ["previous_attempt_incomplete"]
        assert path.read_bytes() == saved
        observed.append(saved)
        if cancel:
            raise asyncio.CancelledError
        waits.append(delay)
        clock[0] += delay

    requests = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(429) if requests == 1 else translated_response(request)

    monkeypatch.setattr("digest.translation.asyncio.sleep", observe)
    route = respx.post("https://api.groq.com/openai/v1/chat/completions").mock(side_effect=respond)
    if cancel:
        with pytest.raises(asyncio.CancelledError):
            await translate_fields(fields, cfg, tmp_path, execution=execution, retry_explicit_429=True)
    else:
        result = await translate_fields(fields, cfg, tmp_path, execution=execution, retry_explicit_429=True)
        assert result.status == "translated" and result.calls == 2
    assert len(observed) == 1 and route.call_count == (1 if cancel else 2)
    path, = tmp_path.glob("*.json")
    saved = path.read_bytes()
    assert json.loads(saved)["status"] == ("incomplete" if cancel else "translated")
    replay = await translate_fields(fields, cfg, tmp_path, execution=ModelExecution(), retry_explicit_429=True)
    assert replay.calls == 0 and path.read_bytes() == saved


@respx.mock
@pytest.mark.parametrize("combined", [False, True])
async def test_retry_final_write_failure_never_releases_reservation_or_makes_third_call(
    tmp_path: Path, retry_clock: tuple[list[float], list[float]], monkeypatch: pytest.MonkeyPatch, combined: bool,
) -> None:
    cfg, execution = config(), ModelExecution()
    cfg.translation = replace(cfg.translation, timeout_seconds=150)
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, headers={"retry-after": "0"}) if calls == 1 else translated_response(request)

    route = respx.post("https://api.groq.com/openai/v1/chat/completions").mock(side_effect=respond)
    def fail_write(*_: object) -> None:
        raise OSError("write failed")

    monkeypatch.setattr("digest.translation.atomic_json_write", fail_write)
    fields = {"a": "Original."}
    result = await translate_fields(fields, cfg, tmp_path, execution=execution, retry_explicit_429=True,
                                    closing_summary="Kind." if combined else None)
    assert result.calls == route.call_count == execution.request_state(cfg.llm).requests_attempted == 2
    assert result.status == ("translated" if combined else "fallback")
    if combined:
        assert result.optional is not None and result.optional.reasons == ["closing_cache_unavailable"]
    path, = tmp_path.glob("*.json")
    saved = path.read_bytes()
    assert json.loads(saved)["status"] == "reserved"
    replay = await translate_fields(fields, cfg, tmp_path, execution=ModelExecution(), retry_explicit_429=True,
                                    closing_summary="Kind." if combined else None)
    assert replay.status == "fallback" and replay.calls == 0 and path.read_bytes() == saved


@respx.mock
@pytest.mark.parametrize("failure", ["rejection_projection", "reservation_write"])
async def test_missing_rejection_facts_or_failed_reservation_cannot_authorize_retry(
    tmp_path: Path, retry_clock: tuple[list[float], list[float]], monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    cfg, execution = config(), ModelExecution()
    route = respx.post("https://api.groq.com/openai/v1/chat/completions").respond(429)

    def fail(*_: object) -> None:
        raise OSError("Unavailable evidence")

    monkeypatch.setattr("digest.llm._http_rejection" if failure == "rejection_projection"
                        else "digest.translation.os.fsync", fail)
    result = await translate_fields({"a": "Original."}, cfg, tmp_path, execution=execution, retry_explicit_429=True)
    assert result.status == "fallback"
    assert route.call_count == result.calls == (1 if failure == "rejection_projection" else 0)
    saved = next(tmp_path.glob("*.json")).read_bytes()
    assert "http_rejection" not in json.loads(saved)
    replay = await translate_fields(
        {"a": "Original."}, cfg, tmp_path, execution=ModelExecution(), retry_explicit_429=True,
    )
    assert replay.calls == 0 and next(tmp_path.glob("*.json")).read_bytes() == saved


async def test_opted_in_closing_keeps_configured_and_earlier_deadline_minimum(
    tmp_path: Path, retry_clock: tuple[list[float], list[float]], monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    from digest import translation

    cfg = config()
    cfg.translation = replace(cfg.translation, timeout_seconds=180)
    card = ArticleSummary("Main", "https://example.com/main", "Source", "Tech", "Original.")
    closer = replace(card, title="Closing", link="https://example.com/closing", summary="Kind.")
    deadlines: list[float] = []
    timeout_at = asyncio.timeout_at

    def record_timeout(deadline: float) -> asyncio.Timeout:
        deadlines.append(deadline)
        return timeout_at(deadline)

    async def answer(
        _role: Any, messages: list[dict[str, str]], _config: Any, **_kwargs: Any,
    ) -> tuple[str, dict[str, str]]:
        fields = json.loads(messages[1]["content"])["fields"]
        return json.dumps({"translations": [{"id": item["id"], "text": item["text"]} for item in fields]}), {
            "finish_reason": "stop",
        }

    original = translation._translate_publication_presentation

    async def delayed_entry(*args: Any, **kwargs: Any) -> Any:
        retry_clock[0][0] += 10
        return await original(*args, **kwargs)

    monkeypatch.setattr("digest.translation.asyncio.timeout_at", record_timeout)
    monkeypatch.setattr("digest.translation.complete", answer)
    monkeypatch.setattr("digest.translation._translate_publication_presentation", delayed_entry)
    await translation.translate_publication_with_closing(
        "", [card], [], closer, cfg, tmp_path / "closing", execution=ModelExecution(), retry_explicit_429=True,
    )
    await translate_fields({"a": "Original."}, cfg, tmp_path / "earlier", execution=ModelExecution(),
                           deadline=130, retry_explicit_429=True)
    assert deadlines == [280, 130]  # Outer closing deadline survives entry delay; no source-level 150 clamp.


@pytest.mark.asyncio
async def test_fresh_synthesis_status_changes_only_normal_notice_request_and_hash_with_closer(tmp_path: Path) -> None:
    from digest.application.presentation import combined_summary, deferred_review_status
    from digest.translation import translate_publication_with_closing

    cfg = config()
    cfg.telegram.delivery_mode = "compact"
    cfg.translation = replace(cfg.translation, max_calls=1)
    main = ArticleSummary("Main", "https://example.com/main", "Main source", "Tech", "Only 20 pilot clients.")
    closer = ArticleSummary("Closing", "https://example.com/closer", "Other source", "World", "Volunteers helped.")
    calls = []

    async def answer(role: Any, messages: list[dict[str, str]], routed: Any, **kwargs: Any) -> tuple[str, dict]:
        calls.append((role, messages, asdict(routed.llm), {key: value for key, value in kwargs.items()
                                                       if key != "execution"}))
        fields = json.loads(messages[1]["content"])["fields"]
        return json.dumps({"translations": [{"id": item["id"], "text": "Перевод: " + item["text"]}
                                            for item in fields]}), {"finish_reason": "stop"}

    statuses = [combined_summary([], None, True, "en"),
                combined_summary([], None, True, "en", synthesis_enabled=True)]
    assert statuses == [deferred_review_status("en"),
                        "Counter-signal stage postponed until after primary delivery."]
    with patch("digest.translation.complete", side_effect=answer) as complete:
        for index, status in enumerate(statuses):
            await translate_publication_with_closing(status, [main], [], closer, cfg, tmp_path / str(index),
                                                     selection_binding="same-selection", execution=ModelExecution())
    assert complete.await_count == 2
    first, second = calls
    assert first[0] == second[0] and first[2:] == second[2:]
    assert first[1][0] == second[1][0]
    requests = [json.loads(call[1][1]["content"]) for call in calls]
    fields = [{item["id"]: item["text"] for item in request["fields"]} for request in requests]
    assert set(fields[0]) == set(fields[1]) == {"category_digest", f"article:{article_hash(main.title, main.link)}",
                                             "closing.summary"}
    assert [item.pop("category_digest") for item in fields] == statuses
    assert fields[0] == fields[1] == {f"article:{article_hash(main.title, main.link)}": main.summary,
                                     "closing.summary": closer.summary}
    requests[0].pop("fields")
    requests[1].pop("fields")
    assert requests[0] == requests[1]
    records = [json.loads(next((tmp_path / str(index)).glob("*.json")).read_text()) for index in range(2)]
    assert records[0]["request_sha256"] != records[1]["request_sha256"]
    assert records[0]["required_fields"] == records[1]["required_fields"]
    assert records[0]["optional"] == records[1]["optional"]
    assert records[0]["max_output_tokens"] == records[1]["max_output_tokens"] == cfg.translation.max_output_tokens
    assert all(record["temperature"] == 0 and record["status"] == "translated" for record in records)
    assert "Independent comparison" not in records[1]["canonical"]["category_digest"]


@pytest.mark.asyncio
@pytest.mark.parametrize("stored_opt_in,current_enabled", [(False, False), (False, True), (True, False), (True, True)])
async def test_archive_status_follows_retained_snapshot_notice_not_current_flag(
    stored_opt_in: bool, current_enabled: bool,
) -> None:
    from datetime import date

    from digest.application.presentation import deferred_review_status
    from digest.application.publication import assemble_publication
    from digest.config import SynthesisConfig
    from tests.test_preparation import _snapshot

    cfg = fixture_config()
    cfg.review.review_led_only = True
    cfg.synthesis = SynthesisConfig(current_enabled)
    snapshot = _snapshot()
    status = deferred_review_status(cfg.radar.language, synthesis_enabled=stored_opt_in)
    snapshot = replace(snapshot, combined="ModelView remains incomplete.\n\n" + status)
    with patch("digest.application.source_attribution.main_attribution_occurrences", return_value={}):
        assembly = await assemble_publication(snapshot, cfg, publication_date=date(2026, 10, 13),
                                              execution=ModelExecution())
    assert assembly.irritator_status.text == status
    assert assembly.combined == snapshot.combined
    assert assembly.snapshot == snapshot


def test_opted_in_status_localization_and_category_summary_stay_deterministic() -> None:
    from digest.application.presentation import combined_summary, deferred_review_status

    assert deferred_review_status("ru", synthesis_enabled=True) == (
        "Этап контрсигналов отложен до завершения основной доставки."
    )
    assert combined_summary([], "Category trends", False, "en", synthesis_enabled=True) == "Category trends"


@pytest.mark.asyncio
@pytest.mark.parametrize("translated", [False, True])
@pytest.mark.parametrize("synthesis_enabled", [False, True])
async def test_static_reader_notice_localizes_and_deduplicates_without_changing_canonical_inputs(
    translated: bool, synthesis_enabled: bool,
) -> None:
    from digest.application.presentation import deferred_review_status, reader_publication_notice
    from digest.presentation.review import primary_notice

    cfg = config()
    cfg.telegram.delivery_mode = "compact"
    with patch("digest.application.review.complete", side_effect=fixture_response):
        result = await run_primary_review(fixture_articles(), cfg, execution=ModelExecution())
    canonical = primary_notice(result, "en") + "\n\n" + deferred_review_status(
        "en", synthesis_enabled=synthesis_enabled,
    )
    status = TranslationResult({}, "translated" if translated else "fallback").notice_for("ru")
    presented = ("Статический перевод." if translated else canonical) + "\n\n" + status
    before = deepcopy(asdict(result.report))
    with patch("digest.translation.complete", side_effect=AssertionError("No presentation calls")):
        shown = reader_publication_notice(canonical, presented, result, cfg)
        assert reader_publication_notice("Unknown historical notice", presented, result, cfg) == presented
    assert shown == "Автоматический обзор RSS-выдержек. " + status
    assert result.chosen.review.model not in shown and "Model view" not in shown
    assert "независимое сравнение" not in shown.lower() and "контрсигналов" not in shown
    assert asdict(result.report) == before and canonical.startswith("Model view")
