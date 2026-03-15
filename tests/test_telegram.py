"""Tests for src/telegram.py — Telegram delivery module."""

from __future__ import annotations

import pytest
import respx
import httpx
from unittest.mock import patch

from src.telegram import escape_markdownv2, split_message, send_digest
from src.config import Config, LLMConfig, DeliveryConfig, DigestConfig


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def make_config(*, telegram: bool = True, markdown_to_repo: bool = False) -> Config:
    return Config(
        llm=LLMConfig(provider="anthropic", model="claude-sonnet-4-20250514"),
        delivery=DeliveryConfig(
            telegram=telegram,
            markdown_to_repo=markdown_to_repo,
            markdown_dir="digests",
        ),
        digest=DigestConfig(
            language="ru",
            max_articles_per_source=5,
            max_total_articles=30,
            summary_style="analytical",
        ),
        sources=[],
    )


# ---------------------------------------------------------------------------
# escape_markdownv2
# ---------------------------------------------------------------------------


def test_escape_plain_text_unchanged() -> None:
    assert escape_markdownv2("Hello world") == "Hello world"


def test_escape_period_and_exclamation() -> None:
    result = escape_markdownv2("Hello. World!")
    assert r"\." in result
    assert r"\!" in result


def test_escape_all_special_chars() -> None:
    specials = r"\_*[]()~`>#+-=|{}.!"
    result = escape_markdownv2(specials)
    for ch in specials:
        assert f"\\{ch}" in result


def test_escape_empty_string() -> None:
    assert escape_markdownv2("") == ""


def test_escape_underscore_in_url_description() -> None:
    text = "some_variable and another_one"
    result = escape_markdownv2(text)
    assert r"some\_variable" in result
    assert r"another\_one" in result


# ---------------------------------------------------------------------------
# split_message
# ---------------------------------------------------------------------------


def test_split_short_message_not_split() -> None:
    text = "Short message."
    assert split_message(text, max_len=4096) == ["Short message."]


def test_split_exactly_at_limit_not_split() -> None:
    text = "a" * 4096
    chunks = split_message(text, max_len=4096)
    assert len(chunks) == 1
    assert chunks[0] == text


def test_split_long_message_at_paragraph_boundary() -> None:
    part1 = "A" * 100
    part2 = "B" * 100
    text = part1 + "\n\n" + part2
    chunks = split_message(text, max_len=150)
    assert len(chunks) == 2
    assert chunks[0] == part1
    assert chunks[1] == part2


def test_split_multiple_paragraphs_packed() -> None:
    # Each paragraph is 50 chars, max is 120 — two paragraphs fit, third needs new chunk
    para = "X" * 50
    text = "\n\n".join([para, para, para])
    chunks = split_message(text, max_len=120)
    # First two fit (50 + 2 + 50 = 102 <= 120), third is separate
    assert len(chunks) == 2
    assert chunks[0] == para + "\n\n" + para
    assert chunks[1] == para


def test_split_single_long_paragraph_at_newline() -> None:
    line1 = "A" * 60
    line2 = "B" * 60
    para = line1 + "\n" + line2
    chunks = split_message(para, max_len=70)
    assert all(len(c) <= 70 for c in chunks)
    assert len(chunks) == 2


def test_split_very_long_single_line_hard_split() -> None:
    text = "Z" * 5000
    chunks = split_message(text, max_len=4096)
    assert all(len(c) <= 4096 for c in chunks)
    assert "".join(chunks) == text


def test_split_empty_string() -> None:
    assert split_message("", max_len=100) == [""]


# ---------------------------------------------------------------------------
# send_digest — disabled delivery
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_digest_disabled_skips() -> None:
    config = make_config(telegram=False)
    # Should return without making any HTTP call — no mock needed
    await send_digest("Some digest text", config)


