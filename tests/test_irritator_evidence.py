"""Offline contract tests for the post-delivery, evidence-bound Irritator."""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from digest.adapters.models.execution import ModelExecution
from digest.application.review_request import build_evidence_bundle
from digest.config import Config, ProviderConfig
from digest.domain.editorial.reviews import EvidenceBundle
from digest.irritator.evidence_stage import (
    MAX_OUTPUT_TOKENS,
    MAX_RANKING_CANDIDATES,
    MAX_RANKING_JSON_CHARS,
    MAX_SOURCE_RESULTS,
    _admit_ranking,
    _bounded_signals,
    _parse_narrative,
    _parse_rankings,
    _ranking_signal_payload,
    run_evidence_irritator,
)
from digest.irritator.ranker import RANK_RELATION_CONTRACT
from digest.llm import LLMRole
from digest.review_checkpoint import FullSourceEvidence
from scripts.review_fixture import fixture_articles, fixture_config
from tests.factories import make_article, make_signal
from tests.test_review_checkpoint import _full_source_evidence


def _bundle(config: Config) -> EvidenceBundle:
    return build_evidence_bundle(fixture_articles(), config.review)


def _narrative(bundle: EvidenceBundle | FullSourceEvidence) -> dict[str, Any]:
    item = bundle.items[0]
    return {"narratives": [{
        "claim": "The proposed rollout can improve reliability.", "category": item.category,
        "implicit_assumptions": ["Benchmarks transfer to deployments."],
        "why_worth_challenging": "Operational constraints may change the outcome.",
        "evidence_ids": [item.evidence_id], "quotes": {item.evidence_id: item.title},
    }], "limitations": ["Only the supplied RSS excerpts were considered."]}


def _queries(count: int = 1, *, anchor: str = "Benchmark") -> dict[str, Any]:
    return {"queries": [{"query": anchor if index == 0 else f"rollout documented limitations {index}",
                          "intent": "Find deployment caveats."} for index in range(count)], "limitations": []}


def _ranking(url: str = "https://external.example/caveat") -> dict[str, Any]:
    quote_id = _ranking_signal_payload(make_signal(url=url, title="Deployment limitations"))["title"][0]["id"]
    return {"rankings": [{"url": url, "score": 8, "relation": "complicates",
                           "reasoning": "Documented deployment limitations complicate the rollout claim.",
                           "quote_id": quote_id}], "limitations": []}


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
    execution = ModelExecution()
    config = fixture_config()
    config.llm.max_retries = 3
    config.llm.max_concurrent_requests = 4
    config.llm.min_request_interval_seconds = 0
    config.llm.providers = [ProviderConfig("gemini", "unapproved-fallback", ["fallback"])]
    config.irritator.sources = ["hackernews"]
    bundle = _bundle(config)
    original_bundle, original_config = asdict(bundle), asdict(config)
    caller_state = execution.request_state(config.llm)
    caller_state.request_limit = 0
    caller_state.unavailable_until[(config.review.secondary.provider, config.review.secondary.model)] = float("inf")
    original_narrative = _narrative(bundle)["narratives"][0]
    model = _mock_model(bundle)
    signal = make_signal(url="https://external.example/caveat", title="Deployment limitations")
    with (
        patch("digest.irritator.evidence_stage.complete", model),
        patch("digest.irritator.evidence_stage.search_hackernews", AsyncMock(return_value=[signal])),
    ):
        async with _offline_client() as client:
            result = await run_evidence_irritator(bundle, config, client, execution=execution)
    assert result.status == "complete"
    assert result.ranked_signals[0].signal.url == signal.url
    assert result.ranked_signals[0].relation == "complicates"
    assert result.ranked_signals[0].quote == signal.title
    assert model.await_count == 3
    assert RANK_RELATION_CONTRACT in model.await_args_list[2].args[1][0]["content"]
    extraction = model.await_args_list[0].args[1][0]["content"]
    assert "source-attributed assertion or announced decision" in extraction
    assert "Duplicate reports of one event are not independent support" in extraction
    assert asdict(bundle) == original_bundle
    assert asdict(config) == original_config
    assert result.bundle_id == bundle.bundle_id
    assert "Limited coverage" in result.coverage
    assert [call.args[0] for call in model.await_args_list] == [
        LLMRole.EXTRACT_NARRATIVES, LLMRole.GENERATE_QUERIES, LLMRole.RANK_SIGNALS,
    ]
    copied_configs = []
    derived = model.await_args_list[0].kwargs["execution"]
    assert derived is not execution
    for index, call in enumerate(model.await_args_list):
        payload = json.loads(call.args[1][1]["content"])
        if index == 0:
            assert payload["evidence"] == json.loads(json.dumps(original_bundle))
        else:
            assert payload["narrative"] == {
                "claim": original_narrative["claim"], "category": original_narrative["category"],
                "evidence_ids": original_narrative["evidence_ids"], "quotes": original_narrative["quotes"],
            }
            if index == 1:
                assert payload["exploratory_hypotheses"] == {
                    "implicit_assumptions": original_narrative["implicit_assumptions"],
                    "why_worth_challenging": original_narrative["why_worth_challenging"],
                }
                assert "unverified model interpretation" in call.args[1][0]["content"]
            else:
                assert "exploratory_hypotheses" not in payload
                for hypothesis in [*original_narrative["implicit_assumptions"],
                                   original_narrative["why_worth_challenging"]]:
                    assert hypothesis not in call.args[1][1]["content"]
            assert payload["evidence"]["bundle_id"] == bundle.bundle_id
            assert payload["evidence"]["items"] == [asdict(bundle.items[0])]
            assert payload["evidence"]["limited_to_narrative_citations"] is True
        bounded = call.args[2]
        copied_configs.append(bounded)
        assert bounded is not config and bounded.llm is not config.llm
        assert bounded.llm.max_retries == 0
        assert bounded.llm.max_concurrent_requests == 1
        assert call.kwargs["execution"] is derived
        state = derived.request_state(bounded.llm)
        assert state is not caller_state and state.semaphore._value == 1
        assert state.request_limit is None and state.unavailable_until == {}
        assert bounded.llm.min_request_interval_seconds >= 65
        assert call.kwargs["max_output_tokens"] <= MAX_OUTPUT_TOKENS
        override = call.kwargs["provider_override"]
        assert (override.name, override.model) == (config.review.secondary.provider, config.review.secondary.model)
    assert all(item is copied_configs[0] for item in copied_configs)
    assert asdict(result.narratives[0]) == {**original_narrative, "typography_normalized": []}
    serialized = json.loads(json.dumps(asdict(result)))
    assert serialized["narratives"][0] == {**original_narrative, "typography_normalized": []}
    assert len(serialized["diagnostics"]) == 6
    assert next(d for d in result.diagnostics if d.stage == "narrative").resolved_model == "approved-model"


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", [
    "unknown_id", "duplicate_id", "unmatched_quote_id", "fabricated_quote", "empty_quote", "too_many",
    "extra_field", "unknown_category", "overlong_response", "bad_assumptions", "invalid_json",
])
async def test_narrative_contract_rejects_malformed_ids_quotes_or_unbounded_output(mutation: str) -> None:
    execution = ModelExecution()
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
    elif mutation == "overlong_response":
        item["claim"] = "x" * 16000
    elif mutation == "bad_assumptions":
        item["implicit_assumptions"] = [None]
    text = "not JSON" if mutation == "invalid_json" else json.dumps(response)
    model = AsyncMock(return_value=(text, {}))
    with patch("digest.irritator.evidence_stage.complete", model):
        async with _offline_client() as client:
            result = await run_evidence_irritator(bundle, config, client, execution=execution)
    assert result.status == "error"
    assert not result.narratives
    assert next(d for d in result.diagnostics if d.stage == "narrative").status == "error"
    assert model.await_count == 1
    assert not result.source_attempts


@pytest.mark.asyncio
async def test_invalid_evidence_hash_is_rejected_before_any_model_or_search() -> None:
    execution = ModelExecution()
    config = fixture_config()
    bundle = replace(_bundle(config), bundle_id="forged-checkpoint")
    with patch("digest.irritator.evidence_stage.complete", AsyncMock()) as model:
        async with _offline_client() as client:
            result = await run_evidence_irritator(bundle, config, client, execution=execution)
    assert result.status == "error"
    assert result.diagnostics[0].error == "ValueError"
    model.assert_not_awaited()


