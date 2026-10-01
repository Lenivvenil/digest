"""Conservative, opt-in pacing for the isolated publication worker only."""
from __future__ import annotations

import math
import time
from datetime import datetime

from digest.config import Config, ReviewModelConfig
from digest.editorial_state import Attempt, EditorialState
from digest.llm import ProviderResponseDiagnostics

FALLBACK_SECONDS = 65.0
TELEMETRY_MAX_AGE_SECONDS = 120.0


def timestamp(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.timestamp() if parsed.tzinfo is not None else 0.0
    except ValueError:
        return 0.0


def _events(state: EditorialState, provider: str) -> list[tuple[float, str, Attempt]]:
    events = []
    for article in state.articles.values():
        for work in article.publications.values():
            for attempt in work.attempts:
                writing = attempt.stage in {"draft", "repair"}
                name = work.writer_provider if writing else work.verifier_provider
                model = work.writer_model if writing else work.verifier_model
                if name == provider:
                    events.append((timestamp(attempt.started_at), model, attempt))
    return sorted(events, key=lambda item: item[0])


def _observation(attempt: Attempt, now: float) -> ProviderResponseDiagnostics | None:
    observation = attempt.provider_diagnostics
    if observation is None or not 200 <= observation.status_code < 300:
        return None
    age = now - timestamp(observation.observed_at)
    if not 0 <= age <= TELEMETRY_MAX_AGE_SECONDS:
        return None
    return observation


def _header_boundary(observation: ProviderResponseDiagnostics, reserve: int, now: float) -> float | None:
    numeric = observation.numeric_headers
    limit = numeric.get("x-ratelimit-limit-tokens")
    tokens = numeric.get("x-ratelimit-remaining-tokens")
    requests = numeric.get("x-ratelimit-remaining-requests")
    if limit is not None and limit < reserve:
        return math.inf
    if limit is None or tokens is None or requests is None:
        return None
    ready = now
    for header, exhausted in (("x-ratelimit-reset-tokens", tokens < reserve),
                              ("x-ratelimit-reset-requests", requests < 1)):
        if exhausted:
            reset = observation.reset_headers.get(header)
            boundary = timestamp(reset.server_retry_at) if reset else 0.0
            if not boundary:
                return None
            ready = max(ready, boundary)
    return ready


def next_request_time(state: EditorialState, config: Config, route: ReviewModelConfig,
                      reserve: int, *, now: float | None = None) -> tuple[float, str]:
    now = time.time() if now is None else now
    settings = config.enrichment
    assert settings is not None
    blocked = max(now, timestamp(state.provider_unavailable_until.get(route.provider)),
                  timestamp(state.provider_next_eligible.get(route.provider)))
    events = _events(state, route.provider)
    if not events:
        return blocked, "first request; configured cooldown preserved"
    last_time, last_model, last_attempt = events[-1]
    floor = max(config.llm.min_request_interval_seconds, 60 / settings.requests_per_minute)
    blocked = max(blocked, last_time + floor)
    fallback = max(blocked, last_time + max(floor, FALLBACK_SECONDS))
    if settings.pacing != "provider_aware":
        return fallback, "fixed enrichment spacing; operator floor preserved"
    matching = [item for item in events if item[1] == route.model]
    known = matching[-1][2].provider_diagnostics if matching else None
    if known is not None:
        limit = known.numeric_headers.get("x-ratelimit-limit-tokens")
        if limit is not None and limit < reserve:
            return math.inf, "request exceeds last observed route token limit; technical pending"
        if known.numeric_headers.get("x-ratelimit-remaining-requests") == 0:
            reset = known.reset_headers.get("x-ratelimit-reset-requests")
            if reset is not None and timestamp(reset.server_retry_at) > now:
                return max(blocked, timestamp(reset.server_retry_at)), "server RPD reset boundary preserved"
            if reset is None:
                return (max(blocked, timestamp(known.observed_at) + 86400),
                        "RPD exhausted; local 24h fallback, reset unknown")
    target = _observation(matching[-1][2], now) if matching else None
    latest = _observation(last_attempt, now)
    # Both target-route knowledge and latest shared-provider consumption must be fresh.
    # Never add model/key budgets together as if they were independent organization pools.
    if target is None or latest is None:
        return fallback, "missing/stale route telemetry; conservative fallback"
    boundaries = [_header_boundary(item, reserve, now) for item in (target, latest)]
    if any(value is None for value in boundaries):
        return fallback, "incomplete quota/reset telemetry; conservative fallback"
    ready = max(blocked, *(value for value in boundaries if value is not None))
    return ready, f"provider-aware reset bounds from {route.provider}/{last_model}; operator/RPM floor preserved"
