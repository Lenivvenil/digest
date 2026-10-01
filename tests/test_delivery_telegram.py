"""Tests for src.delivery.telegram."""

from __future__ import annotations

import json
import re
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from digest.delivery.telegram import (
    _MAX_RETRIES,
    ArticleDeliveryResult,
    _send_chunk,
    escape_markdownv2,
    send_article_cards,
    send_counter_signals,
    split_message,
    to_markdownv2,
)
from digest.irritator import IrritatorStatus
from digest.radar.collector import article_hash
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

    def test_prior_paragraph_not_repeated_before_oversized_paragraph(self) -> None:
        prefix = "old"
        long_paragraph = "x" * 30
        chunks = split_message(prefix + "\n\n" + long_paragraph, max_len=10)
        assert chunks[0] == prefix
        assert "".join(chunks[1:]) == long_paragraph
        assert all(len(chunk) <= 10 for chunk in chunks)

    def test_empty_returns_list(self) -> None:
        result = split_message("")
        assert result == [""]

    def test_custom_max_len(self) -> None:
        text = "short\n\nmedium\n\nlonger"
        result = split_message(text, max_len=10)
        assert len(result) >= 2


def _make_config(telegram_enabled: bool = True) -> Any:
    class TelegramCfg:
        enabled = telegram_enabled
        split_messages = True
    class Cfg:
        telegram = TelegramCfg()
    return Cfg()


