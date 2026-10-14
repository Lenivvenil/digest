"""Synthetic provider quota diagnostics never retain arbitrary response strings."""

from __future__ import annotations

import json
from dataclasses import asdict
from unittest.mock import patch

import httpx
import pytest

from digest.adapters.models.rejection import QuotaRejection, groq_quota_rejection
from digest.llm import _http_rejection


def quota_message(
    dimension: str = "tokens per minute (TPM)", *, model: str = "configured-model",
    limit: str = "100", used: str = "80", requested: str = "30", retry: str | None = "2.5s",
    tier: bool = True, suffix: str = "",
) -> str:
    tier_text = " service tier `PRIVATE_TIER`" if tier else ""
    retry_text = f" Please try again in {retry}." if retry is not None else ""
    return (
        f"Rate limit reached for model `{model}` in organization `PRIVATE_ORGANIZATION`{tier_text} "
        f"on {dimension}: Limit {limit}, Used {used}, Requested {requested}.{retry_text}{suffix}"
    )


def quota_body(message: object, kind: object = "tokens", code: object = "rate_limit_exceeded") -> bytes:
    return json.dumps({"error": {"message": message, "type": kind, "code": code}}).encode()


def parse(
    body: bytes, provider: str = "groq", model: str = "configured-model", status: int = 429,
) -> QuotaRejection | None:
    return groq_quota_rejection(body, provider=provider, model=model, http_status=status)


@pytest.mark.parametrize("phrase,dimension,kind", [
    ("tokens per minute (TPM)", "TPM", "tokens"),
    ("tokens per day (TPD)", "TPD", "tokens"),
    ("requests per minute (RPM)", "RPM", "requests"),
    ("requests per day (RPD)", "RPD", "requests"),
    ("input tokens per minute (ITPM)", "ITPM", "tokens"),
    ("output tokens per minute (OTPM)", "OTPM", "tokens"),
])
@pytest.mark.parametrize("tier", [False, True])
@pytest.mark.parametrize("suffix", [
    "", " Visit https://console.groq.com/docs/rate-limits for more information.",
    " Need more tokens? Upgrade to Dev Tier today at https://console.groq.com/settings/billing",
])
def test_recognized_full_clause_retains_only_enum_and_numbers(
    phrase: str, dimension: str, kind: str, tier: bool, suffix: str,
) -> None:
    result = parse(quota_body(quota_message(phrase, tier=tier, suffix=suffix), kind))
    assert result == QuotaRejection(dimension, 100, 80, 30, 2.5)
    assert result is not None
    saved = json.dumps(asdict(result))
    assert not any(value in saved for value in ("PRIVATE", "configured-model", "https://", "Rate limit"))


@pytest.mark.parametrize("duration,expected", [
    (None, None), ("0s", 0.0), ("399ms", 0.399), ("0.5ms", 0.0005), ("86400000ms", 86400.0),
    ("60s", 60.0), ("1.000000001s", 1.000000001), ("2m", 120.0), ("1h", 3600.0),
    ("2m3.5s", 123.5), ("1h2m3s", 3723.0), ("24h", 86400.0), ("1440m", 86400.0), ("86400s", 86400.0),
])
def test_duration_is_bounded_numeric_metadata(duration: str | None, expected: float | None) -> None:
    assert parse(quota_body(quota_message(retry=duration))) == QuotaRejection("TPM", 100, 80, 30, expected)


@pytest.mark.parametrize("duration", [
    "", "1e2s", "NaNs", "Infinitys", "-1s", "+1s", "１s", ".5s", "1.s", "1.0000000001s",
    "86400.001s", "86400001ms", "25h", "1441m", "1h60m", "1m60s", "1h60s", "1s1m", "1m1m",
    "1ms2s", " 1s", "1s ", "1\ns", "1s. PRIVATE", "99999999999999999999999999999999999s",
])
def test_unknown_duration_rejects_entire_detail(duration: str) -> None:
    assert parse(quota_body(quota_message(retry=duration))) is None


@pytest.mark.parametrize("field", ["limit", "used", "requested"])
@pytest.mark.parametrize("value", [
    "", "-1", "+1", "1.5", "1e3", "~3", "01", "１２", "1,000", "NaN", "true", "null",
    "9223372036854775808", "9" * 20, "3 PRIVATE_KEY", "https://example.invalid/SECRET",
])
def test_counts_require_complete_bounded_integer_trio(field: str, value: str) -> None:
    assert parse(quota_body(quota_message(**{field: value}))) is None


def test_zero_and_maximum_counts_are_retained_without_inferred_semantics() -> None:
    assert parse(quota_body(quota_message(limit="0", used=str(2**63 - 1), requested="0"))) == (
        QuotaRejection("TPM", 0, 2**63 - 1, 0, 2.5)
    )


