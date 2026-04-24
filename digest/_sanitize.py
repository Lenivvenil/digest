"""Prompt-injection sanitization helpers.

sanitize_article() strips patterns from article fields that could manipulate
LLM behaviour (e.g. "Ignore previous instructions") and enforces per-field
length limits so that a crafted feed entry cannot flood the prompt.
"""

from __future__ import annotations

import re

# Maximum lengths for each field after sanitization
_TITLE_MAX = 200
_DESCRIPTION_MAX = 500
_SOURCE_MAX = 80

# Patterns that are characteristic of prompt-injection payloads.
# Each entry is a compiled regex; all matches are replaced with a placeholder.
_INJECTION_PATTERNS: list[re.Pattern[str]] = [
    re.compile(
        r"ignore\s+(all\s+)?(previous|prior|above|earlier)\s+(instructions?|prompts?|context|rules?)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(you\s+are\s+now|act\s+as|pretend\s+(to\s+be|you\s+are)|roleplay\s+as)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"(system\s*:|<\s*/?system\s*>|<\s*/?prompt\s*>|<\s*/?instructions?\s*>)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(disregard|forget|override|bypass)\s+(your\s+)?(instructions?|rules?|constraints?|guidelines?)",
        re.IGNORECASE,
    ),
    re.compile(r"```\s*(system|prompt|instructions?)", re.IGNORECASE),
    # Null bytes and control characters (except tab, LF, CR)
    re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]"),
]

_PLACEHOLDER = "[removed]"


def _sanitize_field(text: str, max_len: int) -> str:
    """Apply injection-pattern scrubbing and length-limit to a single field."""
    for pattern in _INJECTION_PATTERNS:
        text = pattern.sub(_PLACEHOLDER, text)
    # Collapse any resulting runs of whitespace (don't strip leading/trailing)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text[:max_len]


def sanitize_article(
    title: str,
    description: str,
    source: str,
) -> tuple[str, str, str]:
    """Return sanitized (title, description, source) safe for use in LLM prompts.

    Applies injection-pattern removal and enforces maximum field lengths.
    The link field is not sanitized here — it is already validated by
    _dns_pinning.validate_url() during collection.
    """
    return (
        _sanitize_field(title, _TITLE_MAX),
        _sanitize_field(description, _DESCRIPTION_MAX),
        _sanitize_field(source, _SOURCE_MAX),
    )