# ---------------------------------------------------------------------------
# _send_chunk (retry exhaustion must never masquerade as a successful send)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestSendChunk:
    async def test_repeated_rate_limits_raise(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        sleep = AsyncMock()
        monkeypatch.setattr("digest.delivery.telegram.asyncio.sleep", sleep)
        api_url = "https://api.telegram.org/botfake-token/sendMessage"

        with respx.mock:
            route = respx.post(api_url).mock(
                return_value=httpx.Response(429, headers={"Retry-After": "3"}),
            )
            async with httpx.AsyncClient() as client:
                with pytest.raises(httpx.HTTPStatusError) as exc_info:
                    await _send_chunk(client, api_url, "123", "test")

        assert exc_info.value.response.status_code == 429
        assert route.call_count == _MAX_RETRIES
        assert sleep.await_count == _MAX_RETRIES - 1
        assert all(call.args == (3,) for call in sleep.await_args_list)

    async def test_rate_limit_then_success(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        sleep = AsyncMock()
        monkeypatch.setattr("digest.delivery.telegram.asyncio.sleep", sleep)
        api_url = "https://api.telegram.org/botfake-token/sendMessage"

        with respx.mock:
            route = respx.post(api_url).mock(side_effect=[
                httpx.Response(429, headers={"Retry-After": "1"}),
                httpx.Response(200, json={"ok": True}),
            ])
            async with httpx.AsyncClient() as client:
                await _send_chunk(client, api_url, "123", "test")

        assert route.call_count == 2
        sleep.assert_awaited_once_with(1)


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

    @pytest.mark.parametrize("language,name,challenge", [
        ("en", "Irritator", "Challenges or complicates"),
        ("ru", "Раздражатор", "Оспаривает или уточняет"),
    ])
    async def test_static_labels_follow_canonical_language(
        self, monkeypatch: pytest.MonkeyPatch, language: str, name: str, challenge: str,
    ) -> None:
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
        config = _make_config()
        config.radar = SimpleNamespace(language=language)
        config.translation = SimpleNamespace(target_language="ru" if language == "en" else "en")
        signal = _make_ranked_signal()
        with respx.mock:
            route = respx.post(re.compile(r"api\.telegram\.org")).mock(
                return_value=httpx.Response(200, json={"ok": True})
            )
            await send_counter_signals([signal], config)
            await send_counter_signals([], config, IrritatorStatus("0 signals", "empty"))
        populated, empty = [json.loads(call.request.content) for call in route.calls]
        assert name.upper() in populated["text"]
        assert challenge + ":" in populated["text"]
        assert signal.reasoning in populated["text"]
        assert escape_markdownv2(signal.signal.url) in populated["text"]
        assert name + ": 0 signals" in empty["text"]
        assert empty["disable_notification"] is True

    async def test_missing_token_returns_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
        result = await send_counter_signals([_make_ranked_signal()], _make_config())
        assert result is False

    async def test_empty_signals_no_status_no_send(self) -> None:
        result = await send_counter_signals([], _make_config(), irritator_status=None)
        assert result is False

    async def test_empty_signals_sends_status_silent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Empty-level status (no signals, no error) is sent with disable_notification=True."""
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")

        with respx.mock:
            route = respx.post(re.compile(r"api\.telegram\.org")).mock(
                return_value=httpx.Response(200, json={"ok": True})
            )
            result = await send_counter_signals(
                [], _make_config(),
                irritator_status=IrritatorStatus("3 narratives, 0 signals", "empty"),
            )

        assert result is False
        assert route.called
        payload = json.loads(route.calls[0].request.content)
        assert payload.get("disable_notification") is True

    async def test_empty_signals_error_sends_loud(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Error-level status is sent as a loud notification (no disable_notification)."""
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")

        with respx.mock:
            route = respx.post(re.compile(r"api\.telegram\.org")).mock(
                return_value=httpx.Response(200, json={"ok": True})
            )
            result = await send_counter_signals(
                [], _make_config(),
                irritator_status=IrritatorStatus("query generation failed: timeout", "error"),
            )

        assert result is False
        assert route.called
        payload = json.loads(route.calls[0].request.content)
        assert not payload.get("disable_notification", False)


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


def _make_top(
    title: str = "Test Article",
    link: str = "https://example.com/article",
    source: str = "hackernews",
    category: str = "tech",
    summary: str = "A short LLM summary.",
) -> Any:
    from digest.radar.summarizer import ArticleSummary
    return ArticleSummary(
        title=title, link=link, source=source, category=category, summary=summary,
    )


@pytest.mark.asyncio
class TestSendArticleCards:
    @pytest.fixture(autouse=True)
    def no_sleep(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("digest.delivery.telegram.asyncio.sleep", AsyncMock())

    async def test_sends_cards_with_keyboard(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")

        articles = {"tech": [_make_article(), _make_article(title="Second", link="https://b.com")]}
        top = [_make_top(), _make_top(title="Second", link="https://b.com")]

        with respx.mock:
            route = respx.post(re.compile(r"api\.telegram\.org")).mock(
                return_value=httpx.Response(200, json={"ok": True})
            )
            result = await send_article_cards(articles, _make_config(), top_articles=top)

        assert (result.attempted, result.sent, result.failed) == (2, 2, 0)
        assert len(result.article_source_map) == 2
        assert result.delivered_hashes == {
            article_hash(article.title, article.link) for article in top
        }
        assert route.call_count == 2

    async def test_returns_hash_source_map(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")

        articles = {"ai": [_make_article(source="reddit")]}
        top = [_make_top(source="reddit", category="ai")]

        with respx.mock:
            respx.post(re.compile(r"api\.telegram\.org")).mock(
                return_value=httpx.Response(200, json={"ok": True})
            )
            result = await send_article_cards(articles, _make_config(), top_articles=top)

        assert (result.attempted, result.sent, result.failed) == (1, 1, 0)
        assert len(result.article_source_map) == 1
        source_name = list(result.article_source_map.values())[0]
        assert source_name == "reddit"
        hash_key = list(result.article_source_map.keys())[0]
        assert len(hash_key) == 8

    async def test_missing_token_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
        result = await send_article_cards(
            {"tech": [_make_article()]}, _make_config(), top_articles=[_make_top()],
        )
        assert result == ArticleDeliveryResult()

    async def test_card_failure_continues(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")

        articles = {"tech": [_make_article(), _make_article(title="Good", link="https://good.com")]}
        top = [_make_top(), _make_top(title="Good", link="https://good.com")]

        call_count = 0

        def _side_effect(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise httpx.ConnectError("fail")
            return httpx.Response(200, json={"ok": True})

        with respx.mock:
            respx.post(re.compile(r"api\.telegram\.org")).mock(side_effect=_side_effect)
            result = await send_article_cards(articles, _make_config(), top_articles=top)

        good_hash = article_hash("Good", "https://good.com")
        assert (result.attempted, result.sent, result.failed) == (2, 1, 1)
        assert result.article_source_map == {good_hash[:8]: "hackernews"}
        assert result.delivered_hashes == {good_hash}

    async def test_all_cards_fail_returns_no_delivery(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
        articles = {"tech": [_make_article(), _make_article(title="Second", link="https://b.com")]}
        top = [_make_top(), _make_top(title="Second", link="https://b.com")]

        with respx.mock:
            route = respx.post(re.compile(r"api\.telegram\.org")).mock(
                side_effect=httpx.ConnectError("Cannot connect"),
            )
            result = await send_article_cards(articles, _make_config(), top_articles=top)

        assert route.call_count == 2
        assert result == ArticleDeliveryResult(attempted=2, failed=2)

    async def test_exhausted_rate_limit_marks_card_failed(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")

        with respx.mock:
            route = respx.post(re.compile(r"api\.telegram\.org")).mock(
                return_value=httpx.Response(429, headers={"Retry-After": "0"}),
            )
            result = await send_article_cards(
                {"tech": [_make_article()]}, _make_config(), top_articles=[_make_top()],
            )

        assert route.call_count == _MAX_RETRIES
        assert result == ArticleDeliveryResult(attempted=1, failed=1)

    async def test_only_selected_delivered_articles_are_attributed(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
        articles = {"tech": [_make_article(), _make_article(title="Not picked", link="https://b.com")]}

        with respx.mock:
            respx.post(re.compile(r"api\.telegram\.org")).mock(
                return_value=httpx.Response(200, json={"ok": True}),
            )
            result = await send_article_cards(
                articles, _make_config(), top_articles=[_make_top(source="Rewritten source")],
            )

        full_hash = article_hash("Test Article", "https://example.com/article")
        assert result.article_source_map == {full_hash[:8]: "hackernews"}
        assert result.delivered_hashes == {full_hash}

    async def test_sends_top_articles_with_summaries(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")

        top = [_make_top(title="Big News", source="TechCrunch", category="AI",
                         summary="This is an important development in AI.")]
        articles = {"AI": [_make_article(title="Big News")]}

        with respx.mock:
            route = respx.post(re.compile(r"api\.telegram\.org")).mock(
                return_value=httpx.Response(200, json={"ok": True})
            )
            result = await send_article_cards(articles, _make_config(), top_articles=top)

        assert (result.attempted, result.sent, result.failed) == (1, 1, 0)
        assert route.call_count == 1

    @pytest.mark.parametrize("language,enabled,note", [
        ("en", True, "Votes are processed on digest runs; private owner chat only"),
        ("en", False, "Votes are processed on digest runs; private owner chat only"),
        ("ru", True, "Оценки обрабатываются при запусках дайджеста; только личный чат владельца"),
        ("ru", False, "Оценки обрабатываются при запусках дайджеста; только личный чат владельца"),
    ])
    async def test_card_includes_async_feedback_note(
        self, monkeypatch: pytest.MonkeyPatch, language: str, enabled: bool, note: str,
    ) -> None:
        """Each card must carry the italicised async-feedback note."""
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")

        articles = {"tech": [_make_article()]}
        top = [_make_top()]

        config = _make_config()
        config.radar = SimpleNamespace(language=language)
        config.adaptive = SimpleNamespace(enabled=enabled)
        with respx.mock:
            route = respx.post(re.compile(r"api\.telegram\.org")).mock(
                return_value=httpx.Response(200, json={"ok": True})
            )
            await send_article_cards(articles, config, top_articles=top)

        assert route.call_count == 1
        payload = json.loads(route.calls[0].request.content)
        text: str = payload["text"]
        assert f"_{escape_markdownv2(note)}_" in text

        hash8 = article_hash(top[0].title, top[0].link)[:8]
        assert payload["reply_markup"]["inline_keyboard"] == [[
            {"text": "👍", "callback_data": f"fb:a:g:{hash8}"},
            {"text": "👎", "callback_data": f"fb:a:b:{hash8}"},
        ]]

    async def test_skips_send_when_top_articles_empty(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Regression: without top_articles the function must NOT fan out raw feed."""
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")

        articles = {
            "tech": [
                _make_article(title=f"A{i}", link=f"https://example.com/{i}")
                for i in range(60)
            ],
        }

        with respx.mock:
            route = respx.post(re.compile(r"api\.telegram\.org")).mock(
                return_value=httpx.Response(200, json={"ok": True})
            )
            result_none = await send_article_cards(articles, _make_config())
            result_empty = await send_article_cards(articles, _make_config(), top_articles=[])

        # No selected cards means no sends and no delivery attribution.
        assert route.call_count == 0
        assert result_none == ArticleDeliveryResult()
        assert result_empty == ArticleDeliveryResult()
