"""Telegram Bot API delivery for the daily digest."""

from __future__ import annotations

import asyncio
import logging
import os
import re

import httpx

from src.collector import Article, article_hash
from src.config import Config

logger = logging.getLogger(__name__)


class TelegramPartialDeliveryError(Exception):
    """Raised when at least one chunk was sent but subsequent chunks failed.

    The caller should persist the dedup cache to avoid re-sending the
    already-delivered chunks, but must NOT treat this as a successful delivery.
    """


# Telegram message size limit in characters
_MAX_MESSAGE_LEN = 4096

# Conservative split limit: leaves ~7% headroom for any post-split expansion
# (e.g. hard-split backoff across backslash runs, future to_markdownv2 changes).
_SPLIT_LIMIT = 3800

# MarkdownV2 special characters that must be escaped (outside entities)
_MARKDOWNV2_SPECIAL = r"_*[]()~`>#+-=|{}.!"

# Private-use-area sentinels used by to_markdownv2 to protect formatting
# markers from being escaped by escape_markdownv2.
_BOLD_OPEN = "\ue000"
_BOLD_CLOSE = "\ue001"
# Sentinels that delimit link placeholders (e.g. \ue002N\ue003 where N is a digit string)
_LINK_PH_OPEN = "\ue002"
_LINK_PH_CLOSE = "\ue003"


def escape_markdownv2(text: str) -> str:
    """Escape special characters for Telegram MarkdownV2 parse mode.

    All special characters outside of explicit formatting entities must be
    escaped with a preceding backslash.
    """
    # Characters to escape per Telegram docs
    chars = r"\_*[]()~`>#+-=|{}.!"
    pattern = "([" + re.escape(chars) + "])"
    return re.sub(pattern, r"\\\1", text)


def to_markdownv2(text: str) -> str:
    """Convert standard Markdown text to Telegram MarkdownV2 format.

    Converts the most common LLM-generated Markdown patterns to their
    MarkdownV2 equivalents so that formatting renders in Telegram:
      - ## Heading     →  *Heading* (bold, MarkdownV2 has no native headings)
      - **bold**       →  *bold*
      - [text](url)    →  [text](url)  (inline link, properly escaped)

    All remaining special characters are escaped so the message is accepted
    by the Telegram API without accidental parse errors.
    """
    # First pass: extract inline links and replace with PUA-char placeholders so
    # that [ ] ( ) and URL characters are not mangled by escape_markdownv2.
    link_map: dict[str, str] = {}

    def _protect_link(m: re.Match[str]) -> str:
        link_text = m.group(1)
        url = m.group(2)
        # Link text is regular MarkdownV2 inline content — escape all special chars.
        escaped_text = escape_markdownv2(link_text)
        # Un-escape Markdown escape sequences in the URL (e.g. \) → )) before
        # re-encoding for MarkdownV2 where only \ and ) need escaping.
        url_unescaped = re.sub(r"\\(.)", r"\1", url)
        url_v2 = url_unescaped.replace("\\", "\\\\").replace(")", "\\)")
        placeholder = f"{_LINK_PH_OPEN}{len(link_map)}{_LINK_PH_CLOSE}"
        link_map[placeholder] = f"[{escaped_text}]({url_v2})"
        return placeholder

    # URL group allows: plain chars, \X escape sequences, and exactly one level of (…)
    # nesting.  This correctly handles Wikipedia-style URLs like /wiki/Foo_(bar) and
    # \) escapes.  Deeply-nested parens (e.g. a_(b_(c))) are not supported.
    text = re.sub(
        r"\[([^\]\n]+)\]\(((?:[^()\s\\]|\\.|(?:\([^)\s]*\)))*)\)",
        _protect_link,
        text,
    )

    lines: list[str] = []
    for line in text.split("\n"):
        # ## Heading … → bold heading
        heading_match = re.match(r"^#{1,6}\s+(.+)$", line)
        if heading_match:
            lines.append(f"{_BOLD_OPEN}{heading_match.group(1)}{_BOLD_CLOSE}")
            continue
        # **bold** → placeholder
        line = re.sub(
            r"\*\*(.+?)\*\*",
            lambda m: f"{_BOLD_OPEN}{m.group(1)}{_BOLD_CLOSE}",
            line,
        )
        lines.append(line)

    # Escape all MarkdownV2 special chars; private-use sentinels pass through
    # unescaped because they are not in the escape set.
    escaped = escape_markdownv2("\n".join(lines))

    # Restore bold sentinels as MarkdownV2 bold syntax.
    result = escaped.replace(_BOLD_OPEN, "*").replace(_BOLD_CLOSE, "*")

    # Restore link placeholders.  PUA chars are not in the escape set so they
    # survive escape_markdownv2 unchanged.
    for placeholder, link in link_map.items():
        result = result.replace(placeholder, link)

    return result


