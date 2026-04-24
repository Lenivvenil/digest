"""Telegram Bot API delivery for radar digest and counter-signals."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_API_BASE = "https://api.telegram.org/bot{token}/sendMessage"
_SPLIT_LIMIT = 3800
_MAX_MESSAGE_LEN = 4096
_MAX_RETRIES = 3

# Private Use Area sentinels for safe markdown conversion
_BOLD_OPEN = "\ue000"
_BOLD_CLOSE = "\ue001"
_LINK_PH_OPEN = "\ue002"
_LINK_PH_CLOSE = "\ue003"


def escape_markdownv2(text: str) -> str:
    """Escape special characters for Telegram MarkdownV2."""
    return re.sub(r"([\_*\[\]()~`>#\+\-=|{}.!])", r"\\\1", text)


def to_markdownv2(text: str) -> str:
    """Convert standard Markdown to Telegram MarkdownV2 format."""
    # 1. Protect links: [text](url) → sentinel placeholders
    links: list[tuple[str, str]] = []

    def _protect_link(m: re.Match[str]) -> str:
        link_text = escape_markdownv2(m.group(1))
        url = m.group(2).replace("\\", "\\\\").replace(")", "\\)")
        links.append((link_text, url))
        return f"{_LINK_PH_OPEN}{len(links) - 1}{_LINK_PH_CLOSE}"

    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", _protect_link, text)

    # 2. Headings: ## Heading → *Heading* (bold)
    text = re.sub(
        r"^#{1,6}\s+(.+)$",
        lambda m: f"{_BOLD_OPEN}{m.group(1)}{_BOLD_CLOSE}",
        text,
        flags=re.MULTILINE,
    )

    # 3. Bold: **text** → sentinel bold
    text = re.sub(
        r"\*\*(.+?)\*\*",
        lambda m: f"{_BOLD_OPEN}{m.group(1)}{_BOLD_CLOSE}",
        text,
    )

    # 4. Escape remaining special chars
    text = escape_markdownv2(text)

    # 5. Restore bold sentinels → MarkdownV2 bold
    text = text.replace(escape_markdownv2(_BOLD_OPEN), "*")
    text = text.replace(escape_markdownv2(_BOLD_CLOSE), "*")
    text = text.replace(_BOLD_OPEN, "*")
    text = text.replace(_BOLD_CLOSE, "*")

    # 6. Restore link sentinels
    def _restore_link(m: re.Match[str]) -> str:
        idx = int(m.group(1))
        lt, url = links[idx]
        return f"[{lt}]({url})"

    # Clean escaped sentinels first
    text = text.replace(escape_markdownv2(_LINK_PH_OPEN), _LINK_PH_OPEN)
    text = text.replace(escape_markdownv2(_LINK_PH_CLOSE), _LINK_PH_CLOSE)
    text = re.sub(
        f"{re.escape(_LINK_PH_OPEN)}(\\d+){re.escape(_LINK_PH_CLOSE)}",
        _restore_link,
        text,
    )

    return text


def split_message(text: str, max_len: int = _SPLIT_LIMIT) -> list[str]:
    """Split text into chunks at paragraph boundaries."""
    if len(text) <= max_len:
        return [text]

    chunks: list[str] = []
    current = ""

    for paragraph in text.split("\n\n"):
        candidate = f"{current}\n\n{paragraph}" if current else paragraph
        if len(candidate) <= max_len:
            current = candidate
        else:
            if current:
                chunks.append(current)
            # If single paragraph too long, split by newlines
            if len(paragraph) > max_len:
                for line in paragraph.split("\n"):
                    if current and len(current) + len(line) + 1 <= max_len:
                        current = f"{current}\n{line}"
                    else:
                        if current:
                            chunks.append(current)
                        # Hard split if single line too long
                        while len(line) > max_len:
                            split_at = max_len
                            while split_at > 0 and line[split_at - 1] == "\\":
                                split_at -= 1
                            if split_at == 0:
                                split_at = max_len
                            chunks.append(line[:split_at])
                            line = line[split_at:]
                        current = line
            else:
                current = paragraph

    if current:
        chunks.append(current)

    return chunks or [text]


async def _send_chunk(
    client: httpx.AsyncClient,
    api_url: str,
    chat_id: str,
    md2_text: str,
    disable_notification: bool = False,
    reply_markup: dict[str, Any] | None = None,
) -> None:
    """Send a single MarkdownV2 chunk to Telegram with retry logic."""
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": md2_text,
        "parse_mode": "MarkdownV2",
        "disable_notification": disable_notification,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup

    for attempt in range(_MAX_RETRIES):
        try:
            resp = await client.post(api_url, json=payload, timeout=30.0)
            if resp.status_code == 400:
                # Fallback: strip escapes and send as plain text
                logger.warning("MarkdownV2 rejected, falling back to plain text")
                plain = re.sub(r"\\(.)", r"\1", md2_text)
                payload["text"] = plain
                payload.pop("parse_mode", None)
                resp = await client.post(api_url, json=payload, timeout=30.0)
            resp.raise_for_status()
            return
        except (httpx.HTTPStatusError, httpx.TimeoutException) as exc:
            if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429:
                retry_after = int(exc.response.headers.get("Retry-After", 2))
                await asyncio.sleep(retry_after)
            elif attempt < _MAX_RETRIES - 1:
                await asyncio.sleep(2 ** attempt)
            else:
                raise


async def send_radar(text: str, config: Any) -> bool:
    """Send radar digest to Telegram."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        logger.warning("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set, skipping")
        return False

    api_url = _API_BASE.format(token=token)
    md2 = to_markdownv2(text)
    chunks = split_message(md2)

    async with httpx.AsyncClient() as client:
        for i, chunk in enumerate(chunks):
            if len(chunk) > _MAX_MESSAGE_LEN:
                chunk = chunk[: _MAX_MESSAGE_LEN - 1] + "\u2026"
                logger.warning("Chunk %d truncated to %d chars", i, _MAX_MESSAGE_LEN)
            await _send_chunk(client, api_url, chat_id, chunk)
            if i < len(chunks) - 1:
                await asyncio.sleep(1)

    logger.info("Radar digest sent to Telegram (%d chunks)", len(chunks))
    return True


