"""Thin httpx-based LLM client with role-based dispatch and provider fallback.

Supports OpenAI-compatible APIs (Groq, DeepSeek) and Google Gemini.
Provider selection is driven by LLMRole — each provider in config declares
which roles it handles. On error (429/5xx/timeout), the next provider is tried.
"""

from __future__ import annotations

import asyncio
import enum
import json
import logging
import math
import os
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any, Literal

import httpx

logger = logging.getLogger(__name__)

# Registry of OpenAI-compatible providers
_OPENAI_COMPAT: dict[str, dict[str, str]] = {
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "api_key_env": "GROQ_API_KEY",
    },
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "api_key_env": "DEEPSEEK_API_KEY",
    },
    "mistral": {
        "base_url": "https://api.mistral.ai/v1",
        "api_key_env": "MISTRAL_API_KEY",
    },
}


class LLMRole(str, enum.Enum):
    SUMMARIZE = "summarize"
    EXTRACT_NARRATIVES = "extract_narratives"
    GENERATE_QUERIES = "generate_queries"
    RANK_SIGNALS = "rank_signals"
    FALLBACK = "fallback"
    REVIEW_EVIDENCE = "review_evidence"


_GROQ_REASONING_MODELS = frozenset({"openai/gpt-oss-20b", "openai/gpt-oss-120b"})


def _reasoning_options(provider: Any, model: Any, reasoning_effort: str | None,
                       include_reasoning: bool | None) -> dict[str, Any]:
    if reasoning_effort is None and include_reasoning is None:
        return {}
    if provider != "groq" or not isinstance(model, str) or model not in _GROQ_REASONING_MODELS:
        raise ValueError("Reasoning options require an explicit supported Groq GPT-OSS model override.")
    if reasoning_effort is not None and reasoning_effort not in ("low", "medium", "high"):
        raise ValueError("Groq GPT-OSS reasoning_effort must be low, medium or high.")
    if include_reasoning is not None and type(include_reasoning) is not bool:
        raise ValueError("include_reasoning must be a boolean when specified.")
    options: dict[str, Any] = {}
    if reasoning_effort is not None:
        options["reasoning_effort"] = reasoning_effort
    if include_reasoning is not None:
        options["include_reasoning"] = include_reasoning
    return options


async def _openai_compat_call(
    client: httpx.AsyncClient,
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict[str, str]],
    temperature: float,
    max_output_tokens: int | None = None,
    *,
    reasoning_effort: str | None = None,
    include_reasoning: bool | None = None,
) -> tuple[str, dict[str, Any]]:
    """Single call to an OpenAI-compatible chat/completions endpoint."""
    options = _reasoning_options(
        "groq" if base_url == _OPENAI_COMPAT["groq"]["base_url"] else None,
        model, reasoning_effort, include_reasoning,
    )
    body: dict[str, Any] = {"model": model, "messages": messages, "temperature": temperature, **options}
    if max_output_tokens is not None:
        token_field = "max_completion_tokens" if "api.groq.com" in base_url else "max_tokens"
        body[token_field] = max_output_tokens
    resp = await client.post(
        f"{base_url}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json=body,
        timeout=60.0,
    )
    resp.raise_for_status()
    data = resp.json()
    choices = data.get("choices", [])
    if not choices:
        raise ValueError("OpenAI-compat returned no choices.")
    raw_text = choices[0].get("message", {}).get("content")
    text: str = raw_text if isinstance(raw_text, str) else ""
    if not text and choices[0].get("finish_reason") != "length":
        raise ValueError("OpenAI-compat returned empty content.")
    raw_usage = data.get("usage", {})
    raw_usage = raw_usage if isinstance(raw_usage, dict) else {}
    numeric_keys = {"prompt_tokens", "completion_tokens", "total_tokens", "prompt_time", "completion_time",
                    "queue_time", "total_time"}
    usage: dict[str, Any] = {
        key: value for key, value in raw_usage.items() if key in numeric_keys and
        (type(value) is int and value >= 0 or type(value) is float and math.isfinite(value) and value >= 0)
    }
    details = raw_usage.get("completion_tokens_details")
    reasoning_tokens = details.get("reasoning_tokens") if isinstance(details, dict) else None
    if type(reasoning_tokens) is int and reasoning_tokens >= 0:
        usage["reasoning_tokens"] = reasoning_tokens
    if isinstance(choices[0].get("finish_reason"), str):
        usage["finish_reason"] = choices[0]["finish_reason"]
    if isinstance(data.get("model"), str):
        usage["resolved_model"] = data["model"]
    usage["provider_diagnostics"] = provider_response_diagnostics(resp)
    return text, usage


