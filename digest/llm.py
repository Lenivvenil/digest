"""Thin httpx-based LLM client with role-based dispatch and provider fallback.

Supports OpenAI-compatible APIs (Groq, DeepSeek) and Google Gemini.
Provider selection is driven by LLMRole — each provider in config declares
which roles it handles. On error (429/5xx/timeout), the next provider is tried.
"""

from __future__ import annotations

import asyncio
import enum
import logging
import math
import os
import time
from typing import Any

import httpx

from digest._serialization import extract_json
from digest.adapters.models.execution import ModelExecution, RequestState

# Compatibility export; JSON parsing is owned by the pure serialization module.
_extract_json = extract_json

logger = logging.getLogger(__name__)

# Runtime capability guard: both counting and generation reserve the shared cycle.
MODEL_BUDGET_PROTOCOL = 1

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


def _request_timeout(default: float, requested: float | None) -> float:
    """Allow a caller's deadline to shorten, never extend, the HTTP timeout."""
    if requested is None:
        return default
    if isinstance(requested, bool) or not math.isfinite(requested) or requested <= 0:
        raise ValueError("LLM request timeout must be finite and greater than zero")
    return min(default, requested)


def _validate_review_output_controls(
    provider: str, model: str, reasoning_effort: str | None, response_format: dict[str, Any] | None,
) -> None:
    """Keep opt-in review controls confined to the supported Groq model."""
    if reasoning_effort is None and response_format is None:
        return
    if provider != "groq" or model != "openai/gpt-oss-120b":
        raise ValueError("Review output controls require Groq openai/gpt-oss-120b")
    if reasoning_effort is not None and reasoning_effort != "low":
        raise ValueError("Review reasoning_effort must be low or None")
    if response_format is not None:
        schema = response_format.get("json_schema") if isinstance(response_format, dict) else None
        if (not isinstance(response_format, dict) or response_format.get("type") != "json_schema"
                or not isinstance(schema, dict) or not isinstance(schema.get("name"), str)
                or not schema["name"].strip() or schema.get("strict") is not True
                or not isinstance(schema.get("schema"), dict)):
            raise ValueError("Review response_format requires a named strict JSON schema")


