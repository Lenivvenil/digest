"""Tests for src.delivery.telegram."""

from __future__ import annotations

import json
import re
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
import respx

from digest.delivery.telegram import (
    _MAX_RETRIES,
    ArticleDeliveryResult,
    IssueDeliveryResult,
    _render_compact_issue,
    _send_chunk,
    escape_markdownv2,
    send_article_cards,
    send_compact_issue,
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
        monkeypatch.setattr("digest.adapters.telegram.delivery.asyncio.sleep", sleep)
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
        monkeypatch.setattr("digest.adapters.telegram.delivery.asyncio.sleep", sleep)
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
        monkeypatch.setattr("digest.adapters.telegram.delivery.asyncio.sleep", AsyncMock())

    async def test_without_username_sends_command_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
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
        for call, article in zip(route.calls, top, strict=True):
            payload = json.loads(call.request.content)
            assert "reply_markup" not in payload
            assert escape_markdownv2(f"/vote g {article_hash(article.title, article.link)[:8]}") in payload["text"]

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
        ("en", False, "Tap a vote button, then Start to send it."),
        ("ru", False, "Нажмите оценку, затем Start (Запустить), чтобы отправить голос."),
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
        config.telegram.bot_username = "example_digest_bot"
        with respx.mock:
            route = respx.post(re.compile(r"api\.telegram\.org")).mock(
                return_value=httpx.Response(200, json={"ok": True})
            )
            await send_article_cards(articles, config, top_articles=top)

        assert route.call_count == 1
        payload = json.loads(route.calls[0].request.content)
        text: str = payload["text"]
        assert escape_markdownv2(note) in text
        assert escape_markdownv2("/vote g ") in text

        hash8 = article_hash(top[0].title, top[0].link)[:8]
        assert payload["reply_markup"]["inline_keyboard"] == [[
            {"text": "👍", "url": f"https://t.me/example_digest_bot?start=vote_g_{hash8}"},
            {"text": "👎", "url": f"https://t.me/example_digest_bot?start=vote_b_{hash8}"},
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


# ---------------------------------------------------------------------------
# compact issue: lossless preparation, one-attempt dispatch, confirmed coverage
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestSendCompactIssue:
    @pytest.fixture(autouse=True)
    def credentials(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")

    @staticmethod
    def config() -> Any:
        config = _make_config()
        config.telegram.bot_username = "example_digest_bot"
        return config

    async def test_lossless_shared_and_spanning_chunks_with_indexed_votes(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("digest.presentation.telegram._SPLIT_LIMIT", 240)
        long_url = "https://example.com/" + "path_" * 24 + "?a=1&b=2"
        top = [
            _make_top(title="One", link="https://a.test/1", summary="First.", source="A"),
            _make_top(title="Two", link="https://a.test/2", summary="Second.", source="B"),
            _make_top(title="Three 🚀 [complete]", link=long_url, summary="🧭_detail " * 90, source="C"),
        ]
        notice = "Review: complete. Translation: original."
        config = self.config()
        chunks, ranges = _render_compact_issue(top, config, notice)
        callback = Mock()

        def accepted(request: httpx.Request) -> httpx.Response:
            callback.assert_called_once_with()
            return httpx.Response(200, json={"ok": True})

        with respx.mock:
            route = respx.post(re.compile(r"api\.telegram\.org")).mock(
                side_effect=accepted,
            )
            result = await send_compact_issue(top, config, notice=notice, before_send=callback)

        texts = [json.loads(call.request.content)["text"] for call in route.calls]
        recovered = re.sub(r"\\(.)", r"\1", "".join(texts))
        expected = "\n\n".join(
            f"{index}. {article.title}\n{article.summary}\n{article.source}\n{article.link}"
            for index, article in enumerate(top, 1)
        )
        assert recovered == expected + "\n\n" + notice + "\n" + (
            "Tap a vote button, then Start to send it. "
            "Processed on the next digest run; private owner chat only."
        )
        assert all(len(text.encode("utf-16-le")) // 2 <= 240 for text in texts)
        assert sum(escape_markdownv2(long_url) in text for text in texts) == 1
        assert ranges[0].covering_chunks == ranges[1].covering_chunks == (0,)
        assert len(ranges[2].covering_chunks) > 1
        for index, (article, article_range) in enumerate(zip(top, ranges, strict=True), 1):
            rows = [
                (chunk_index, row) for chunk_index, chunk in enumerate(chunks)
                if chunk.reply_markup for row in chunk.reply_markup["inline_keyboard"]
                if row[0]["text"] == f"{index}👍"
            ]
            full_hash = article_hash(article.title, article.link)
            assert rows == [(article_range.covering_chunks[-1], [
                {"text": f"{index}👍", "url": f"https://t.me/example_digest_bot?start=vote_g_{full_hash[:8]}"},
                {"text": f"{index}👎", "url": f"https://t.me/example_digest_bot?start=vote_b_{full_hash[:8]}"},
            ])]
        assert result.complete and result.outcome == "sent"
        assert (result.attempted, result.sent, result.failed) == (3, 3, 0)
        assert result.total_chunks == result.attempted_chunks == result.confirmed_chunks == len(texts)
        assert result.delivered_hashes == {article_hash(article.title, article.link) for article in top}
        assert result.article_source_map == {
            article_hash(article.title, article.link)[:8]: article.source for article in top
        }
        callback.assert_called_once_with()

    @pytest.mark.parametrize("second_response,outcome", [
        (httpx.Response(429, headers={"Retry-After": "0"}), "failed"),
        (httpx.Response(200, json={"ok": False}), "failed"),
        (httpx.ReadTimeout("uncertain receipt"), "unknown"),
        (httpx.Response(200, text="invalid receipt"), "unknown"),
        (httpx.Response(200, json={"result": {}}), "unknown"),
    ])
    async def test_stops_without_retry_and_keeps_only_confirmed_article_coverage(
        self, monkeypatch: pytest.MonkeyPatch,
        second_response: httpx.Response | Exception, outcome: str,
    ) -> None:
        monkeypatch.setattr("digest.presentation.telegram._SPLIT_LIMIT", 200)
        top = [
            _make_top(title="First", link="https://a.test/1", summary="Accepted", source="A"),
            _make_top(title="Spanning", link="https://a.test/2", summary="x" * 800, source="B"),
            _make_top(title="Unattempted", link="https://a.test/3", summary="Later", source="C"),
        ]
        callback = Mock()
        with respx.mock:
            route = respx.post(re.compile(r"api\.telegram\.org")).mock(side_effect=[
                httpx.Response(200, json={"ok": True}), second_response,
            ])
            result = await send_compact_issue(top, self.config(), before_send=callback)

        full_hash = article_hash(top[0].title, top[0].link)
        assert route.call_count == result.attempted_chunks == 2
        assert result.confirmed_chunks == 1
        assert result.total_chunks > 2
        assert result.outcome == outcome and not result.complete
        assert (result.attempted, result.sent, result.failed) == (2, 1, 1)
        assert result.delivered_hashes == {full_hash}
        assert result.article_source_map == {full_hash[:8]: "A"}
        callback.assert_called_once_with()

    async def test_final_notice_failure_does_not_complete_issue(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("digest.presentation.telegram._SPLIT_LIMIT", 200)
        top = [_make_top(title="Article", link="https://a.test/1", summary="Complete", source="A")]
        config = self.config()
        notice = "n" * 300
        chunks, ranges = _render_compact_issue(top, config, notice)
        assert ranges[0].covering_chunks[-1] < len(chunks) - 1
        with respx.mock:
            route = respx.post(re.compile(r"api\.telegram\.org")).mock(side_effect=[
                *[httpx.Response(200, json={"ok": True}) for _ in chunks[:-1]],
                httpx.Response(403, json={"ok": False}),
            ])
            result = await send_compact_issue(top, config, notice=notice)

        assert route.call_count == len(chunks)
        assert result.sent == 1 and result.failed == 0
        assert result.outcome == "failed" and not result.complete
        assert result.confirmed_chunks == result.total_chunks - 1

    async def test_impossible_url_fails_preflight_before_callback_or_post(self) -> None:
        callback = Mock()
        top = [_make_top(link="https://example.com/" + "x" * 4000)]
        with respx.mock:
            route = respx.post(re.compile(r"api\.telegram\.org"))
            result = await send_compact_issue(top, self.config(), before_send=callback)
        assert result == IssueDeliveryResult(outcome="failed")
        assert not route.called
        callback.assert_not_called()

    async def test_callback_exception_prevents_first_post(self) -> None:
        callback = Mock(side_effect=RuntimeError("reservation unavailable"))
        with respx.mock:
            route = respx.post(re.compile(r"api\.telegram\.org"))
            with pytest.raises(RuntimeError, match="reservation unavailable"):
                await send_compact_issue([_make_top()], self.config(), before_send=callback)
        assert not route.called
        callback.assert_called_once_with()

    async def test_total_dispatch_timeout_stops_without_retry(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import asyncio

        monkeypatch.setattr("digest.adapters.telegram.delivery._COMPACT_DISPATCH_SECONDS", 0.01)
        attempted = 0

        async def delayed_response(request: httpx.Request) -> httpx.Response:
            nonlocal attempted
            attempted += 1
            await asyncio.Event().wait()
            raise AssertionError("The bounded dispatch should have cancelled this request")

        with respx.mock:
            respx.post(re.compile(r"api\.telegram\.org")).mock(side_effect=delayed_response)
            result = await send_compact_issue([_make_top(summary="x" * 8000)], self.config())
        assert attempted == result.attempted_chunks == 1
        assert result.outcome == "unknown" and result.confirmed_chunks == 0
        assert result.delivered_hashes == set()

    async def test_missing_credentials_and_empty_issue_skip_callback(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        callback = Mock()
        with respx.mock:
            route = respx.post(re.compile(r"api\.telegram\.org"))
            assert await send_compact_issue([], self.config(), before_send=callback) == IssueDeliveryResult()
            monkeypatch.delenv("TELEGRAM_BOT_TOKEN")
            assert await send_compact_issue([_make_top()], self.config(), before_send=callback) == IssueDeliveryResult()
        assert not route.called
        callback.assert_not_called()

    async def test_notice_without_articles_skips_callback_and_post(self) -> None:
        callback = Mock()
        with respx.mock:
            route = respx.post(re.compile(r"api\.telegram\.org"))
            result = await send_compact_issue(
                [], self.config(), notice="No selected articles.", before_send=callback,
            )
        assert result == IssueDeliveryResult()
        assert not result.complete and not route.called
        callback.assert_not_called()
        assert not IssueDeliveryResult(outcome="sent", total_chunks=1, confirmed_chunks=1).complete

    async def test_global_command_fallback(self) -> None:
        top = [_make_top()]
        with respx.mock:
            route = respx.post(re.compile(r"api\.telegram\.org")).mock(
                return_value=httpx.Response(200, json={"ok": True}),
            )
            article_result = await send_compact_issue(top, _make_config())
        assert route.call_count == 1
        article_payload = json.loads(route.calls[0].request.content)
        assert article_result.complete
        assert "reply_markup" not in article_payload
        assert article_hash(top[0].title, top[0].link)[:8] in article_payload["text"]
        assert article_payload["text"].count("/vote g HASH") == 1


def test_pure_rendering_and_supplement_imports_do_not_load_transport() -> None:
    import subprocess
    import sys

    code = r"""
import sys
from digest.delivery.supplement import split_supplement
from digest.presentation.telegram import escape_markdownv2
assert split_supplement('Pure *copy*', escape_markdownv2) == [r'Pure \*copy\*']
for name in sys.modules:
    assert not name.startswith(('httpx', 'digest.adapters', 'digest.config',
                                'digest.radar', 'digest.irritator', 'digest.post_delivery'))
"""
    subprocess.run([sys.executable, "-c", code], check=True, capture_output=True, text=True)


def test_delivery_compatibility_exports_keep_their_actual_owner_identity() -> None:
    from digest import delivery
    from digest.adapters.telegram import delivery as transport
    from digest.delivery import markdown, supplement, telegram
    from digest.domain.delivery import outcomes
    from digest.presentation import supplement as supplement_view
    from digest.presentation import telegram as view

    assert delivery.ArticleDeliveryResult is telegram.ArticleDeliveryResult is outcomes.ArticleDeliveryResult
    assert telegram.IssueDeliveryResult is outcomes.IssueDeliveryResult
    assert delivery.write_digest is markdown.write_digest
    assert delivery.send_article_cards is telegram.send_article_cards is transport.send_article_cards
    assert delivery.send_counter_signals is telegram.send_counter_signals is transport.send_counter_signals
    assert telegram.send_compact_issue is transport.send_compact_issue
    assert telegram.send_status_message is transport.send_status_message
    assert telegram.escape_markdownv2 is view.escape_markdownv2
    assert telegram.to_markdownv2 is view.to_markdownv2
    assert telegram.split_message is view.split_message
    assert telegram._render_compact_issue is view.render_compact_issue
    assert supplement.signal_text is supplement_view.signal_text
    assert supplement.split_supplement is supplement_view.split_supplement


@pytest.mark.asyncio
async def test_legacy_markdown_fallback_retains_payload_and_http_only_acceptance() -> None:
    api_url = "https://api.telegram.org/botfake-token/sendMessage"
    keyboard = {"inline_keyboard": [[{"text": "Vote", "url": "https://t.me/example?start=vote_g_abcd"}]]}
    with respx.mock:
        route = respx.post(api_url).mock(side_effect=[
            httpx.Response(400), httpx.Response(200, json={"ok": False}),
        ])
        async with httpx.AsyncClient() as client:
            await _send_chunk(client, api_url, "123", r"A \*qualified\* point", True, keyboard)
    assert route.call_count == 2
    first, fallback = [json.loads(call.request.content) for call in route.calls]
    assert first == {
        "chat_id": "123", "text": r"A \*qualified\* point", "parse_mode": "MarkdownV2",
        "disable_notification": True, "reply_markup": keyboard,
    }
    assert fallback == {
        "chat_id": "123", "text": "A *qualified* point", "disable_notification": True, "reply_markup": keyboard,
    }