async def send_article_cards(
    articles_by_category: dict[str, list[Any]],
    config: Any,
    *,
    top_articles: list[Any] | None = None,
) -> dict[str, str]:
    """Send per-article Telegram posts with LLM summaries and voting buttons.

    If *top_articles* (list of ``ArticleSummary``) is provided, those are
    sent as cards with their LLM-generated summaries.  Otherwise falls back
    to raw articles with truncated descriptions.

    The full ``article_source_map`` (hash → source) is always built from
    *articles_by_category* so feedback attribution works for every article.

    Returns mapping of 8-char article hash -> source name.
    """
    from digest.radar.collector import article_hash

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        logger.warning("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set, skipping cards")
        return {}

    api_url = _API_BASE.format(token=token)
    article_source_map: dict[str, str] = {}

    # Build full attribution map from all articles
    for articles in articles_by_category.values():
        for art in articles:
            hash8 = article_hash(art.title, art.link)[:8]
            article_source_map[hash8] = art.source

    # Determine what to send
    cards: list[tuple[str, str, str, str, str]] = []  # (title, link, source, cat, desc)
    if top_articles:
        for a in top_articles:
            cards.append((a.title, a.link, a.source, a.category, a.summary))
    else:
        # Fallback: raw articles with truncated descriptions
        for category, articles in articles_by_category.items():
            for art in articles:
                desc = art.description[:200]
                if len(art.description) > 200:
                    desc += "\u2026"
                cards.append((art.title, art.link, art.source, category, desc))

    sent_count = 0
    async with httpx.AsyncClient() as client:
        for title, link, source, category, summary in cards:
            hash8 = article_hash(title, link)[:8]

            title_esc = escape_markdownv2(title)
            url_esc = link.replace("\\", "\\\\").replace(")", "\\)")
            source_esc = escape_markdownv2(source)
            cat_esc = escape_markdownv2(category)
            summary_esc = escape_markdownv2(summary)

            text = (
                f"[{title_esc}]({url_esc})\n\n"
                f"{summary_esc}\n\n"
                f"*{source_esc}* \u00b7 _{cat_esc}_"
            )

            keyboard: dict[str, Any] = {
                "inline_keyboard": [
                    [
                        {"text": "\U0001f44d", "callback_data": f"fb:a:g:{hash8}"},
                        {"text": "\U0001f44e", "callback_data": f"fb:a:b:{hash8}"},
                    ]
                ]
            }

            try:
                await _send_chunk(
                    client,
                    api_url,
                    chat_id,
                    text,
                    reply_markup=keyboard,
                )
                sent_count += 1
            except Exception as exc:
                logger.warning(
                    "Failed to send card for '%s': %s", title[:50], exc,
                )

            await asyncio.sleep(0.5)

    logger.info("Sent %d article cards to Telegram", sent_count)
    return article_source_map


async def send_counter_signals(
    ranked_signals: list[Any],
    config: Any,
    irritator_status: str = "",
) -> bool:
    """Send counter-signals as a separate Telegram message.

    If *ranked_signals* is empty but *irritator_status* is provided, a short
    status message is sent so the user always sees that the pipeline ran.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return False

    api_url = _API_BASE.format(token=token)

    if not ranked_signals:
        if irritator_status:
            msg = f"\U0001f4a2 \u0420\u0430\u0437\u0434\u0440\u0430\u0436\u0430\u0442\u043e\u0440: {irritator_status}"
            status_text = escape_markdownv2(msg)
            async with httpx.AsyncClient() as client:
                await _send_chunk(client, api_url, chat_id, status_text, disable_notification=True)
            logger.info("Irritator status sent to Telegram: %s", irritator_status)
        return False

    header = (
        "\U0001f4a2\U0001f525 "
        "*\u0420\u0410\u0417\u0414\u0420\u0410\u0416\u0410\u0422\u041e\u0420* "
        "\U0001f525\U0001f4a2"
    )
    lines = [f"{header}\n"]
    for r in ranked_signals:
        title = escape_markdownv2(r.signal.title)
        url = r.signal.url.replace("\\", "\\\\").replace(")", "\\)")
        reasoning = escape_markdownv2(r.reasoning)
        narrative = escape_markdownv2(r.narrative_claim[:80])
        lines.append(
            f"\u26a1 *\\[{r.score}/10\\]* [{title}]({url})\n"
            f"\u2192 \u041e\u0441\u043f\u0430\u0440\u0438\u0432\u0430\u0435\u0442: \u00ab{narrative}\u00bb\n"
            f"_{reasoning}_\n"
        )

    text = "\n".join(lines)
    md2 = to_markdownv2(text)
    chunks = split_message(md2)

    async with httpx.AsyncClient() as client:
        for chunk in chunks:
            if len(chunk) > _MAX_MESSAGE_LEN:
                chunk = chunk[: _MAX_MESSAGE_LEN - 1] + "\u2026"
            await _send_chunk(client, api_url, chat_id, chunk)

    logger.info("Counter-signals sent to Telegram (%d signals)", len(ranked_signals))
    return True