def openai_request_body(
    model: str, messages: list[dict[str, str]], temperature: float,
    max_output_tokens: int | None = None, *, groq: bool = False,
    reasoning_effort: str | None = None,
    response_format: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Share the complete wire payload with full-source admission accounting."""
    _validate_review_output_controls("groq" if groq else "", model, reasoning_effort, response_format)
    body: dict[str, Any] = {"model": model, "messages": messages, "temperature": temperature}
    if max_output_tokens is not None:
        token_field = "max_completion_tokens" if groq else "max_tokens"
        body[token_field] = max_output_tokens
    if reasoning_effort is not None:
        body["reasoning_effort"] = reasoning_effort
    if response_format is not None:
        body["response_format"] = response_format
    return body


async def _openai_compat_call(
    client: httpx.AsyncClient,
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict[str, str]],
    temperature: float,
    max_output_tokens: int | None = None,
    *,
    request_timeout_seconds: float | None = None,
    reasoning_effort: str | None = None,
    response_format: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Single call to an OpenAI-compatible chat/completions endpoint."""
    control_kwargs: dict[str, Any] = {}
    if reasoning_effort is not None:
        control_kwargs["reasoning_effort"] = reasoning_effort
    if response_format is not None:
        control_kwargs["response_format"] = response_format
    body = openai_request_body(
        model, messages, temperature, max_output_tokens, groq="api.groq.com" in base_url, **control_kwargs,
    )
    resp = await client.post(
        f"{base_url}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json=body,
        timeout=_request_timeout(60.0, request_timeout_seconds),
    )
    resp.raise_for_status()
    data = resp.json()
    choices = data.get("choices", [])
    if not choices:
        raise ValueError(f"OpenAI-compat returned no choices: {str(data)[:200]}")
    text: str = choices[0].get("message", {}).get("content", "")
    if not text:
        raise ValueError(f"OpenAI-compat returned empty content: {str(data)[:200]}")
    usage: dict[str, Any] = dict(data.get("usage", {}))
    if reasoning_effort is not None or response_format is not None:
        for kind in ("limit", "remaining"):
            for unit in ("requests", "tokens"):
                value = resp.headers.get(f"x-ratelimit-{kind}-{unit}")
                if value is not None and value.isascii() and value.isdecimal():
                    try:
                        usage[f"rate_limit_{kind}_{unit}"] = int(value)
                    except ValueError:
                        pass  # Oversized numeric headers must not invalidate a completed review.
    if isinstance(choices[0].get("finish_reason"), str):
        usage["finish_reason"] = choices[0]["finish_reason"]
    if isinstance(data.get("model"), str):
        usage["resolved_model"] = data["model"]
    return text, usage


def gemini_request_body(
    messages: list[dict[str, str]],
    temperature: float,
    max_output_tokens: int | None = None,
) -> dict[str, Any]:
    """Build the exact Gemini input shared by generation and token preflight."""
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
    return body


def _gemini_final_text(candidate: Any) -> str:
    """Concatenate ordered final text like google-genai; never expose hidden parts.

    https://github.com/googleapis/python-genai/blob/main/google/genai/types.py
    Unsupported tool/media output fails closed instead of returning a partial answer.
    """
    if not isinstance(candidate, dict) or not isinstance(candidate.get("content"), dict):
        raise ValueError("Gemini returned invalid content.")
    parts = candidate["content"].get("parts")
    if not isinstance(parts, list) or not parts:
        raise ValueError("Gemini returned no final text.")
    metadata = {"thought", "thoughtSignature", "thought_signature", "partMetadata"}
    texts = []
    for part in parts:
        if not isinstance(part, dict) or not part or set(part) - (metadata | {"text"}):
            raise ValueError("Gemini returned unsupported response parts.")
        if "thought" in part and type(part["thought"]) is not bool:
            raise ValueError("Gemini returned invalid thought metadata.")
        if part.get("thought") is True:
            continue
        if "text" not in part:
            continue  # Opaque signature/metadata-only parts have no final text.
        if not isinstance(part["text"], str):
            raise ValueError("Gemini returned invalid final text.")
        texts.append(part["text"])
    text = "".join(texts)
    if not text.strip():
        raise ValueError("Gemini returned no final text.")
    return text


async def _gemini_call(
    client: httpx.AsyncClient,
    api_key: str,
    model: str,
    messages: list[dict[str, str]],
    temperature: float,
    max_output_tokens: int | None = None,
    *,
    request_timeout_seconds: float | None = None,
) -> tuple[str, dict[str, Any]]:
    """Call Google Gemini generateContent API."""
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent"
    )
    body = gemini_request_body(messages, temperature, max_output_tokens)
    resp = await client.post(
        url,
        headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
        json=body,
        timeout=_request_timeout(120.0, request_timeout_seconds),
    )
    resp.raise_for_status()
    data = resp.json()
    candidates = data.get("candidates") if isinstance(data, dict) else None
    if not isinstance(candidates, list) or not candidates:
        raise ValueError("Gemini returned no candidates.")
    text = _gemini_final_text(candidates[0])
    usage_meta = data.get("usageMetadata", {})
    usage: dict[str, Any] = {
        "prompt_tokens": usage_meta.get("promptTokenCount", 0),
        "completion_tokens": usage_meta.get("candidatesTokenCount", 0),
    }
    if isinstance(candidates[0].get("finishReason"), str):
        usage["finish_reason"] = candidates[0]["finishReason"]
    if isinstance(data.get("modelVersion"), str):
        usage["resolved_model"] = data["modelVersion"]
    return text, usage