# ---------------------------------------------------------------------------
# send_digest — missing credentials
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_digest_missing_token_logs_warning(caplog: pytest.LogCaptureFixture) -> None:
    config = make_config(telegram=True)
    with patch.dict("os.environ", {}, clear=True):
        import logging
        with caplog.at_level(logging.WARNING, logger="src.telegram"):
            await send_digest("Some digest text", config)
    assert "TELEGRAM_BOT_TOKEN" in caplog.text or "credentials are missing" in caplog.text


@pytest.mark.asyncio
async def test_send_digest_missing_chat_id_logs_warning(caplog: pytest.LogCaptureFixture) -> None:
    config = make_config(telegram=True)
    env = {"TELEGRAM_BOT_TOKEN": "abc123"}
    with patch.dict("os.environ", env, clear=True):
        import logging
        with caplog.at_level(logging.WARNING, logger="src.telegram"):
            await send_digest("Some digest text", config)
    assert "credentials are missing" in caplog.text


# ---------------------------------------------------------------------------
# send_digest — successful delivery
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_send_digest_single_message() -> None:
    config = make_config(telegram=True)
    env = {"TELEGRAM_BOT_TOKEN": "mytoken", "TELEGRAM_CHAT_ID": "12345"}
    url = "https://api.telegram.org/botmytoken/sendMessage"

    respx.post(url).mock(return_value=httpx.Response(200, json={"ok": True}))

    with patch.dict("os.environ", env, clear=True):
        await send_digest("Hello digest", config)

    assert respx.calls.call_count == 1
    sent_payload = respx.calls[0].request.content
    import json
    payload = json.loads(sent_payload)
    assert payload["chat_id"] == "12345"
    assert payload["parse_mode"] == "MarkdownV2"
    assert "Hello digest" in payload["text"]


@pytest.mark.asyncio
@respx.mock
async def test_send_digest_splits_long_message() -> None:
    config = make_config(telegram=True)
    env = {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "99"}
    url = "https://api.telegram.org/bottok/sendMessage"

    # Create text that requires splitting: two paragraphs of 3000 chars each
    text = "A" * 3000 + "\n\n" + "B" * 3000

    respx.post(url).mock(return_value=httpx.Response(200, json={"ok": True}))

    with patch.dict("os.environ", env, clear=True):
        await send_digest(text, config)

    assert respx.calls.call_count == 2


@pytest.mark.asyncio
@respx.mock
async def test_send_digest_fallback_to_plain_text_on_400() -> None:
    """When MarkdownV2 is rejected (HTTP 400), falls back to plain text."""
    config = make_config(telegram=True)
    env = {"TELEGRAM_BOT_TOKEN": "tok2", "TELEGRAM_CHAT_ID": "42"}
    url = "https://api.telegram.org/bottok2/sendMessage"

    # First call (MarkdownV2) returns 400, second call (plain) returns 200
    respx.post(url).mock(
        side_effect=[
            httpx.Response(400, json={"ok": False, "description": "Bad Request"}),
            httpx.Response(200, json={"ok": True}),
        ]
    )

    with patch.dict("os.environ", env, clear=True):
        await send_digest("Test *message*", config)

    assert respx.calls.call_count == 2
    import json
    plain_payload = json.loads(respx.calls[1].request.content)
    assert "parse_mode" not in plain_payload


@pytest.mark.asyncio
@respx.mock
async def test_send_digest_raises_on_http_error() -> None:
    """HTTP 500 errors from Telegram propagate as exceptions."""
    config = make_config(telegram=True)
    env = {"TELEGRAM_BOT_TOKEN": "tok3", "TELEGRAM_CHAT_ID": "7"}
    url = "https://api.telegram.org/bottok3/sendMessage"

    respx.post(url).mock(return_value=httpx.Response(500, json={"ok": False}))

    with patch.dict("os.environ", env, clear=True):
        with pytest.raises(httpx.HTTPStatusError):
            await send_digest("Error test", config)