@pytest.mark.asyncio
async def test_search_fanout_result_input_and_output_caps_are_enforced() -> None:
    execution = ModelExecution()
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
    active, peak = 0, 0

    async def concurrent_search(*args: Any) -> list[Any]:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0)
        active -= 1
        return raw

    search = AsyncMock(side_effect=concurrent_search)
    with (
        patch("digest.irritator.evidence_stage.complete", model),
        patch("digest.irritator.evidence_stage.search_hackernews", search),
        patch("digest.irritator.evidence_stage.search_arxiv", search),
        patch("digest.irritator.evidence_stage.search_devto", search),
    ):
        async with _offline_client() as client:
            result = await run_evidence_irritator(bundle, config, client, execution=execution)
    assert result.status == "complete"
    assert model.await_count == 3
    assert search.await_count == 9
    assert peak == 3 and active == 0
    assert len(result.source_attempts) == 9
    assert all(attempt.result_count == MAX_SOURCE_RESULTS for attempt in result.source_attempts)
    assert all(attempt.omitted_count == 15 for attempt in result.source_attempts)
    assert {attempt.source for attempt in result.source_attempts} == {"hackernews", "arxiv", "devto"}
    assert result.ranking_audit is not None
    assert all(candidate.query_indices == [0, 1, 2] for candidate in result.ranking_audit.candidates)
    ranking_payload = json.loads(model.await_args_list[2].args[1][1]["content"])
    assert len(ranking_payload["signals"]) <= MAX_RANKING_CANDIDATES
    assert len(json.dumps(ranking_payload["signals"], ensure_ascii=False)) <= MAX_RANKING_JSON_CHARS
    assert ranking_payload["max_ranked"] == 3
    assert len(result.ranked_signals) <= 3


@pytest.mark.asyncio
async def test_all_sources_failed_is_error_not_empty() -> None:
    execution = ModelExecution()
    config = fixture_config()
    config.irritator.sources = ["hackernews", "arxiv", "devto"]
    bundle = _bundle(config)
    model = _mock_model(bundle)
    search = AsyncMock(side_effect=httpx.ConnectError("sensitive transport error body"))
    with (
        patch("digest.irritator.evidence_stage.complete", model),
        patch("digest.irritator.evidence_stage.search_hackernews", search),
        patch("digest.irritator.evidence_stage.search_arxiv", search),
        patch("digest.irritator.evidence_stage.search_devto", search),
    ):
        async with _offline_client() as client:
            result = await run_evidence_irritator(bundle, config, client, execution=execution)
    assert result.status == "error"
    assert all(attempt.status == "error" for attempt in result.source_attempts)
    assert all(attempt.error == "ConnectError" for attempt in result.source_attempts)
    assert "sensitive transport" not in json.dumps(asdict(result))
    assert model.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("found_signal", [True, False])
async def test_partial_source_failure_is_incomplete_even_with_zero_results(found_signal: bool) -> None:
    execution = ModelExecution()
    config = fixture_config()
    config.irritator.sources = ["hackernews", "devto"]
    bundle = _bundle(config)
    model = _mock_model(bundle)
    raw = [make_signal(url="https://external.example/caveat", title="Deployment limitations")] if found_signal else []
    with (
        patch("digest.irritator.evidence_stage.complete", model),
        patch("digest.irritator.evidence_stage.search_hackernews", AsyncMock(return_value=raw)),
        patch("digest.irritator.evidence_stage.search_devto", AsyncMock(side_effect=RuntimeError("failure"))),
    ):
        async with _offline_client() as client:
            result = await run_evidence_irritator(bundle, config, client, execution=execution)
    assert result.status == "incomplete"
    assert len(result.ranked_signals) == int(found_signal)
    assert model.await_count == (3 if found_signal else 2)
    assert next(d for d in result.diagnostics if d.stage == "search").status == "incomplete"


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_stage", ["narrative", "queries", "ranking"])
async def test_llm_stage_failure_is_never_empty_and_never_retried(failed_stage: str) -> None:
    execution = ModelExecution()
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
            result = await run_evidence_irritator(bundle, config, client, execution=execution)
    assert result.status in {"error", "incomplete"}
    assert model.await_count == failed_index + 1
    assert next(d for d in result.diagnostics if d.stage == failed_stage).error == "RuntimeError"
    assert "raw private response" not in json.dumps(asdict(result))


@pytest.mark.asyncio
async def test_invalid_ranking_marks_pipeline_incomplete_without_retry() -> None:
    execution = ModelExecution()
    config = fixture_config()
    config.irritator.sources = ["hackernews"]
    bundle = _bundle(config)
    ranking = _ranking()
    ranking["rankings"][0]["extra"] = "not a permitted field"
    model = AsyncMock(side_effect=[(json.dumps(_narrative(bundle)), {}), (json.dumps(_queries()), {}),
                                  (json.dumps(ranking), {})])
    with (
        patch("digest.irritator.evidence_stage.complete", model),
        patch("digest.irritator.evidence_stage.search_hackernews", AsyncMock(return_value=[
            make_signal(url="https://external.example/caveat", title="Deployment limitations"),
        ])),
    ):
        async with _offline_client() as client:
            result = await run_evidence_irritator(bundle, config, client, execution=execution)
    assert result.status == "incomplete"
    assert not result.ranked_signals
    assert next(d for d in result.diagnostics if d.stage == "ranking").status == "error"
    assert model.await_count == 3


@pytest.mark.parametrize("relation", ["supports", "context", "insufficient"])
def test_high_scoring_non_counter_relations_excluded(relation: str) -> None:
    bundle = _bundle(fixture_config())
    narrative = _parse_narrative(json.dumps(_narrative(bundle)), bundle)[0][0]
    signal = make_signal(url="https://external.example/caveat", title="Deployment limitations")
    ranking = _ranking()
    ranking["rankings"][0].update(relation=relation, score=10)
    admission = _admit_ranking([signal], 1, 3, {})
    response = _parse_rankings(json.dumps(ranking), admission, narrative)
    assert response.ranked_signals == []
    counts = ", ".join(f"{label}={int(label == relation)}" for label in ("supports", "context", "insufficient"))
    assert response.limitations == [f"Ranking omitted non-counter signals: {counts}."]


def test_mixed_relations_preserve_genuine_complication_and_exact_quote() -> None:
    bundle = _bundle(fixture_config())
    narrative = _parse_narrative(json.dumps(_narrative(bundle)), bundle)[0][0]
    supportive = make_signal(url="https://external.example/support", title="Staged rollout reduces failures")
    complication = make_signal(url="https://external.example/caveat",
                               title="Stateful services require a maintenance window for rollout")
    low_score = make_signal(url="https://external.example/weak", title="Limited rollout caveat")
    entries = []
    for signal, relation, score, reason in [
        (supportive, "supports", 10, "The measured failure reduction supports improved reliability."),
        (complication, "complicates", 5, "The required maintenance window limits reliability during rollout."),
        (low_score, "complicates", 4, "The caveat has limited relevance."),
    ]:
        entries.append({"url": signal.url, "relation": relation, "score": score, "reasoning": reason,
                        "quote_id": _ranking_signal_payload(signal)["title"][0]["id"]})
    admission = _admit_ranking([supportive, complication, low_score], 5, 3, {})
    response = _parse_rankings(
        json.dumps({"rankings": entries, "limitations": ["Search is limited."]}), admission, narrative,
    )
    ranked = response.ranked_signals
    assert len(ranked) == 1
    assert ranked[0].signal == complication and ranked[0].relation == "complicates"
    assert ranked[0].quote == complication.title and ranked[0].score == 5
    assert set(asdict(ranked[0])) == {
        "signal", "score", "reasoning", "narrative_claim", "relation", "quote", "typography_normalized",
    }
    assert response.limitations == ["Search is limited.",
                                    "Ranking omitted non-counter signals: supports=1, context=0, insufficient=0."]


@pytest.mark.asyncio
async def test_all_non_counter_relations_are_honest_empty_with_omission_counts() -> None:
    execution = ModelExecution()
    config = fixture_config()
    config.irritator.sources = ["hackernews"]
    bundle = _bundle(config)
    signals = [make_signal(url=f"https://external.example/{relation}", title="Deployment limitations")
               for relation in ("supports", "context", "insufficient")]
    entries = []
    for signal, relation in zip(signals, ("supports", "context", "insufficient"), strict=True):
        item = _ranking(signal.url)["rankings"][0]
        item.update(relation=relation, score=10)
        entries.append(item)
    model = AsyncMock(side_effect=[
        (json.dumps(_narrative(bundle)), {}), (json.dumps(_queries()), {}),
        (json.dumps({"rankings": entries, "limitations": []}), {}),
    ])
    with (
        patch("digest.irritator.evidence_stage.complete", model),
        patch("digest.irritator.evidence_stage.search_hackernews", AsyncMock(return_value=signals)),
    ):
        async with _offline_client() as client:
            result = await run_evidence_irritator(bundle, config, client, execution=execution)
    assert result.status == "empty" and result.ranked_signals == []
    assert all(d.status not in {"error", "incomplete"} for d in result.diagnostics)
    ranking_stage = next(d for d in result.diagnostics if d.stage == "ranking")
    assert ranking_stage.status == "empty" and ranking_stage.output_count == 0
    assert "Ranking omitted non-counter signals: supports=1, context=1, insufficient=1." in result.limitations
    assert model.await_count == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["narrative", "queries", "search", "validation", "ranking"])
