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
    def test_no_special_chars(self) -> None:
        assert escape_markdownv2("hello world") == "hello world"

    def test_all_special_chars(self) -> None:
        special = r"\_*[]()~`>#+-=|{}.!"
        result = escape_markdownv2(special)
        assert result == "".join("\\" + ch for ch in special)

    def test_backslash_before_reserved_text_and_at_end(self) -> None:
        assert escape_markdownv2(r"\*bold\* unpaired\* and tail" + "\\") == r"\\\*bold\\\* unpaired\\\* and tail\\"

    def test_empty_string(self) -> None:
        assert escape_markdownv2("") == ""


# ---------------------------------------------------------------------------
# to_markdownv2
# ---------------------------------------------------------------------------


class TestToMarkdownV2:
    def test_mixed_content(self) -> None:
        result = to_markdownv2("## Title\n\n**bold** and [link](https://x.com), price is 5.99")
        assert "*Title*" in result
        assert "##" not in result
        assert "*bold*" in result
        assert "[link](https://x.com)" in result
        assert "5\\.99" in result

    def test_link_label_and_destination_keep_distinct_escape_rules(self) -> None:
        result = to_markdownv2(r"[label\*](https://x.test/a\b)")
        assert result == r"[label\\\*](https://x.test/a\\b)"


def test_card_label_and_destination_keep_distinct_escape_rules() -> None:
    from digest.presentation.telegram import render_article_card

    card = render_article_card(r"label\*", r"https://x.test/a\b)", "source", "category", "summary", _make_config())
    assert card.text.startswith(r"[label\\\*](https://x.test/a\\b\))" + "\n")


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
        self,
        monkeypatch: pytest.MonkeyPatch,
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
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        sleep = AsyncMock()
        monkeypatch.setattr("digest.adapters.telegram.delivery.asyncio.sleep", sleep)
        api_url = "https://api.telegram.org/botfake-token/sendMessage"

        with respx.mock:
            route = respx.post(api_url).mock(
                side_effect=[
                    httpx.Response(429, headers={"Retry-After": "1"}),
                    httpx.Response(200, json={"ok": True}),
                ]
            )
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
        url=url,
        title=title,
        score=score,
        reasoning=reasoning,
        narrative_claim=narrative_claim,
    )