async def _anthropic_call(
    client: httpx.AsyncClient,
    api_key: str,
    model: str,
    messages: list[dict[str, str]],
    temperature: float,
    max_output_tokens: int | None = None,
    *,
    request_timeout_seconds: float | None = None,
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
        timeout=_request_timeout(120.0, request_timeout_seconds),
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
    if isinstance(data.get("stop_reason"), str):
        usage["finish_reason"] = "stop" if data["stop_reason"] == "end_turn" else data["stop_reason"]
    if isinstance(data.get("model"), str):
        usage["resolved_model"] = data["model"]
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


def _request_state(config: Any, execution: ModelExecution) -> RequestState:
    return execution.request_state(config.llm)


def set_request_limit(config: Any, execution: ModelExecution, limit: int) -> None:
    """Cap attempts for this execution/event loop without resetting spent requests."""
    if type(limit) is not int or limit < 0:
        raise ValueError("LLM request limit must be a nonnegative integer")
    _request_state(config, execution).request_limit = limit


def request_budget_remaining(config: Any, execution: ModelExecution) -> int | None:
    """Return remaining attempts, or None for the default unlimited runtime."""
    state = _request_state(config, execution)
    from digest.model_budget import ModelBudgetError, execution_from_env

    local = max(0, state.request_limit - state.requests_attempted) if state.request_limit is not None else None
    try:
        shared = execution_from_env()
    except ModelBudgetError:
        return 0  # The reservation point still raises the precise error before dispatch.
    return min(local, shared.remaining) if local is not None and shared is not None else (
        shared.remaining if shared is not None else local)


def request_wait_seconds(config: Any, execution: ModelExecution) -> float:
    """Return current pacing delay so a caller can respect its own deadline."""
    state = _request_state(config, execution)
    interval = getattr(config.llm, "min_request_interval_seconds", 0.0)
    _sync_cycle_pacing(state)
    return max(0.0, _pacing_deadline(state, interval) - time.monotonic())


def _reserve_request(
    state: RequestState, provider: str = "unspecified", model: str = "unspecified", kind: str = "generate",
) -> None:
    """Reserve before dispatch; even missing credentials consume an attempt.

    No await occurs between checking and incrementing the per-loop counter.
    """
    if state.request_limit is not None and state.requests_attempted >= state.request_limit:
        raise RuntimeError("LLM request budget exhausted")
    from digest.model_budget import reserve_request_from_env

    reserve_request_from_env(provider=provider, model=model, kind=kind)
    state.requests_attempted += 1


def _sync_cycle_pacing(state: RequestState) -> None:
    from digest.model_budget import execution_from_env

    shared = execution_from_env()
    if shared is not None and shared.last_reserved_at is not None:
        elapsed = max(0.0, time.time() - shared.last_reserved_at.timestamp())
        previous = time.monotonic() - elapsed
        state.last_request_at = max(state.last_request_at or previous, previous)


def _pacing_deadline(state: RequestState, interval: float) -> float:
    # A stricter stage interval applies to the preceding shared request too.
    current_floor = state.last_request_at + interval if state.last_request_at is not None else 0.0
    return max(state.next_request_at, current_floor)


async def _pace_request(state: RequestState, interval: float) -> None:
    async with state.spacing_lock:
        _sync_cycle_pacing(state)
        wait = _pacing_deadline(state, interval) - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        state.last_request_at = time.monotonic()
        state.next_request_at = state.last_request_at + interval


async def _call_provider(
    client: httpx.AsyncClient, provider: Any,
    messages: list[dict[str, str]], temperature: float, max_output_tokens: int | None = None,
    *,
    request_timeout_seconds: float | None = None,
    reasoning_effort: str | None = None,
    response_format: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any]] | None:
    _validate_review_output_controls(provider.name, provider.model, reasoning_effort, response_format)
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
    timeout_kwargs: dict[str, Any] = ({"request_timeout_seconds": request_timeout_seconds}
                                      if request_timeout_seconds is not None else {})
    if provider.name == "anthropic":
        return await _anthropic_call(
            client, api_key, provider.model, messages, temperature, max_output_tokens, **timeout_kwargs,
        )
    if provider.name == "gemini":
        return await _gemini_call(
            client, api_key, provider.model, messages, temperature, max_output_tokens, **timeout_kwargs,
        )
    control_kwargs: dict[str, Any] = {}
    if reasoning_effort is not None:
        control_kwargs["reasoning_effort"] = reasoning_effort
    if response_format is not None:
        control_kwargs["response_format"] = response_format
    return await _openai_compat_call(
        client, _OPENAI_COMPAT[provider.name]["base_url"], api_key,
        provider.model, messages, temperature, max_output_tokens, **timeout_kwargs, **control_kwargs,
    )


def _safe_provider_error(exc: Exception) -> str:
    """Log status and a machine code, never response text, prompts or credentials."""
    if not isinstance(exc, httpx.HTTPStatusError):
        return type(exc).__name__
    code = "unknown"
    try:
        body = exc.response.json()
        error = body.get("error", {}) if isinstance(body, dict) else {}
        if isinstance(error, dict):
            candidate = error.get("code") or error.get("status") or error.get("type")
            known_codes = {
                "model_not_found", "model_decommissioned", "rate_limit_exceeded",
                "insufficient_quota", "insufficient_balance", "invalid_api_key",
                "permission_denied", "RESOURCE_EXHAUSTED", "UNAVAILABLE", "NOT_FOUND",
            }
            if isinstance(candidate, str) and candidate in known_codes:
                code = candidate
    except ValueError:
        pass
    return f"HTTP {exc.response.status_code} code={code}"


def _provider_cooldown(state: RequestState, provider: Any, exc: Exception) -> None:
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


