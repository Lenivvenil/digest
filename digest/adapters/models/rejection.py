"""Bounded provider-reported quota facts, never retry policy or raw error text."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import NoReturn

_DIMENSIONS = {
    "tokens per minute (TPM)": ("TPM", "tokens"),
    "tokens per day (TPD)": ("TPD", "tokens"),
    "requests per minute (RPM)": ("RPM", "requests"),
    "requests per day (RPD)": ("RPD", "requests"),
    "input tokens per minute (ITPM)": ("ITPM", "tokens"),
    "output tokens per minute (OTPM)": ("OTPM", "tokens"),
}
_UINT = r"(?:0|[1-9][0-9]{0,18})"
_SUFFIXES = (
    " Visit https://console.groq.com/docs/rate-limits for more information.",
    " Need more tokens? Upgrade to Dev Tier today at https://console.groq.com/settings/billing",
)
_MESSAGE = re.compile(
    r"Rate limit reached for model `(?P<model>[A-Za-z0-9_./:-]{1,128})` "
    r"in organization `[A-Za-z0-9_-]{1,128}`(?: service tier `[A-Za-z0-9_-]{1,32}`)? on "
    r"(?P<dimension>" + "|".join(re.escape(phrase) for phrase in _DIMENSIONS) + r"): "
    rf"Limit (?P<limit>{_UINT}), Used (?P<used>{_UINT}), Requested (?P<requested>{_UINT})\."
    r"(?: Please try again in (?P<retry>[0-9.hms]{1,32})\.)?"
    r"(?:" + "|".join(re.escape(suffix) for suffix in _SUFFIXES) + r")?"
)
_DURATION = re.compile(
    r"(?:(?P<h>[0-9]{1,2})h)?(?:(?P<m>[0-9]{1,4})m)?(?:(?P<s>[0-9]{1,5}(?:\.[0-9]{1,9})?)s)?"
)


@dataclass(frozen=True)
class QuotaRejection:
    """One positively parsed clause; counts are provider claims, not measured use."""

    dimension: str
    limit: int
    used: int
    requested: int
    retry_hint_seconds: float | None = None


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Ambiguous diagnostic JSON")
        result[key] = value
    return result


def _reject_constant(_value: str) -> NoReturn:
    raise ValueError("Nonfinite diagnostic JSON")


def _duration_seconds(value: str) -> float | None:
    milliseconds = re.fullmatch(r"([0-9]{1,8}(?:\.[0-9]{1,9})?)ms", value)
    if milliseconds is not None:
        seconds = float(milliseconds[1]) / 1000
    else:
        match = _DURATION.fullmatch(value)
        if match is None or not any(match.groups()):
            return None
        hours, minutes, part_seconds = (float(part or "0") for part in match.groups())
        if (match["h"] is not None and minutes >= 60
                or (match["h"] is not None or match["m"] is not None) and part_seconds >= 60):
            return None
        seconds = hours * 3600 + minutes * 60 + part_seconds
    return seconds if math.isfinite(seconds) and 0 <= seconds <= 86400 else None


def groq_quota_rejection(
    body: bytes, *, provider: str | None, model: str | None, http_status: int,
) -> QuotaRejection | None:
    """Recognize one complete Groq template; never harvest numbers from arbitrary prose.

    Deliberately narrower than every possible provider message. Unknown grammar,
    missing dimensions, approximate counts and extra text remain unknown. Limits
    and body retry hints never control admission, cooldowns or retries.
    """
    if provider != "groq" or http_status != 429 or not model or len(body) > 16384:
        return None
    try:
        raw = json.loads(body, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        return None
    error = raw.get("error") if isinstance(raw, dict) else None
    if not isinstance(error, dict) or error.get("code") != "rate_limit_exceeded":
        return None
    message = error.get("message")
    if not isinstance(message, str) or len(message) > 2048:
        return None
    match = _MESSAGE.fullmatch(message)
    if match is None or match["model"] != model:
        return None
    dimension, kind = _DIMENSIONS[match["dimension"]]
    if error.get("type") != kind:
        return None
    limit, used, requested = (int(match[name]) for name in ("limit", "used", "requested"))
    if max(limit, used, requested) > 2**63 - 1:
        return None
    retry = _duration_seconds(match["retry"]) if match["retry"] is not None else None
    if match["retry"] is not None and retry is None:
        return None
    return QuotaRejection(dimension, limit, used, requested, retry)