async def _gemini_call(
    client: httpx.AsyncClient,
    api_key: str,
    model: str,
    messages: list[dict[str, str]],
    temperature: float,
    max_output_tokens: int | None = None,
) -> tuple[str, dict[str, Any]]:
    """Call Google Gemini generateContent API."""
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent"
    )
    system_parts: list[dict[str, Any]] = []
    contents: list[dict[str, Any]] = []
    for msg in messages:
        if msg["role"] == "system":
            system_parts.append({"text": msg["content"]})
        else:
            role = "user" if msg["role"] == "user" else "model"
            contents.append({"role": role, "parts": [{"text": msg["content"]}]})
    body: dict[str, Any] = {
        "contents": contents,
        "generationConfig": {"temperature": temperature},
    }
    if max_output_tokens is not None:
        body["generationConfig"]["maxOutputTokens"] = max_output_tokens
    if system_parts:
        body["systemInstruction"] = {"parts": system_parts}
    resp = await client.post(
        url,
        headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
        json=body,
        timeout=120.0,
    )
    resp.raise_for_status()
    data = resp.json()
    candidates = data.get("candidates", [])
    if not candidates:
        raise ValueError(f"Gemini returned no candidates: {str(data)[:200]}")
    parts = candidates[0].get("content", {}).get("parts", [])
    raw_text = parts[0].get("text") if parts else None
    text: str = raw_text if isinstance(raw_text, str) else ""
    if not text and candidates[0].get("finishReason") != "MAX_TOKENS":
        raise ValueError(f"Gemini returned empty response: {str(data)[:200]}")
    usage_meta = data.get("usageMetadata", {})
    usage: dict[str, Any] = {
        "prompt_tokens": usage_meta.get("promptTokenCount", 0),
        "completion_tokens": usage_meta.get("candidatesTokenCount", 0),
    }
    if isinstance(candidates[0].get("finishReason"), str):
        usage["finish_reason"] = candidates[0]["finishReason"]
    if isinstance(data.get("modelVersion"), str):
        usage["resolved_model"] = data["modelVersion"]
    usage["provider_diagnostics"] = provider_response_diagnostics(resp)
    return text, usage


async def _anthropic_call(
    client: httpx.AsyncClient,
    api_key: str,
    model: str,
    messages: list[dict[str, str]],
    temperature: float,
    max_output_tokens: int | None = None,
) -> tuple[str, dict[str, Any]]:
    """Call Anthropic Messages API."""
    system_text = ""
    api_messages: list[dict[str, str]] = []
    for msg in messages:
        if msg["role"] == "system":
            system_text = msg["content"]
        else:
            api_messages.append({"role": msg["role"], "content": msg["content"]})
    body: dict[str, Any] = {
        "model": model,
        "max_tokens": max_output_tokens if max_output_tokens is not None else 4096,
        "messages": api_messages,
        "temperature": temperature,
    }
    if system_text:
        body["system"] = system_text
    resp = await client.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json=body,
        timeout=120.0,
    )
    resp.raise_for_status()
    data = resp.json()
    content_blocks = data.get("content", [])
    if not content_blocks or not content_blocks[0].get("text"):
        raise ValueError(f"Anthropic returned empty response: {str(data)[:200]}")
    text: str = content_blocks[0]["text"]
    usage: dict[str, Any] = {
        "prompt_tokens": data.get("usage", {}).get("input_tokens", 0),
        "completion_tokens": data.get("usage", {}).get("output_tokens", 0),
    }
    if isinstance(data.get("model"), str):
        usage["resolved_model"] = data["model"]
    usage["provider_diagnostics"] = provider_response_diagnostics(resp)
    return text, usage


