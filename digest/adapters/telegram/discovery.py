"""Bounded Telegram approval-card transport, independent of persistent state."""
from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from digest.domain.catalog.exploration import ProposalDelivery as ProposalDelivery
from digest.domain.catalog.proposals import PendingSource

logger = logging.getLogger(__name__)


async def send_source_approval_message(
    source: PendingSource, bot_token: str, chat_id: str, bot_username: str = "",
) -> ProposalDelivery:
    """Send source decision links, or commands when no valid bot username is set."""
    username_valid = isinstance(bot_username, str) and re.fullmatch(r"[A-Za-z0-9_]{5,32}", bot_username)
    text = (
        f"New RSS source suggested:\n\n"
        f"{source.name}\n"
        f"Category: {source.category}\n"
        f"URL: {source.url}\n\n"
        "Add to config as a trial source?\n\n"
    )
    if username_valid:
        text += "Tap Add or Reject, then tap Start.\n"
    text += "Or send " if username_valid else "Send "
    text += (
        f"/source ok {source.source_hash} to add it or "
        f"/source no {source.source_hash} to reject it.\n"
        "Your decision is collected on the next run. "
        "Telegram keeps uncollected decision messages for at most 24 hours."
    )
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
    }
    if username_valid:
        payload["reply_markup"] = {
            "inline_keyboard": [[
                {"text": "Add", "url": f"https://t.me/{bot_username}?start=source_ok_{source.source_hash}"},
                {"text": "Reject", "url": f"https://t.me/{bot_username}?start=source_no_{source.source_hash}"},
            ]],
        }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json=payload,
            )
            resp.raise_for_status()
            result = resp.json()
            if result.get("ok") is False:
                return ProposalDelivery("rejected")
            message_id = result.get("result", {}).get("message_id")
            if result.get("ok") is not True or type(message_id) is not int or message_id <= 0:
                return ProposalDelivery("unknown")
            return ProposalDelivery("confirmed", message_id)
    except httpx.HTTPStatusError as exc:
        # An explicit Bot API rejection is distinguishable from an uncertain send.
        try:
            rejected = exc.response.json().get("ok") is False
        except (ValueError, AttributeError):
            rejected = False
        return ProposalDelivery("rejected" if rejected else "unknown")
    except Exception as exc:
        logger.warning("Source proposal delivery uncertain (%s)", type(exc).__name__)
        return ProposalDelivery("unknown")
