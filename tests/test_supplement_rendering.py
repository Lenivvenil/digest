"""Full supplementary meaning survives both transports and Telegram boundaries."""

from __future__ import annotations

import asyncio
import json
import re
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from digest.delivery.markdown import _build_counter_signals_section
from digest.delivery.supplement import signal_text, split_supplement
from digest.delivery.telegram import escape_markdownv2, send_counter_signals
from digest.irritator.evidence_stage import EvidenceIrritatorResult
from digest.post_delivery import _render_result, _send_supplement
from scripts.review_fixture import fixture_config
from tests.factories import make_ranked_signal


def _decode(chunks: list[str]) -> str:
    return "".join(re.sub(r"\\(.)", r"\1", chunk) for chunk in chunks)


def test_split_preserves_unicode_escaping_whitespace_and_atomic_url() -> None:
    url = "https://example.com/one_(two)?a=1&b=2"
    text = "Prior paragraph\n\n" + "😀\\*_ " * 40 + url + "\n" + "x" * 350 + " FINAL QUALIFICATION"
    chunks = split_supplement(text, escape_markdownv2, max_units=80)
    assert _decode(chunks) == text
    assert all(len(chunk.encode("utf-16-le")) // 2 <= 80 for chunk in chunks)
    assert sum(escape_markdownv2(url) in chunk for chunk in chunks) == 1


@pytest.mark.asyncio
async def test_both_supplement_paths_keep_all_archived_signal_content(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    from digest.irritator import IrritatorStatus

    config = fixture_config()
    config.radar.language = "en"
    config.telegram.enabled = True
    signals = [
        make_ranked_signal(
            title=f"Evidence {index}",
            url=f"https://example.com/{index}",
            reasoning="😀\\*_ " * 800 + f" ONLY CONDITION {index}",
            narrative_claim="An assumption " * 40 + f" EXCEPTION {index}",
        )
        for index in range(3)
    ]
    result = EvidenceIrritatorResult(schema_version=1, bundle_id="bundle", status="complete", ranked_signals=signals)
    archived = _render_result(result)
    legacy_archive = _build_counter_signals_section(signals)
    with respx.mock, patch("digest.adapters.telegram.delivery.asyncio.sleep", AsyncMock()):
        route = respx.post(re.compile(r"api\.telegram\.org")).mock(return_value=httpx.Response(200, json={"ok": True}))
        status = IrritatorStatus("One source failed; retained valid counter-evidence", "incomplete")
        await send_counter_signals(signals, config, status)
        split_at = route.call_count
        assert await _send_supplement(result, config) == "sent"
    payloads = [json.loads(call.request.content) for call in route.calls]
    assert "IRRITATOR" in _decode([p["text"] for p in payloads[:split_at]])
    assert "Irritator status: incomplete — " + status.text in _decode([p["text"] for p in payloads[:split_at]])
    for subset in (payloads[:split_at], payloads[split_at:]):
        text = _decode([payload["text"] for payload in subset])
        assert all(len(payload["text"].encode("utf-16-le")) // 2 <= 3800 for payload in subset)
        for signal in signals:
            assert signal_text(signal) in text
            assert signal_text(signal) in archived
            assert signal.reasoning in legacy_archive
            assert signal.narrative_claim in legacy_archive


@pytest.mark.asyncio
async def test_impossible_url_fails_before_either_sender_contacts_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    config = fixture_config()
    config.telegram.enabled = True
    signals = [make_ranked_signal(url="https://example.com/" + "x" * 4000)]
    result = EvidenceIrritatorResult(schema_version=1, bundle_id="bundle", status="complete", ranked_signals=signals)
    assert signals[0].signal.url in _render_result(result)
    with patch("httpx.AsyncClient", side_effect=AssertionError("No network before payload validation")):
        with pytest.raises(ValueError, match="URL exceeds"):
            await send_counter_signals(signals, config)
        with pytest.raises(ValueError, match="URL exceeds"):
            await _send_supplement(result, config)


@pytest.mark.asyncio
async def test_legacy_supplement_dispatch_has_total_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    monkeypatch.setattr("digest.adapters.telegram.delivery._SUPPLEMENT_DISPATCH_SECONDS", 0.02)

    async def stalled_send(*args: object, **kwargs: object) -> None:
        await asyncio.Event().wait()

    with patch("digest.adapters.telegram.delivery._send_chunk", side_effect=stalled_send) as sender:
        with pytest.raises(TimeoutError):
            await send_counter_signals([make_ranked_signal(reasoning="long " * 3000)], fixture_config())
    sender.assert_awaited_once()


@pytest.mark.parametrize(
    "sentence", ["A bounded condition remains important.", "Оговорка остаётся важной: лишь пилот 😀."],
)
def test_split_prefers_complete_sentences_without_losing_whitespace(sentence: str) -> None:
    text = f"{sentence} {sentence} {sentence}"
    budget = len(escape_markdownv2(sentence + " ").encode("utf-16-le")) // 2 + 15
    chunks = split_supplement(text, escape_markdownv2, max_units=budget)
    assert _decode(chunks) == text
    assert [re.sub(r"\\(.)", r"\1", chunk).strip() for chunk in chunks] == [sentence] * 3


def test_split_prefers_paragraphs_and_never_loses_a_late_qualification() -> None:
    first = "One complete paragraph."
    second = "The next claim has a material condition. Only pilot users qualify."
    text = first + "\n\n" + second
    chunks = split_supplement(text, escape_markdownv2, max_units=80)
    assert chunks[0] == escape_markdownv2(first + "\n\n")
    assert _decode(chunks) == text
    assert chunks[-1].endswith(escape_markdownv2("Only pilot users qualify."))


@pytest.mark.parametrize("language", ["en", "ru"])
def test_coverage_preserves_full_source_qualification_and_unknown_caveats(language: str) -> None:
    from digest.domain.investigation.coverage import FULL_SOURCE_COVERAGE
    from digest.presentation.supplement import _reader_coverage

    result = EvidenceIrritatorResult(1, "synthetic", coverage=FULL_SOURCE_COVERAGE)
    text = _reader_coverage(result, language)
    assert ("выбранным моделью" if language == "ru" else "model-selected") in text
    assert ("не независимое подтверждение" if language == "ru" else "nor independent corroboration") in text
    result.coverage = "Unknown scope: measurement applies only to a limited trial."
    assert _reader_coverage(result, language) == result.coverage


@pytest.mark.parametrize(
    "value,language,expected",
    [
        ("2026-11-10T01:30:59.123+02:00", "en", "09 Nov 2026"),
        ("2026-11-10T01:30:59.123+02:00", "ru", "09.11.2026"),
        ("2026-11-09", "ru", "09.11.2026"),
        ("unknown", "ru", "unknown"),
    ],
)
def test_reader_dates_keep_calendar_context_without_raw_timestamps(value: str, language: str, expected: str) -> None:
    from digest.presentation.supplement import reader_date

    assert reader_date(value, language) == expected


def test_closed_reader_caveat_mapping_keeps_substantive_and_unfamiliar_conditions() -> None:
    from digest.presentation.supplement import _reader_limitations

    unknown = "Only institutions with audited recovery plans are covered; no claim about other participants."
    result = EvidenceIrritatorResult(1, "synthetic", limitations=[
        "No independent verification of deployment readiness.",
        "Scope limited to network infrastructure, not direct banking impact.",
        "All external signals are from developer-focused blogs, not primary regulatory sources.",
        "Potential for schedule changes not captured in excerpt.",
        "Information may be outdated before the rollout.",
        "3 of 9 source/query searches failed or were unavailable.",
        "Evidence is a sanitized RSS excerpt, not the full article.",
        "Excerpt may omit additional qualifiers or context.",
        "Vendor-specific blogs may be biased.",
        unknown,
        "No independent verification of an unfamiliar measured outcome.",
        "Scope limited to an unfamiliar trial, not direct banking impact.",
    ])
    original = list(result.limitations)
    notes = _reader_limitations(result, "ru")
    assert "Готовность к внедрению независимо не проверена." in notes
    assert "Охват ограничен сетевой инфраструктурой; прямое влияние на банки не оценивалось." in notes
    assert any("первичных источников регуляторов" in item for item in notes)
    assert "Изменения расписания могли не попасть в отрывок." in notes
    assert "Сведения могут устареть до запуска." in notes
    assert notes.count("Часть поисков недоступна: 3 из 9.") == 1
    assert unknown in notes and original[-1] in notes and original[-2] in notes
    assert result.limitations == original
    assert "Vendor-specific blogs may be biased." not in notes


def test_unfamiliar_coverage_cannot_authorize_dropping_a_known_excerpt_caveat() -> None:
    from digest.presentation.supplement import _reader_limitations

    limitation = "Evidence consists of short excerpts; full articles were not reviewed."
    result = EvidenceIrritatorResult(1, "synthetic", coverage="Unfamiliar scope.", limitations=[limitation])
    assert _reader_limitations(result, "ru") == [limitation]
