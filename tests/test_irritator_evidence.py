"""Offline contract tests for the post-delivery, evidence-bound Irritator."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from dataclasses import asdict, replace
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from digest.config import Config, ProviderConfig
from digest.irritator.evidence_stage import (
    MAX_OUTPUT_TOKENS,
    MAX_RANKING_CANDIDATES,
    MAX_RANKING_JSON_CHARS,
    MAX_SOURCE_RESULTS,
    run_evidence_irritator,
)
from digest.llm import LLMRole
from digest.review import EvidenceBundle, build_evidence_bundle
from scripts.review_fixture import fixture_articles, fixture_config
from tests.factories import make_signal


def _bundle(config: Config) -> EvidenceBundle:
    return build_evidence_bundle(fixture_articles(), config.review)


def _narrative(bundle: EvidenceBundle) -> dict[str, Any]:
    item = bundle.items[0]
    return {"narratives": [{
        "claim": "The proposed rollout can improve reliability.", "category": item.category,
        "implicit_assumptions": ["Benchmarks transfer to deployments."],
        "why_worth_challenging": "Operational constraints may change the outcome.",
        "evidence_ids": [item.evidence_id], "quotes": {item.evidence_id: item.title},
    }], "limitations": ["Only the supplied RSS excerpts were considered."]}


def _queries(count: int = 1) -> dict[str, Any]:
    return {"queries": [{"query": f"rollout documented limitations {index}",
                          "intent": "Find deployment caveats."} for index in range(count)], "limitations": []}


def _ranking(url: str = "https://external.example/caveat") -> dict[str, Any]:
    return {"rankings": [{"url": url, "score": 8, "relation": "complicates",
                           "reasoning": "Documented deployment limitations complicate the rollout claim.",
                           "quote": "Deployment limitations"}], "limitations": []}


def _mock_model(bundle: EvidenceBundle, *, query_count: int = 1) -> AsyncMock:
    return AsyncMock(side_effect=[
        (json.dumps(_narrative(bundle)), {"resolved_model": "approved-model", "completion_tokens": 100}),
        (json.dumps(_queries(query_count)), {}), (json.dumps(_ranking()), {}),
    ])


def _offline_client() -> httpx.AsyncClient:
    def forbidden(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"Unexpected HTTP: {request.method} {request.url}")

    return httpx.AsyncClient(transport=httpx.MockTransport(forbidden))


@pytest.mark.asyncio
async def test_original_bundle_and_config_preserved_with_strict_llm_budget() -> None:
    config = fixture_config()
    config.llm.max_retries = 3
    config.llm.max_concurrent_requests = 4
    config.llm.min_request_interval_seconds = 0
    config.llm.providers = [ProviderConfig("gemini", "unapproved-fallback", ["fallback"])]
    config.irritator.sources = ["hackernews"]
    bundle = _bundle(config)
    original_bundle, original_config = asdict(bundle), asdict(config)
    model = _mock_model(bundle)
    signal = make_signal(url="https://external.example/caveat", title="Deployment limitations")
    with (
        patch("digest.irritator.evidence_stage.complete", model),
        patch("digest.irritator.evidence_stage.search_hackernews", AsyncMock(return_value=[signal])),
    ):
        async with _offline_client() as client:
            result = await run_evidence_irritator(bundle, config, client)
    assert result.status == "complete"
    assert result.ranked_signals[0].signal.url == signal.url
    assert result.ranked_signals[0].relation == "complicates"
    assert result.ranked_signals[0].quote == signal.title
    assert model.await_count == 3
    assert asdict(bundle) == original_bundle
    assert asdict(config) == original_config
    assert result.bundle_id == bundle.bundle_id
    assert "Limited coverage" in result.coverage
    assert [call.args[0] for call in model.await_args_list] == [
        LLMRole.EXTRACT_NARRATIVES, LLMRole.GENERATE_QUERIES, LLMRole.RANK_SIGNALS,
    ]
    copied_configs = []
    for index, call in enumerate(model.await_args_list):
        payload = json.loads(call.args[1][1]["content"])
        if index == 0:
            assert payload["evidence"] == json.loads(json.dumps(original_bundle))
        else:
            assert payload["evidence"]["bundle_id"] == bundle.bundle_id
            assert payload["evidence"]["items"] == [asdict(bundle.items[0])]
            assert payload["evidence"]["limited_to_narrative_citations"] is True
        bounded = call.args[2]
        copied_configs.append(bounded)
        assert bounded is not config and bounded.llm is not config.llm
        assert bounded.llm.max_retries == 0
        assert bounded.llm.max_concurrent_requests == 1
        assert bounded.llm.min_request_interval_seconds >= 65
        assert call.kwargs["max_output_tokens"] <= MAX_OUTPUT_TOKENS
        override = call.kwargs["provider_override"]
        assert (override.name, override.model) == (config.review.secondary.provider, config.review.secondary.model)
    assert all(item is copied_configs[0] for item in copied_configs)
    serialized = json.loads(json.dumps(asdict(result)))
    assert serialized["narratives"][0]["evidence_ids"] == [bundle.items[0].evidence_id]
    assert len(serialized["diagnostics"]) == 6
    assert next(d for d in result.diagnostics if d.stage == "narrative").resolved_model == "approved-model"


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", [
    "unknown_id", "duplicate_id", "unmatched_quote_id", "fabricated_quote", "empty_quote", "too_many",
    "extra_field", "unknown_category", "overlong_claim", "bad_assumptions", "invalid_json",
])
async def test_narrative_contract_rejects_malformed_ids_quotes_or_unbounded_output(mutation: str) -> None:
    config = fixture_config()
    bundle = _bundle(config)
    response = _narrative(bundle)
    item = response["narratives"][0]
    identity = bundle.items[0].evidence_id
    if mutation == "unknown_id":
        item["evidence_ids"] = ["not-in-original-evidence"]
    elif mutation == "duplicate_id":
        item["evidence_ids"] *= 2
    elif mutation == "unmatched_quote_id":
        item["quotes"]["not-in-original-evidence"] = "invented"
    elif mutation == "fabricated_quote":
        item["quotes"][identity] = "A model opinion is not original evidence."
    elif mutation == "empty_quote":
        item["quotes"][identity] = ""
    elif mutation == "too_many":
        response["narratives"] *= 2
    elif mutation == "extra_field":
        item["url"] = "https://invented.example/"
    elif mutation == "unknown_category":
        item["category"] = "invented-category"
    elif mutation == "overlong_claim":
        item["claim"] = "x" * 601
    elif mutation == "bad_assumptions":
        item["implicit_assumptions"] = [None]
    text = "not JSON" if mutation == "invalid_json" else json.dumps(response)
    model = AsyncMock(return_value=(text, {}))
    with patch("digest.irritator.evidence_stage.complete", model):
        async with _offline_client() as client:
            result = await run_evidence_irritator(bundle, config, client)
    assert result.status == "error"
    assert not result.narratives
    assert next(d for d in result.diagnostics if d.stage == "narrative").status == "error"
    assert model.await_count == 1
    assert not result.source_attempts


@pytest.mark.asyncio
async def test_invalid_evidence_hash_is_rejected_before_any_model_or_search() -> None:
    config = fixture_config()
    bundle = replace(_bundle(config), bundle_id="forged-checkpoint")
    with patch("digest.irritator.evidence_stage.complete", AsyncMock()) as model:
        async with _offline_client() as client:
            result = await run_evidence_irritator(bundle, config, client)
    assert result.status == "error"
    assert result.diagnostics[0].error == "ValueError"
    model.assert_not_awaited()


@pytest.mark.asyncio
async def test_search_fanout_result_input_and_output_caps_are_enforced() -> None:
    config = fixture_config()
    config.irritator.queries_per_narrative = 20
    config.irritator.top_signals = 20
    config.irritator.sources = ["devto", "reddit", "hackernews", "hackernews", "arxiv", "lobsters"]
    bundle = _bundle(config)
    model = _mock_model(bundle, query_count=3)
    raw = [make_signal(url=f"https://external.example/{i}", title="Deployment limitations") for i in range(25)]
    ranking = _ranking(raw[0].url)
    model.side_effect = [(json.dumps(_narrative(bundle)), {}), (json.dumps(_queries(3)), {}),
                         (json.dumps(ranking), {})]
    search = AsyncMock(return_value=raw)
    with (
        patch("digest.irritator.evidence_stage.complete", model),
        patch("digest.irritator.evidence_stage.search_hackernews", search),
        patch("digest.irritator.evidence_stage.search_arxiv", search),
        patch("digest.irritator.evidence_stage.search_lobsters", search),
    ):
        async with _offline_client() as client:
            result = await run_evidence_irritator(bundle, config, client)
    assert result.status == "complete"
    assert model.await_count == 3
    assert search.await_count == 9
    assert len(result.source_attempts) == 9
    assert all(attempt.result_count == MAX_SOURCE_RESULTS for attempt in result.source_attempts)
    assert all(attempt.omitted_count == 15 for attempt in result.source_attempts)
    assert {attempt.source for attempt in result.source_attempts} == {"hackernews", "arxiv", "lobsters"}
    ranking_payload = json.loads(model.await_args_list[2].args[1][1]["content"])
    assert len(ranking_payload["signals"]) <= MAX_RANKING_CANDIDATES
    assert len(json.dumps(ranking_payload["signals"], ensure_ascii=False)) <= MAX_RANKING_JSON_CHARS
    assert ranking_payload["max_ranked"] == 3
    assert len(result.ranked_signals) <= 3


@pytest.mark.asyncio
async def test_all_sources_failed_is_error_not_empty() -> None:
    config = fixture_config()
    config.irritator.sources = ["hackernews", "arxiv", "lobsters"]
    bundle = _bundle(config)
    model = _mock_model(bundle)
    search = AsyncMock(side_effect=httpx.ConnectError("sensitive transport error body"))
    with (
        patch("digest.irritator.evidence_stage.complete", model),
        patch("digest.irritator.evidence_stage.search_hackernews", search),
        patch("digest.irritator.evidence_stage.search_arxiv", search),
        patch("digest.irritator.evidence_stage.search_lobsters", search),
    ):
        async with _offline_client() as client:
            result = await run_evidence_irritator(bundle, config, client)
    assert result.status == "error"
    assert all(attempt.status == "error" for attempt in result.source_attempts)
    assert all(attempt.error == "ConnectError" for attempt in result.source_attempts)
    assert "sensitive transport" not in json.dumps(asdict(result))
    assert model.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("found_signal", [True, False])
async def test_partial_source_failure_is_incomplete_even_with_zero_results(found_signal: bool) -> None:
    config = fixture_config()
    config.irritator.sources = ["hackernews", "arxiv"]
    bundle = _bundle(config)
    model = _mock_model(bundle)
    raw = [make_signal(url="https://external.example/caveat", title="Deployment limitations")] if found_signal else []
    with (
        patch("digest.irritator.evidence_stage.complete", model),
        patch("digest.irritator.evidence_stage.search_hackernews", AsyncMock(return_value=raw)),
        patch("digest.irritator.evidence_stage.search_arxiv", AsyncMock(side_effect=RuntimeError("failure"))),
    ):
        async with _offline_client() as client:
            result = await run_evidence_irritator(bundle, config, client)
    assert result.status == "incomplete"
    assert len(result.ranked_signals) == int(found_signal)
    assert model.await_count == (3 if found_signal else 2)
    assert next(d for d in result.diagnostics if d.stage == "search").status == "incomplete"


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_stage", ["narrative", "queries", "ranking"])
async def test_llm_stage_failure_is_never_empty_and_never_retried(failed_stage: str) -> None:
    config = fixture_config()
    config.irritator.sources = ["hackernews"]
    bundle = _bundle(config)
    responses: list[Any] = [(json.dumps(_narrative(bundle)), {}), (json.dumps(_queries()), {}),
                            (json.dumps(_ranking()), {})]
    failed_index = {"narrative": 0, "queries": 1, "ranking": 2}[failed_stage]
    responses[failed_index] = RuntimeError("Quota: raw private response text must not be stored")
    model = AsyncMock(side_effect=responses)
    with (
        patch("digest.irritator.evidence_stage.complete", model),
        patch("digest.irritator.evidence_stage.search_hackernews", AsyncMock(return_value=[
            make_signal(url="https://external.example/caveat", title="Deployment limitations"),
        ])),
    ):
        async with _offline_client() as client:
            result = await run_evidence_irritator(bundle, config, client)
    assert result.status in {"error", "incomplete"}
    assert model.await_count == failed_index + 1
    assert next(d for d in result.diagnostics if d.stage == failed_stage).error == "RuntimeError"
    assert "raw private response" not in json.dumps(asdict(result))


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["invented_url", "duplicate_url", "bad_quote", "float_score", "bool_score",
                                       "high_score", "bad_relation", "too_many", "extra_field", "empty_unexplained"])
async def test_ranking_contract_rejects_entire_response(mutation: str) -> None:
    config = fixture_config()
    config.irritator.sources = ["hackernews"]
    bundle = _bundle(config)
    ranking = _ranking()
    item = ranking["rankings"][0]
    if mutation == "invented_url":
        item["url"] = "https://invented.example/not-a-real-source-result"
    elif mutation == "duplicate_url":
        ranking["rankings"].append(deepcopy(item))
    elif mutation == "bad_quote":
        item["quote"] = "Fabricated external evidence"
    elif mutation == "float_score":
        item["score"] = 8.0
    elif mutation == "bool_score":
        item["score"] = True
    elif mutation == "high_score":
        item["score"] = 11
    elif mutation == "bad_relation":
        item["relation"] = "supports"
    elif mutation == "too_many":
        ranking["rankings"] *= 4
    elif mutation == "extra_field":
        item["secret"] = "not a permitted field"
    else:
        ranking["rankings"] = []
    model = AsyncMock(side_effect=[(json.dumps(_narrative(bundle)), {}), (json.dumps(_queries()), {}),
                                  (json.dumps(ranking), {})])
    with (
        patch("digest.irritator.evidence_stage.complete", model),
        patch("digest.irritator.evidence_stage.search_hackernews", AsyncMock(return_value=[
            make_signal(url="https://external.example/caveat", title="Deployment limitations"),
        ])),
    ):
        async with _offline_client() as client:
            result = await run_evidence_irritator(bundle, config, client)
    assert result.status == "incomplete"
    assert not result.ranked_signals
    assert next(d for d in result.diagnostics if d.stage == "ranking").status == "error"
    assert model.await_count == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["narrative", "queries", "search", "validation", "ranking"])
async def test_true_empty_has_no_failure_diagnostics(stage: str) -> None:
    config = fixture_config()
    config.irritator.sources = ["hackernews"]
    if stage == "validation":
        config.filters.blocklist_keywords = ["Deployment"]
    bundle = _bundle(config)
    narrative = _narrative(bundle) if stage != "narrative" else {
        "narratives": [], "limitations": ["No defensible narrative in the supplied excerpts."],
    }
    queries = _queries() if stage != "queries" else {
        "queries": [], "limitations": ["No useful independent search query can be derived."],
    }
    ranking = _ranking() if stage != "ranking" else {
        "rankings": [], "limitations": ["None of these external snippets contradict or complicate the claim."],
    }
    raw = [] if stage == "search" else [
        make_signal(url="https://external.example/caveat", title="Deployment limitations"),
    ]
    with (
        patch("digest.irritator.evidence_stage.complete", AsyncMock(side_effect=[
            (json.dumps(narrative), {}), (json.dumps(queries), {}), (json.dumps(ranking), {}),
        ])),
        patch("digest.irritator.evidence_stage.search_hackernews", AsyncMock(return_value=raw)),
    ):
        async with _offline_client() as client:
            result = await run_evidence_irritator(bundle, config, client)
    assert result.status == "empty"
    assert all(d.status not in {"error", "incomplete"} for d in result.diagnostics)
    assert next(d for d in result.diagnostics if d.stage == stage).status == "empty"


@pytest.mark.asyncio
async def test_no_safe_sources_is_error_and_does_not_query_other_adapters() -> None:
    config = fixture_config()
    config.irritator.sources = ["reddit", "devto"]
    bundle = _bundle(config)
    with patch("digest.irritator.evidence_stage.complete", _mock_model(bundle)) as model:
        async with _offline_client() as client:
            result = await run_evidence_irritator(bundle, config, client)
    assert result.status == "error"
    assert next(d for d in result.diagnostics if d.stage == "search").error == "NoConfiguredSafeSources"
    assert not result.source_attempts
    assert model.await_count == 2


@pytest.mark.asyncio
async def test_timeout_returns_diagnostic_and_does_not_mutate_evidence() -> None:
    config = fixture_config()
    bundle = _bundle(config)
    original = asdict(bundle)

    async def slow(*args: Any, **kwargs: Any) -> tuple[str, dict[str, Any]]:
        await asyncio.sleep(5)
        raise AssertionError("Deadline was not applied")

    with patch("digest.irritator.evidence_stage.complete", side_effect=slow) as model:
        async with _offline_client() as client:
            result = await run_evidence_irritator(bundle, config, client, timeout_seconds=0.005)
    assert result.status == "error"
    assert next(d for d in result.diagnostics if d.stage == "narrative").error == "TimeoutError"
    assert model.await_count == 1
    assert asdict(bundle) == original


@pytest.mark.asyncio
async def test_existing_search_adapters_and_validator_run_with_mock_http() -> None:
    config = fixture_config()
    config.irritator.sources = ["hackernews", "arxiv", "lobsters"]
    config.irritator.check_liveness = True
    bundle = _bundle(config)
    requests: list[httpx.Request] = []

    def adapter(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "GET", "Liveness HTTP is outside this bounded experiment"
        if request.url.host == "hn.algolia.com":
            return httpx.Response(200, json={"hits": [{"title": "Deployment limitations",
                                                      "url": "https://external.example/caveat", "points": 10}]})
        if request.url.host == "export.arxiv.org":
            return httpx.Response(200, text='<feed xmlns="http://www.w3.org/2005/Atom"></feed>')
        if request.url.host == "lobste.rs":
            return httpx.Response(503, text="Server failed")
        raise AssertionError("Unexpected endpoint")

    with patch("digest.irritator.evidence_stage.complete", _mock_model(bundle)):
        async with httpx.AsyncClient(transport=httpx.MockTransport(adapter)) as client:
            result = await run_evidence_irritator(bundle, config, client)
    assert result.status == "incomplete"
    assert len(requests) == 3
    assert result.ranked_signals[0].signal.url == "https://external.example/caveat"
    assert any("liveness" in limitation for limitation in result.limitations)
    assert {attempt.source: attempt.status for attempt in result.source_attempts} == {
        "hackernews": "complete", "arxiv": "empty", "lobsters": "error",
    }


@pytest.mark.asyncio
async def test_llm_provider_429_is_one_http_request_without_retry_or_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    config = fixture_config()
    config.llm.max_retries = 3
    config.llm.providers = [ProviderConfig("gemini", "fallback-model", ["fallback"])]
    bundle = _bundle(config)
    monkeypatch.setenv("GROQ_API_KEY", "synthetic-test-key")
    monkeypatch.setenv("GEMINI_API_KEY", "synthetic-test-key")
    requests: list[httpx.Request] = []

    def rejected(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.host == "api.groq.com"
        assert json.loads(request.content)["max_completion_tokens"] == MAX_OUTPUT_TOKENS
        return httpx.Response(429, json={"error": {"code": "quota", "message": "Synthetic error"}})

    real_client = httpx.AsyncClient
    llm_client = real_client(transport=httpx.MockTransport(rejected))
    async with _offline_client() as search_client:
        with patch("digest.llm.httpx.AsyncClient", return_value=llm_client):
            result = await run_evidence_irritator(bundle, config, search_client)
    assert result.status == "error"
    assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("source,body", [
    ("hackernews", '{}'), ("hackernews", '{"hits": null}'),
    ("lobsters", '{}'), ("lobsters", '{"results": "not a list"}'),
    ("arxiv", '<html><body>Temporarily unavailable</body></html>'),
    ("arxiv", '<feed xmlns="http://www.w3.org/2005/Atom"><entry>'
              '<id>https://arxiv.org/api/errors#incorrect_id_format</id></entry></feed>'),
])
async def test_malformed_success_response_is_failure_not_empty(source: str, body: str) -> None:
    config = fixture_config()
    config.irritator.sources = [source]
    bundle = _bundle(config)

    def invalid_response(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body)

    with patch("digest.irritator.evidence_stage.complete", _mock_model(bundle)) as model:
        async with httpx.AsyncClient(transport=httpx.MockTransport(invalid_response)) as client:
            original_hooks = list(client.event_hooks["response"])
            result = await run_evidence_irritator(bundle, config, client)
            assert client.event_hooks["response"] == original_hooks
    assert result.status == "error"
    assert result.source_attempts[0].status == "error"
    assert result.source_attempts[0].error_detail
    assert model.await_count == 2


@pytest.mark.asyncio
async def test_search_redirect_is_not_followed_by_redirect_enabled_client() -> None:
    config = fixture_config()
    config.irritator.sources = ["hackernews"]
    bundle = _bundle(config)
    requests = []

    def redirect(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(302, headers={"location": "https://unexpected.example/"})

    with patch("digest.irritator.evidence_stage.complete", _mock_model(bundle)):
        async with httpx.AsyncClient(transport=httpx.MockTransport(redirect), follow_redirects=True) as client:
            result = await run_evidence_irritator(bundle, config, client)
    assert result.status == "error"
    assert len(requests) == 1
    assert requests[0].url.host == "hn.algolia.com"


@pytest.mark.asyncio
async def test_large_external_urls_respect_serialized_ranking_budget() -> None:
    config = fixture_config()
    config.irritator.sources = ["hackernews", "arxiv"]
    bundle = _bundle(config)
    raw = [make_signal(url=f"https://external.example/{index}/" + "a" * 1800,
                       title="Deployment limitations " * 10, snippet="Full snippet. " * 100) for index in range(10)]
    model = AsyncMock(side_effect=[(json.dumps(_narrative(bundle)), {}), (json.dumps(_queries()), {}),
                                  (json.dumps({"rankings": [], "limitations": ["No strong counter-evidence."]}), {})])
    with (
        patch("digest.irritator.evidence_stage.complete", model),
        patch("digest.irritator.evidence_stage.search_hackernews", AsyncMock(return_value=raw)),
        patch("digest.irritator.evidence_stage.search_arxiv", AsyncMock(return_value=[])),
    ):
        async with _offline_client() as client:
            result = await run_evidence_irritator(bundle, config, client)
    payload = json.loads(model.await_args_list[2].args[1][1]["content"])
    assert len(json.dumps(payload["signals"], ensure_ascii=False)) <= MAX_RANKING_JSON_CHARS
    assert 0 < len(payload["signals"]) < 10
    diagnostic = next(item for item in result.diagnostics if item.stage == "ranking")
    assert diagnostic.omitted_count == len(raw) - len(payload["signals"])
    assert result.status == "empty"


@pytest.mark.asyncio
async def test_parser_failure_retains_only_known_safe_contract_reason() -> None:
    config = fixture_config()
    bundle = _bundle(config)
    narrative = _narrative(bundle)
    narrative["narratives"][0]["quotes"][bundle.items[0].evidence_id] = "Fabricated source quotation"
    with patch("digest.irritator.evidence_stage.complete", AsyncMock(return_value=(json.dumps(narrative), {}))):
        async with _offline_client() as client:
            result = await run_evidence_irritator(bundle, config, client)
    diagnostic = next(item for item in result.diagnostics if item.stage == "narrative")
    assert diagnostic.error == "ValueError"
    assert diagnostic.error_detail == "Narrative quote is not in original evidence."
    assert "Fabricated source quotation" not in json.dumps(asdict(result))


@pytest.mark.asyncio
async def test_search_deadline_marks_attempt_and_restores_client_hooks() -> None:
    config = fixture_config()
    config.irritator.sources = ["hackernews"]
    bundle = _bundle(config)

    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        raise AssertionError("Search deadline was not applied")

    with patch("digest.irritator.evidence_stage.complete", _mock_model(bundle)):
        async with httpx.AsyncClient(transport=httpx.MockTransport(slow)) as client:
            original_hooks = list(client.event_hooks["response"])
            result = await run_evidence_irritator(bundle, config, client, timeout_seconds=0.01)
            assert client.event_hooks["response"] == original_hooks
    assert result.status == "incomplete"
    diagnostic = next(item for item in result.diagnostics if item.stage == "search")
    assert diagnostic.status == "error" and diagnostic.error == "TimeoutError"
    assert result.source_attempts[0].status == "error"
    assert result.source_attempts[0].error == "CancelledError"


@pytest.mark.asyncio
async def test_more_than_three_generated_queries_is_rejected_without_search() -> None:
    config = fixture_config()
    bundle = _bundle(config)
    model = AsyncMock(side_effect=[(json.dumps(_narrative(bundle)), {}), (json.dumps(_queries(4)), {})])
    with patch("digest.irritator.evidence_stage.complete", model):
        async with _offline_client() as client:
            result = await run_evidence_irritator(bundle, config, client)
    assert result.status == "incomplete"
    assert not result.source_attempts
    diagnostic = next(item for item in result.diagnostics if item.stage == "queries")
    assert diagnostic.error_detail == "Invalid response entry count."
