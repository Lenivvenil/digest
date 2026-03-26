"""Tests for src/telegram.py — Telegram delivery module."""

from __future__ import annotations

import pytest
import respx
import httpx
from unittest.mock import patch

from src.telegram import (
    escape_markdownv2, to_markdownv2, split_message, send_digest,
    send_article_cards, TelegramPartialDeliveryError, _send_chunk,
)
from src.collector import Article
from src.config import Config, LLMConfig, ProviderConfig, DeliveryConfig, DigestConfig


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def make_config(*, telegram: bool = True, markdown_to_repo: bool = False) -> Config:
    return Config(
        llm=LLMConfig(providers=[ProviderConfig(name="anthropic", model="claude-sonnet-4-20250514")]),
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
# to_markdownv2
# ---------------------------------------------------------------------------


def test_to_markdownv2_plain_text_unchanged() -> None:
    assert to_markdownv2("Hello world") == "Hello world"


def test_to_markdownv2_bold_converted() -> None:
    result = to_markdownv2("**bold text**")
    assert result == "*bold text*"


def test_to_markdownv2_heading_converted() -> None:
    result = to_markdownv2("## Section Title")
    assert result == "*Section Title*"


def test_to_markdownv2_heading_all_levels() -> None:
    for level in range(1, 7):
        hashes = "#" * level
        result = to_markdownv2(f"{hashes} Heading")
        assert result == "*Heading*", f"Failed for level {level}"


def test_to_markdownv2_special_chars_escaped() -> None:
    result = to_markdownv2("Price is 5.00!")
    assert r"\." in result
    assert r"\!" in result


def test_to_markdownv2_bold_with_special_chars_inside() -> None:
    # Special chars inside bold content should still be escaped
    result = to_markdownv2("**hello. world!**")
    assert result.startswith("*")
    assert result.endswith("*")
    assert r"\." in result


def test_to_markdownv2_multiline() -> None:
    text = "## Title\n\n**bold** line\n\nPlain text."
    result = to_markdownv2(text)
    assert "*Title*" in result
    assert "*bold*" in result
    assert r"\." in result


def test_to_markdownv2_inline_link_rendered() -> None:
    result = to_markdownv2("[OpenAI](https://openai.com)")
    # Should produce a MarkdownV2 inline link, not escaped brackets/parens
    assert result == "[OpenAI](https://openai.com)"


def test_to_markdownv2_inline_link_text_special_chars_escaped() -> None:
    # Dots and other special chars in link text must be escaped
    result = to_markdownv2("[Hello. World](https://example.com)")
    assert result == r"[Hello\. World](https://example.com)"


def test_to_markdownv2_inline_link_url_paren_escaped() -> None:
    # The regex only matches balanced parens in URLs. Unbalanced parens terminate the URL.
    # Input: [title](https://example.com/path)end)
    # The regex matches [title](https://example.com/path) and leaves "end)" as text.
    # MarkdownV2 requires ) to be escaped in text, so output should be:
    # [title](https://example.com/path)end\)
    result = to_markdownv2("[title](https://example.com/path)end)")
    assert result == "[title](https://example.com/path)end\\)"


def test_to_markdownv2_inline_link_url_with_balanced_parens() -> None:
    # Wikipedia-style URLs with balanced parentheses must not be truncated.
    result = to_markdownv2("[Foo](https://en.wikipedia.org/wiki/Foo_(bar))")
    # The ) inside the URL must be escaped for MarkdownV2; ( is left as-is.
    assert result == r"[Foo](https://en.wikipedia.org/wiki/Foo_(bar\))"


def test_to_markdownv2_inline_link_surrounding_text_escaped() -> None:
    # Special chars outside the link are still escaped
    result = to_markdownv2("See [Article](https://example.com) for details.")
    assert result == r"See [Article](https://example.com) for details\."


def test_to_markdownv2_multiple_inline_links() -> None:
    result = to_markdownv2("[A](https://a.com) and [B](https://b.com)")
    assert "[A](https://a.com)" in result
    assert "[B](https://b.com)" in result


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


def test_split_does_not_cut_between_backslash_and_escaped_char() -> None:
    # "A" * 4095 + r"\!" is 4097 chars. A naive split at 4096 would leave the
    # first chunk ending with a bare "\" — invalid MarkdownV2.
    text = "A" * 4095 + r"\!"
    chunks = split_message(text, max_len=4096)
    assert all(len(c) <= 4096 for c in chunks)
    assert not chunks[0].endswith("\\"), "first chunk must not end with a bare backslash"
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
    # Fallback strips MarkdownV2 escape backslashes — * is escaped as \* in md2,
    # so the plain fallback restores it back to *
    assert plain_payload["text"] == "Test *message*"


@pytest.mark.asyncio
@respx.mock
async def test_send_digest_raises_on_http_error() -> None:
    """HTTP 500 errors from Telegram propagate as exceptions after retries."""
    config = make_config(telegram=True)
    env = {"TELEGRAM_BOT_TOKEN": "tok3", "TELEGRAM_CHAT_ID": "7"}
    url = "https://api.telegram.org/bottok3/sendMessage"

    respx.post(url).mock(return_value=httpx.Response(500, json={"ok": False}))

    with patch("asyncio.sleep"), patch.dict("os.environ", env, clear=True):
        with pytest.raises(httpx.HTTPStatusError):
            await send_digest("Error test", config)


@pytest.mark.asyncio
@respx.mock
async def test_send_digest_partial_delivery_raises_partial_error() -> None:
    """When first chunk succeeds but second chunk fails, TelegramPartialDeliveryError is raised."""
    config = make_config(telegram=True)
    env = {"TELEGRAM_BOT_TOKEN": "tok4", "TELEGRAM_CHAT_ID": "8"}
    url = "https://api.telegram.org/bottok4/sendMessage"

    # Two paragraphs of 3000 chars each — forces two chunks
    text = "A" * 3000 + "\n\n" + "B" * 3000

    respx.post(url).mock(
        side_effect=[
            httpx.Response(200, json={"ok": True}),   # first chunk succeeds
            httpx.Response(500, json={"ok": False}),   # second chunk, attempt 1
            httpx.Response(500, json={"ok": False}),   # second chunk, attempt 2
            httpx.Response(500, json={"ok": False}),   # second chunk, attempt 3
        ]
    )

    with patch("asyncio.sleep"), patch.dict("os.environ", env, clear=True):
        with pytest.raises(TelegramPartialDeliveryError):
            await send_digest(text, config)

    # First chunk (1) + second chunk exhausts 3 retries (3) = 4 total
    assert respx.calls.call_count == 4


# ---------------------------------------------------------------------------
# send_article_cards
# ---------------------------------------------------------------------------


def _make_article(title: str, link: str, source: str, category: str, desc: str = "") -> Article:
    return Article(
        title=title,
        link=link,
        source=source,
        category=category,
        description=desc or f"Description of {title}",
        pub_date=None,
    )


@pytest.mark.asyncio
@respx.mock
async def test_send_article_cards_sends_one_message_per_article() -> None:
    """send_article_cards sends one message per article with 👍/👎 buttons."""

    config = make_config(telegram=True)
    env = {"TELEGRAM_BOT_TOKEN": "cardtoken", "TELEGRAM_CHAT_ID": "55"}
    url = "https://api.telegram.org/botcardtoken/sendMessage"

    respx.post(url).mock(return_value=httpx.Response(200, json={"ok": True}))

    articles_by_category = {
        "AI": [
            _make_article("Article One", "https://a.com/1", "Source A", "AI"),
            _make_article("Article Two", "https://a.com/2", "Source B", "AI"),
        ],
    }

    with patch("asyncio.sleep"), patch.dict("os.environ", env, clear=True):
        result = await send_article_cards(articles_by_category, config)

    assert respx.calls.call_count == 2
    assert len(result) == 2


@pytest.mark.asyncio
@respx.mock
async def test_send_article_cards_has_correct_callback_data() -> None:
    """Each article card has fb:a:g:HASH and fb:a:b:HASH buttons."""
    import json as _json
    from src.collector import article_hash

    config = make_config(telegram=True)
    env = {"TELEGRAM_BOT_TOKEN": "cbtoken", "TELEGRAM_CHAT_ID": "66"}
    url = "https://api.telegram.org/botcbtoken/sendMessage"

    respx.post(url).mock(return_value=httpx.Response(200, json={"ok": True}))

    article = _make_article("My Title", "https://example.com/article", "My Source", "Tech")
    expected_hash = article_hash("My Title", "https://example.com/article")[:8]

    with patch("asyncio.sleep"), patch.dict("os.environ", env, clear=True):
        result = await send_article_cards({"Tech": [article]}, config)

    payload = _json.loads(respx.calls[0].request.content)
    keyboard = payload["reply_markup"]["inline_keyboard"][0]
    assert keyboard[0]["callback_data"] == f"fb:a:g:{expected_hash}"
    assert keyboard[1]["callback_data"] == f"fb:a:b:{expected_hash}"
    assert len(keyboard[0]["callback_data"].encode()) <= 64
    assert result == {expected_hash: "My Source"}


@pytest.mark.asyncio
@respx.mock
async def test_send_article_cards_disable_notification() -> None:
    """Each article card is sent with disable_notification=True."""
    import json as _json

    config = make_config(telegram=True)
    env = {"TELEGRAM_BOT_TOKEN": "notiftoken", "TELEGRAM_CHAT_ID": "77"}
    url = "https://api.telegram.org/botnotiftoken/sendMessage"

    respx.post(url).mock(return_value=httpx.Response(200, json={"ok": True}))

    article = _make_article("Title", "https://x.com/1", "Src", "Cat")
    with patch("asyncio.sleep"), patch.dict("os.environ", env, clear=True):
        await send_article_cards({"Cat": [article]}, config)

    payload = _json.loads(respx.calls[0].request.content)
    assert payload.get("disable_notification") is True


@pytest.mark.asyncio
@respx.mock
async def test_send_article_cards_returns_source_map() -> None:
    """Returned dict maps article short hash → source name."""
    from src.collector import article_hash

    config = make_config(telegram=True)
    env = {"TELEGRAM_BOT_TOKEN": "maptoken", "TELEGRAM_CHAT_ID": "88"}
    url = "https://api.telegram.org/botmaptoken/sendMessage"

    respx.post(url).mock(return_value=httpx.Response(200, json={"ok": True}))

    articles = [
        _make_article("T1", "https://s1.com/a", "Source One", "X"),
        _make_article("T2", "https://s2.com/b", "Source Two", "Y"),
    ]
    with patch("asyncio.sleep"), patch.dict("os.environ", env, clear=True):
        result = await send_article_cards({"X": [articles[0]], "Y": [articles[1]]}, config)

    assert result[article_hash("T1", "https://s1.com/a")[:8]] == "Source One"
    assert result[article_hash("T2", "https://s2.com/b")[:8]] == "Source Two"


@pytest.mark.asyncio
@respx.mock
async def test_send_article_cards_continues_on_failure() -> None:
    """If one article send fails, the rest are still attempted."""
    from src.collector import article_hash

    config = make_config(telegram=True)
    env = {"TELEGRAM_BOT_TOKEN": "failtoken", "TELEGRAM_CHAT_ID": "99"}
    url = "https://api.telegram.org/botfailtoken/sendMessage"

    respx.post(url).mock(
        side_effect=[
            httpx.Response(500, json={"ok": False}),  # first article fails (3 retries)
            httpx.Response(500, json={"ok": False}),
            httpx.Response(500, json={"ok": False}),
            httpx.Response(200, json={"ok": True}),  # second article succeeds
        ]
    )

    articles = [
        _make_article("Fail", "https://f.com/1", "Src A", "Z"),
        _make_article("OK", "https://f.com/2", "Src B", "Z"),
    ]
    with patch("asyncio.sleep"), patch.dict("os.environ", env, clear=True):
        result = await send_article_cards({"Z": articles}, config)

    # Both articles attempted; second one recorded in map
    ok_hash = article_hash("OK", "https://f.com/2")[:8]
    assert ok_hash in result
    assert result[ok_hash] == "Src B"


@pytest.mark.asyncio
async def test_send_article_cards_disabled_returns_empty() -> None:
    """No HTTP calls when telegram delivery is disabled."""
    config = make_config(telegram=False)
    article = _make_article("T", "https://x.com", "S", "C")
    result = await send_article_cards({"C": [article]}, config)
    assert result == {}


# ---------------------------------------------------------------------------
# _send_chunk retry behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_send_chunk_retries_503_then_ok() -> None:
    """_send_chunk retries on 503 and succeeds on the second attempt."""
    url = "https://api.telegram.org/botretrytoken/sendMessage"
    respx.post(url).mock(
        side_effect=[
            httpx.Response(503, json={"ok": False}),
            httpx.Response(200, json={"ok": True}),
        ]
    )

    async with httpx.AsyncClient() as client:
        with patch("asyncio.sleep") as mock_sleep:
            await _send_chunk(client, url, "42", "hello")

    assert respx.calls.call_count == 2
    mock_sleep.assert_called_once_with(1)  # 2**0 = 1 on first attempt


@pytest.mark.asyncio
@respx.mock
async def test_send_chunk_respects_retry_after() -> None:
    """_send_chunk sleeps for Retry-After seconds on 429."""
    url = "https://api.telegram.org/botretryafter/sendMessage"
    respx.post(url).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "5"}, json={"ok": False}),
            httpx.Response(200, json={"ok": True}),
        ]
    )

    async with httpx.AsyncClient() as client:
        with patch("asyncio.sleep") as mock_sleep:
            await _send_chunk(client, url, "42", "hello")

    assert respx.calls.call_count == 2
    mock_sleep.assert_called_once_with(5.0)