@pytest.mark.parametrize("case", [
    "prefix", "suffix", "newline", "two_clauses", "unknown_dimension", "mismatched_unit", "absent_dimension",
    "extra_count", "missing_used", "arbitrary_url", "changed_url", "joined_suffixes", "organization_url",
    "organization_quote", "organization_newline", "organization_too_long", "tier_too_long", "unicode_space",
])
def test_unrecognized_or_injected_prose_is_never_harvested(case: str) -> None:
    original = quota_message()
    variants = {
        "prefix": "User prompt: " + original,
        "suffix": original + " PRIVATE_PROMPT",
        "newline": original + "\n",
        "two_clauses": original + " " + original,
        "unknown_dimension": quota_message("widgets per minute (WPM)"),
        "mismatched_unit": quota_message("requests per minute (TPM)"),
        "absent_dimension": quota_message(""),
        "extra_count": original.replace(", Used 80,", ", Used 80, Used 99,"),
        "missing_used": original.replace(", Used 80", ""),
        "arbitrary_url": original + " https://example.invalid/PRIVATE_KEY",
        "changed_url": original + " Visit https://console.groq.com/docs/rate-limits?PRIVATE for more information.",
        "joined_suffixes": original + " Visit https://console.groq.com/docs/rate-limits for more information."
        + " Need more tokens? Upgrade to Dev Tier today at https://console.groq.com/settings/billing",
        "organization_url": original.replace("PRIVATE_ORGANIZATION", "https://example.invalid/PRIVATE"),
        "organization_quote": original.replace("PRIVATE_ORGANIZATION", "PRIVATE`KEY"),
        "organization_newline": original.replace("PRIVATE_ORGANIZATION", "PRIVATE\nKEY"),
        "organization_too_long": original.replace("PRIVATE_ORGANIZATION", "a" * 129),
        "tier_too_long": original.replace("PRIVATE_TIER", "a" * 33),
        "unicode_space": original.replace(" on ", "\u00a0on "),
    }
    assert parse(quota_body(variants[case])) is None


@pytest.mark.parametrize("provider,model,status,code,kind", [
    ("mistral", "configured-model", 429, "rate_limit_exceeded", "tokens"),
    ("groq", "different-model", 429, "rate_limit_exceeded", "tokens"),
    ("groq", "", 429, "rate_limit_exceeded", "tokens"),
    ("groq", "configured-model", 400, "rate_limit_exceeded", "tokens"),
    ("groq", "configured-model", 413, "rate_limit_exceeded", "tokens"),
    ("groq", "configured-model", 429, "insufficient_quota", "tokens"),
    ("groq", "configured-model", 429, "rate_limit_exceeded", "requests"),
    ("groq", "configured-model", 429, "rate_limit_exceeded", None),
    ("groq", "configured-model", 429, "rate_limit_exceeded", ["tokens"]),
])
def test_route_status_code_and_unit_type_are_bound(
    provider: str, model: str, status: int, code: str, kind: object,
) -> None:
    assert parse(quota_body(quota_message(), kind, code), provider, model, status) is None


@pytest.mark.parametrize("message", [None, 123, True, {}, [], "a" * 2049])
def test_invalid_message_types_and_size_stay_unknown(message: object) -> None:
    assert parse(quota_body(message)) is None


@pytest.mark.parametrize("body", [
    b"", b"not JSON", b"\xff", b"null", b"[]", b'{"error": []}', b'{"error": null}',
    b'{"error": {"code": "rate_limit_exceeded"}}',
    b'{"error": {}, "error": {}}', b'{"error": {"message": "a", "message": "b"}}',
    b'{"error": {}, "ignored": {"duplicate": 1, "duplicate": 2}}',
    b'{"error": {}, "ignored": NaN}', b'{"error": {}, "ignored": Infinity}',
    b'{"error": {}, "ignored": -Infinity}',
    b'[' * 2000 + b']' * 2000,
])
def test_malformed_ambiguous_or_nonfinite_json_stays_unknown(body: bytes) -> None:
    assert parse(body) is None


def test_exact_body_size_cap_and_ignored_private_fields() -> None:
    raw = json.loads(quota_body(quota_message()))
    raw["error"].update(api_key="PRIVATE_KEY", prompt="PRIVATE_PROMPT", request_id="PRIVATE_REQUEST_ID")
    raw["url"] = "https://example.invalid/PRIVATE_URL"
    body = json.dumps(raw).encode()
    expected = QuotaRejection("TPM", 100, 80, 30, 2.5)
    assert parse(body) == expected
    assert parse(body + b" " * (16384 - len(body))) == expected
    assert parse(body + b" " * (16385 - len(body))) is None
    assert "PRIVATE" not in json.dumps(asdict(expected))


def test_parser_exception_preserves_header_status_and_machine_code() -> None:
    response = httpx.Response(429, content=quota_body(quota_message()), headers={"retry-after": "7"})
    with patch("digest.llm.groq_quota_rejection", side_effect=ValueError("PRIVATE_BODY")):
        facts = _http_rejection(response, provider="groq", model="configured-model")
    assert facts.http_status == 429 and facts.provider_code == "rate_limit_exceeded"
    assert facts.retry_after_seconds == 7 and facts.quota is None
