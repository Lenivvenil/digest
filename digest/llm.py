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
from typing import Any

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


async def _openai_compat_call(
    client: httpx.AsyncClient,
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict[str, str]],
    temperature: float,
    max_output_tokens: int | None = None,
) -> tuple[str, dict[str, Any]]:
    """Single call to an OpenAI-compatible chat/completions endpoint."""
    body: dict[str, Any] = {"model": model, "messages": messages, "temperature": temperature}
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
        raise ValueError(f"OpenAI-compat returned no choices: {str(data)[:200]}")
    text: str = choices[0].get("message", {}).get("content", "")
    if not text:
        raise ValueError(f"OpenAI-compat returned empty content: {str(data)[:200]}")
    usage: dict[str, Any] = dict(data.get("usage", {}))
    if isinstance(choices[0].get("finish_reason"), str):
        usage["finish_reason"] = choices[0]["finish_reason"]
    if isinstance(data.get("model"), str):
        usage["resolved_model"] = data["model"]
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
    if not parts or not parts[0].get("text"):
        raise ValueError(f"Gemini returned empty response: {str(data)[:200]}")
    text: str = parts[0]["text"]
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


@dataclass
class _RequestState:
    """Per-config, per-event-loop request limits shared by all pipeline stages."""

    loop: asyncio.AbstractEventLoop
    semaphore: asyncio.Semaphore
    spacing_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    next_request_at: float = 0.0
    unavailable_until: dict[tuple[str, str], float] = field(default_factory=dict)


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
) -> tuple[str, dict[str, Any]] | None:
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
        provider.model, messages, temperature, max_output_tokens,
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
) -> tuple[str, dict[str, Any]]:
    """Bounded LLM calls. Explicit model slots never silently fall back."""
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
                try:
                    result = await _call_provider(client, provider, messages, temperature, max_output_tokens)
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
                    delay = _retry_delay(exc, attempt, max_wait)
                    if attempt >= retries or delay is None:
                        break
                    logger.info("Retrying %s in %.1fs", provider.name, delay)
                    await asyncio.sleep(delay)
    raise RuntimeError(f"All providers failed for role '{role.value}'. Last error: {last_error}")


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