async def test_true_empty_has_no_failure_diagnostics(stage: str) -> None:
    execution = ModelExecution()
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
            result = await run_evidence_irritator(bundle, config, client, execution=execution)
    assert result.status == "empty"
    assert all(d.status not in {"error", "incomplete"} for d in result.diagnostics)
    assert next(d for d in result.diagnostics if d.stage == stage).status == "empty"


@pytest.mark.asyncio
async def test_no_safe_sources_is_error_and_does_not_query_other_adapters() -> None:
    execution = ModelExecution()
    config = fixture_config()
    config.irritator.sources = ["reddit", "lobsters"]
    bundle = _bundle(config)
    with patch("digest.irritator.evidence_stage.complete", _mock_model(bundle)) as model:
        async with _offline_client() as client:
            result = await run_evidence_irritator(bundle, config, client, execution=execution)
    assert result.status == "error"
    assert next(d for d in result.diagnostics if d.stage == "search").error == "NoConfiguredSafeSources"
    assert not result.source_attempts
    assert model.await_count == 2


@pytest.mark.asyncio
async def test_timeout_returns_diagnostic_and_does_not_mutate_evidence() -> None:
    execution = ModelExecution()
    config = fixture_config()
    bundle = _bundle(config)
    original = asdict(bundle)

    async def slow(*args: Any, **kwargs: Any) -> tuple[str, dict[str, Any]]:
        await asyncio.sleep(5)
        raise AssertionError("Deadline was not applied")

    with patch("digest.irritator.evidence_stage.complete", side_effect=slow) as model:
        async with _offline_client() as client:
            result = await run_evidence_irritator(bundle, config, client, timeout_seconds=0.005, execution=execution)
    assert result.status == "error"
    assert next(d for d in result.diagnostics if d.stage == "narrative").error == "TimeoutError"
    assert model.await_count == 1
    assert asdict(bundle) == original


@pytest.mark.asyncio
@pytest.mark.parametrize("third_source,expected_requests", [("lobsters", 2), ("devto", 3)])
async def test_existing_search_adapters_and_validator_run_with_mock_http(
    third_source: str, expected_requests: int,
) -> None:
    execution = ModelExecution()
    config = fixture_config()
    config.irritator.sources = ["hackernews", "arxiv", third_source]
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
        if request.url.host == "dev.to":
            return httpx.Response(200, json=[])
        raise AssertionError("Unexpected endpoint")

    with patch("digest.irritator.evidence_stage.complete", _mock_model(bundle)):
        async with httpx.AsyncClient(transport=httpx.MockTransport(adapter)) as client:
            result = await run_evidence_irritator(bundle, config, client, execution=execution)
    assert result.status == "complete"
    assert len(requests) == expected_requests
    assert result.ranked_signals[0].signal.url == "https://external.example/caveat"
    assert any("liveness" in limitation for limitation in result.limitations)
    expected_statuses = {"hackernews": "complete", "arxiv": "empty"}
    if third_source == "devto":
        expected_statuses["devto"] = "empty"
    assert {attempt.source: attempt.status for attempt in result.source_attempts} == expected_statuses


@pytest.mark.asyncio
async def test_llm_provider_429_is_one_http_request_without_retry_or_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    execution = ModelExecution()
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
            result = await run_evidence_irritator(bundle, config, search_client, execution=execution)
    assert result.status == "error"
    assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("source,body", [
    ("hackernews", '{}'), ("hackernews", '{"hits": null}'),
    ("arxiv", '<html><body>Temporarily unavailable</body></html>'),
    ("arxiv", '<feed xmlns="http://www.w3.org/2005/Atom"><entry>'
              '<id>https://arxiv.org/api/errors#incorrect_id_format</id></entry></feed>'),
])
async def test_malformed_success_response_is_failure_not_empty(source: str, body: str) -> None:
    execution = ModelExecution()
    config = fixture_config()
    config.irritator.sources = [source]
    bundle = _bundle(config)

    def invalid_response(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body)

    with patch("digest.irritator.evidence_stage.complete", _mock_model(bundle)) as model:
        async with httpx.AsyncClient(transport=httpx.MockTransport(invalid_response)) as client:
            original_hooks = list(client.event_hooks["response"])
            result = await run_evidence_irritator(bundle, config, client, execution=execution)
            assert client.event_hooks["response"] == original_hooks
    assert result.status == "error"
    assert result.source_attempts[0].status == "error"
    assert result.source_attempts[0].error_detail
    assert model.await_count == 2


