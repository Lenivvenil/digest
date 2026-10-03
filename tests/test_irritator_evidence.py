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
    _bounded_signals,
    _parse_narrative,
    _parse_rankings,
    _ranking_signal_payload,
    run_evidence_irritator,
)
from digest.irritator.ranker import RANK_RELATION_CONTRACT
from digest.llm import LLMRole
from digest.review import EvidenceBundle, build_evidence_bundle
from scripts.review_fixture import fixture_articles, fixture_config
from tests.factories import make_article, make_signal


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
    config = fixture_config()
    config.llm.max_retries = 3
    config.llm.max_concurrent_requests = 4
    config.llm.min_request_interval_seconds = 0
    config.llm.providers = [ProviderConfig("gemini", "unapproved-fallback", ["fallback"])]
    config.irritator.sources = ["hackernews"]
    bundle = _bundle(config)
    original_bundle, original_config = asdict(bundle), asdict(config)
    original_narrative = _narrative(bundle)["narratives"][0]
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
    for index, call in enumerate(model.await_args_list):
        payload = json.loads(call.args[1][1]["content"])
        if index == 0:
            assert payload["evidence"] == json.loads(json.dumps(original_bundle))
        else:
            assert payload["narrative"] == {
                "claim": original_narrative["claim"], "category": original_narrative["category"],
                "evidence_ids": original_narrative["evidence_ids"], "quotes": original_narrative["quotes"],
            }
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
        item["quote_id"] = "unknown-id"
    elif mutation == "float_score":
        item["score"] = 8.0
    elif mutation == "bool_score":
        item["score"] = True
    elif mutation == "high_score":
        item["score"] = 11
    elif mutation == "bad_relation":
        item["relation"] = "unknown"
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


@pytest.mark.parametrize("relation", ["supports", "context", "insufficient"])
def test_high_scoring_non_counter_relations_excluded(relation: str) -> None:
    bundle = _bundle(fixture_config())
    narrative = _parse_narrative(json.dumps(_narrative(bundle)), bundle)[0][0]
    signal = make_signal(url="https://external.example/caveat", title="Deployment limitations")
    ranking = _ranking()
    ranking["rankings"][0].update(relation=relation, score=10)
    ranked, limitations = _parse_rankings(json.dumps(ranking), [signal], narrative, 3, 1)
    assert ranked == []
    counts = ", ".join(f"{label}={int(label == relation)}" for label in ("supports", "context", "insufficient"))
    assert limitations == [f"Ranking omitted non-counter signals: {counts}."]


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
    ranked, limitations = _parse_rankings(json.dumps({"rankings": entries, "limitations": ["Search is limited."]}),
                                          [supportive, complication, low_score], narrative, 3, 5)
    assert len(ranked) == 1
    assert ranked[0].signal == complication and ranked[0].relation == "complicates"
    assert ranked[0].quote == complication.title and ranked[0].score == 5
    assert set(asdict(ranked[0])) == {
        "signal", "score", "reasoning", "narrative_claim", "relation", "quote", "typography_normalized",
    }
    assert limitations == ["Search is limited.",
                           "Ranking omitted non-counter signals: supports=1, context=0, insufficient=0."]


@pytest.mark.parametrize(("field", "value"), [
    ("quote_id", "unknown-id"), ("relation", "unknown"), ("score", True), ("reasoning", "  "),
])
def test_non_counter_entries_validated_before_filtering(field: str, value: Any) -> None:
    bundle = _bundle(fixture_config())
    narrative = _parse_narrative(json.dumps(_narrative(bundle)), bundle)[0][0]
    signals = [make_signal(url=url, title="Deployment limitations")
               for url in ("https://external.example/caveat", "https://external.example/other")]
    ranking = _ranking(signals[0].url)
    invalid = _ranking(signals[1].url)["rankings"][0]
    invalid.update(relation="supports", score=1)
    invalid[field] = value
    ranking["rankings"].append(invalid)
    with pytest.raises(ValueError):
        _parse_rankings(json.dumps(ranking), signals, narrative, 3, 5)


@pytest.mark.parametrize("mutation", ["duplicate_url", "cross_url_quote", "missing_relation", "extra_field"])
def test_non_counter_entries_preserve_identity_and_shape_checks(mutation: str) -> None:
    bundle = _bundle(fixture_config())
    narrative = _parse_narrative(json.dumps(_narrative(bundle)), bundle)[0][0]
    signals = [make_signal(url=url, title="Deployment limitations")
               for url in ("https://external.example/caveat", "https://external.example/other")]
    ranking = _ranking(signals[0].url)
    ranking["rankings"][0].update(relation="supports", score=1)
    item = _ranking(signals[1].url)["rankings"][0]
    if mutation == "duplicate_url":
        item["url"] = signals[0].url
    elif mutation == "cross_url_quote":
        ranking["rankings"][0]["quote_id"] = item["quote_id"]
    elif mutation == "missing_relation":
        del ranking["rankings"][0]["relation"]
    else:
        ranking["rankings"][0]["extra"] = "unexpected"
    ranking["rankings"].append(item)
    with pytest.raises(ValueError):
        _parse_rankings(json.dumps(ranking), signals, narrative, 3, 5)


