"""Strict one-attempt prepared Telegram protocol, without persisted-state access."""

from __future__ import annotations

from typing import Any, Literal

import httpx

from digest.adapters.telegram.diagnostics import request as telegram_request


def accepted_message_id(response: httpx.Response, owner: str) -> int | None:
    value = response.json()
    if response.status_code != 200 or not isinstance(value, dict) or value.get("ok") is not True:
        return None
    receipt = value.get("result")
    if not isinstance(receipt, dict) or type(receipt.get("message_id")) is not int or receipt["message_id"] <= 0:
        return None
    chat = receipt.get("chat")
    if not isinstance(chat, dict) or type(chat.get("id")) is not int or str(chat["id"]) != owner:
        return None
    return int(receipt["message_id"])


async def send_prepared_chunk(
    client: httpx.AsyncClient,
    token: str,
    payload: dict[str, Any],
    owner: str,
    *,
    timeout_seconds: float,
) -> tuple[Literal["confirmed", "failed", "unknown"], int | None]:
    """One POST only; the application persists attempted/confirmed progress around it."""
    response = await telegram_request(
        client, "POST",
        f"https://api.telegram.org/bot{token}/sendMessage",
        json=payload,
        timeout=timeout_seconds,
    )
    if 400 <= response.status_code < 500:
        return "failed", None
    message_id = accepted_message_id(response, owner)
    return ("unknown", None) if message_id is None else ("confirmed", message_id)