@pytest.mark.asyncio
async def test_search_redirect_is_not_followed_by_redirect_enabled_client() -> None:
    execution = ModelExecution()
    config = fixture_config()
    config.irritator.sources = ["hackernews"]
    bundle = _bundle(config)
    requests = []

    def redirect(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(302, headers={"location": "https://unexpected.example/"})

    with patch("digest.irritator.evidence_stage.complete", _mock_model(bundle)):
        async with httpx.AsyncClient(transport=httpx.MockTransport(redirect), follow_redirects=True) as client:
            result = await run_evidence_irritator(bundle, config, client, execution=execution)
    assert result.status == "error"
    assert len(requests) == 1
    assert requests[0].url.host == "hn.algolia.com"


@pytest.mark.asyncio
async def test_large_external_urls_respect_serialized_ranking_budget() -> None:
    execution = ModelExecution()
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
            result = await run_evidence_irritator(bundle, config, client, execution=execution)
    payload = json.loads(model.await_args_list[2].args[1][1]["content"])
    assert len(json.dumps(payload["signals"], ensure_ascii=False)) <= MAX_RANKING_JSON_CHARS
    assert 0 < len(payload["signals"]) < 10
    assert result.status == "incomplete"
    diagnostic = next(item for item in result.diagnostics if item.stage == "ranking")
    assert diagnostic.omitted_count == len(raw) - len(payload["signals"])


@pytest.mark.asyncio
async def test_quote_failure_preserves_bounded_private_evidence_but_no_raw_response(
    caplog: pytest.LogCaptureFixture,
) -> None:
    execution = ModelExecution()
    config = fixture_config()
    bundle = _bundle(config)
    narrative = _narrative(bundle)
    narrative["narratives"][0]["quotes"][bundle.items[0].evidence_id] = "Fabricated source quotation"
    with patch("digest.irritator.evidence_stage.complete", AsyncMock(return_value=(json.dumps(narrative), {}))):
        async with _offline_client() as client:
            result = await run_evidence_irritator(bundle, config, client, execution=execution)
    diagnostic = next(item for item in result.diagnostics if item.stage == "narrative")
    assert diagnostic.error == "NarrativeQuoteMismatch"
    assert diagnostic.error_detail == "Narrative quote is not in original evidence."
    assert asdict(diagnostic.rejected_quote) == {
        "bundle_id": bundle.bundle_id, "evidence_id": bundle.items[0].evidence_id,
        "quote": "Fabricated source quotation",
    }
    assert result.status == "error" and not result.narratives and not result.source_attempts
    assert "Fabricated source quotation" not in caplog.text
    assert "raw_response" not in asdict(diagnostic) and diagnostic.response_sha256
    # Unknown IDs and overlong fields are rejected before a quote diagnostic exists.
    evidence = bundle.items[0]
    overlong = "x" * (max(len(evidence.title), len(evidence.excerpt)) + 1)
    for identity, quote in (("unknown-source", "Synthetic quote"), (evidence.evidence_id, overlong)):
        rejected = _narrative(bundle)
        rejected["narratives"][0]["evidence_ids"] = [identity]
        rejected["narratives"][0]["quotes"] = {identity: quote}
        with patch("digest.irritator.evidence_stage.complete", AsyncMock(return_value=(json.dumps(rejected), {}))):
            async with _offline_client() as client:
                failed = await run_evidence_irritator(bundle, config, client, execution=execution)
        assert all(item.rejected_quote is None for item in failed.diagnostics)


@pytest.mark.asyncio
@pytest.mark.parametrize("source,pacing", [("hackernews", False), ("devto", False), ("devto", True)])
async def test_search_deadline_marks_attempt_and_restores_client_hooks(source: str, pacing: bool) -> None:
    execution = ModelExecution()
    config = fixture_config()
    config.irritator.sources = [source]
    bundle = _bundle(config)
    requests: list[httpx.Request] = []
    if pacing:
        from digest.irritator.sources.devto import _request_state

        _request_state().next_start = asyncio.get_running_loop().time() + 60

    async def slow(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        await asyncio.sleep(5)
        raise AssertionError("Search deadline was not applied")

    with patch("digest.irritator.evidence_stage.complete", _mock_model(bundle)):
        async with httpx.AsyncClient(transport=httpx.MockTransport(slow)) as client:
            original_hooks = list(client.event_hooks["response"])
            result = await run_evidence_irritator(bundle, config, client, timeout_seconds=0.01, execution=execution)
            assert client.event_hooks["response"] == original_hooks
    assert result.status == "incomplete"
    diagnostic = next(item for item in result.diagnostics if item.stage == "search")
    assert diagnostic.status == "error" and diagnostic.error == "TimeoutError"
    assert result.source_attempts[0].status == "error"
    assert result.source_attempts[0].error == "CancelledError"
    assert len(requests) == (0 if pacing else 1)


@pytest.mark.asyncio
async def test_more_than_three_generated_queries_is_rejected_without_search() -> None:
    execution = ModelExecution()
    config = fixture_config()
    bundle = _bundle(config)
    model = AsyncMock(side_effect=[(json.dumps(_narrative(bundle)), {}), (json.dumps(_queries(4)), {})])
    with patch("digest.irritator.evidence_stage.complete", model):
        async with _offline_client() as client:
            result = await run_evidence_irritator(bundle, config, client, execution=execution)
    assert result.status == "incomplete"
    assert not result.source_attempts
    diagnostic = next(item for item in result.diagnostics if item.stage == "queries")
    assert diagnostic.error_detail == "Invalid response entry count."


@pytest.mark.parametrize(("source_hyphen", "model_hyphen"), [("-", "-"), ("\u2011", "-")])
def test_narrative_hyphen_alignment_recovers_exact_original_quote(source_hyphen: str, model_hyphen: str) -> None:
    config = fixture_config()
    article = make_article(title=f"API{source_hyphen}powered systems")
    bundle = build_evidence_bundle({article.category: [article]}, config.review)
    original = asdict(bundle)
    response = _narrative(bundle)
    identity = bundle.items[0].evidence_id
    response["narratives"][0]["quotes"][identity] = f"API{model_hyphen}powered systems"
    narratives, _ = _parse_narrative(json.dumps(response), bundle)
    assert narratives[0].quotes[identity] == article.title
    assert narratives[0].quotes[identity] in bundle.items[0].title
    assert narratives[0].typography_normalized == ([identity] if source_hyphen != model_hyphen else [])
    assert asdict(bundle) == original


@pytest.mark.parametrize("text", [
    "API-powered limitations", "API\u2011powered limitations", "tradeofff", "literal ... text",
])
def test_ranking_selected_id_preserves_exact_source_text(text: str) -> None:
    bundle = _bundle(fixture_config())
    narrative = _parse_narrative(json.dumps(_narrative(bundle)), bundle)[0][0]
    signal = make_signal(url="https://external.example/caveat", title=text)
    original = asdict(signal)
    ranking = _ranking(signal.url)
    ranking["rankings"][0]["quote_id"] = _ranking_signal_payload(signal)["title"][0]["id"]
    admission = _admit_ranking([signal], 5, 3, {})
    ranked = _parse_rankings(json.dumps(ranking), admission, narrative).ranked_signals
    assert ranked[0].quote == text and not ranked[0].typography_normalized
    assert asdict(signal) == original
    # Archive shape remains literal text; no new ID is needed to read old outcomes.
    assert "quote_id" not in asdict(ranked[0]) and asdict(ranked[0])["quote"] == text
    other = replace(signal, url="https://other.example/same-text")
    ranking["rankings"][0]["url"] = other.url
    with pytest.raises(ValueError, match="not bound"):
        _parse_rankings(json.dumps(ranking), _admit_ranking([other], 5, 3, {}), narrative)


@pytest.mark.parametrize("bad_quote", [
    "API\u2212powered systems", "API\u2013powered systems", "API\u2014powered systems",
    "API-powered platforms", "API-powered ... systems", "API-powered … systems",
])
def test_typography_tolerance_still_rejects_semantic_changes_or_splicing(bad_quote: str) -> None:
    config = fixture_config()
    article = make_article(title="API-powered systems", description="Original evidence describing API-powered systems.")
    bundle = build_evidence_bundle({article.category: [article]}, config.review)
    response = _narrative(bundle)
    response["narratives"][0]["quotes"][bundle.items[0].evidence_id] = bad_quote
    with pytest.raises(ValueError, match="not in original evidence"):
        _parse_narrative(json.dumps(response), bundle)


@pytest.mark.parametrize("model_hyphen", ["-", "\u2011"])
def test_narrative_quote_length_is_bound_to_source_before_typography_repair(model_hyphen: str) -> None:
    config = fixture_config()
    article = make_article(description="API-powered systems " * 10 + "end.")
    bundle = build_evidence_bundle({article.category: [article]}, config.review)
    response = _narrative(bundle)
    identity = bundle.items[0].evidence_id
    quote = article.description.replace("-", model_hyphen)
    assert len(quote) == len(bundle.items[0].excerpt) == 204
    response["narratives"][0]["quotes"][identity] = quote
    narrative = _parse_narrative(json.dumps(response), bundle)[0][0]
    assert narrative.quotes[identity] == bundle.items[0].excerpt
    assert narrative.typography_normalized == ([identity] if model_hyphen != "-" else [])
    response["narratives"][0]["quotes"][identity] = quote + "!"
    with patch("digest.irritator.evidence_stage.canonical_evidence_quote", side_effect=AssertionError("Too early")):
        with pytest.raises(ValueError, match="source_quote:too_long"):
            _parse_narrative(json.dumps(response), bundle)


def test_one_bad_citation_rejects_whole_narrative_after_an_allowed_repair() -> None:
    config = fixture_config()
    articles = [make_article(title="API-powered systems"),
                make_article(link="https://example.com/2", title="Other item")]
    bundle = build_evidence_bundle({articles[0].category: articles}, config.review)
    response = _narrative(bundle)
    cited = response["narratives"][0]
    cited["evidence_ids"] = [item.evidence_id for item in bundle.items]
    cited["quotes"] = {bundle.items[0].evidence_id: bundle.items[0].title.replace("-", "\u2011"),
                       bundle.items[1].evidence_id: "Invented evidence"}
    with pytest.raises(ValueError, match="not in original evidence"):
        _parse_narrative(json.dumps(response), bundle)


@pytest.mark.asyncio
@pytest.mark.parametrize("initialized", [False, True])
async def test_opt_in_translation_observes_the_stage_runtime_pacing(initialized: bool) -> None:
    execution = ModelExecution()
    from digest.config import TranslationConfig
    from digest.llm import _request_state

    config = fixture_config()
    config.translation = TranslationConfig(enabled=True)
    config.llm.max_concurrent_requests = 6
    if initialized:
        execution.request_state(config.llm)
    caller_execution = execution

    async def stages(bundle, bounded, client, result, *, execution):
        shared = caller_execution.request_state(config.llm)
        assert execution is not caller_execution
        assert _request_state(bounded, execution) is shared
        assert shared.semaphore._value == 6 and bounded.llm.max_concurrent_requests == 1
        assert bounded.llm.min_request_interval_seconds == 65
        shared.next_request_at = 195.0
        result.status = "empty"

    with patch("digest.irritator.evidence_stage._run_stages", side_effect=stages):
        async with httpx.AsyncClient() as client:
            result = await run_evidence_irritator(_bundle(config), config, client, execution=execution)
    assert result.status == "empty" and _request_state(config, execution).next_request_at == 195.0


def test_generated_prose_uses_whole_response_budget_and_safe_field_diagnostics() -> None:
    from digest.irritator.evidence_stage import MAX_RESPONSE_CHARS, _safe_error_detail

    bundle = _bundle(fixture_config())
    raw = _narrative(bundle)
    raw["narratives"][0]["claim"] = "Useful context. " * 50
    narrative = _parse_narrative(json.dumps(raw), bundle)[0][0]
    signal = make_signal(url="https://external.example/caveat", title="Deployment limitations")
    ranking = _ranking(signal.url)
    reasoning = "Relevant qualification. " * 40
    ranking["rankings"][0]["reasoning"] = reasoning
    ranking["limitations"] = ["Incomplete external evidence. " * 20]
    admission = _admit_ranking([signal], 5, 3, {})
    parsed = _parse_rankings(json.dumps(ranking), admission, narrative)
    assert parsed.ranked_signals[0].reasoning == reasoning.strip() and len(parsed.limitations[0]) > 400
    for value, code in [(None, "invalid_type"), ("  ", "empty")]:
        ranking["rankings"][0]["reasoning"] = value
        with pytest.raises(ValueError) as error:
            _parse_rankings(json.dumps(ranking), admission, narrative)
        assert _safe_error_detail(error.value) == f"reasoning:{code}"
    ranking["rankings"][0]["reasoning"] = "x" * MAX_RESPONSE_CHARS
    with pytest.raises(ValueError, match="Response exceeds"):
        _parse_rankings(json.dumps(ranking), admission, narrative)


@pytest.mark.asyncio
async def test_full_source_narrative_uses_late_literal_passages_instead_of_rss(tmp_path: Path) -> None:
    execution = ModelExecution()
    config = fixture_config()
    rss_bundle = _bundle(config)
    source_evidence = _full_source_evidence(tmp_path, rss_bundle)
    response = _narrative(source_evidence)
    item = source_evidence.items[0]
    response["narratives"][0]["quotes"][item.evidence_id] = item.excerpt
    response["limitations"] = ["These are provider-reported results from selected source passages."]
    model = AsyncMock(side_effect=[
        (json.dumps(response), {}),
        (json.dumps({"queries": [], "limitations": ["No useful external query was identified."]}), {}),
    ])
    before = asdict(source_evidence)
    with (patch("digest.irritator.evidence_stage.complete", model),
          patch("digest.source_admission.count_gpt_input", return_value=1000)):
        async with _offline_client() as client:
            result = await run_evidence_irritator(
                rss_bundle, config, client, source_evidence=source_evidence, require_full_source=True,
                execution=execution,
            )
    assert result.status == "empty"
    assert result.bundle_id == rss_bundle.bundle_id
    assert result.source_bundle_id == source_evidence.bundle_id
    assert result.narratives[0].quotes == {item.evidence_id: item.excerpt}
    assert not result.narratives[0].typography_normalized
    assert item.start > 500
    assert model.await_count == 2 and not result.source_attempts
    for index, call in enumerate(model.await_args_list):
        payload = json.loads(call.args[1][1]["content"])
        assert payload["evidence"]["bundle_id"] == source_evidence.bundle_id
        if index == 0:
            assert payload["evidence"] == json.loads(json.dumps(before))
            assert "not independent confirmation" in call.args[1][0]["content"]
        else:
            assert payload["evidence"]["items"] == [json.loads(json.dumps(asdict(item)))]
        assert "A model-only reading angle" not in call.args[1][1]["content"]
    assert asdict(source_evidence) == before


@pytest.mark.parametrize("mutation", ["title", "translated", "hyphen", "other_span", "rss_id"])
def test_full_source_quotes_require_exact_text_from_the_identified_span(mutation: str, tmp_path: Path) -> None:
    rss_bundle = _bundle(fixture_config())
    evidence = _full_source_evidence(tmp_path, rss_bundle)
    response = _narrative(evidence)
    narrative = response["narratives"][0]
    item = evidence.items[0]
    quote = item.excerpt
    if mutation == "title":
        quote = item.title
    elif mutation == "translated":
        quote = "The system definitely makes every deployment reliable."
    elif mutation == "hyphen":
        quote = item.excerpt.replace("API-powered", "API\u2011powered")
    elif mutation == "other_span":
        quote = evidence.items[1].excerpt
    else:
        narrative["evidence_ids"] = [rss_bundle.items[0].evidence_id]
        narrative["quotes"] = {rss_bundle.items[0].evidence_id: rss_bundle.items[0].title}
    if mutation != "rss_id":
        narrative["quotes"] = {item.evidence_id: quote}
    with pytest.raises(ValueError):
        _parse_narrative(json.dumps(response), evidence)


@pytest.mark.asyncio
async def test_required_full_source_without_provenance_stays_pending_without_model_or_search() -> None:
    execution = ModelExecution()
    config = fixture_config()
    with patch("digest.irritator.evidence_stage.complete", AsyncMock()) as model:
        async with _offline_client() as client:
            result = await run_evidence_irritator(_bundle(config), config, client, require_full_source=True,
                execution=execution)
    assert result.status == "incomplete"
    assert result.diagnostics[0].error == "FullSourceEvidencePending"
    assert result.diagnostics[1].status == "not_run"
    assert not result.narratives and not result.source_attempts
    model.assert_not_awaited()


@pytest.mark.asyncio
async def test_invalid_full_source_hash_fails_before_model_and_never_falls_back_to_rss(tmp_path: Path) -> None:
    execution = ModelExecution()
    config = fixture_config()
    rss_bundle = _bundle(config)
    evidence = replace(_full_source_evidence(tmp_path, rss_bundle), bundle_id="tampered")
    with patch("digest.irritator.evidence_stage.complete", AsyncMock()) as model:
        async with _offline_client() as client:
            result = await run_evidence_irritator(rss_bundle, config, client, source_evidence=evidence,
                execution=execution)
    assert result.status == "error"
    assert result.diagnostics[0].error_detail == "Full-source evidence hash mismatch."
    assert result.diagnostics[1].status == "not_run"
    assert not result.source_attempts
    model.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("query", [
    "empirical studies on frequency of AI‑generated malware or phishing attacks bypassing existing security controls",
    "evaluations of macOS Full Disk Access permission changes and their actual impact on preventing AI‑based abuse",
    "research on limitations of current AI agents in automating credential theft "
    "or account abuse compared to human attackers",
])
async def test_research_prose_is_incomplete_before_any_source_request(query: str) -> None:
    execution = ModelExecution()
    config = fixture_config()
    bundle = _bundle(config)
    model = AsyncMock(side_effect=[
        (json.dumps(_narrative(bundle)), {}),
        (json.dumps({"queries": [{"query": query, "intent": "Find limitations"}], "limitations": []}), {}),
    ])
    with patch("digest.irritator.evidence_stage.complete", model), \
            patch("digest.irritator.evidence_stage.search_hackernews", AsyncMock()) as search:
        async with _offline_client() as client:
            result = await run_evidence_irritator(bundle, config, client, execution=execution)
    assert result.status == "incomplete"
    assert model.await_count == 2
    search.assert_not_awaited()
    assert any(item.error_detail == "Invalid lexical query contract." for item in result.diagnostics)


def test_rank_segments_preserve_whole_fields_and_bind_changed_evidence() -> None:
    text = "  tradeofff. " + "Long exact source phrase. " * 35 + "\nFinal qualification.  "
    signal = make_signal(title="😀 Exact title", snippet=text)
    before = asdict(signal)
    payload = _ranking_signal_payload(signal)
    for field in ("title", "snippet"):
        assert "".join(item["text"] for item in payload[field]) == getattr(signal, field)
        assert all(0 < len(item["text"]) <= 200 for item in payload[field])
        assert len({item["id"] for item in payload[field]}) == len(payload[field])
    assert payload == _ranking_signal_payload(signal) and asdict(signal) == before
    changed = _ranking_signal_payload(replace(signal, snippet=text.replace("tradeofff", "tradeoff")))
    assert payload["snippet"][0]["id"] != changed["snippet"][0]["id"]


def test_complete_abstract_preserves_exact_late_evidence_before_packet_admission() -> None:
    abstract = ("Background  with exact spacing. " * 30
                + "Our evaluation preserves utility while reducing the measured attacks. "
                + "Only the tested deployment was evaluated; tradeofff remains workload dependent.")
    signal = make_signal(title="Exact  title", snippet=abstract)
    validated = _bounded_signals([signal], "arxiv")
    assert validated[0].title == signal.title
    assert validated[0].snippet == abstract
    candidates = _admit_ranking(validated, 5, 3, {}).signals
    assert len(candidates) == 1
    payload = _ranking_signal_payload(candidates[0])
    assert "".join(part["text"] for part in payload["snippet"]) == abstract
    assert len(json.dumps([payload], ensure_ascii=False)) <= MAX_RANKING_JSON_CHARS
    oversized = make_signal(url="https://example.org/too-large", snippet="x" * 9000)
    assert _admit_ranking(_bounded_signals([oversized, signal], "arxiv"), 5, 3, {}).signals == tuple(validated)


@pytest.mark.asyncio
async def test_no_complete_candidate_fits_skips_rank_and_reports_incomplete() -> None:
    execution = ModelExecution()
    config = fixture_config()
    config.irritator.sources = ["arxiv"]
    bundle = _bundle(config)
    model = AsyncMock(side_effect=[(json.dumps(_narrative(bundle)), {}), (json.dumps(_queries()), {})])
    signal = make_signal(snippet="x" * 9000)
    with (
        patch("digest.irritator.evidence_stage.complete", model),
        patch("digest.irritator.evidence_stage.search_arxiv", AsyncMock(return_value=[signal])),
    ):
        async with _offline_client() as client:
            result = await run_evidence_irritator(bundle, config, client, execution=execution)
    assert model.await_count == 2
    assert result.status == "incomplete"
    ranking = next(item for item in result.diagnostics if item.stage == "ranking")
    assert ranking.status == "incomplete" and ranking.omitted_count == 1
    assert result.ranked_signals == []


@pytest.mark.asyncio
async def test_known_late_qualification_reaches_queries_and_ranking(tmp_path: Path) -> None:
    execution = ModelExecution()
    config = fixture_config()
    config.irritator.sources = ["hackernews"]
    rss = _bundle(config)
    source = _full_source_evidence(tmp_path, rss)
    response = _narrative(source)
    cited, qualification = source.items
    response["narratives"][0]["quotes"][cited.evidence_id] = cited.excerpt
    model = AsyncMock(side_effect=[
        (json.dumps(response), {}), (json.dumps(_queries(anchor="rollout")), {}), (json.dumps(_ranking()), {}),
    ])
    before = asdict(source)
    with (patch("digest.irritator.evidence_stage.complete", model),
          patch("digest.source_admission.count_gpt_input", return_value=1000),
          patch("digest.irritator.evidence_stage.search_hackernews", AsyncMock(return_value=[
              make_signal(url="https://external.example/caveat", title="Deployment limitations"),
          ]))):
        async with _offline_client() as client:
            result = await run_evidence_irritator(rss, config, client, source_evidence=source, execution=execution)
    assert result.status == "complete" and model.await_count == 3
    assert result.narratives[0].claim == response["narratives"][0]["claim"]
    assert result.narratives[0].evidence_ids == [cited.evidence_id]
    assert result.narratives[0].quotes == {cited.evidence_id: cited.excerpt}
    for index, call in enumerate(model.await_args_list[1:], start=1):
        payload = json.loads(call.args[1][1]["content"])
        assert ("exploratory_hypotheses" in payload) is (index == 1)
        assert "implicit_assumptions" not in payload["narrative"]
        assert payload["evidence"]["items"] == [json.loads(json.dumps(asdict(cited)))]
        assert payload["evidence"]["qualification_context"] == [json.loads(json.dumps(asdict(qualification)))]
        assert payload["evidence"]["limited_to_narrative_citations"] is False
        assert payload["evidence"]["complete_article_context"] is False
        assert "not new narrative claims or complete article context" in call.args[1][0]["content"]
    assert asdict(source) == before


@pytest.mark.parametrize("difference", ["article", "snapshot", "body"])
def test_qualification_context_does_not_cross_source_bindings(tmp_path: Path, difference: str) -> None:
    from digest.irritator.evidence_stage import _narrative_context
    from digest.review_checkpoint import _identity_hash

    config = fixture_config()
    source = _full_source_evidence(tmp_path, _bundle(config))
    cited, qualification = source.items
    response = _narrative(source)
    response["narratives"][0]["quotes"][cited.evidence_id] = cited.excerpt
    narrative = _parse_narrative(json.dumps(response), source)[0][0]
    if difference == "article":
        from digest.radar.collector import article_hash

        title, url = "Another source article", "https://another.example/announcement"
        unrelated = replace(qualification, article_id=article_hash(title, url), title=title, url=url, span_id=99)
    else:
        field = "source_sha256" if difference == "snapshot" else "body_sha256"
        unrelated = replace(qualification, **{field: "b" * 64}, span_id=99)
    unrelated = replace(unrelated, evidence_id=_identity_hash(unrelated, "evidence_id"))
    context = _narrative_context(replace(source, items=(*source.items, unrelated)), narrative)
    assert [item["evidence_id"] for item in context["qualification_context"]] == [qualification.evidence_id]
    assert unrelated.evidence_id not in json.dumps(context)
    # A qualification already cited remains in the citation set, without duplication.
    narrative.evidence_ids.append(qualification.evidence_id)
    context = _narrative_context(source, narrative)
    assert context["qualification_context"] == [] and len(context["items"]) == 2


@pytest.mark.asyncio
async def test_oversized_complete_context_stops_before_optional_queries(tmp_path: Path) -> None:
    execution = ModelExecution()
    import hashlib

    from digest.domain.editorial.reviews import MAX_EVIDENCE_JSON_CHARS
    from digest.review_checkpoint import _identity_hash

    config = fixture_config()
    rss = _bundle(config)
    source = _full_source_evidence(tmp_path, rss)
    cited, qualification = source.items
    # Valid source-context metadata, rather than excerpt text alone, exceeds the envelope.
    qualification = replace(qualification, source="Publisher " + "x" * MAX_EVIDENCE_JSON_CHARS)
    qualification = replace(qualification, evidence_id=_identity_hash(qualification, "evidence_id"))
    source = replace(source, items=(cited, qualification))
    source = replace(source, bundle_id=_identity_hash(source, "bundle_id"))
    response = _narrative(source)
    response["narratives"][0]["quotes"][cited.evidence_id] = cited.excerpt
    model = AsyncMock(return_value=(json.dumps(response), {}))
    before = hashlib.sha256(json.dumps(asdict(source), sort_keys=True).encode()).hexdigest()
    with (patch("digest.irritator.evidence_stage.complete", model),
          patch("digest.source_admission.count_gpt_input", return_value=1000),
          patch("digest.irritator.evidence_stage.search_hackernews", AsyncMock()) as search):
        async with _offline_client() as client:
            result = await run_evidence_irritator(rss, config, client, source_evidence=source, execution=execution)
    assert result.status == "incomplete" and model.await_count == 1
    search.assert_not_awaited()
    assert result.narratives and not result.queries and not result.ranked_signals
    assert next(d for d in result.diagnostics if d.stage == "queries").error == "QualificationContextBudget"
    assert any("not provider token admission" in item for item in result.limitations)
    assert hashlib.sha256(json.dumps(asdict(source), sort_keys=True).encode()).hexdigest() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("hold", ["unsupported", "oversized", "budget"])
async def test_full_source_stage_holds_before_unadmitted_generation(tmp_path: Path, hold: str) -> None:
    execution = ModelExecution()
    from digest import llm
    from digest.config import ReviewModelConfig

    config = fixture_config()
    rss = _bundle(config)
    source = _full_source_evidence(tmp_path, rss)
    if hold == "unsupported":
        config.review.secondary = ReviewModelConfig("groq", "unverified-model")
    if hold == "budget":
        llm.set_request_limit(config, execution, 0)
    with (patch("digest.source_admission.count_gpt_input", return_value=100_000 if hold == "oversized" else 1000),
          patch("digest.irritator.evidence_stage.complete", AsyncMock()) as generate,
          patch("digest.irritator.evidence_stage.search_hackernews", AsyncMock()) as search):
        async with _offline_client() as client:
            result = await run_evidence_irritator(rss, config, client, source_evidence=source, execution=execution)
    assert result.status == "incomplete"
    generate.assert_not_awaited()
    search.assert_not_awaited()
    diagnostic = next(item for item in result.diagnostics if item.stage == "narrative")
    assert diagnostic.admission is not None and not diagnostic.admission.admitted
    assert diagnostic.error == {
        "unsupported": "technical_unknown_profile", "oversized": "technical_admission_capacity",
        "budget": "technical_request_budget",
    }[hold]
    assert diagnostic.admission.output_reserve == MAX_OUTPUT_TOKENS
    if hold == "oversized":
        assert diagnostic.admission.method == "estimated" and diagnostic.admission.exact_count is None
        assert diagnostic.admission.input_estimate is not None
    if hold == "budget":
        assert llm.request_budget_remaining(config, execution) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["provider", "model", "output_reserve", "request_sha256"])
async def test_full_source_dispatch_requires_exact_admission_binding(tmp_path: Path, mutation: str) -> None:
    execution = ModelExecution()
    from digest.source_admission import RequestAdmission, admit_request

    config = fixture_config()
    rss = _bundle(config)
    source = _full_source_evidence(tmp_path, rss)

    async def changed(*args: Any, **kwargs: Any) -> RequestAdmission:
        record = await admit_request(*args, **kwargs)
        assert record.admitted
        return replace(record, **{mutation: 1 if mutation == "output_reserve" else "different"})

    with (patch("digest.source_admission.count_gpt_input", return_value=1000),
          patch("digest.irritator.evidence_stage.admit_request", side_effect=changed),
          patch("digest.irritator.evidence_stage.complete", AsyncMock()) as generate):
        async with _offline_client() as client:
            result = await run_evidence_irritator(rss, config, client, source_evidence=source, execution=execution)
    assert result.status == "incomplete"
    generate.assert_not_awaited()
    assert next(item for item in result.diagnostics if item.stage == "narrative").error == "technical_request_binding"


@pytest.mark.asyncio
async def test_legacy_rss_path_does_not_add_source_count_or_admission() -> None:
    execution = ModelExecution()
    config = fixture_config()
    rss = _bundle(config)
    model = AsyncMock(side_effect=[
        (json.dumps(_narrative(rss)), {}),
        (json.dumps({"queries": [], "limitations": ["No useful query."]}), {}),
    ])
    with (patch("digest.irritator.evidence_stage.complete", model),
          patch("digest.irritator.evidence_stage.admit_request", side_effect=AssertionError("No source admission"))):
        async with _offline_client() as client:
            result = await run_evidence_irritator(rss, config, client, execution=execution)
    assert result.status == "empty" and model.await_count == 2
    assert all(item.admission is None for item in result.diagnostics)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider,configured,expected,attempts", [
    ("gemini", 20.0, 20.0, 6), ("groq", 20.0, 65.0, 3),
    ("gemini", 90.0, 90.0, 6), ("groq", 90.0, 90.0, 3),
])
async def test_full_source_route_pacing_keeps_one_deadline_and_counter(
    tmp_path: Path, provider: str, configured: float, expected: float, attempts: int,
) -> None:
    execution = ModelExecution()
    import time

    from digest import llm
    from digest.config import ReviewModelConfig
    from digest.source_admission import RequestAdmission, admit_request

    config = fixture_config()
    config.review.secondary = ReviewModelConfig(
        provider, "gemini-3.8-flash" if provider == "gemini" else "openai/gpt-oss-120b",
    )
    config.llm.min_request_interval_seconds = configured
    config.irritator.sources = ["hackernews"]
    llm.set_request_limit(config, execution, 10)
    shared = llm._request_state(config, execution)
    rss = _bundle(config)
    source = _full_source_evidence(tmp_path, rss)
    narrative = _narrative(source)
    narrative["narratives"][0]["quotes"][source.items[0].evidence_id] = source.items[0].excerpt
    responses = iter([narrative, _queries(anchor="rollout"), _ranking()])
    deadlines: list[float] = []

    async def admission(messages: Any, bounded: Any, **kwargs: Any) -> RequestAdmission:
        assert kwargs["execution"] is not execution
        assert llm._request_state(bounded, kwargs["execution"]) is shared
        assert shared.semaphore._value == config.llm.max_concurrent_requests == 4
        assert bounded.llm.max_concurrent_requests == 1
        assert bounded.llm.min_request_interval_seconds == expected
        deadlines.append(kwargs["deadline"])
        return await admit_request(messages, bounded, **kwargs)

    async def count(messages: Any, bounded: Any, **kwargs: Any) -> int:
        actual = kwargs["provider_override"]
        assert actual.name == provider and actual.model == config.review.secondary.model
        await llm._pace_request(shared, bounded.llm.min_request_interval_seconds)
        llm._reserve_request(shared, actual.name, actual.model, "count")
        return 1000

    async def generate(client: Any, actual: Any, messages: Any, *args: Any, **kwargs: Any) -> Any:
        assert actual.name == provider and actual.model == config.review.secondary.model
        return json.dumps(next(responses)), {"finish_reason": "stop"}

    started = time.monotonic()
    with (patch("digest.irritator.evidence_stage.admit_request", side_effect=admission),
          patch("digest.source_admission.count_gpt_input", return_value=1000),
          patch("digest.llm.count_gemini_tokens", side_effect=count),
          patch("digest.llm._pace_request", AsyncMock()) as pace,
          patch("digest.llm._call_provider", side_effect=generate),
          patch("digest.irritator.evidence_stage.search_hackernews", AsyncMock(return_value=[
              make_signal(url="https://external.example/caveat", title="Deployment limitations"),
          ]))):
        async with _offline_client() as client:
            result = await run_evidence_irritator(rss, config, client, source_evidence=source, execution=execution)
    assert result.status == "complete"
    assert len(deadlines) == 3 and len(set(deadlines)) == 1
    assert started + 180 <= deadlines[0] <= started + 181
    assert llm.request_budget_remaining(config, execution) == 10 - attempts
    assert [call.args[1] for call in pace.call_args_list] == [expected] * attempts
    assert config.llm.min_request_interval_seconds == configured


