"""Thin httpx-based LLM client with role-based dispatch and provider fallback.

Supports OpenAI-compatible APIs (Groq, DeepSeek) and Google Gemini.
Provider selection is driven by LLMRole — each provider in config declares
which roles it handles. On error (429/5xx/timeout), the next provider is tried.
"""

from __future__ import annotations

import enum
import json
import logging
import os
import re
import time
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


async def _openai_compat_call(
    client: httpx.AsyncClient,
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict[str, str]],
    temperature: float,
) -> tuple[str, dict[str, Any]]:
    """Single call to an OpenAI-compatible chat/completions endpoint."""
    resp = await client.post(
        f"{base_url}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"model": model, "messages": messages, "temperature": temperature},
        timeout=60.0,
    )
    resp.raise_for_status()
    data = resp.json()
    text: str = data["choices"][0]["message"]["content"]
    usage: dict[str, Any] = data.get("usage", {})
    return text, usage


async def _gemini_call(
    client: httpx.AsyncClient,
    api_key: str,
    model: str,
    messages: list[dict[str, str]],
    temperature: float,
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
    text = data["candidates"][0]["content"]["parts"][0]["text"]
    usage_meta = data.get("usageMetadata", {})
    usage: dict[str, Any] = {
        "prompt_tokens": usage_meta.get("promptTokenCount", 0),
        "completion_tokens": usage_meta.get("candidatesTokenCount", 0),
    }
    return text, usage


async def _anthropic_call(
    client: httpx.AsyncClient,
    api_key: str,
    model: str,
    messages: list[dict[str, str]],
    temperature: float,
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
        "max_tokens": 4096,
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
    text = data["content"][0]["text"]
    usage: dict[str, Any] = {
        "prompt_tokens": data.get("usage", {}).get("input_tokens", 0),
        "completion_tokens": data.get("usage", {}).get("output_tokens", 0),
    }
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
            from src.config import ProviderConfig

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


async def complete(
    role: LLMRole,
    messages: list[dict[str, str]],
    config: Any,
    *,
    temperature: float = 0.3,
    category: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Call LLM for *role*, trying providers in priority order with fallback.

    If *category* is provided and a routing rule matches, the routed provider
    is tried first before falling back to the normal role-based chain.

    Returns (response_text, usage_dict).
    Raises RuntimeError if all providers fail.
    """
    providers = _resolve_routed_providers(role, category, config)
    if not providers:
        raise RuntimeError(
            f"No providers configured for role '{role.value}'. "
            "Check llm.providers[].role in config.yaml."
        )
    async with httpx.AsyncClient() as client:
        last_error: Exception | None = None
        for provider in providers:
            t0 = time.monotonic()
            try:
                if provider.name == "anthropic":
                    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
                    if not api_key:
                        logger.warning("ANTHROPIC_API_KEY not set, skipping anthropic")
                        continue
                    text, usage = await _anthropic_call(
                        client, api_key, provider.model, messages, temperature
                    )
                elif provider.name == "gemini":
                    api_key = os.environ.get("GEMINI_API_KEY", "")
                    if not api_key:
                        logger.warning("GEMINI_API_KEY not set, skipping gemini")
                        continue
                    text, usage = await _gemini_call(
                        client, api_key, provider.model, messages, temperature
                    )
                elif provider.name in _OPENAI_COMPAT:
                    meta = _OPENAI_COMPAT[provider.name]
                    api_key = os.environ.get(meta["api_key_env"], "")
                    if not api_key:
                        logger.warning(
                            "%s not set, skipping %s",
                            meta["api_key_env"],
                            provider.name,
                        )
                        continue
                    text, usage = await _openai_compat_call(
                        client,
                        meta["base_url"],
                        api_key,
                        provider.model,
                        messages,
                        temperature,
                    )
                else:
                    logger.warning("Unknown provider '%s', skipping", provider.name)
                    continue
                elapsed = time.monotonic() - t0
                completion_tokens = usage.get("completion_tokens") or usage.get(
                    "candidatesTokenCount", "?"
                )
                logger.info(
                    "LLM %s/%s role=%s tokens=%s latency=%.1fs",
                    provider.name,
                    provider.model,
                    role.value,
                    completion_tokens,
                    elapsed,
                )
                return text, usage
            except (
                httpx.HTTPStatusError,
                httpx.TimeoutException,
                httpx.RequestError,
            ) as exc:
                elapsed = time.monotonic() - t0
                logger.warning(
                    "Provider %s failed for role %s after %.1fs: %s",
                    provider.name,
                    role.value,
                    elapsed,
                    exc,
                )
                last_error = exc
                continue
    raise RuntimeError(
        f"All providers failed for role '{role.value}'. Last error: {last_error}"
    )


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