@pytest.mark.asyncio
async def test_all_non_counter_relations_are_honest_empty_with_omission_counts() -> None:
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
            result = await run_evidence_irritator(bundle, config, client)
    assert result.status == "empty" and result.ranked_signals == []
    assert all(d.status not in {"error", "incomplete"} for d in result.diagnostics)
    ranking_stage = next(d for d in result.diagnostics if d.stage == "ranking")
    assert ranking_stage.status == "empty" and ranking_stage.output_count == 0
    assert "Ranking omitted non-counter signals: supports=1, context=1, insufficient=1." in result.limitations
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
        raise AssertionError("Unexpected endpoint")

    with patch("digest.irritator.evidence_stage.complete", _mock_model(bundle)):
        async with httpx.AsyncClient(transport=httpx.MockTransport(adapter)) as client:
            result = await run_evidence_irritator(bundle, config, client)
    assert result.status == "incomplete"
    assert len(requests) == 2
    assert result.ranked_signals[0].signal.url == "https://external.example/caveat"
    assert any("liveness" in limitation for limitation in result.limitations)
    assert {attempt.source: attempt.status for attempt in result.source_attempts} == {
        "hackernews": "complete", "arxiv": "empty", "lobsters": "unavailable",
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
    assert result.status == "incomplete"
    diagnostic = next(item for item in result.diagnostics if item.stage == "ranking")
    assert diagnostic.omitted_count == len(raw) - len(payload["signals"])


@pytest.mark.asyncio
async def test_quote_failure_preserves_bounded_private_evidence_but_no_raw_response(
    caplog: pytest.LogCaptureFixture,
) -> None:
    config = fixture_config()
    bundle = _bundle(config)
    narrative = _narrative(bundle)
    narrative["narratives"][0]["quotes"][bundle.items[0].evidence_id] = "Fabricated source quotation"
    with patch("digest.irritator.evidence_stage.complete", AsyncMock(return_value=(json.dumps(narrative), {}))):
        async with _offline_client() as client:
            result = await run_evidence_irritator(bundle, config, client)
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
                failed = await run_evidence_irritator(bundle, config, client)
        assert all(item.rejected_quote is None for item in failed.diagnostics)


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


@pytest.mark.parametrize("source_hyphen", ["-", "\u2010", "\u2011"])
@pytest.mark.parametrize("model_hyphen", ["-", "\u2010", "\u2011"])
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
    ranked, _ = _parse_rankings(json.dumps(ranking), [signal], narrative, 3, 5)
    assert ranked[0].quote == text and not ranked[0].typography_normalized
    assert asdict(signal) == original
    # Archive shape remains literal text; no new ID is needed to read old outcomes.
    assert "quote_id" not in asdict(ranked[0]) and asdict(ranked[0])["quote"] == text
    other = replace(signal, url="https://other.example/same-text")
    ranking["rankings"][0]["url"] = other.url
    with pytest.raises(ValueError, match="not bound"):
        _parse_rankings(json.dumps(ranking), [other], narrative, 3, 5)


@pytest.mark.parametrize("bad_quote", [
    "API\u2212powered systems", "API\u2013powered systems", "API\u2014powered systems",
    "API-powered platforms", "API-powered ... systems", "API-powered … systems",
])
def test_typography_tolerance_still_rejects_semantic_changes_or_splicing(bad_quote: str) -> None:
    config = fixture_config()
    article = make_article(title="API-powered systems", description="Original evidence describing API-powered systems.")
    bundle = build_evidence_bundle({article.category: [article]}, config.review)
    response = _narrative(bundle)
    narrative = _parse_narrative(json.dumps(response), bundle)[0][0]
    response["narratives"][0]["quotes"][bundle.items[0].evidence_id] = bad_quote
    with pytest.raises(ValueError, match="not in original evidence"):
        _parse_narrative(json.dumps(response), bundle)
    signal = make_signal(url="https://external.example/caveat", title=article.title)
    ranking = _ranking(signal.url)
    ranking["rankings"][0]["quote"] = bad_quote
    with pytest.raises(ValueError, match="Invalid ranking fields"):
        _parse_rankings(json.dumps(ranking), [signal], narrative, 3, 5)


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
    signal = make_signal(url="https://external.example/caveat", snippet=article.description)
    ranking = _ranking(signal.url)
    ranking["rankings"][0]["quote"] = article.description.replace("-", "\u2011")
    with patch("digest.irritator.evidence_stage.canonical_evidence_quote", side_effect=AssertionError("Too early")):
        with pytest.raises(ValueError, match="source_quote:too_long"):
            _parse_narrative(json.dumps(response), bundle)
        with pytest.raises(ValueError, match="Invalid ranking fields"):
            _parse_rankings(json.dumps(ranking), [signal], narrative, 3, 5)


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
async def test_opt_in_translation_observes_the_stage_runtime_pacing() -> None:
    from digest.config import TranslationConfig
    from digest.llm import _request_state

    config = fixture_config()
    config.translation = TranslationConfig(enabled=True)
    shared = _request_state(config)

    async def stages(bundle, bounded, client, result):
        assert _request_state(bounded) is shared
        assert bounded.llm.min_request_interval_seconds == 65
        shared.next_request_at = 195.0
        result.status = "empty"

    with patch("digest.irritator.evidence_stage._run_stages", side_effect=stages):
        async with httpx.AsyncClient() as client:
            result = await run_evidence_irritator(_bundle(config), config, client)
    assert result.status == "empty" and _request_state(config).next_request_at == 195.0


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
    parsed, limitations = _parse_rankings(json.dumps(ranking), [signal], narrative, 3, 5)
    assert parsed[0].reasoning == reasoning.strip() and len(limitations[0]) > 400
    for value, code in [(None, "invalid_type"), ("  ", "empty")]:
        ranking["rankings"][0]["reasoning"] = value
        with pytest.raises(ValueError) as error:
            _parse_rankings(json.dumps(ranking), [signal], narrative, 3, 5)
        assert _safe_error_detail(error.value) == f"reasoning:{code}"
    ranking["rankings"][0]["reasoning"] = "x" * MAX_RESPONSE_CHARS
    with pytest.raises(ValueError, match="Response exceeds"):
        _parse_rankings(json.dumps(ranking), [signal], narrative, 3, 5)


@pytest.mark.asyncio
@pytest.mark.parametrize("query", [
    "empirical studies on frequency of AI‑generated malware or phishing attacks bypassing existing security controls",
    "evaluations of macOS Full Disk Access permission changes and their actual impact on preventing AI‑based abuse",
    "research on limitations of current AI agents in automating credential theft "
    "or account abuse compared to human attackers",
])
async def test_research_prose_is_incomplete_before_any_source_request(query: str) -> None:
    config = fixture_config()
    bundle = _bundle(config)
    model = AsyncMock(side_effect=[
        (json.dumps(_narrative(bundle)), {}),
        (json.dumps({"queries": [{"query": query, "intent": "Find limitations"}], "limitations": []}), {}),
    ])
    with patch("digest.irritator.evidence_stage.complete", model), \
            patch("digest.irritator.evidence_stage.search_hackernews", AsyncMock()) as search:
        async with _offline_client() as client:
            result = await run_evidence_irritator(bundle, config, client)
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
    from digest.irritator.evidence_stage import _ranking_candidates

    abstract = ("Background  with exact spacing. " * 30
                + "Our evaluation preserves utility while reducing the measured attacks. "
                + "Only the tested deployment was evaluated; tradeofff remains workload dependent.")
    signal = make_signal(title="Exact  title", snippet=abstract)
    validated = _bounded_signals([signal], "arxiv")
    assert validated[0].title == signal.title
    assert validated[0].snippet == abstract
    candidates = _ranking_candidates(validated)
    assert len(candidates) == 1
    payload = _ranking_signal_payload(candidates[0])
    assert "".join(part["text"] for part in payload["snippet"]) == abstract
    assert len(json.dumps([payload], ensure_ascii=False)) <= MAX_RANKING_JSON_CHARS
    oversized = make_signal(url="https://example.org/too-large", snippet="x" * 9000)
    assert _ranking_candidates(_bounded_signals([oversized, signal], "arxiv")) == validated


@pytest.mark.asyncio
async def test_no_complete_candidate_fits_skips_rank_and_reports_incomplete() -> None:
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
            result = await run_evidence_irritator(bundle, config, client)
    assert model.await_count == 2
    assert result.status == "incomplete"
    ranking = next(item for item in result.diagnostics if item.stage == "ranking")
    assert ranking.status == "incomplete" and ranking.omitted_count == 1
    assert result.ranked_signals == []