def split_message(text: str, max_len: int = _MAX_MESSAGE_LEN) -> list[str]:
    """Split text into chunks of at most max_len chars, splitting at paragraph boundaries.

    Paragraphs are separated by double newlines. If a single paragraph exceeds
    max_len, it is split at the nearest newline boundary, or hard-split as a
    last resort.
    """
    if len(text) <= max_len:
        return [text]

    chunks: list[str] = []
    paragraphs = text.split("\n\n")
    current = ""

    for para in paragraphs:
        # +2 for the "\n\n" separator we'd add
        candidate = current + ("\n\n" if current else "") + para
        if len(candidate) <= max_len:
            current = candidate
        else:
            if current:
                chunks.append(current)
            # If a single paragraph is too long, split at newlines
            if len(para) > max_len:
                lines = para.split("\n")
                current = ""
                for line in lines:
                    candidate_line = current + ("\n" if current else "") + line
                    if len(candidate_line) <= max_len:
                        current = candidate_line
                    else:
                        if current:
                            chunks.append(current)
                        # Hard split if a single line exceeds limit.
                        # Avoid cutting inside a backslash escape sequence
                        # (e.g. \! or \\) which would produce invalid MarkdownV2.
                        # Back up through the entire run of trailing backslashes
                        # so the chunk never ends on a lone backslash.
                        while len(line) > max_len:
                            split_at = max_len
                            while split_at > 0 and line[split_at - 1] == "\\":
                                split_at -= 1
                            # Guard against degenerate max_len=1 with a leading backslash
                            if split_at == 0:
                                split_at = max_len
                            chunks.append(line[:split_at])
                            line = line[split_at:]
                        current = line
            else:
                current = para

    if current:
        chunks.append(current)

    return chunks if chunks else [text]


async def send_digest(text: str, config: Config) -> bool:
    """Send the digest text to Telegram.

    Returns True if the message was actually sent, False if delivery was
    disabled or credentials were missing.
    Long messages are split at paragraph boundaries and sent sequentially with
    a 1-second delay between parts.
    """
    if not config.delivery.telegram:
        logger.debug("Telegram delivery is disabled in config, skipping.")
        return False

    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if not bot_token or not chat_id:
        logger.warning(
            "Telegram delivery is enabled but credentials are missing. "
            "Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID environment variables. "
            "See https://core.telegram.org/bots#how-do-i-create-a-bot for setup instructions."
        )
        return False

    # Convert to MarkdownV2 first (escaping + link restoration), then split
    # using a conservative limit to leave headroom for edge cases in the
    # hard-split backoff logic. Apply a hard clamp as a final safety net.
    md2_text = to_markdownv2(text)
    chunks = split_message(md2_text, max_len=_SPLIT_LIMIT)
    # Hard clamp: if any chunk still exceeds 4096 (shouldn't happen in practice
    # but guards against future changes), truncate and log a warning.
    safe_chunks: list[str] = []
    for chunk in chunks:
        if len(chunk) > _MAX_MESSAGE_LEN:
            chunk = chunk[: _MAX_MESSAGE_LEN - 1] + "…"
            logger.warning(
                "A digest chunk exceeded Telegram's 4096-char limit even after "
                "conservative splitting — truncated. Check to_markdownv2 output size."
            )
        safe_chunks.append(chunk)
    chunks = safe_chunks
    api_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"

    any_sent = False
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            for i, chunk in enumerate(chunks):
                if i > 0:
                    await asyncio.sleep(1)
                await _send_chunk(client, api_url, chat_id, chunk)
                any_sent = True
    except Exception as exc:
        if any_sent:
            # At least one chunk was delivered but a later chunk failed.
            # Raise TelegramPartialDeliveryError so the caller can:
            #   1. persist the dedup cache (to avoid re-sending already-delivered chunks)
            #   2. treat the delivery as failed (non-zero exit, failure notification)
            raise TelegramPartialDeliveryError(
                f"Partial Telegram delivery: one or more chunks failed after at least "
                f"one chunk was already sent. Cause: {exc}"
            ) from exc
        raise

    return True