@pytest.mark.asyncio
@pytest.mark.parametrize("full_source", [False, True], ids=["rss", "full-source"])
async def test_queries_without_literal_source_anchor_reach_search(tmp_path: Path, full_source: bool) -> None:
    execution = ModelExecution()
    config = fixture_config()
    config.irritator.sources = ["hackernews", "arxiv", "devto"]
    bundle = _bundle(config)
    source = _full_source_evidence(tmp_path, bundle) if full_source else None
    narrative = _narrative(source or bundle)
    if source is not None:
        narrative["narratives"][0]["quotes"][source.items[0].evidence_id] = source.items[0].excerpt
    # Preserve the formerly blocked set; reaching mocked search makes no quality claim.
    queries = ["Benchmark reports cancelled", "Vendor benchmark controversy", "Model latency scandal"]
    # A phrase occurring only in model hypotheses must never become source provenance.
    narrative["narratives"][0]["implicit_assumptions"] = [queries[0]]
    response = {"queries": [{"query": query, "intent": "Test a possible opposing hypothesis."}
                            for query in queries], "limitations": []}
    model = AsyncMock(side_effect=[(json.dumps(narrative), {}), (json.dumps(response), {})])
    with (patch("digest.irritator.evidence_stage.complete", model),
          patch("digest.source_admission.count_gpt_input", return_value=1000),
          patch("digest.irritator.evidence_stage.search_hackernews", AsyncMock(return_value=[])) as hn,
          patch("digest.irritator.evidence_stage.search_arxiv", AsyncMock(return_value=[])) as arxiv,
          patch("digest.irritator.evidence_stage.search_devto", AsyncMock(return_value=[])) as devto):
        async with _offline_client() as client:
            result = await run_evidence_irritator(bundle, config, client, source_evidence=source, execution=execution)
    assert result.status == "empty" and model.await_count == 2
    assert [item.query for item in result.queries] == queries and result.query_anchor is None
    assert [item.intent for item in result.queries] == [item["intent"] for item in response["queries"]]
    diagnostic = next(item for item in result.diagnostics if item.stage == "queries")
    assert diagnostic.status == "complete" and diagnostic.error == ""
    assert diagnostic.output_count == 3 and len(result.source_attempts) == 9
    assert all(item.status == "empty" for item in result.source_attempts)
    for search in (hn, arxiv, devto):
        assert [call.args[0] for call in search.await_args_list] == queries
    assert not any("source-text anchor" in item for item in result.limitations)


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["missing_intent", "extra_field", "invalid_entries"])
async def test_invalid_query_schema_still_stops_before_search(mutation: str) -> None:
    execution = ModelExecution()
    config = fixture_config()
    config.irritator.sources = ["hackernews"]
    bundle = _bundle(config)
    response = _queries(anchor="reliability evaluation")
    if mutation == "missing_intent":
        del response["queries"][0]["intent"]
    elif mutation == "extra_field":
        response["queries"][0]["source_fact"] = "Unsupported claim"
    else:
        response["queries"] = {"query": "reliability evaluation", "intent": "Explore a hypothesis."}
    model = AsyncMock(side_effect=[(json.dumps(_narrative(bundle)), {}), (json.dumps(response), {})])
    with (patch("digest.irritator.evidence_stage.complete", model),
          patch("digest.irritator.evidence_stage.search_hackernews", AsyncMock()) as search):
        async with _offline_client() as client:
            result = await run_evidence_irritator(bundle, config, client, execution=execution)
    assert result.status == "incomplete" and model.await_count == 2
    assert not result.queries and not result.source_attempts and result.query_anchor is None
    diagnostic = next(item for item in result.diagnostics if item.stage == "queries")
    assert diagnostic.status == "error" and diagnostic.error == "ValueError"
    search.assert_not_awaited()