@pytest.mark.asyncio
@respx.mock
async def test_send_chunk_fails_after_3_retries() -> None:
    """_send_chunk raises HTTPStatusError after all 3 attempts are exhausted."""
    url = "https://api.telegram.org/botfail3/sendMessage"
    respx.post(url).mock(return_value=httpx.Response(503, json={"ok": False}))

    async with httpx.AsyncClient() as client:
        with patch("asyncio.sleep"):
            with pytest.raises(httpx.HTTPStatusError):
                await _send_chunk(client, url, "42", "hello")

    assert respx.calls.call_count == 3


@pytest.mark.asyncio
@respx.mock
async def test_send_chunk_no_retry_on_400() -> None:
    """HTTP 400 triggers plain-text fallback, not transient retry."""
    url = "https://api.telegram.org/bot400/sendMessage"
    respx.post(url).mock(
        side_effect=[
            httpx.Response(400, json={"ok": False, "description": "Bad Request"}),
            httpx.Response(200, json={"ok": True}),
        ]
    )

    async with httpx.AsyncClient() as client:
        with patch("asyncio.sleep") as mock_sleep:
            await _send_chunk(client, url, "42", r"hello \*world\*")

    # Exactly 2 calls: MarkdownV2 attempt + plain-text fallback, no retry loop
    assert respx.calls.call_count == 2
    mock_sleep.assert_not_called()