async def send_article_cards(
    articles_by_category: dict[str, list[Article]],
    config: Config,
) -> dict[str, str]:
    """Send per-article messages with 👍/👎 buttons after the digest.

    Each article is sent as a separate silent Telegram message containing the
    title (as a link), source, category, and a short description snippet.
    Buttons carry callback_data ``fb:a:g:<hash>`` / ``fb:a:b:<hash>`` where
    ``<hash>`` is the first 8 hex chars of the article's MD5 identity hash.

    Returns a mapping of ``{article_hash_short: source_name}`` so the caller
    can persist it in ``FeedbackStore.article_source_map`` for later attribution.
    Non-critical: logs a warning and continues on any per-message failure.
    """
    if not config.delivery.telegram:
        return {}
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not bot_token or not chat_id:
        return {}

    api_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    article_source_map: dict[str, str] = {}

    async with httpx.AsyncClient(timeout=30.0) as client:
        first = True
        for articles in articles_by_category.values():
            for article in articles:
                full_hash = article_hash(article.title, article.link)
                short_hash = full_hash[:8]
                article_source_map[short_hash] = article.source

                # Build card text: title as link, then source / category / snippet
                title_escaped = escape_markdownv2(article.title)
                url_v2 = article.link.replace("\\", "\\\\").replace(")", "\\)")
                title_link = f"[{title_escaped}]({url_v2})"
                source_escaped = escape_markdownv2(article.source)
                category_escaped = escape_markdownv2(article.category)
                snippet = article.description[:200].rstrip()
                if len(article.description) > 200:
                    snippet += "…"
                snippet_escaped = escape_markdownv2(snippet)
                card_text = (
                    f"{title_link}\n"
                    f"*{source_escaped}* · _{category_escaped}_\n"
                    f"{snippet_escaped}"
                )

                keyboard: dict[str, list[list[dict[str, str]]]] = {
                    "inline_keyboard": [
                        [
                            {"text": "\U0001f44d", "callback_data": f"fb:a:g:{short_hash}"},
                            {"text": "\U0001f44e", "callback_data": f"fb:a:b:{short_hash}"},
                        ]
                    ]
                }

                if not first:
                    await asyncio.sleep(0.5)
                first = False

                try:
                    await _send_chunk(
                        client,
                        api_url,
                        chat_id,
                        card_text,
                        reply_markup=keyboard,
                        disable_notification=True,
                    )
                except Exception as exc:
                    logger.warning(
                        "Failed to send article card for '%s': %s", article.title, exc
                    )

    logger.info("Sent %d article cards.", len(article_source_map))
    return article_source_map


_RETRY_STATUSES = {429, 500, 502, 503, 504}


async def _send_chunk(
    client: httpx.AsyncClient,
    api_url: str,
    chat_id: str,
    md2_text: str,
    *,
    reply_markup: dict[str, list[list[dict[str, str]]]] | None = None,
    disable_notification: bool = False,
) -> None:
    """Send a single pre-converted MarkdownV2 chunk to Telegram.

    The caller is responsible for passing text that has already been converted
    via to_markdownv2() and split to fit within Telegram's 4096-char limit.
    On HTTP 400 (parse error), retries as plain text by stripping MarkdownV2
    escape backslashes from the same chunk.
    Transient errors (429/5xx/timeout) are retried up to 3 times with exponential
    backoff, honouring the Retry-After header when present.
    """
    payload: dict[str, object] = {
        "chat_id": chat_id,
        "text": md2_text,
        "parse_mode": "MarkdownV2",
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    if disable_notification:
        payload["disable_notification"] = True

    response: httpx.Response | None = None
    for attempt in range(3):
        try:
            response = await client.post(api_url, json=payload)
        except httpx.TimeoutException:
            if attempt < 2:
                logger.warning(
                    "Telegram request timed out, retry %d/3 in %ds", attempt + 1, 2 ** attempt
                )
                await asyncio.sleep(2 ** attempt)
                continue
            logger.error("Telegram request timed out after 3 attempts")
            raise

        # HTTP 400 is a permanent parse error — fall back to plain text, do not retry.
        if response.status_code == 400:
            logger.warning(
                "Telegram rejected message with MarkdownV2 (HTTP 400), retrying as plain text. "
                "Response: %s",
                response.text,
            )
            plain_text = re.sub(r"\\(.)", r"\1", md2_text)
            payload_plain: dict[str, object] = {"chat_id": chat_id, "text": plain_text}
            if reply_markup is not None:
                payload_plain["reply_markup"] = reply_markup
            response = await client.post(api_url, json=payload_plain)

        # Transient server/rate-limit errors — back off and retry.
        if response.status_code in _RETRY_STATUSES:
            retry_after = response.headers.get("Retry-After")
            sleep = float(retry_after) if retry_after else 2 ** attempt
            logger.warning(
                "Telegram HTTP %d, retry %d/3 in %.0fs",
                response.status_code,
                attempt + 1,
                sleep,
            )
            if attempt < 2:
                await asyncio.sleep(sleep)
            continue

        # Success or permanent non-retryable error.
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.error(
                "Failed to send Telegram message (HTTP %d): %s",
                exc.response.status_code,
                exc.response.text,
            )
            raise
        logger.info("Telegram message chunk sent successfully (%d chars).", len(md2_text))
        return

    # All 3 attempts exhausted — raise on the last response.
    if response is not None:
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.error(
                "Failed to send Telegram message (HTTP %d) after 3 attempts: %s",
                exc.response.status_code,
                exc.response.text,
            )
            raise
