"""Telegram Bot API delivery for radar digest and counter-signals."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from digest.irritator import IrritatorStatus
    from digest.radar.summarizer import ArticleSummary

import httpx

from digest.delivery.supplement import signal_text, split_supplement

logger = logging.getLogger(__name__)

_API_BASE = "https://api.telegram.org/bot{token}/sendMessage"
# Static presentation labels follow canonical generation language, not translation targets.
_LABELS = {
    "en": {
        "feedback": ("Tap a vote button, then Start to send it. "
                     "Processed on the next digest run; private owner chat only."),
        "feedback_plain": "Votes are processed on digest runs; private owner chat only.",
        "fallback": "Or send /vote g {hash} (good) or /vote b {hash} (bad).",
        "irritator": "Irritator",
    },
    "ru": {
        "feedback": ("Нажмите оценку, затем Start (Запустить), чтобы отправить голос. "
                     "Учтём при следующем выпуске; только личный чат владельца."),
        "feedback_plain": "Оценки обрабатываются при запусках дайджеста; только личный чат владельца.",
        "fallback": "Или отправьте /vote g {hash} (полезно) либо /vote b {hash} (неполезно).",
        "irritator": "Раздражатор",
    },
}
_SPLIT_LIMIT = 3800
_MAX_MESSAGE_LEN = 4096
_MAX_RETRIES = 3
# Supplement-only total dispatch cap; primary sender and retry policy stay unchanged.
_SUPPLEMENT_DISPATCH_SECONDS = 30.0 * _MAX_RETRIES
_COMPACT_DISPATCH_SECONDS = 30.0

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


@dataclass
class IssueDeliveryResult(ArticleDeliveryResult):
    """Confirmed article coverage and the independent whole-issue outcome.

    Article attempts count blocks touched by an attempted chunk; incomplete
    attempted blocks count as failed, including uncertain deliveries. A final
    notice can fail even when every article has been confirmed. ``unknown``
    means Telegram acceptance could not be established and must not be retried.
    """

    outcome: Literal["sent", "failed", "unknown", "skipped"] = "skipped"
    total_chunks: int = 0
    attempted_chunks: int = 0
    confirmed_chunks: int = 0

    @property
    def complete(self) -> bool:
        return self.outcome == "sent" and self.sent > 0 and self.confirmed_chunks == self.total_chunks > 0


@dataclass
class _IssueArticleRange:
    start: int
    end: int
    full_hash: str
    source: str
    covering_chunks: tuple[int, ...] = ()


@dataclass
class _IssueChunk:
    text: str
    reply_markup: dict[str, Any] | None = None


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
            username = getattr(config.telegram, "bot_username", "")
            notice = labels["feedback"] if username else labels["feedback_plain"]
            async_note = escape_markdownv2(notice + "\n" + labels["fallback"].format(hash=hash8))
            text = (
                f"[{title_esc}]({url_esc})\n\n"
                f"{summary_esc}\n\n"
                f"*{source_esc}* \u00b7 _{cat_esc}_\n"
                f"_{async_note}_"
            )

            keyboard: dict[str, Any] | None = {
                "inline_keyboard": [
                    [
                        {"text": "\U0001f44d", "url": f"https://t.me/{username}?start=vote_g_{hash8}"},
                        {"text": "\U0001f44e", "url": f"https://t.me/{username}?start=vote_b_{hash8}"},
                    ]
                ]
            } if username else None

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


def _render_compact_issue(
    articles: list[ArticleSummary], config: Any, notice: str,
) -> tuple[list[_IssueChunk], list[_IssueArticleRange]]:
    """Prepare every chunk and map escaped article ranges before dispatch."""
    from digest.radar.collector import article_hash

    username = getattr(config.telegram, "bot_username", "")
    parts: list[str] = []
    ranges: list[_IssueArticleRange] = []
    offset = 0
    for index, article in enumerate(articles, 1):
        full_hash = article_hash(article.title, article.link)
        block = f"{index}. {article.title}\n{article.summary}\n{article.source}\n{article.link}"
        if not username:
            block += f"\n[{full_hash[:8]}]"
        if parts:
            offset += len(escape_markdownv2("\n\n"))
        end = offset + len(escape_markdownv2(block))
        ranges.append(_IssueArticleRange(offset, end, full_hash, article.source))
        parts.append(block)
        offset = end

    footer = [notice] if notice else []
    if articles:
        labels = _labels(config)
        footer.append(labels["feedback"] if username else labels["feedback_plain"])
        if not username:
            footer.append(labels["fallback"].format(hash="HASH"))
    if footer:
        parts.append("\n".join(footer))
    encoded_chunks = split_supplement("\n\n".join(parts), escape_markdownv2, _SPLIT_LIMIT)
    chunks = [_IssueChunk(text) for text in encoded_chunks]
    chunk_ranges: list[tuple[int, int]] = []
    offset = 0
    for chunk in chunks:
        chunk_ranges.append((offset, offset + len(chunk.text)))
        offset += len(chunk.text)

    for index, article_range in enumerate(ranges, 1):
        article_range.covering_chunks = tuple(
            chunk_index for chunk_index, (start, end) in enumerate(chunk_ranges)
            if start < article_range.end and article_range.start < end
        )
        if username:
            final_chunk = chunks[article_range.covering_chunks[-1]]
            if final_chunk.reply_markup is None:
                final_chunk.reply_markup = {"inline_keyboard": []}
            hash8 = article_range.full_hash[:8]
            final_chunk.reply_markup["inline_keyboard"].append([
                {"text": f"{index}👍", "url": f"https://t.me/{username}?start=vote_g_{hash8}"},
                {"text": f"{index}👎", "url": f"https://t.me/{username}?start=vote_b_{hash8}"},
            ])
    return chunks, ranges


async def send_compact_issue(
    articles: list[ArticleSummary],
    config: Any,
    *,
    notice: str = "",
    before_send: Callable[[], None] | None = None,
) -> IssueDeliveryResult:
    """Send one lossless issue with a single attempt per transport chunk.

    The caller reserves the issue durably. Its synchronous ``before_send``
    callback marks the local dispatch boundary after all rendering succeeds,
    immediately before the first POST. Callback exceptions propagate without
    a request. No rejection, uncertain receipt or timeout causes a retry or
    plaintext fallback; subsequent chunks stop and confirmed coverage survives.
    Empty article selections are skipped even if an issue notice is supplied.
    """
    result = IssueDeliveryResult()
    if not articles:
        return result
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        logger.warning("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set, skipping compact issue")
        return result

    try:
        chunks, ranges = _render_compact_issue(articles, config, notice)
    except ValueError:
        result.outcome = "failed"
        logger.warning("Compact Telegram issue cannot fit a required atomic text element")
        return result
    result.total_chunks = len(chunks)
    if not chunks:
        return result

    payloads: list[dict[str, Any]] = []
    for chunk in chunks:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": chunk.text,
            "parse_mode": "MarkdownV2",
            "disable_notification": False,
        }
        if chunk.reply_markup is not None:
            payload["reply_markup"] = chunk.reply_markup
        payloads.append(payload)

    api_url = _API_BASE.format(token=token)
    async with httpx.AsyncClient(follow_redirects=False) as client:
        if before_send is not None:
            before_send()
        try:
            async with asyncio.timeout(_COMPACT_DISPATCH_SECONDS):
                for payload in payloads:
                    result.attempted_chunks += 1
                    response = await client.post(api_url, json=payload, timeout=_COMPACT_DISPATCH_SECONDS)
                    if 400 <= response.status_code < 500:
                        result.outcome = "failed"
                        break
                    receipt = response.json()
                    if isinstance(receipt, dict) and receipt.get("ok") is False:
                        result.outcome = "failed"
                        break
                    if response.status_code != 200 or not isinstance(receipt, dict) or receipt.get("ok") is not True:
                        result.outcome = "unknown"
                        break
                    result.confirmed_chunks += 1
                else:
                    result.outcome = "sent"
        except (httpx.HTTPError, TimeoutError, ValueError):
            result.outcome = "unknown"

    for article_range in ranges:
        if any(index < result.attempted_chunks for index in article_range.covering_chunks):
            result.attempted += 1
            if all(index < result.confirmed_chunks for index in article_range.covering_chunks):
                result.sent += 1
                result.delivered_hashes.add(article_range.full_hash)
                result.article_source_map[article_range.full_hash[:8]] = article_range.source
            else:
                result.failed += 1
    logger.info(
        "Compact Telegram issue: %s, %d/%d chunks confirmed, %d articles confirmed",
        result.outcome, result.confirmed_chunks, result.total_chunks, result.sent,
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
    if irritator_status is not None:
        text += f"\n\nIrritator status: {irritator_status.level} — {irritator_status.text}"
    chunks = split_supplement(text, escape_markdownv2)

    async with asyncio.timeout(_SUPPLEMENT_DISPATCH_SECONDS), httpx.AsyncClient() as client:
        for chunk in chunks:
            await _send_chunk(client, api_url, chat_id, chunk)

    logger.info("Counter-signals sent to Telegram (%d signals)", len(ranked_signals))
    return True
