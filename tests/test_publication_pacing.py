"""Quota evidence may reduce fallback delay, never operator floors or saved cooldowns."""
from dataclasses import replace
from datetime import UTC, datetime

from digest.config import EnrichmentConfig, ReviewModelConfig, _load_enrichment
from digest.editorial_state import ArticleWork, Attempt, EditorialState, PublicationWork
from digest.llm import ProviderResponseDiagnostics, RetryTiming
from digest.publication_pacing import next_request_time
from scripts.review_fixture import fixture_config

NOW = 1_780_000_000.0


def iso(value: float) -> str:
    return datetime.fromtimestamp(value, UTC).isoformat()


def fixture() -> tuple:
    config = fixture_config()
    route = ReviewModelConfig("groq", "openai/gpt-oss-120b")
    config.enrichment = EnrichmentConfig(ReviewModelConfig("groq", "qwen/qwen3.8-27b"), route)
    config.llm.min_request_interval_seconds = 20
    work = PublicationWork("binding", "a" * 64, "v", "en", "groq", "qwen/qwen3.8-27b", "groq", route.model)
    observation = ProviderResponseDiagnostics(200, "unknown", "unknown", "unknown", iso(NOW - 1),
        {"x-ratelimit-limit-tokens": 8000, "x-ratelimit-remaining-tokens": 3000,
         "x-ratelimit-remaining-requests": 999}, reset_headers={
            "x-ratelimit-reset-tokens": RetryTiming(26, "duration", iso(NOW + 25))})
    work.attempts.append(Attempt("id", "check", "key", "prompt", iso(NOW - 5), "success",
                                 provider_diagnostics=observation))
    article = ArticleWork("id", "title", "https://example.com", "", "source", "category", None, iso(NOW))
    article.publications[work.binding] = work
    state = EditorialState(articles={"id": article}, order=["id"])
    return config, route, state, work


def test_provider_aware_is_explicit_and_respects_operator_and_rpm_floors() -> None:
    config, route, state, _ = fixture()
    assert next_request_time(state, config, route, 5600, now=NOW)[0] == NOW + 60
    config.enrichment = replace(config.enrichment, pacing="provider_aware")
    assert next_request_time(state, config, route, 5600, now=NOW)[0] == NOW + 25
    config.llm.min_request_interval_seconds = 65
    assert next_request_time(state, config, route, 5600, now=NOW)[0] == NOW + 60
    config.llm.min_request_interval_seconds = 0
    config.enrichment = replace(config.enrichment, requests_per_minute=1)
    assert next_request_time(state, config, route, 5600, now=NOW)[0] == NOW + 55


def test_stale_or_other_route_headers_cannot_remove_conservative_spacing() -> None:
    config, route, state, work = fixture()
    config.enrichment = replace(config.enrichment, pacing="provider_aware")
    observation = work.attempts[0].provider_diagnostics
    work.attempts[0].provider_diagnostics = replace(observation, observed_at=iso(NOW - 121))
    assert next_request_time(state, config, route, 1000, now=NOW)[0] == NOW + 60
    work.attempts[0].provider_diagnostics = observation
    other = ReviewModelConfig("groq", "qwen/qwen3.8-27b")
    assert next_request_time(state, config, other, 1000, now=NOW)[0] == NOW + 60


def test_daily_and_saved_cooldowns_survive_header_freshness_expiry() -> None:
    config, route, state, work = fixture()
    config.enrichment = replace(config.enrichment, pacing="provider_aware")
    observation = work.attempts[0].provider_diagnostics
    headers = dict(observation.numeric_headers, **{"x-ratelimit-remaining-requests": 0})
    work.attempts[0].provider_diagnostics = replace(observation, observed_at=iso(NOW - 300),
        numeric_headers=headers, reset_headers={
            "x-ratelimit-reset-requests": RetryTiming(3600, "duration", iso(NOW + 3300))})
    assert next_request_time(state, config, route, 5000, now=NOW)[0] == NOW + 3300
    state.provider_unavailable_until["groq"] = iso(NOW + 86400)
    assert next_request_time(state, config, route, 5000, now=NOW)[0] == NOW + 86400


def test_routes_are_explicit_without_changing_legacy_defaults() -> None:
    assert _load_enrichment({}) is None
    config = _load_enrichment({"enrichment": {
        "writer": {"provider": "groq", "model": "explicit-writer"},
        "verifier": {"provider": "gemini", "model": "explicit-verifier"},
    }})
    assert config is not None and config.writer.model == "explicit-writer"
    assert config.verifier.model == "explicit-verifier" and config.pacing == "fixed"