def _providers_for_role(role: LLMRole, config: Any) -> list[Any]:
    """Return providers for *role* in priority order, with fallback providers appended."""
    role_str = role.value
    matching: list[Any] = []
    fallback: list[Any] = []
    for p in config.llm.providers:
        if role_str in p.role:
            matching.append(p)
        elif "fallback" in p.role:
            fallback.append(p)
    matching_names = {p.name for p in matching}
    deduped_fallback = [p for p in fallback if p.name not in matching_names]
    return matching + deduped_fallback


def _resolve_routed_providers(
    role: LLMRole, category: str | None, config: Any
) -> list[Any]:
    """Resolve providers respecting per-category routing when available.

    If a routing rule matches the category, the routed provider is tried first,
    followed by the normal role-based fallback chain (deduped).
    """
    role_fallbacks = _providers_for_role(role, config)
    if not category or not hasattr(config.llm, "routing") or not config.llm.routing:
        return role_fallbacks

    for route in config.llm.routing:
        if category in route.categories:
            # Build a synthetic provider config for the routed entry
            from digest.config import ProviderConfig

            routed = ProviderConfig(
                name=route.provider,
                model=route.model,
                role=[role.value],
            )
            # Prepend routed provider, then append role-based fallbacks (deduped)
            seen = {routed.name}
            result = [routed]
            for p in role_fallbacks:
                if p.name not in seen:
                    result.append(p)
                    seen.add(p.name)
            return result

    return role_fallbacks


QuotaAxis = Literal["unknown", "requests", "tokens", "requests_per_minute", "requests_per_day",
                    "tokens_per_minute", "tokens_per_day"]
QuotaAxisSource = Literal["unknown", "error_quota_axis", "error_type"]
_NUMERIC_QUOTA_HEADERS = frozenset({
    "x-ratelimit-limit-requests", "x-ratelimit-limit-tokens",
    "x-ratelimit-remaining-requests", "x-ratelimit-remaining-tokens",
})
_RESET_QUOTA_HEADERS = frozenset({"x-ratelimit-reset-requests", "x-ratelimit-reset-tokens"})
_KNOWN_ERROR_CODES = frozenset({
    "model_not_found", "model_decommissioned", "rate_limit_exceeded", "insufficient_quota",
    "insufficient_balance", "invalid_api_key", "permission_denied", "RESOURCE_EXHAUSTED",
    "UNAVAILABLE", "NOT_FOUND",
})
_QUOTA_AXES: dict[str, QuotaAxis] = {
    "requests": "requests", "tokens": "tokens", "requests_per_minute": "requests_per_minute",
    "requests_per_day": "requests_per_day", "tokens_per_minute": "tokens_per_minute",
    "tokens_per_day": "tokens_per_day", "rpm": "requests_per_minute", "rpd": "requests_per_day",
    "tpm": "tokens_per_minute", "tpd": "tokens_per_day",
}


@dataclass(frozen=True)
class RetryTiming:
    seconds: float
    format: Literal["seconds", "duration", "http_date"]
    server_retry_at: str


@dataclass(frozen=True)
class ProviderResponseDiagnostics:
    """Allowlisted transport observations, independent of editorial success or failure."""

    status_code: int
    error_code: str
    quota_axis: QuotaAxis
    quota_axis_source: QuotaAxisSource
    observed_at: str
    numeric_headers: dict[str, float] = field(default_factory=dict)
    retry_after: RetryTiming | None = None
    reset_headers: dict[str, RetryTiming] = field(default_factory=dict)


# Existing checkpoints serialize fields, not this Python name. Keep old imports compatible.
ProviderFailureDiagnostics = ProviderResponseDiagnostics


class LLMProviderError(RuntimeError):
    """The same failure contract, carrying only this call's sanitized diagnostics."""

    def __init__(self, message: str, diagnostics: ProviderFailureDiagnostics | None = None) -> None:
        super().__init__(message)
        self.diagnostics = diagnostics


