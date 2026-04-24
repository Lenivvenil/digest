"""Blocklist filtering for articles and content."""

from __future__ import annotations


def is_blocked(text: str, blocklist: list[str]) -> bool:
    """Return True if *text* contains any keyword from *blocklist*.

    Case-insensitive substring match. Returns False for empty blocklist.
    """
    if not blocklist:
        return False
    text_lower = text.lower()
    return any(kw.lower() in text_lower for kw in blocklist)
