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
    signals = [make_ranked_signal(
        title=f"Evidence {index}", url=f"https://example.com/{index}",
        reasoning="😀\\*_ " * 800 + f" ONLY CONDITION {index}",
        narrative_claim="An assumption " * 40 + f" EXCEPTION {index}",
    ) for index in range(3)]
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