async def count_gemini_tokens(
    messages: list[dict[str, str]],
    config: Any,
    *,
    execution: ModelExecution,
    provider_override: Any,
    temperature: float = 0.1,
    max_output_tokens: int = 2048,
) -> int:
    """Count the exact Gemini request once, sharing pacing and attempt limits.

    See https://ai.google.dev/api/tokens for generateContentRequest semantics.
    Provider errors fail closed without retries, fallback, or response-body logs.
    """
    if provider_override.name != "gemini":
        raise ValueError("Token preflight requires the configured Gemini provider")
    if not messages or not any(message["content"].strip() for message in messages):
        raise ValueError("Token preflight requires a nonempty request")
    provider = provider_override
    body = gemini_request_body(messages, temperature, max_output_tokens)
    body["model"] = f"models/{provider.model}"
    state = _request_state(config, execution)
    interval = getattr(config.llm, "min_request_interval_seconds", 0.0)
    key = (provider.name, provider.model)
    async with state.semaphore, httpx.AsyncClient() as client:
        if state.unavailable_until.get(key, 0) > time.monotonic():
            raise RuntimeError("Gemini token preflight provider is unavailable for this run")
        await _pace_request(state, interval)
        if state.unavailable_until.get(key, 0) > time.monotonic():
            raise RuntimeError("Gemini token preflight provider is unavailable for this run")
        _reserve_request(state, provider.name, provider.model, "count")
        api_key = os.environ.get("GEMINI_API_KEY", "")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY not set for token preflight")
        try:
            response = await client.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{provider.model}:countTokens",
                headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
                json={"generateContentRequest": body},
                timeout=10.0,
            )
            response.raise_for_status()
            data = response.json()
            count = data.get("totalTokens") if isinstance(data, dict) else None
            if type(count) is not int or count <= 0:
                raise ValueError("Gemini token preflight returned an invalid token count")
        except (httpx.HTTPError, ValueError) as exc:
            _provider_cooldown(state, provider, exc)
            raise RuntimeError(f"Gemini token preflight failed: {_safe_provider_error(exc)}") from None
    return count


async def complete(
    role: LLMRole,
    messages: list[dict[str, str]],
    config: Any,
    *,
    execution: ModelExecution,
    temperature: float = 0.3,
    category: str | None = None,
    provider_override: Any | None = None,
    max_output_tokens: int | None = None,
    request_timeout_seconds: float | None = None,
    reasoning_effort: str | None = None,
    response_format: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Bounded LLM calls. Explicit model slots never silently fall back."""
    control_kwargs: dict[str, Any] = {}
    if reasoning_effort is not None or response_format is not None:
        if role != LLMRole.REVIEW_EVIDENCE or provider_override is None:
            raise ValueError("Review output controls require REVIEW_EVIDENCE and an explicit provider_override")
        _validate_review_output_controls(
            provider_override.name, provider_override.model, reasoning_effort, response_format,
        )
        if reasoning_effort is not None:
            control_kwargs["reasoning_effort"] = reasoning_effort
        if response_format is not None:
            control_kwargs["response_format"] = response_format
    timeout_kwargs: dict[str, Any] = ({"request_timeout_seconds": _request_timeout(120.0, request_timeout_seconds)}
                                      if request_timeout_seconds is not None else {})
    providers = ([provider_override] if provider_override is not None
                 else _resolve_routed_providers(role, category, config))
    if not providers:
        raise RuntimeError(
            f"No providers configured for role '{role.value}'. "
            "Check llm.providers[].role in config.yaml."
        )
    state = _request_state(config, execution)
    retries = getattr(config.llm, "max_retries", 0)
    interval = getattr(config.llm, "min_request_interval_seconds", 0.0)
    max_wait = getattr(config.llm, "retry_max_wait_seconds", 60.0)
    last_error = "providers unavailable or credentials missing"
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
                _reserve_request(state, provider.name, provider.model, "generate")
                t0 = time.monotonic()
                try:
                    result = await _call_provider(
                        client, provider, messages, temperature, max_output_tokens, **timeout_kwargs, **control_kwargs,
                    )
                    if result is None:
                        break
                    text, usage = result
                    logger.info(
                        "LLM %s/%s role=%s tokens=%s latency=%.1fs",
                        provider.name, provider.model, role.value,
                        usage.get("completion_tokens", "?"), time.monotonic() - t0,
                    )
                    return text, usage
                except (httpx.HTTPError, ValueError) as exc:
                    last_error = _safe_provider_error(exc)
                    _provider_cooldown(state, provider, exc)
                    logger.warning(
                        "Provider %s/%s failed for role %s: %s",
                        provider.name, provider.model, role.value, last_error,
                    )
                    if request_timeout_seconds is not None and isinstance(exc, httpx.TimeoutException):
                        break
                    delay = _retry_delay(exc, attempt, max_wait)
                    if attempt >= retries or delay is None:
                        break
                    logger.info("Retrying %s in %.1fs", provider.name, delay)
                    await asyncio.sleep(delay)
    raise RuntimeError(f"All providers failed for role '{role.value}'. Last error: {last_error}")