@pytest.mark.asyncio
async def test_source_anchor_preserves_three_queries_and_exploratory_hypotheses() -> None:
    execution = ModelExecution()
    config = fixture_config()
    config.irritator.sources = ["hackernews"]
    bundle = _bundle(config)
    queries = ['"benchmark reports"', "latency measurement", "model comparison"]
    response = {"queries": [{"query": query, "intent": "Evaluate the stated finding."}
                            for query in queries], "limitations": []}
    model = AsyncMock(side_effect=[(json.dumps(_narrative(bundle)), {}), (json.dumps(response), {})])
    with (patch("digest.irritator.evidence_stage.complete", model),
          patch("digest.irritator.evidence_stage.search_hackernews", AsyncMock(return_value=[])) as search):
        async with _offline_client() as client:
            result = await run_evidence_irritator(bundle, config, client, execution=execution)
    assert result.status == "empty" and model.await_count == 2 and search.await_count == 3
    assert [item.query for item in result.queries] == queries
    anchor = result.query_anchor
    assert anchor is not None and anchor.query_index == 0 and anchor.query == queries[0]
    assert anchor.evidence_bundle_id == bundle.bundle_id and anchor.evidence_id == bundle.items[0].evidence_id
    assert anchor.field == "title" and anchor.matched_text == bundle.items[0].title[anchor.start:anchor.end]
    assert anchor.matched_text == "Benchmark reports"
    assert any("neutrality and retrieval usefulness are not certified" in item for item in result.limitations)


