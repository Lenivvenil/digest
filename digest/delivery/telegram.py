"""Telegram Bot API delivery for radar digest and counter-signals."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from digest.irritator import IrritatorStatus

import httpx

from digest.delivery.supplement import signal_text, split_supplement

logger = logging.getLogger(__name__)

_API_BASE = "https://api.telegram.org/bot{token}/sendMessage"
# Static presentation labels follow canonical generation language, not translation targets.
_LABELS = {
    "en": {
        "feedback": "Votes are processed on digest runs; private owner chat only",
        "irritator": "Irritator",
    },
    "ru": {
        "feedback": "Оценки обрабатываются при запусках дайджеста; только личный чат владельца",
        "irritator": "Раздражатор",
    },
}
_SPLIT_LIMIT = 3800
_MAX_MESSAGE_LEN = 4096
_MAX_RETRIES = 3
# Supplement-only total dispatch cap; primary sender and retry policy stay unchanged.
_SUPPLEMENT_DISPATCH_SECONDS = 30.0 * _MAX_RETRIES

# Private Use Area sentinels for safe markdown conversion
_BOLD_OPEN = "\ue000"
_BOLD_CLOSE = "\ue001"
_LINK_PH_OPEN = "\ue002"
_LINK_PH_CLOSE = "\ue003"


@dataclass
class ArticleDeliveryResult:
    """Delivery counts and attribution for cards Telegram actually accepted.

    ``article_source_map`` uses the 8-character callback hashes, while
    ``delivered_hashes`` contains full hashes for the collector's dedup cache.
    Skipped delivery (including missing credentials) has zero attempts.
    """

    attempted: int = 0
    sent: int = 0
    failed: int = 0
    article_source_map: dict[str, str] = field(default_factory=dict)
    delivered_hashes: set[str] = field(default_factory=set)


def _labels(config: Any) -> dict[str, str]:
    language = getattr(getattr(config, "radar", None), "language", "en")
    return _LABELS.get(language, _LABELS["en"])


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
                current = ""
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
            if attempt == _MAX_RETRIES - 1:
                raise
            if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429:
                retry_after = int(exc.response.headers.get("Retry-After", 2))
                await asyncio.sleep(retry_after)
            else:
                await asyncio.sleep(2 ** attempt)


async def send_article_cards(
    articles_by_category: dict[str, list[Any]],
    config: Any,
    *,
    top_articles: list[Any] | None = None,
) -> ArticleDeliveryResult:
    """Send per-article Telegram posts with LLM summaries and voting buttons.

    Only sends cards when *top_articles* (list of ``ArticleSummary``) is a
    non-empty list. If it is ``None`` or empty (e.g. the LLM picker failed),
    no cards are sent — preventing accidental fan-out of every raw feed
    article when summarization is unavailable. The caller is expected to
    surface an explicit status message in that case.

    Returns explicit delivery counts and attribution for successfully sent
    cards only. Raw feed articles and failed cards are never reported as
    delivered. Attribution uses the original feed source when available.
    """
    from digest.radar.collector import article_hash

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        logger.warning("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set, skipping cards")
        return ArticleDeliveryResult()

    if not top_articles:
        total = sum(len(v) for v in articles_by_category.values())
        logger.warning(
            "send_article_cards skipped: no top_articles "
            "(would have flooded %d raw articles)",
            total,
        )
        return ArticleDeliveryResult()

    api_url = _API_BASE.format(token=token)
    source_by_hash = {
        article_hash(art.title, art.link): art.source
        for articles in articles_by_category.values()
        for art in articles
    }

    # (title, link, source, cat, desc)
    cards: list[tuple[str, str, str, str, str]] = [
        (a.title, a.link, a.source, a.category, a.summary) for a in top_articles
    ]

    result = ArticleDeliveryResult()
    async with httpx.AsyncClient() as client:
        for title, link, source, category, summary in cards:
            full_hash = article_hash(title, link)
            hash8 = full_hash[:8]

            title_esc = escape_markdownv2(title)
            url_esc = link.replace("\\", "\\\\").replace(")", "\\)")
            source_esc = escape_markdownv2(source)
            cat_esc = escape_markdownv2(category)
            summary_esc = escape_markdownv2(summary)

            labels = _labels(config)
            async_note = escape_markdownv2(labels["feedback"])
            text = (
                f"[{title_esc}]({url_esc})\n\n"
                f"{summary_esc}\n\n"
                f"*{source_esc}* \u00b7 _{cat_esc}_\n"
                f"_{async_note}_"
            )

            keyboard: dict[str, Any] = {
                "inline_keyboard": [
                    [
                        {"text": "\U0001f44d", "callback_data": f"fb:a:g:{hash8}"},
                        {"text": "\U0001f44e", "callback_data": f"fb:a:b:{hash8}"},
                    ]
                ]
            }

            result.attempted += 1
            try:
                await _send_chunk(
                    client,
                    api_url,
                    chat_id,
                    text,
                    reply_markup=keyboard,
                )
                result.sent += 1
                result.article_source_map[hash8] = source_by_hash.get(full_hash, source)
                result.delivered_hashes.add(full_hash)
            except Exception as exc:
                result.failed += 1
                logger.warning(
                    "Failed to send card for '%s': %s", title[:50], exc,
                )

            await asyncio.sleep(0.5)

    logger.info(
        "Telegram article cards: %d attempted, %d sent, %d failed",
        result.attempted, result.sent, result.failed,
    )
    return result


async def send_counter_signals(
    ranked_signals: list[Any],
    config: Any,
    irritator_status: "IrritatorStatus | None" = None,
) -> bool:
    """Send counter-signals as a separate Telegram message.

    If *ranked_signals* is empty but *irritator_status* is provided, a short
    status message is sent. Notification is loud on error, silent on empty.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return False

    api_url = _API_BASE.format(token=token)

    if not ranked_signals:
        if irritator_status is not None:
            prefix = f"💢 {_labels(config)['irritator']}: "
            chunks = split_supplement(prefix + irritator_status.text, escape_markdownv2)
            disable_notification = irritator_status.level != "error"
            async with asyncio.timeout(_SUPPLEMENT_DISPATCH_SECONDS), httpx.AsyncClient() as client:
                for chunk in chunks:
                    await _send_chunk(
                        client, api_url, chat_id, chunk,
                        disable_notification=disable_notification,
                    )
            logger.info("Irritator status sent to Telegram: %s", irritator_status.text)
        return False

    labels = _labels(config)
    language = getattr(getattr(config, "radar", None), "language", "en")
    text = "\n\n".join([
        f"💢🔥 {labels['irritator'].upper()} 🔥💢",
        *(signal_text(ranked, language) for ranked in ranked_signals),
    ])
    chunks = split_supplement(text, escape_markdownv2)

    async with asyncio.timeout(_SUPPLEMENT_DISPATCH_SECONDS), httpx.AsyncClient() as client:
        for chunk in chunks:
            await _send_chunk(client, api_url, chat_id, chunk)

    logger.info("Counter-signals sent to Telegram (%d signals)", len(ranked_signals))
    return True
