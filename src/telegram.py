"""Telegram Bot API delivery for the daily digest."""

from __future__ import annotations

import asyncio
import logging
import os
import re

import httpx

from src.config import Config

logger = logging.getLogger(__name__)

# Telegram message size limit in characters
_MAX_MESSAGE_LEN = 4096

# MarkdownV2 special characters that must be escaped (outside entities)
_MARKDOWNV2_SPECIAL = r"_*[]()~`>#+-=|{}.!"


def escape_markdownv2(text: str) -> str:
    """Escape special characters for Telegram MarkdownV2 parse mode.

    All special characters outside of explicit formatting entities must be
    escaped with a preceding backslash.
    """
    # Characters to escape per Telegram docs
    chars = r"\_*[]()~`>#+-=|{}.!"
    pattern = "([" + re.escape(chars) + "])"
    return re.sub(pattern, r"\\\1", text)


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
                        # Hard split if a single line exceeds limit
                        while len(line) > max_len:
                            chunks.append(line[:max_len])
                            line = line[max_len:]
                        current = line
            else:
                current = para

    if current:
        chunks.append(current)

    return chunks if chunks else [text]


async def send_digest(text: str, config: Config) -> None:
    """Send the digest text to Telegram.

    If Telegram delivery is disabled in config, returns silently.
    If credentials are missing, logs a warning and returns without crashing.
    Long messages are split at paragraph boundaries and sent sequentially with
    a 1-second delay between parts.
    """
    if not config.delivery.telegram:
        logger.debug("Telegram delivery is disabled in config, skipping.")
        return

    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if not bot_token or not chat_id:
        logger.warning(
            "Telegram delivery is enabled but credentials are missing. "
            "Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID environment variables. "
            "See https://core.telegram.org/bots#how-do-i-create-a-bot for setup instructions."
        )
        return

    chunks = split_message(text)
    api_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"

    async with httpx.AsyncClient(timeout=30.0) as client:
        for i, chunk in enumerate(chunks):
            if i > 0:
                await asyncio.sleep(1)
            await _send_chunk(client, api_url, chat_id, chunk)


async def _send_chunk(
    client: httpx.AsyncClient,
    api_url: str,
    chat_id: str,
    text: str,
) -> None:
    """Send a single message chunk to Telegram."""
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "MarkdownV2",
    }
    try:
        response = await client.post(api_url, json=payload)
        if response.status_code == 400:
            # MarkdownV2 parse errors — fall back to plain text
            logger.warning(
                "Telegram rejected message with MarkdownV2 (HTTP 400), retrying as plain text. "
                "Response: %s",
                response.text,
            )
            payload_plain = {"chat_id": chat_id, "text": text}
            response = await client.post(api_url, json=payload_plain)
        response.raise_for_status()
        logger.info("Telegram message chunk sent successfully (%d chars).", len(text))
    except httpx.HTTPStatusError as exc:
        logger.error(
            "Failed to send Telegram message (HTTP %d): %s",
            exc.response.status_code,
            exc.response.text,
        )
        raise
    except httpx.RequestError as exc:
        logger.error("Network error while sending Telegram message: %s", exc)
        raise