@pytest.mark.asyncio
async def test_uncited_evidence_cannot_supply_the_query_anchor() -> None:
    execution = ModelExecution()
    config = fixture_config()
    config.irritator.sources = ["hackernews"]
    bundle = _bundle(config)
    response = _queries(anchor=bundle.items[1].title)
    model = AsyncMock(side_effect=[(json.dumps(_narrative(bundle)), {}), (json.dumps(response), {})])
    with (patch("digest.irritator.evidence_stage.complete", model),
          patch("digest.irritator.evidence_stage.search_hackernews", AsyncMock(return_value=[])) as search):
        async with _offline_client() as client:
            result = await run_evidence_irritator(bundle, config, client, execution=execution)
    assert result.status == "empty" and result.query_anchor is None and len(result.source_attempts) == 1
    assert model.await_count == 2
    assert next(item for item in result.diagnostics if item.stage == "queries").status == "complete"
    assert search.await_args is not None and search.await_args.args[0] == bundle.items[1].title


@pytest.mark.asyncio
async def test_query_anchor_binds_actual_full_source_qualification_context(tmp_path: Path) -> None:
    execution = ModelExecution()
    config = fixture_config()
    config.irritator.sources = ["hackernews"]
    rss = _bundle(config)
    source = _full_source_evidence(tmp_path, rss)
    cited, qualification = source.items
    narrative = _narrative(source)
    narrative["narratives"][0]["quotes"][cited.evidence_id] = cited.excerpt
    model = AsyncMock(side_effect=[
        (json.dumps(narrative), {}), (json.dumps(_queries(anchor="trial deployment")), {}),
    ])
    with (patch("digest.irritator.evidence_stage.complete", model),
          patch("digest.source_admission.count_gpt_input", return_value=1000),
          patch("digest.irritator.evidence_stage.search_hackernews", AsyncMock(return_value=[]))):
        async with _offline_client() as client:
            result = await run_evidence_irritator(rss, config, client, source_evidence=source, execution=execution)
    assert result.status == "empty" and model.await_count == 2
    anchor = result.query_anchor
    assert anchor is not None and anchor.evidence_bundle_id == source.bundle_id != rss.bundle_id
    assert anchor.evidence_id == qualification.evidence_id and anchor.field == "excerpt"
    assert qualification.excerpt[anchor.start:anchor.end] == anchor.matched_text == "trial deployment"
    assert result.narratives[0].evidence_ids == [cited.evidence_id]