# ---------------------------------------------------------------------------
# Fix #6: conservative split limit + hard clamp tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_send_digest_uses_conservative_split_limit() -> None:
    """send_digest splits using _SPLIT_LIMIT (3800), not the raw 4096 limit."""
    config = make_config()
    url = "https://api.telegram.org/botTOKEN/sendMessage"
    respx.post(url).mock(return_value=httpx.Response(200, json={"ok": True}))

    # Build a text that would fit in one 4096-char chunk but exceeds 3800 after conversion.
    # Use simple ASCII so to_markdownv2 doesn't significantly expand it, but make it
    # long enough that split_message(max_len=3800) splits it into 2 parts.
    paragraph_a = "A" * 1900 + " word"
    paragraph_b = "B" * 1900 + " word"
    text = paragraph_a + "\n\n" + paragraph_b  # total ~3810 chars — over 3800, under 4096

    with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "TOKEN", "TELEGRAM_CHAT_ID": "42"}):
        sent = await send_digest(text, config)

    assert sent is True
    # Must have been split into 2 chunks (conservative limit kicked in)
    assert respx.calls.call_count == 2


@pytest.mark.asyncio
@respx.mock
async def test_send_digest_hard_clamps_oversized_chunk() -> None:
    """A chunk that somehow exceeds 4096 after splitting is truncated with a warning."""
    import src.telegram as tg_module

    config = make_config()
    url = "https://api.telegram.org/botTOKEN/sendMessage"
    respx.post(url).mock(return_value=httpx.Response(200, json={"ok": True}))

    # Patch split_message to return a single chunk longer than _MAX_MESSAGE_LEN
    oversized = "X" * 5000
    with (
        patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "TOKEN", "TELEGRAM_CHAT_ID": "42"}),
        patch.object(tg_module, "split_message", return_value=[oversized]),
        patch.object(tg_module.logger, "warning") as mock_warn,
    ):
        sent = await send_digest("any text", config)

    assert sent is True
    # The oversized chunk must have been truncated to ≤ 4096 chars before sending
    sent_body = respx.calls[0].request.content
    import json

    payload = json.loads(sent_body)
    assert len(payload["text"]) <= 4096
    # Warning must have been logged
    mock_warn.assert_called_once()
    assert "4096" in mock_warn.call_args[0][0] or "truncated" in mock_warn.call_args[0][0].lower()
