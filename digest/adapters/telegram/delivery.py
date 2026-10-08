"""Telegram delivery protocols with deliberately distinct acceptance and retry policies."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import httpx

from digest.domain.catalog.articles import article_hash
from digest.domain.delivery.outcomes import (
    ArticleCoverage,
    ArticleDeliveryResult,
    IssueDeliveryResult,
    project_issue_coverage,
)
from digest.presentation.telegram import (
    escape_markdownv2,
    render_article_card,
    render_compact_issue,
    render_counter_signals,
    render_post_delivery_supplement,
)

if TYPE_CHECKING:
    from digest.config import Config
    from digest.domain.editorial.summaries import ArticleSummary
    from digest.irritator import IrritatorStatus
    from digest.irritator.evidence_stage import EvidenceIrritatorResult

logger = logging.getLogger(__name__)

_API_BASE = "https://api.telegram.org/bot{token}/sendMessage"
_MAX_RETRIES = 3
# Legacy supplements retain card retries; post-delivery supplements never retry.
_SUPPLEMENT_DISPATCH_SECONDS = 30.0 * _MAX_RETRIES
_COMPACT_DISPATCH_SECONDS = 30.0
_POST_DELIVERY_DISPATCH_SECONDS = 30.0


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
                await asyncio.sleep(2**attempt)


async def send_status_message(text: str, *, disable_notification: bool = False) -> bool:
    """Send a legacy notice/footer with the existing retry and fallback policy.

    Missing credentials skip transport. The calling application decides whether
    a transport error is an optional-notice failure or a publication warning.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return False
    async with httpx.AsyncClient() as client:
        await _send_chunk(
            client,
            f"https://api.telegram.org/bot{token}/sendMessage",
            chat_id,
            escape_markdownv2(text),
            disable_notification=disable_notification,
        )
    return True


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
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        logger.warning("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set, skipping cards")
        return ArticleDeliveryResult()

    if not top_articles:
        total = sum(len(v) for v in articles_by_category.values())
        logger.warning(
            "send_article_cards skipped: no top_articles (would have flooded %d raw articles)",
            total,
        )
        return ArticleDeliveryResult()

    api_url = _API_BASE.format(token=token)
    source_by_hash = {
        article_hash(art.title, art.link): art.source for articles in articles_by_category.values() for art in articles
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

            card = render_article_card(title, link, source, category, summary, config)

            result.attempted += 1
            try:
                await _send_chunk(
                    client,
                    api_url,
                    chat_id,
                    card.text,
                    reply_markup=card.reply_markup,
                )
                result.sent += 1
                result.article_source_map[hash8] = source_by_hash.get(full_hash, source)
                result.delivered_hashes.add(full_hash)
            except Exception as exc:
                result.failed += 1
                logger.warning(
                    "Failed to send card for '%s': %s",
                    title[:50],
                    exc,
                )

            await asyncio.sleep(0.5)

    logger.info(
        "Telegram article cards: %d attempted, %d sent, %d failed",
        result.attempted,
        result.sent,
        result.failed,
    )
    return result


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
        chunks, ranges = render_compact_issue(articles, config, notice)
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

    result = project_issue_coverage(
        (ArticleCoverage(article.full_hash, article.source, article.covering_chunks) for article in ranges),
        vote_protocol="legacy8",
        outcome=result.outcome,
        total_chunks=result.total_chunks,
        attempted_chunks=result.attempted_chunks,
        confirmed_chunks=result.confirmed_chunks,
    )
    logger.info(
        "Compact Telegram issue: %s, %d/%d chunks confirmed, %d articles confirmed",
        result.outcome,
        result.confirmed_chunks,
        result.total_chunks,
        result.sent,
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
            chunks, disable_notification = render_counter_signals(ranked_signals, config, irritator_status)
            async with asyncio.timeout(_SUPPLEMENT_DISPATCH_SECONDS), httpx.AsyncClient() as client:
                for chunk in chunks:
                    await _send_chunk(
                        client,
                        api_url,
                        chat_id,
                        chunk,
                        disable_notification=disable_notification,
                    )
            logger.info("Irritator status sent to Telegram: %s", irritator_status.text)
        return False

    chunks, _ = render_counter_signals(ranked_signals, config, irritator_status)

    async with asyncio.timeout(_SUPPLEMENT_DISPATCH_SECONDS), httpx.AsyncClient() as client:
        for chunk in chunks:
            await _send_chunk(client, api_url, chat_id, chunk)

    logger.info("Counter-signals sent to Telegram (%d signals)", len(ranked_signals))
    return True


async def send_post_delivery_supplement(result: EvidenceIrritatorResult, config: Config, *, notice: str = "") -> str:
    """Send silent chunks once, accepting HTTP success plus an explicit Telegram ok."""
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not config.telegram.enabled or not token or not chat:
        return "not_configured"
    from digest.domain.investigation.coverage import FULL_SOURCE_COVERAGE

    chunks = render_post_delivery_supplement(
        result,
        config,
        full_source=result.coverage == FULL_SOURCE_COVERAGE,
        notice=notice,
    )
    # Never retry an uncertain POST: Telegram has no idempotency key for sendMessage.
    async with asyncio.timeout(_POST_DELIVERY_DISPATCH_SECONDS), httpx.AsyncClient() as client:
        for text in chunks:
            response = await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={
                    "chat_id": chat,
                    "text": text,
                    "parse_mode": "MarkdownV2",
                    "disable_notification": True,
                },
                timeout=30.0,
            )
            response.raise_for_status()
            body = response.json()
            if not isinstance(body, dict) or body.get("ok") is not True:
                raise ValueError("Telegram did not confirm the supplement.")
    return "sent"