@pytest.mark.asyncio
async def test_exact_cited_url_is_not_ranked_as_external_evidence() -> None:
    execution = ModelExecution()
    config = fixture_config()
    config.irritator.sources = ["devto"]
    bundle = _bundle(config)
    cited_url = bundle.items[0].url
    other_document = "https://example.com/another-document"
    description = "  Observed benefit only in a controlled trial.\nOutside that scope, results are unknown.  "
    raw = [make_signal(url=url, title="Deployment limitations", snippet=description) for url in (
        cited_url, other_document, "https://another.example/evidence",
    )]
    ranking = _ranking(other_document)
    ranking["rankings"][0]["quote_id"] = _ranking_signal_payload(raw[1])["snippet"][0]["id"]
    model = AsyncMock(side_effect=[
        (json.dumps(_narrative(bundle)), {}), (json.dumps(_queries()), {}),
        (json.dumps(ranking), {}),
    ])
    requests: list[httpx.Request] = []

    def search(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.host == "dev.to" and request.url.path == "/api/articles/search"
        return httpx.Response(200, json=[{
            "url": item.url, "title": item.title, "description": item.snippet,
            "published_at": "2026-10-08T10:00:00Z",
        } for item in raw])

    with patch("digest.irritator.evidence_stage.complete", model):
        async with httpx.AsyncClient(transport=httpx.MockTransport(search)) as client:
            result = await run_evidence_irritator(bundle, config, client, execution=execution)
    assert result.status == "complete" and model.await_count == 3
    assert len(requests) == 1
    assert result.excluded_cited_source_urls == [cited_url]
    ranking_payload = json.loads(model.await_args_list[2].args[1][1]["content"])
    assert {item["url"] for item in ranking_payload["signals"]} == {other_document, "https://another.example/evidence"}
    assert result.ranked_signals[0].signal.url == other_document
    assert result.ranked_signals[0].quote == description
    assert result.ranked_signals[0].signal.source_name == "devto"
    assert result.ranking_audit is not None
    assert all(candidate.signal.snippet == description and candidate.query_indices == [0]
               for candidate in result.ranking_audit.candidates)
    validation = next(item for item in result.diagnostics if item.stage == "validation")
    assert (validation.input_count, validation.output_count, validation.omitted_count) == (3, 2, 1)
    assert result.source_attempts[0].result_count == 3


@pytest.mark.asyncio
async def test_verified_final_url_exclusion_preserves_counts_and_different_documents(tmp_path: Path) -> None:
    execution = ModelExecution()
    config = fixture_config()
    config.irritator.sources = ["hackernews"]
    rss = _bundle(config)
    final_url = "https://provider.example/canonical-announcement"
    source = _full_source_evidence(tmp_path, rss, final_url=final_url)
    cited = source.items[0]
    other_document = "https://provider.example/followup-document"
    raw = [make_signal(url=url, title="Deployment limitations") for url in (
        cited.url, cited.url, final_url, other_document,
    )]
    narrative = _narrative(source)
    narrative["narratives"][0]["quotes"][cited.evidence_id] = cited.excerpt
    model = AsyncMock(side_effect=[
        (json.dumps(narrative), {}), (json.dumps(_queries(anchor="rollout")), {}),
        (json.dumps(_ranking(other_document)), {}),
    ])
    with (patch("digest.irritator.evidence_stage.complete", model),
          patch("digest.source_admission.count_gpt_input", return_value=1000),
          patch("digest.irritator.evidence_stage.search_hackernews", AsyncMock(return_value=raw))):
        async with _offline_client() as client:
            result = await run_evidence_irritator(rss, config, client, source_evidence=source, execution=execution)
    assert result.status == "complete" and model.await_count == 3
    assert result.excluded_cited_source_urls == sorted([cited.url, final_url])
    ranking_payload = json.loads(model.await_args_list[2].args[1][1]["content"])
    assert [item["url"] for item in ranking_payload["signals"]] == [other_document]
    validation = next(item for item in result.diagnostics if item.stage == "validation")
    assert (validation.input_count, validation.output_count, validation.omitted_count) == (4, 1, 3)
    assert result.source_attempts[0].result_count == 4


@pytest.mark.asyncio
async def test_only_self_source_hits_skip_ranking_with_explicit_exclusion() -> None:
    execution = ModelExecution()
    config = fixture_config()
    config.irritator.sources = ["hackernews"]
    bundle = _bundle(config)
    cited_url = bundle.items[0].url
    model = AsyncMock(side_effect=[(json.dumps(_narrative(bundle)), {}), (json.dumps(_queries()), {})])
    with (patch("digest.irritator.evidence_stage.complete", model),
          patch("digest.irritator.evidence_stage.search_hackernews", AsyncMock(return_value=[
              make_signal(url=cited_url),
          ])) as search):
        async with _offline_client() as client:
            result = await run_evidence_irritator(bundle, config, client, execution=execution)
    assert result.status == "empty" and model.await_count == 2 and search.await_count == 1
    assert result.excluded_cited_source_urls == [cited_url] and not result.ranked_signals
    assert next(item for item in result.diagnostics if item.stage == "ranking").status == "not_run"
    assert any("repeat known cited sources" in limitation for limitation in result.limitations)
    assert "not confirmation" in result.coverage