@pytest.mark.asyncio
class TestSendCounterSignals:
    @pytest.mark.parametrize(
        "language,name,challenge",
        [
            ("en", "Irritator", "Challenges or complicates"),
            ("ru", "Раздражатор", "Оспаривает или уточняет"),
        ],
    )
    async def test_static_labels_follow_canonical_language(
        self,
        monkeypatch: pytest.MonkeyPatch,
        language: str,
        name: str,
        challenge: str,
    ) -> None:
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
        config = _make_config()
        if language == "ru":
            config.radar = SimpleNamespace(language=language)
        config.translation = SimpleNamespace(target_language="ru" if language == "en" else "en")
        signal = _make_ranked_signal()
        with respx.mock:
            route = respx.post(re.compile(r"api\.telegram\.org")).mock(
                return_value=httpx.Response(200, json={"ok": True})
            )
            populated_result = await send_counter_signals([signal], config)
            empty_result = await send_counter_signals([], config, IrritatorStatus("0 signals", "empty"))
        assert populated_result is True
        assert empty_result is False
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

    async def test_empty_signals_error_sends_loud(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Error-level status is sent as a loud notification (no disable_notification)."""
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")

        with respx.mock:
            route = respx.post(re.compile(r"api\.telegram\.org")).mock(
                return_value=httpx.Response(200, json={"ok": True})
            )
            result = await send_counter_signals(
                [],
                _make_config(),
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
        title=title,
        link=link,
        source=source,
        category=category,
        summary=summary,
    )


@pytest.mark.asyncio
class TestSendArticleCards:
    @pytest.fixture(autouse=True)
    def no_sleep(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("digest.adapters.telegram.delivery.asyncio.sleep", AsyncMock())

    async def test_without_username_sends_command_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")

        top = [
            _make_top(
                title=f"Synthetic collision article {number}",
                link=f"https://example.invalid/article/{number}",
                source=source,
            )
            for number, source in ((42028, "A"), (53130, "B"))
        ]
        articles = {
            "tech": [_make_article(title=article.title, link=article.link, source=article.source) for article in top]
        }

        with respx.mock:
            route = respx.post(re.compile(r"api\.telegram\.org")).mock(
                return_value=httpx.Response(200, json={"ok": True})
            )
            result = await send_article_cards(articles, _make_config(), top_articles=top)

        assert (result.attempted, result.sent, result.failed) == (2, 2, 0)
        assert result.article_source_map == {
            "cdb96691fa65888074dc4008dd039e3f": "A",
            "cdb96691b648d61a1fbaba5907095a7d": "B",
        }
        assert result.delivered_hashes == {article_hash(article.title, article.link) for article in top}
        assert route.call_count == 2
        for call, article in zip(route.calls, top, strict=True):
            payload = json.loads(call.request.content)
            assert "reply_markup" not in payload
            assert escape_markdownv2(f"/vote g {article_hash(article.title, article.link)}") in payload["text"]

    async def test_missing_token_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
        result = await send_article_cards(
            {"tech": [_make_article()]},
            _make_config(),
            top_articles=[_make_top()],
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
        assert result.article_source_map == {good_hash: "hackernews"}
        assert result.delivered_hashes == {good_hash}

    async def test_all_cards_fail_returns_no_delivery(
        self,
        monkeypatch: pytest.MonkeyPatch,
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
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")

        with respx.mock:
            route = respx.post(re.compile(r"api\.telegram\.org")).mock(
                return_value=httpx.Response(429, headers={"Retry-After": "0"}),
            )
            result = await send_article_cards(
                {"tech": [_make_article()]},
                _make_config(),
                top_articles=[_make_top()],
            )

        assert route.call_count == _MAX_RETRIES
        assert result == ArticleDeliveryResult(attempted=1, failed=1)

    async def test_only_selected_delivered_articles_are_attributed(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
        articles = {"tech": [_make_article(), _make_article(title="Not picked", link="https://b.com")]}

        with respx.mock:
            respx.post(re.compile(r"api\.telegram\.org")).mock(
                return_value=httpx.Response(200, json={"ok": True}),
            )
            result = await send_article_cards(
                articles,
                _make_config(),
                top_articles=[_make_top(source="Rewritten source")],
            )

        full_hash = article_hash("Test Article", "https://example.com/article")
        assert result.article_source_map == {full_hash: "hackernews"}
        assert result.delivered_hashes == {full_hash}

    async def test_sends_top_articles_with_summaries(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")

        top = [
            _make_top(
                title="Big News", source="TechCrunch", category="AI", summary="This is an important development in AI."
            )
        ]
        articles = {"AI": [_make_article(title="Big News")]}

        with respx.mock:
            route = respx.post(re.compile(r"api\.telegram\.org")).mock(
                return_value=httpx.Response(200, json={"ok": True})
            )
            result = await send_article_cards(articles, _make_config(), top_articles=top)

        assert (result.attempted, result.sent, result.failed) == (1, 1, 0)
        assert route.call_count == 1

    @pytest.mark.parametrize(
        "language,enabled,note",
        [
            ("en", False, "Tap a vote button, then Start to send it."),
            ("ru", False, "Нажмите оценку, затем Start (Запустить), чтобы отправить голос."),
        ],
    )
    async def test_card_includes_async_feedback_note(
        self,
        monkeypatch: pytest.MonkeyPatch,
        language: str,
        enabled: bool,
        note: str,
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

        full_hash = article_hash(top[0].title, top[0].link)
        assert payload["reply_markup"]["inline_keyboard"] == [
            [
                {"text": "👍", "url": f"https://t.me/example_digest_bot?start=vote_g_{full_hash}"},
                {"text": "👎", "url": f"https://t.me/example_digest_bot?start=vote_b_{full_hash}"},
            ]
        ]

    async def test_skips_send_when_top_articles_empty(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Regression: without top_articles the function must NOT fan out raw feed."""
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")

        articles = {
            "tech": [_make_article(title=f"A{i}", link=f"https://example.com/{i}") for i in range(60)],
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
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("digest.presentation.telegram._SPLIT_LIMIT", 240)
        long_url = "https://example.com/" + "path_" * 24 + "?a=1&b=2"
        top = [
            _make_top(title="One", link="https://a.test/1", summary="First.", source="A"),
            _make_top(title="Two", link="https://a.test/2", summary="Second.", source="B"),
            _make_top(
                title="Three 🚀 [complete]", link=long_url,
                summary=(r"🧭_detail \*literal\* " + "\\" * 3) * 90, source="C",
            ),
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
        assert all((len(text) - len(text.rstrip("\\"))) % 2 == 0 for text in texts)
        recovered = "".join(re.sub(r"\\(.)", r"\1", text) for text in texts)
        expected = "\n\n".join(
            f"{index}. {article.title}\n{article.summary}\n{article.source}\n{article.link}"
            for index, article in enumerate(top, 1)
        )
        assert recovered == expected + "\n\n" + notice + "\n" + (
            "Tap a vote button, then Start to send it. Processed on the next digest run; private owner chat only."
        )
        assert all(len(text.encode("utf-16-le")) // 2 <= 240 for text in texts)
        assert sum(escape_markdownv2(long_url) in text for text in texts) == 1
        assert ranges[0].covering_chunks == ranges[1].covering_chunks == (0,)
        assert len(ranges[2].covering_chunks) > 1
        for index, (article, article_range) in enumerate(zip(top, ranges, strict=True), 1):
            rows = [
                (chunk_index, row)
                for chunk_index, chunk in enumerate(chunks)
                if chunk.reply_markup
                for row in chunk.reply_markup["inline_keyboard"]
                if row[0]["text"] == f"{index}👍"
            ]
            full_hash = article_hash(article.title, article.link)
            assert rows == [
                (
                    article_range.covering_chunks[-1],
                    [
                        {"text": f"{index}👍", "url": f"https://t.me/example_digest_bot?start=vote_g_{full_hash}"},
                        {"text": f"{index}👎", "url": f"https://t.me/example_digest_bot?start=vote_b_{full_hash}"},
                    ],
                )
            ]
        assert result.complete and result.outcome == "sent"
        assert (result.attempted, result.sent, result.failed) == (3, 3, 0)
        assert result.total_chunks == result.attempted_chunks == result.confirmed_chunks == len(texts)
        assert result.delivered_hashes == {article_hash(article.title, article.link) for article in top}
        assert result.article_source_map == {
            article_hash(article.title, article.link): article.source for article in top
        }
        callback.assert_called_once_with()

    @pytest.mark.parametrize(
        "second_response,outcome",
        [
            (httpx.Response(429, headers={"Retry-After": "0"}), "failed"),
            (httpx.Response(200, json={"ok": False}), "failed"),
            (httpx.ReadTimeout("uncertain receipt"), "unknown"),
            (httpx.Response(200, text="invalid receipt"), "unknown"),
            (httpx.Response(200, json={"result": {}}), "unknown"),
        ],
    )
    async def test_stops_without_retry_and_keeps_only_confirmed_article_coverage(
        self,
        monkeypatch: pytest.MonkeyPatch,
        second_response: httpx.Response | Exception,
        outcome: str,
    ) -> None:
        monkeypatch.setattr("digest.presentation.telegram._SPLIT_LIMIT", 200)
        top = [
            _make_top(title="First", link="https://a.test/1", summary="Accepted", source="A"),
            _make_top(title="Spanning", link="https://a.test/2", summary="x" * 800, source="B"),
            _make_top(title="Unattempted", link="https://a.test/3", summary="Later", source="C"),
        ]
        callback = Mock()
        with respx.mock:
            route = respx.post(re.compile(r"api\.telegram\.org")).mock(
                side_effect=[
                    httpx.Response(200, json={"ok": True}),
                    second_response,
                ]
            )
            result = await send_compact_issue(top, self.config(), before_send=callback)

        full_hash = article_hash(top[0].title, top[0].link)
        assert route.call_count == result.attempted_chunks == 2
        assert result.confirmed_chunks == 1
        assert result.total_chunks > 2
        assert result.outcome == outcome and not result.complete
        assert (result.attempted, result.sent, result.failed) == (2, 1, 1)
        assert result.delivered_hashes == {full_hash}
        assert result.article_source_map == {full_hash: "A"}
        callback.assert_called_once_with()

    async def test_final_notice_failure_does_not_complete_issue(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("digest.presentation.telegram._SPLIT_LIMIT", 200)
        top = [_make_top(title="Article", link="https://a.test/1", summary="Complete", source="A")]
        config = self.config()
        notice = "n" * 300
        chunks, ranges = _render_compact_issue(top, config, notice)
        assert ranges[0].covering_chunks[-1] < len(chunks) - 1
        with respx.mock:
            route = respx.post(re.compile(r"api\.telegram\.org")).mock(
                side_effect=[
                    *[httpx.Response(200, json={"ok": True}) for _ in chunks[:-1]],
                    httpx.Response(403, json={"ok": False}),
                ]
            )
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
        self,
        monkeypatch: pytest.MonkeyPatch,
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
        self,
        monkeypatch: pytest.MonkeyPatch,
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
                [],
                self.config(),
                notice="No selected articles.",
                before_send=callback,
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
        assert article_hash(top[0].title, top[0].link) in article_payload["text"]
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
    literal = r"A \*qualified\* point" + "\\"
    encoded = escape_markdownv2(literal)
    with respx.mock:
        route = respx.post(api_url).mock(
            side_effect=[
                httpx.Response(400),
                httpx.Response(200, json={"ok": False}),
            ]
        )
        async with httpx.AsyncClient() as client:
            await _send_chunk(client, api_url, "123", encoded, True, keyboard)
    assert route.call_count == 2
    first, fallback = [json.loads(call.request.content) for call in route.calls]
    assert first == {
        "chat_id": "123",
        "text": encoded,
        "parse_mode": "MarkdownV2",
        "disable_notification": True,
        "reply_markup": keyboard,
    }
    assert fallback == {
        "chat_id": "123",
        "text": literal,
        "disable_notification": True,
        "reply_markup": keyboard,
    }


def _synthesis_fixture(language: str = "ru") -> Any:
    from digest.domain.editorial.reviews import EvidenceItem
    from digest.domain.editorial.synthesis import (
        EvidenceBinding,
        SynthesisComparison,
        SynthesisProse,
        SynthesisResult,
        SynthesisSupport,
    )

    quotes = (
        "Queue A accepts payment requests during settlement outages and completes settlement only after recovery.",
        "Gateway B accepts invoice requests during ledger outages and replays them only after recovery.",
    )
    bindings = tuple(
        EvidenceBinding(
            EvidenceItem(str(i) * 32, title, url, source, "Synthetic systems", None, quote, False),
            str(i) * 64, source, feed,
        )
        for i, title, url, source, quote, feed in (
            (1, "Synthetic Queue A", "https://example.com/a", "Synthetic source A", quotes[0],
             "https://example.com/rss"),
            (2, "Synthetic Gateway B", "https://example.org/b", "Synthetic source B", quotes[1],
             "https://example.org/rss"),
        )
    )
    canonical = SynthesisProse(
        "Both examples separate request acceptance from downstream work: requests are accepted during an outage, "
        "while the dependent operation waits for recovery.",
        "The excerpts describe behavior; neither provides production-scale recovery measurements or verified "
        "end-to-end completion.",
    )
    presentation = SynthesisProse(
        "Оба примера разделяют приём запросов и последующую обработку: запросы принимаются во время сбоя, "
        "а зависимая операция откладывается до восстановления.",
        "В отрывках описано поведение; ни один не содержит измерений восстановления в промышленном масштабе "
        "или подтверждения сквозного завершения.",
    ) if language == "ru" else None
    supports = tuple(SynthesisSupport(b.evidence.evidence_id, b.occurrence_sha256, "excerpt", b.evidence.excerpt)
                     for b in bindings)
    return SynthesisResult(1, language, "comparison", None,
                           SynthesisComparison("shared_mechanism", supports, canonical, presentation)), bindings


@pytest.mark.parametrize("language", ["en", "ru"])
def test_synthesis_text_preserves_original_quotes_links_and_complete_localized_labels(language: str) -> None:
    from digest.presentation.telegram import synthesis_text

    result, bindings = _synthesis_fixture(language)
    text = synthesis_text(result, bindings)
    prose = result.comparison.presentation if language == "ru" else result.comparison.canonical
    expected = ["Сопоставление выбранных материалов" if language == "ru" else "Comparison of selected items",
                prose.relationship, prose.qualification]
    for binding in bindings:
        expected.extend([("Источник: " if language == "ru" else "Source: ") + binding.source,
                         binding.evidence.excerpt, binding.evidence.url])
    expected.append(
        "Вывод по ограниченным RSS-отрывкам; смысловое соответствие перевода независимо не проверено."
        if language == "ru" else "Interpretation of limited RSS excerpts."
    )
    assert text == "\n".join(expected)
    units = len(escape_markdownv2(text).encode("utf-16-le")) // 2
    assert units == 727 if language == "ru" else units <= 1000
    assert "https://example.com/rss" not in text  # Visible credit is the retained article URL.


def test_synthesis_text_budget_counts_escaping_and_emoji_without_shortening() -> None:
    from dataclasses import replace

    from digest.presentation.telegram import synthesis_text

    result, bindings = _synthesis_fixture("en")
    baseline = synthesis_text(result, bindings)
    units = len(escape_markdownv2(baseline).encode("utf-16-le")) // 2
    remaining = 1000 - units
    source = bindings[0].source + "🚀" * (remaining // 2) + "." * (remaining % 2)
    # A dot and a non-BMP character each cost two escaped UTF-16 units.
    if remaining % 2:
        source = source[:-1] + "a"
    exact = (replace(bindings[0], source=source), bindings[1])
    text = synthesis_text(result, exact)
    assert len(escape_markdownv2(text).encode("utf-16-le")) // 2 == 1000
    with pytest.raises(ValueError, match="1000-unit"):
        synthesis_text(result, (replace(exact[0], source=source + "."), exact[1]))
    with pytest.raises(ValueError, match="1000-unit"):
        synthesis_text(result, (replace(bindings[0], evidence=replace(bindings[0].evidence,
                       url="https://example.com/" + "x" * 1000)), bindings[1]))


@pytest.mark.parametrize("defect", ["abstention", "language", "missing_binding", "ambiguous_binding"])
def test_synthesis_text_rejects_unrenderable_values(defect: str) -> None:
    from dataclasses import replace

    from digest.presentation.telegram import synthesis_text

    result, bindings = _synthesis_fixture()
    if defect == "abstention":
        result = replace(result, status="abstained", comparison=None)
    elif defect == "language":
        result = replace(result, comparison=replace(result.comparison, presentation=None))
    elif defect == "missing_binding":
        bindings = bindings[1:]
    else:
        bindings += (bindings[0],)
    with pytest.raises(ValueError):
        synthesis_text(result, bindings)


@pytest.mark.parametrize("closer,with_supplement,username", [(True, True, True), (True, False, True),
                                                          (False, True, False), (False, False, True)])
def test_synthesis_placement_is_separate_from_article_and_supplement_coverage(
    closer: bool, with_supplement: bool, username: bool,
) -> None:
    from digest.presentation.telegram import SupplementPlacement, SynthesisPlacement, render_compact_publication

    config = _make_config()
    config.telegram.bot_username = "example_digest_bot" if username else ""
    cards = [_make_top(title="Main", link="https://example.com/main", summary="Main prose.")]
    if closer:
        cards.append(_make_top(title="Closing", link="https://example.com/closer", summary="Closing prose."))
    supplement = SupplementPlacement("fragment", "Supplementary finding.", 1) if with_supplement else None
    synthesis = SynthesisPlacement(r"Comparison 🚀 [source] \ original.", 1)
    baseline = render_compact_publication(cards, config, "Footer.", supplement)
    assert baseline == render_compact_publication(cards, config, "Footer.", supplement, synthesis=None)
    rendered = render_compact_publication(cards, config, "Footer.", supplement, synthesis=synthesis)
    assert rendered.synthesis == (0,) and baseline.synthesis is None
    assert len(rendered.chunks) == len(baseline.chunks) == 1
    assert [(item.full_hash, item.source, item.covering_chunks) for item in rendered.articles] == [
        (item.full_hash, item.source, item.covering_chunks) for item in baseline.articles
    ]
    assert rendered.supplement == baseline.supplement
    assert rendered.chunks[0].reply_markup == baseline.chunks[0].reply_markup
    plain = re.sub(r"\\(.)", r"\1", rendered.chunks[0].text)
    assert plain.index("Main prose.") < plain.index(synthesis.text) < plain.index("Footer.")
    if supplement:
        assert plain.index(synthesis.text) < plain.index(supplement.text)
        if closer:
            assert plain.index(supplement.text) < plain.index("Closing prose.")
    elif closer:
        assert plain.index(synthesis.text) < plain.index("Closing prose.")


@pytest.mark.parametrize("position", [-1, 2])
def test_synthesis_placement_rejects_outside_article_sequence(position: int) -> None:
    from digest.presentation.telegram import SynthesisPlacement, render_compact_publication

    with pytest.raises(ValueError, match="synthesis publication position"):
        render_compact_publication([_make_top()], _make_config(), "", synthesis=SynthesisPlacement("Text", position))


def test_synthesis_span_reports_split_credit_instead_of_claiming_single_chunk(monkeypatch: pytest.MonkeyPatch) -> None:
    from digest.presentation.telegram import SynthesisPlacement, render_compact_publication

    monkeypatch.setattr("digest.presentation.telegram._SPLIT_LIMIT", 120)
    text = "Quote 🚀 " * 20 + "\nSource A\nhttps://example.com/a"
    rendered = render_compact_publication([], _make_config(), "", synthesis=SynthesisPlacement(text, 0))
    assert rendered.articles == [] and rendered.supplement is None
    assert rendered.synthesis == tuple(range(len(rendered.chunks)))
    assert len(rendered.synthesis) > 1
    assert "".join(chunk.text for chunk in rendered.chunks) == escape_markdownv2(text)
