"""Shared full-request admission profiles; capability evidence is not account quota.

Admission never generates, retries or selects a fallback. The caller must dispatch
the same provider, model and complete wire request that the evidence identifies.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import time
from dataclasses import dataclass, field, replace
from typing import Any, Literal

import httpx

from digest import llm
from digest.adapters.models.execution import ModelExecution
from digest.config import Config, ProviderConfig
from digest.model_budget import ModelBudgetError
from digest.reading_brief_state import Route
from digest.reading_brief_tokens import ESTIMATOR_VERSION, GPT_HASH, TokenProfileUnavailable, count_gpt_input

# Existing reading-brief profiles only; these are not remaining account quota.
# https://ai.google.dev/gemini-api/docs/models/gemini-3.8-flash
INPUT_LIMITS = {("gemini", "gemini-3.8-flash"): 1_048_576}
# https://console.groq.com/docs/models and https://console.groq.com/docs/rate-limits
GROQ_CONTEXT = {"openai/gpt-oss-120b": 131_072}
GROQ_REQUEST_ALLOWANCE = 8_000
GROQ_FRAMING_RESERVE = 256
GROQ_MIN_INTERVAL_SECONDS = 65.0
COUNT_SECONDS = 10.0


@dataclass(frozen=True)
class RequestAdmission:
    """Safe, request-bound evidence, with no raw source or provider error text."""

    provider: str
    model: str
    request_sha256: str
    output_reserve: int
    input_limit: int | None
    status: Literal["admitted", "oversized", "unverified"] = "unverified"
    method: Literal["exact", "estimated", "unverified"] = "unverified"
    exact_count: int | None = None
    input_estimate: int | None = None
    evidence: dict[str, int | str] = field(default_factory=dict)
    error_class: str | None = None

    @property
    def admitted(self) -> bool:
        return self.status == "admitted"


def request_interval(provider: str, configured: float) -> float:
    """Apply the existing Groq envelope spacing, not a universal provider quota."""
    return max(configured, GROQ_MIN_INTERVAL_SECONDS) if provider == "groq" else configured


def route_profile(provider: str, model: str, output: int) -> Route | None:
    if provider == "gemini" and (provider, model) in INPUT_LIMITS:
        return Route(provider, model, INPUT_LIMITS[provider, model], output)
    if provider == "groq" and model in GROQ_CONTEXT:
        allowance = min(GROQ_CONTEXT[model], GROQ_REQUEST_ALLOWANCE) - output
        if allowance > 0:
            return Route(provider, model, allowance, output)
    return None


def wire_request(
    route: Route, messages: list[dict[str, str]], temperature: float = 0.1,
) -> dict[str, Any]:
    if route.provider == "gemini":
        return llm.gemini_request_body(messages, temperature, route.max_output_tokens)
    if route.provider == "groq" and route.model in GROQ_CONTEXT:
        return llm.openai_request_body(route.model, messages, temperature, route.max_output_tokens, groq=True)
    raise ValueError("unknown_reading_profile")


def estimate_record(route: Route, count: int) -> dict[str, int | str]:
    return {"method": ESTIMATOR_VERSION, "tokenizer_sha256": GPT_HASH, "local_input_count": count,
            "input_estimate": (count * 6 + 4) // 5 + GROQ_FRAMING_RESERVE,
            "framing_reserve": GROQ_FRAMING_RESERVE, "output_reserve": route.max_output_tokens,
            "request_allowance": min(GROQ_CONTEXT[route.model], GROQ_REQUEST_ALLOWANCE)}


def estimate_request(route: Route, messages: list[dict[str, str]]) -> dict[str, int | str]:
    # Provider framing is estimated: 20% plus 256 tokens over pinned local
    # content/minimal Harmony counting; the route reserves output separately.
    return estimate_record(route, count_gpt_input(messages))


def request_sha256(route: Route, messages: list[dict[str, str]], temperature: float = 0.1) -> str:
    """Bind the actual provider/model and exact serialized generation body."""
    # httpx's serializer is also used by llm generation. Gemini's actual model
    # lives in its endpoint, so bind provider/model before the exact body bytes.
    header = json.dumps([route.provider, route.model], ensure_ascii=False, separators=(",", ":")).encode()
    encoded = httpx.Request("POST", "https://request.invalid", json=wire_request(route, messages, temperature)).content
    return hashlib.sha256(header + b"\n" + encoded).hexdigest()


async def admit_request(
    messages: list[dict[str, str]], config: Config, *, provider_override: ProviderConfig,
    temperature: float, max_output_tokens: int, deadline: float, execution: ModelExecution,
) -> RequestAdmission:
    """Admit one actual route, or return safe unverified/oversized evidence.

    ``deadline`` is an absolute monotonic deadline. An exact count uses the
    existing LLM request counter and pacing, including its no-retry count path.
    """
    provider = ProviderConfig(provider_override.name, provider_override.model)
    admission = RequestAdmission(provider.name, provider.model, "", max_output_tokens, None)
    if (type(max_output_tokens) is not int or max_output_tokens <= 0
            or not isinstance(temperature, (int, float)) or isinstance(temperature, bool)
            or not math.isfinite(temperature)
            or not isinstance(deadline, (int, float)) or isinstance(deadline, bool) or not math.isfinite(deadline)
            or not messages
            or any(set(message) != {"role", "content"} or message["role"] not in {"system", "user", "assistant"}
                   or not isinstance(message["content"], str) for message in messages)
            or not any(message["content"].strip() for message in messages)):
        return replace(admission, error_class="technical_invalid_request")
    route = route_profile(provider.name, provider.model, max_output_tokens)
    if route is None:
        return replace(admission, error_class="technical_unknown_profile")
    # Snapshot the request before the count yields to another task.
    request_messages = [dict(message) for message in messages]
    admission = replace(admission, input_limit=route.input_tokens,
                        request_sha256=request_sha256(route, request_messages, temperature),
                        method="exact" if route.provider == "gemini" else "estimated")
    if time.monotonic() >= deadline:
        return replace(admission, error_class="technical_deadline")
    try:
        remaining = llm.request_budget_remaining(config, execution)
        # Preserve generation capacity under the existing shared request ceiling;
        # Gemini also needs one slot for its exact count.
        required_requests = 2 if route.provider == "gemini" else 1
        if remaining is not None and remaining < required_requests:
            return replace(admission, error_class="technical_request_budget")
        if route.provider == "groq":
            record = estimate_request(route, request_messages)
            count = int(record["input_estimate"])
            admission = replace(admission, input_estimate=count, evidence=record)
        else:
            wait = llm.request_wait_seconds(config, execution)
            if time.monotonic() + wait + COUNT_SECONDS >= deadline:
                return replace(admission, error_class="technical_deadline")
            async with asyncio.timeout(min(wait + COUNT_SECONDS, max(0, deadline - time.monotonic()))):
                count = await llm.count_gemini_tokens(
                    request_messages, config, execution=execution, provider_override=provider, temperature=temperature,
                    max_output_tokens=max_output_tokens,
                )
            if type(count) is not int or count <= 0:
                return replace(admission, error_class="technical_count_unknown")
            admission = replace(admission, exact_count=count)
    except TokenProfileUnavailable:
        return replace(admission, error_class="technical_tokenizer_profile")
    except ModelBudgetError:
        return replace(admission, error_class="technical_request_budget")
    except TimeoutError:
        return replace(admission, error_class="technical_deadline")
    except (OSError, RuntimeError, ValueError, TypeError, KeyError, httpx.HTTPError):
        return replace(admission, error_class="technical_count_unknown")
    if time.monotonic() >= deadline:
        return replace(admission, error_class="technical_deadline")
    if count > route.input_tokens:
        return replace(admission, status="oversized", error_class="technical_admission_capacity")
    return replace(admission, status="admitted")
