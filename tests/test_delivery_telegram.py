"""Tests for src.delivery.telegram."""

from __future__ import annotations

import re
from typing import Any

import httpx
import pytest
import respx

from src.delivery.telegram import (
    escape_markdownv2,
    send_article_cards,
    send_counter_signals,
    send_radar,
    split_message,
    to_markdownv2,
)
from tests.factories import make_article, make_ranked_signal

# ---------------------------------------------------------------------------
# escape_markdownv2
# ---------------------------------------------------------------------------

class TestEscapeMarkdownV2:
    def test_escapes_special_chars(self) -> None:
        result = escape_markdownv2("hello_world *bold* [link](url)")
        assert "\\_" in result
        assert "\\*" in result
        assert "\\[" in result
        assert "\\(" in result

    def test_no_special_chars(self) -> None:
        assert escape_markdownv2("hello world") == "hello world"

    def test_all_special_chars(self) -> None:
        special = r"\_*[]()~`>#+-=|{}.!"
        result = escape_markdownv2(special)
        for ch in r"\_*[]()~`>#+-=|{}.!":
            assert f"\\{ch}" in result

    def test_empty_string(self) -> None:
        assert escape_markdownv2("") == ""


# ---------------------------------------------------------------------------
# to_markdownv2
# ---------------------------------------------------------------------------

class TestToMarkdownV2:
    def test_heading_to_bold(self) -> None:
        result = to_markdownv2("## Section Title")
        assert "*Section Title*" in result
        assert "##" not in result

    def test_bold_preserved(self) -> None:
        result = to_markdownv2("**important**")
        assert "*important*" in result

    def test_link_preserved(self) -> None:
        result = to_markdownv2("[click](https://example.com)")
        assert "[click](https://example.com)" in result

    def test_special_chars_escaped(self) -> None:
        result = to_markdownv2("price is 5.99")
        assert "5\\.99" in result

    def test_mixed_content(self) -> None:
        result = to_markdownv2("## Title\n\n**bold** and [link](https://x.com)")
        assert "*Title*" in result
        assert "*bold*" in result
        assert "[" in result


# ---------------------------------------------------------------------------
# split_message
# ---------------------------------------------------------------------------

class TestSplitMessage:
    def test_short_message_no_split(self) -> None:
        result = split_message("short text")
        assert result == ["short text"]

    def test_splits_at_paragraph_boundary(self) -> None:
        para1 = "a" * 2000
        para2 = "b" * 2000
        text = f"{para1}\n\n{para2}"
        result = split_message(text)
        assert len(result) == 2
        assert result[0] == para1
        assert result[1] == para2

    def test_hard_split_long_line(self) -> None:
        text = "x" * 8000
        result = split_message(text, max_len=4000)
        assert len(result) >= 2
        assert all(len(c) <= 4000 for c in result)

    def test_empty_returns_list(self) -> None:
        result = split_message("")
        assert result == [""]

    def test_custom_max_len(self) -> None:
        text = "short\n\nmedium\n\nlonger"
        result = split_message(text, max_len=10)
        assert len(result) >= 2


# ---------------------------------------------------------------------------
# send_radar (async, mocked HTTP)
# ---------------------------------------------------------------------------

def _make_config(telegram_enabled: bool = True) -> Any:
    class TelegramCfg:
        enabled = telegram_enabled
        split_messages = True
    class Cfg:
        telegram = TelegramCfg()
    return Cfg()


@pytest.mark.asyncio
class TestSendRadar:
    async def test_sends_successfully(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")

        with respx.mock:
            respx.post(re.compile(r"api\.telegram\.org")).mock(
                return_value=httpx.Response(200, json={"ok": True})
            )
            result = await send_radar("Hello digest", _make_config())

        assert result is True

    async def test_missing_token_returns_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
        result = await send_radar("Hello", _make_config())
        assert result is False

    async def test_sends_multiple_chunks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")

        long_text = ("paragraph " * 500 + "\n\n") * 5

        with respx.mock:
            route = respx.post(re.compile(r"api\.telegram\.org")).mock(
                return_value=httpx.Response(200, json={"ok": True})
            )
            result = await send_radar(long_text, _make_config())

        assert result is True
        assert route.call_count >= 2

    async def test_fallback_on_400(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")

        call_count = 0

        def _side_effect(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return httpx.Response(400, json={"ok": False})
            return httpx.Response(200, json={"ok": True})

        with respx.mock:
            respx.post(re.compile(r"api\.telegram\.org")).mock(side_effect=_side_effect)
            result = await send_radar("test", _make_config())

        assert result is True


# ---------------------------------------------------------------------------
# send_counter_signals (async, mocked HTTP)
# ---------------------------------------------------------------------------

def _make_ranked_signal(
    url: str = "https://example.com/a",
    title: str = "Counter point",
    score: int = 8,
    reasoning: str = "Good reasoning",
    narrative_claim: str = "AI replaces devs",
) -> Any:
    return make_ranked_signal(
        url=url, title=title, score=score,
        reasoning=reasoning, narrative_claim=narrative_claim,
    )


@pytest.mark.asyncio
class TestSendCounterSignals:
    async def test_empty_signals_returns_false(self) -> None:
        result = await send_counter_signals([], _make_config())
        assert result is False

    async def test_sends_successfully(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")

        signals = [_make_ranked_signal(), _make_ranked_signal(url="https://b.com")]

        with respx.mock:
            respx.post(re.compile(r"api\.telegram\.org")).mock(
                return_value=httpx.Response(200, json={"ok": True})
            )
            result = await send_counter_signals(signals, _make_config())

        assert result is True

    async def test_missing_token_returns_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
        result = await send_counter_signals([_make_ranked_signal()], _make_config())
        assert result is False


# ---------------------------------------------------------------------------
# send_article_cards (async, mocked HTTP)
# ---------------------------------------------------------------------------

def _make_article(
    title: str = "Test Article",
    link: str = "https://example.com/article",
    description: str = "A short description of the article",
    source: str = "hackernews",
) -> Any:
    return make_article(title=title, link=link, description=description, source=source)


@pytest.mark.asyncio
class TestSendArticleCards:
    async def test_sends_cards_with_keyboard(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")

        articles = {"tech": [_make_article(), _make_article(title="Second", link="https://b.com")]}

        with respx.mock:
            route = respx.post(re.compile(r"api\.telegram\.org")).mock(
                return_value=httpx.Response(200, json={"ok": True})
            )
            result = await send_article_cards(articles, _make_config())

        assert len(result) == 2
        assert route.call_count == 2

    async def test_returns_hash_source_map(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")

        articles = {"ai": [_make_article(source="reddit")]}

        with respx.mock:
            respx.post(re.compile(r"api\.telegram\.org")).mock(
                return_value=httpx.Response(200, json={"ok": True})
            )
            result = await send_article_cards(articles, _make_config())

        assert len(result) == 1
        source_name = list(result.values())[0]
        assert source_name == "reddit"
        hash_key = list(result.keys())[0]
        assert len(hash_key) == 8

    async def test_missing_token_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
        result = await send_article_cards({"tech": [_make_article()]}, _make_config())
        assert result == {}

    async def test_card_failure_continues(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")

        articles = {"tech": [_make_article(), _make_article(title="Good", link="https://good.com")]}

        call_count = 0

        def _side_effect(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise httpx.ConnectError("fail")
            return httpx.Response(200, json={"ok": True})

        with respx.mock:
            respx.post(re.compile(r"api\.telegram\.org")).mock(side_effect=_side_effect)
            result = await send_article_cards(articles, _make_config())

        # Second card should still be in the map even if first failed
        assert len(result) == 2