def _finite_header_number(value: str) -> float | None:
    if len(value) > 128:
        return None
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _header_timing(value: str, observed_at: datetime, *, allow_duration: bool) -> RetryTiming | None:
    if not value or len(value) > 128:
        return None
    value = value.strip()
    seconds = _finite_header_number(value)
    kind: Literal["seconds", "duration", "http_date"] = "seconds"
    if seconds is None and allow_duration:
        parts = re.findall(r"(\d+(?:\.\d+)?)(ms|s|m|h|d)", value)
        if parts and "".join(number + unit for number, unit in parts) == value:
            units = {"ms": .001, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}
            seconds = sum(float(number) * units[unit] for number, unit in parts)
            kind = "duration"
    if seconds is not None:
        if not math.isfinite(seconds) or seconds < 0:
            return None
        try:
            boundary = observed_at + timedelta(seconds=seconds)
        except (OverflowError, ValueError):
            return None
        return RetryTiming(float(seconds), kind, boundary.isoformat())
    try:
        boundary = parsedate_to_datetime(value)
        if boundary.tzinfo is None:
            return None
        boundary = boundary.astimezone(UTC)
        seconds = max(0.0, (boundary - observed_at).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None
    return RetryTiming(seconds, "http_date", boundary.isoformat())


def _machine_error(response: httpx.Response) -> dict[str, Any]:
    # Examine only a bounded response to derive fixed codes; retain no response prose.
    if len(response.content) > 65536:
        return {}
    try:
        body = response.json()
        error = body.get("error", {}) if isinstance(body, dict) else {}
        return error if isinstance(error, dict) else {}
    except ValueError:
        return {}


def _machine_code(error: dict[str, Any]) -> str:
    for name in ("code", "status", "type"):
        candidate = error.get(name)
        if isinstance(candidate, str) and candidate in _KNOWN_ERROR_CODES:
            return candidate
    return "unknown"


def provider_response_diagnostics(
    response: httpx.Response, observed_at: datetime | None = None,
) -> ProviderResponseDiagnostics:
    """The same observation whitelist on every HTTP response; no retry-policy effects."""
    observed = (observed_at or datetime.now(UTC)).astimezone(UTC)
    error = _machine_error(response) if response.is_error else {}
    axis: QuotaAxis = "unknown"
    source: QuotaAxisSource = "unknown"
    for name, provenance in (("quota_axis", "error_quota_axis"), ("type", "error_type")):
        candidate = error.get(name)
        if isinstance(candidate, str) and candidate.lower() in _QUOTA_AXES:
            axis = _QUOTA_AXES[candidate.lower()]
            source = "error_quota_axis" if provenance == "error_quota_axis" else "error_type"
            break
    numbers = {}
    for name in sorted(_NUMERIC_QUOTA_HEADERS):
        number = _finite_header_number(response.headers.get(name, ""))
        if number is not None:
            numbers[name] = number
    resets = {}
    for name in sorted(_RESET_QUOTA_HEADERS):
        timing = _header_timing(response.headers.get(name, ""), observed, allow_duration=True)
        if timing is not None:
            resets[name] = timing
    return ProviderResponseDiagnostics(
        response.status_code, _machine_code(error), axis, source, observed.isoformat(), numbers,
        _header_timing(response.headers.get("retry-after", ""), observed, allow_duration=False), resets,
    )


def provider_failure_diagnostics(
    exc: Exception, observed_at: datetime | None = None,
) -> ProviderResponseDiagnostics | None:
    """Preserve the existing exception path using the shared response observer."""
    if not isinstance(exc, httpx.HTTPStatusError):
        return None
    return provider_response_diagnostics(exc.response, observed_at)


def validate_response_diagnostics(diagnostic: ProviderResponseDiagnostics) -> None:
    """Keep loaded checkpoints within the same finite, non-secret vocabulary."""
    if (not 100 <= diagnostic.status_code <= 599 or diagnostic.error_code not in _KNOWN_ERROR_CODES | {"unknown"}
            or diagnostic.quota_axis not in set(_QUOTA_AXES.values()) | {"unknown"}
            or diagnostic.quota_axis_source not in {"unknown", "error_quota_axis", "error_type"}
            or not set(diagnostic.numeric_headers) <= _NUMERIC_QUOTA_HEADERS
            or not set(diagnostic.reset_headers) <= _RESET_QUOTA_HEADERS):
        raise ValueError("Invalid sanitized provider diagnostic.")
    if datetime.fromisoformat(diagnostic.observed_at).tzinfo is None:
        raise ValueError("Provider diagnostic timestamp requires timezone.")
    if any(not math.isfinite(value) or value < 0 for value in diagnostic.numeric_headers.values()):
        raise ValueError("Provider diagnostic numbers must be finite and nonnegative.")
    timings = list(diagnostic.reset_headers.values())
    if diagnostic.retry_after is not None:
        timings.append(diagnostic.retry_after)
    for timing in timings:
        if (not math.isfinite(timing.seconds) or timing.seconds < 0
                or timing.format not in {"seconds", "duration", "http_date"}
                or datetime.fromisoformat(timing.server_retry_at).tzinfo is None):
            raise ValueError("Invalid normalized provider retry timing.")


validate_failure_diagnostics = validate_response_diagnostics


@dataclass
class _RequestState:
    """Per-config, per-event-loop request limits shared by all pipeline stages."""

    loop: asyncio.AbstractEventLoop
    semaphore: asyncio.Semaphore
    spacing_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    next_request_at: float = 0.0
    unavailable_until: dict[tuple[str, str], float] = field(default_factory=dict)
    failure_diagnostics: dict[tuple[str, str], ProviderFailureDiagnostics] = field(default_factory=dict)


def _request_state(config: Any) -> _RequestState:
    loop = asyncio.get_running_loop()
    state = getattr(config.llm, "_runtime", None)
    if not isinstance(state, _RequestState) or state.loop is not loop:
        state = _RequestState(
            loop, asyncio.Semaphore(getattr(config.llm, "max_concurrent_requests", 4)),
        )
        config.llm._runtime = state
    return state


async def _pace_request(state: _RequestState, interval: float) -> None:
    async with state.spacing_lock:
        wait = state.next_request_at - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        state.next_request_at = time.monotonic() + interval


async def _call_provider(
    client: httpx.AsyncClient, provider: Any,
    messages: list[dict[str, str]], temperature: float, max_output_tokens: int | None = None,
    *, reasoning_effort: str | None = None, include_reasoning: bool | None = None,
) -> tuple[str, dict[str, Any]] | None:
    options = _reasoning_options(provider.name, provider.model, reasoning_effort, include_reasoning)
    if provider.name == "anthropic":
        env_name = "ANTHROPIC_API_KEY"
    elif provider.name == "gemini":
        env_name = "GEMINI_API_KEY"
    elif provider.name in _OPENAI_COMPAT:
        env_name = _OPENAI_COMPAT[provider.name]["api_key_env"]
    else:
        logger.warning("Unknown provider '%s', skipping", provider.name)
        return None
    api_key = os.environ.get(env_name, "")
    if not api_key:
        logger.warning("%s not set, skipping %s", env_name, provider.name)
        return None
    if provider.name == "anthropic":
        return await _anthropic_call(client, api_key, provider.model, messages, temperature, max_output_tokens)
    if provider.name == "gemini":
        return await _gemini_call(client, api_key, provider.model, messages, temperature, max_output_tokens)
    return await _openai_compat_call(
        client, _OPENAI_COMPAT[provider.name]["base_url"], api_key,
        provider.model, messages, temperature, max_output_tokens, **options,
    )


def _safe_provider_error(exc: Exception) -> str:
    """Log status and a machine code, never response text, prompts or credentials."""
    if not isinstance(exc, httpx.HTTPStatusError):
        return type(exc).__name__
    return f"HTTP {exc.response.status_code} code={_machine_code(_machine_error(exc.response))}"


def _provider_cooldown(state: _RequestState, provider: Any, exc: Exception) -> None:
    """Share server backoff / permanent failures across concurrent stage calls."""
    if not isinstance(exc, httpx.HTTPStatusError):
        return
    key = (provider.name, provider.model)
    if exc.response.status_code in {401, 402, 403, 404}:
        state.unavailable_until[key] = math.inf
    elif exc.response.status_code == 429:
        try:
            delay = float(exc.response.headers.get("retry-after", "60"))
        except ValueError:
            delay = 60.0
        if not math.isfinite(delay) or delay < 0:
            delay = 60.0
        state.unavailable_until[key] = max(
            state.unavailable_until.get(key, 0), time.monotonic() + delay,
        )


def _retry_delay(exc: Exception, attempt: int, max_wait: float) -> float | None:
    """Retry transient errors only. Long server backoffs fall through to fallback."""
    delay = float(2 ** attempt)
    if isinstance(exc, httpx.HTTPStatusError):
        if exc.response.status_code not in {429, 500, 502, 503, 504}:
            return None
        try:
            default_delay = "60" if exc.response.status_code == 429 else "0"
            delay = max(delay, float(exc.response.headers.get("retry-after", default_delay)))
        except ValueError:
            pass
    elif not isinstance(exc, httpx.TransportError):
        return None
    if delay > max_wait:
        return None
    return delay


async def complete(
    role: LLMRole,
    messages: list[dict[str, str]],
    config: Any,
    *,
    temperature: float = 0.3,
    category: str | None = None,
    provider_override: Any | None = None,
    max_output_tokens: int | None = None,
    reasoning_effort: str | None = None,
    include_reasoning: bool | None = None,
) -> tuple[str, dict[str, Any]]:
    """Bounded LLM calls. Explicit model slots never silently fall back."""
    options = _reasoning_options(getattr(provider_override, "name", None),
                                 getattr(provider_override, "model", None), reasoning_effort, include_reasoning)
    providers = ([provider_override] if provider_override is not None
                 else _resolve_routed_providers(role, category, config))
    if not providers:
        raise RuntimeError(
            f"No providers configured for role '{role.value}'. "
            "Check llm.providers[].role in config.yaml."
        )
    state = _request_state(config)
    retries = getattr(config.llm, "max_retries", 0)
    interval = getattr(config.llm, "min_request_interval_seconds", 0.0)
    max_wait = getattr(config.llm, "retry_max_wait_seconds", 60.0)
    last_error = "providers unavailable or credentials missing"
    last_diagnostics: ProviderFailureDiagnostics | None = None
    async with state.semaphore, httpx.AsyncClient() as client:
        for provider in providers:
            if state.unavailable_until.get((provider.name, provider.model), 0) > time.monotonic():
                logger.info("Skipping unavailable provider %s/%s for this run", provider.name, provider.model)
                continue
            for attempt in range(retries + 1):
                await _pace_request(state, interval)
                # A concurrent call may have received a backoff while this one queued.
                if state.unavailable_until.get((provider.name, provider.model), 0) > time.monotonic():
                    break
                t0 = time.monotonic()
                state.failure_diagnostics.pop((provider.name, provider.model), None)
                try:
                    result = await _call_provider(client, provider, messages, temperature, max_output_tokens, **options)
                    if result is None:
                        break
                    text, usage = result
                    if not text and role != LLMRole.REVIEW_EVIDENCE:
                        raise ValueError("Provider exhausted output before producing visible text.")
                    logger.info(
                        "LLM %s/%s role=%s tokens=%s latency=%.1fs",
                        provider.name, provider.model, role.value,
                        usage.get("completion_tokens", "?"), time.monotonic() - t0,
                    )
                    return text, usage
                except (httpx.HTTPError, ValueError) as exc:
                    last_error = _safe_provider_error(exc)
                    last_diagnostics = provider_failure_diagnostics(exc)
                    if last_diagnostics is not None:
                        state.failure_diagnostics[(provider.name, provider.model)] = last_diagnostics
                    _provider_cooldown(state, provider, exc)
                    logger.warning(
                        "Provider %s/%s failed for role %s: %s",
                        provider.name, provider.model, role.value, last_error,
                    )
                    if last_diagnostics is not None:
                        logger.info(
                            "Provider quota observations: axis=%s retry_after=%s resets=%s numeric_headers=%s",
                            last_diagnostics.quota_axis, last_diagnostics.retry_after,
                            last_diagnostics.reset_headers, last_diagnostics.numeric_headers,
                        )
                    delay = _retry_delay(exc, attempt, max_wait)
                    if attempt >= retries or delay is None:
                        break
                    logger.info("Retrying %s in %.1fs", provider.name, delay)
                    await asyncio.sleep(delay)
    raise LLMProviderError(f"All providers failed for role '{role.value}'. Last error: {last_error}",
                           last_diagnostics)


def _extract_json(text: str) -> Any:
    """Extract JSON from LLM response text.

    Handles: raw JSON, JSON in markdown code fences, JSON embedded in text.
    Raises ValueError if no valid JSON is found.
    """
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if fence_match:
        try:
            return json.loads(fence_match.group(1).strip())
        except json.JSONDecodeError:
            pass
    for pattern in (r"\{[\s\S]*\}", r"\[[\s\S]*\]"):
        match = re.search(pattern, text)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass
    raise ValueError(f"No valid JSON found in LLM response: {text[:200]!r}")
