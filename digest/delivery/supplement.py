"""Lossless canonical text for supplementary counter-signals."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any


def signal_text(ranked: Any, language: str = "en") -> str:
    """Keep source identity, assessment and its complete qualification together."""
    label = "Оспаривает или уточняет" if language == "ru" else "Challenges or complicates"
    score = "Оценка" if language == "ru" else "Score"
    return (
        f"{ranked.signal.title}\n{ranked.signal.url}\n"
        f"{label}: {ranked.narrative_claim}\n"
        f"{score}: {ranked.score}/10\n{ranked.reasoning}"
    )


def split_supplement(
    text: str, escape: Callable[[str], str], max_units: int = 3800,
) -> list[str]:
    """Escape independently valid chunks without losing text or splitting URLs.

    The bound conservatively counts UTF-16 code units after Markdown escaping.
    All chunks are prepared before delivery, so an impossible URL fails before send.
    Concatenating decoded chunks recovers the original text exactly.
    """
    if max_units < 4:
        raise ValueError("Supplement chunk budget is too small.")

    def units(value: str) -> int:
        return len(value.encode("utf-16-le")) // 2

    chunks: list[str] = []
    current = ""
    used = 0
    for token in re.split(r"(https?://\S+|\s+)", text):
        if not token:
            continue
        encoded = escape(token)
        size = units(encoded)
        if token.startswith(("https://", "http://")) and size > max_units:
            raise ValueError("Supplement URL exceeds the Telegram message budget.")
        # Prefer intact words and URLs; split only oversized non-URL tokens.
        pieces = [token] if size <= max_units else list(token)
        for piece in pieces:
            encoded = escape(piece)
            size = units(encoded)
            if used + size > max_units:
                chunks.append(current)
                current, used = "", 0
            current += encoded
            used += size
    if current:
        chunks.append(current)
    return chunks
