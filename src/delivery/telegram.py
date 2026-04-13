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
    max_cards: int = 0,
) -> dict[str, str]:
    """Send Telegram cards for top articles with voting buttons.

    If *max_cards* > 0, at most that many cards are sent (highest-priority
    articles first).  The full article_source_map is still returned so
    feedback attribution works for articles covered by the digest text.

    Returns mapping of 8-char article hash -> source name.
    """
    from src.radar.collector import article_hash

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        logger.warning("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set, skipping cards")
        return {}

    api_url = _API_BASE.format(token=token)
    article_source_map: dict[str, str] = {}

    # Build full map first (needed for feedback attribution even if we skip some cards)
    all_articles: list[tuple[str, Any]] = []
    for category, articles in articles_by_category.items():
        for art in articles:
            hash8 = article_hash(art.title, art.link)[:8]
            article_source_map[hash8] = art.source
            all_articles.append((category, art))

    cards_to_send = all_articles
    if max_cards > 0:
        cards_to_send = all_articles[:max_cards]

    sent_count = 0
    async with httpx.AsyncClient() as client:
        for category, art in cards_to_send:
            hash8 = article_hash(art.title, art.link)[:8]

            title_esc = escape_markdownv2(art.title)
            url_esc = art.link.replace("\\", "\\\\").replace(")", "\\)")
            source_esc = escape_markdownv2(art.source)
            cat_esc = escape_markdownv2(category)

            desc = art.description[:200]
            if len(art.description) > 200:
                desc += "\u2026"
            desc_esc = escape_markdownv2(desc)

            text = (
                f"[{title_esc}]({url_esc})\n"
                f"*{source_esc}* \u00b7 _{cat_esc}_\n\n"
                f"{desc_esc}"
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
                    disable_notification=True,
                    reply_markup=keyboard,
                )
                sent_count += 1
            except Exception as exc:
                logger.warning(
                    "Failed to send card for '%s': %s", art.title[:50], exc
                )

            await asyncio.sleep(0.5)

    logger.info("Sent %d/%d article cards to Telegram", sent_count, len(all_articles))
    return article_source_map


async def send_counter_signals(ranked_signals: list[Any], config: Any) -> bool:
    """Send counter-signals as a separate Telegram message."""
    if not ranked_signals:
        return False

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return False

    lines = ["## \u26a0\ufe0f Counter\\-Signals\n"]
    for r in ranked_signals:
        title = escape_markdownv2(r.signal.title)
        url = r.signal.url.replace("\\", "\\\\").replace(")", "\\)")
        reasoning = escape_markdownv2(r.reasoning)
        narrative = escape_markdownv2(r.narrative_claim[:80])
        lines.append(
            f"*\\[{r.score}/10\\]* [{title}]({url})\n"
            f"_{reasoning}_\n"
            f"Narrative: {narrative}\n"
        )

    text = "\n".join(lines)
    md2 = to_markdownv2(text)
    chunks = split_message(md2)

    api_url = _API_BASE.format(token=token)
    async with httpx.AsyncClient() as client:
        for chunk in chunks:
            if len(chunk) > _MAX_MESSAGE_LEN:
                chunk = chunk[: _MAX_MESSAGE_LEN - 1] + "\u2026"
            await _send_chunk(client, api_url, chat_id, chunk)

    logger.info("Counter-signals sent to Telegram")
    return True
