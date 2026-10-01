"""Offline worker contracts; synthetic opinions test mechanics, not editorial quality."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from digest import editorial_state as state_api
from digest import editorial_worker as worker
from digest.config import Config
from digest.editorial_fetch import FetchedArticle
from digest.editorial_state import ArticleWork, Attempt, EditorialState, Generation
from digest.llm import LLMRole
from digest.radar.collector import article_hash
from scripts.review_fixture import fixture_config
from tests.factories import make_article

OPENING = "The vendor reports doubled throughput."
FOOTNOTE = "[1] The reported gain excludes network failures."
PRIMARY_MARKER = "Первичный редакционный маркер."
PEER_MARKER = "Независимый редакционный маркер."
THIRD_MARKER = "Третий редакционный маркер."
NOW = "2026-10-01T03:00:00+00:00"
USAGE = {"prompt_tokens": 71, "completion_tokens": 29}


def source_body(chunks: int = 1) -> str:
    """Exact ASCII chunk boundaries put the qualification in the last segment."""
    assert chunks >= 1
    size = state_api.CHUNK_WEIGHT * chunks
    return OPENING + " " + "x" * (size - len(OPENING) - len(FOOTNOTE) - 2) + " " + FOOTNOTE


def stage_of(payload: Any) -> str:
    return "reduce" if isinstance(payload, list) else "chunk" if "text" in payload else "final"


def synthetic_response(payload: Any, marker: str) -> dict[str, Any]:
    """Use only this call's source/findings, including every supplied support ID."""
    stage = stage_of(payload)
    if stage == "chunk":
        segment = payload["text"]
        quote = OPENING if OPENING in segment else segment[:60]
        claims = [{"kind": "fact", "text": "Источник сообщает результаты испытания системы. " + marker,
                   "quote": quote, "start": payload["start"] + segment.index(quote)}]
        if FOOTNOTE in segment:
            claims.append({"kind": "qualification", "text": "Сноска исключает сетевые сбои из испытания. " + marker,
                           "quote": FOOTNOTE, "start": payload["start"] + segment.index(FOOTNOTE)})
        return {"claims": claims, "empty_reason": ""}
    if stage == "reduce":
        supplied = [claim for node in payload for claim in node["claims"]]
        claims = []
        for kind in ("fact", "qualification"):
            matches = [claim for claim in supplied if claim["kind"] == kind]
            if matches:
                claims.append({"kind": kind, "text": matches[0]["text"],
                               "supports": [claim["claim_id"] for claim in matches]})
        return {"claims": claims, "empty_reason": "" if claims else "Сегменты не содержат проверяемых утверждений."}
    facts = [claim["claim_id"] for claim in payload["findings"] if claim["kind"] == "fact"]
    qualifications = [claim["claim_id"] for claim in payload["findings"] if claim["kind"] == "qualification"]
    return {
        "decision": "ready", "reason": "",
        "fact": {"text": "Поставщик сообщил об увеличении пропускной способности. " + marker, "claim_ids": facts},
        "inference": {"text": "Результат допускает ускорение при сохранении условий испытания. " + marker,
                      "claim_ids": facts},
        "limitation": {"text": "Финальная сноска исключает поведение при сетевых сбоях. " + marker,
                       "claim_ids": qualifications or facts},
        "why_read": {"text": "Оригинал позволяет проверить условия испытания и границы результата. " + marker,
                     "claim_ids": facts + qualifications},
        "value_score": 8, "value_rationale": "Указано конкретное измерение и ограничение применимости.",
        "event_key": "Изменение результатов испытания пропускной способности",
    }


@dataclass
class OfflineProvider:
    config: Config
    bodies: dict[str, str] = field(default_factory=dict)
    calls: list[dict[str, Any]] = field(default_factory=list)
    fetches: list[str] = field(default_factory=list)
    response: Callable[[dict[str, Any], dict[str, Any]], Any] | None = None
    usage: dict[str, Any] = field(default_factory=lambda: dict(USAGE))


@pytest.fixture
def offline(monkeypatch: pytest.MonkeyPatch) -> OfflineProvider:
    adapter = OfflineProvider(fixture_config())
    monkeypatch.setattr(worker, "GROQ_SPACING_SECONDS", 0)

    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("Worker tests must never make a live HTTP request")

    async def fetch(url: str) -> FetchedArticle:
        adapter.fetches.append(url)
        return FetchedArticle(adapter.bodies.get(url, source_body()), url + "?canonical", NOW,
                              "2026-09-30T12:00:00+00:00", "article", ("Complete synthetic public source.",))

    async def complete(role: LLMRole, messages: list[dict[str, str]], config: Config,
                       **kwargs: Any) -> tuple[str, dict[str, Any]]:
        payload = json.loads(messages[1]["content"])
        provider = kwargs["provider_override"]
        call = {"provider": provider.name, "model": provider.model, "stage": stage_of(payload),
                "payload": payload, "messages": deepcopy(messages), "role": role,
                "config": config, "kwargs": kwargs}
        adapter.calls.append(call)
        primary = adapter.config.review.primary
        secondary = adapter.config.review.secondary
        identity = provider.name, provider.model
        marker = (PRIMARY_MARKER if identity == (primary.provider, primary.model) else PEER_MARKER
                  if identity == (secondary.provider, secondary.model) else THIRD_MARKER)
        answer: Any = synthetic_response(payload, marker)
        if adapter.response is not None:
            answer = adapter.response(call, answer)
        if isinstance(answer, Exception):
            raise answer
        return (answer if isinstance(answer, str) else json.dumps(answer, ensure_ascii=False)), adapter.usage

    monkeypatch.setattr("httpx.AsyncClient", forbidden)
    monkeypatch.setattr("httpx.Client", forbidden)
    monkeypatch.setattr(worker, "fetch_article", fetch)
    monkeypatch.setattr(worker, "complete", complete)
    return adapter


def only_article(state: EditorialState) -> ArticleWork:
    assert len(state.articles) == 1
    return state.articles[state.order[0]]


def primary_generation(state: EditorialState, config: Config) -> Generation:
    generation = state_api.current_generation(only_article(state), config.review.primary.provider,
                                              config.review.primary.model)
    assert generation is not None
    return generation


async def test_final_requires_every_chunk_and_every_reduction_before_readiness(
    tmp_path: Path, offline: OfflineProvider,
) -> None:
    article = make_article()
    offline.bodies[article.link] = source_body(5)
    for allowance in range(1, 10):
        result = await worker.run_editorial_pass(offline.config, tmp_path, [article], max_calls=1)
        generation = primary_generation(result.state, offline.config)
        assert len(offline.calls) == allowance
        assert generation.final is None
        assert state_api.ready_results(result.state, offline.config) == []
        assert result.summary.pending == 1 and result.summary.fully_analysed == 0
        assert result.summary.stop_reason == "request_allowance"
        assert result.summary.completed_chunks == min(allowance, 5)
    assert [call["stage"] for call in offline.calls] == ["chunk"] * 5 + ["reduce"] * 4
    final = await worker.run_editorial_pass(offline.config, tmp_path, [], max_calls=1)
    assert offline.calls[-1]["stage"] == "final"
    assert final.summary.ready == final.summary.fully_analysed == 1
    assert final.summary.pending == 0
    assert final.summary.total_chunks == final.summary.completed_chunks == 5
    assert len(offline.fetches) == 1
    work = only_article(final.state)
    generation = primary_generation(final.state, offline.config)
    assert generation.final is not None
    root = next(node for node in generation.nodes.values() if node.node_id == generation.final.root_node_id)
    assert root.chunk_ids == tuple(chunk.chunk_id for chunk in work.chunks)
    assert len(generation.nodes) == 9
    assert state_api.load_state(tmp_path) == final.state


async def test_resume_keeps_completed_nodes_body_and_metadata_immutable(
    tmp_path: Path, offline: OfflineProvider,
) -> None:
    article = make_article(description="Original feed metadata")
    offline.bodies[article.link] = source_body(3)
    first = await worker.run_editorial_pass(offline.config, tmp_path, [article], max_calls=2)
    work = only_article(first.state)
    assert work.body_sha256 is not None
    body = state_api.body_path(tmp_path, work.body_sha256)
    before_body = body.read_bytes(), body.stat().st_mtime_ns
    before_nodes = deepcopy(primary_generation(first.state, offline.config).nodes)
    first_payloads = [call["payload"]["chunk_id"] for call in offline.calls]
    offline.bodies[article.link] = "The remote article has changed since admission."
    article.description = "Updated feed must not overwrite admitted metadata"
    resumed = await worker.run_editorial_pass(offline.config, tmp_path, [article], max_calls=20)
    generation = primary_generation(resumed.state, offline.config)
    assert all(generation.nodes[key] == value for key, value in before_nodes.items())
    assert (body.read_bytes(), body.stat().st_mtime_ns) == before_body
    assert only_article(resumed.state).description == "Original feed metadata"
    assert offline.fetches == [article.link]
    chunks = [call["payload"]["chunk_id"] for call in offline.calls if call["stage"] == "chunk"]
    assert chunks[:2] == first_payloads and len(chunks) == len(set(chunks)) == 3
    before_calls = deepcopy(offline.calls)
    completed = deepcopy(generation)
    again = await worker.run_editorial_pass(offline.config, tmp_path, [article], max_calls=20)
    assert offline.calls == before_calls
    assert primary_generation(again.state, offline.config) == completed
    assert again.summary.calls_this_pass == again.summary.admitted_this_pass == 0


async def test_round_robin_advances_all_articles_and_persists_cursor(
    tmp_path: Path, offline: OfflineProvider,
) -> None:
    articles = [make_article(title=f"Article {index}", link=f"https://example.com/{index}") for index in range(3)]
    for article in articles:
        offline.bodies[article.link] = source_body(4)
    await worker.run_editorial_pass(offline.config, tmp_path, articles, max_calls=4)
    expected_titles = [article.title for article in articles] + [articles[0].title]
    assert [call["payload"]["title"] for call in offline.calls] == expected_titles
    assert [call["payload"]["start"] for call in offline.calls] == [0, 0, 0, state_api.CHUNK_WEIGHT]
    resumed = await worker.run_editorial_pass(offline.config, tmp_path, [], max_calls=2)
    assert [call["payload"]["title"] for call in offline.calls[-2:]] == [article.title for article in articles[1:]]
    assert len(offline.fetches) == 3
    for work in resumed.state.articles.values():
        generation = state_api.current_generation(work, offline.config.review.primary.provider,
                                                  offline.config.review.primary.model)
        assert generation is not None
        assert sum(node.stage == "chunk" for node in generation.nodes.values()) == 2


async def test_primary_ready_without_peer_and_independent_uses_same_source_blindly(
    tmp_path: Path, offline: OfflineProvider,
) -> None:
    article = make_article(description="RSS_POISON_IS_NOT_SOURCE_EVIDENCE")
    offline.bodies[article.link] = source_body(3)
    first = await worker.run_editorial_pass(offline.config, tmp_path, [article], max_calls=20)
    ready = state_api.ready_results(first.state, offline.config)
    assert len(ready) == 1 and not ready[0].independent_complete
    assert "независимый разбор не завершён" in ready[0].to_article_summary().summary
    assert {call["provider"] for call in offline.calls} == {offline.config.review.primary.provider}
    primary_calls = deepcopy(offline.calls)
    primary = deepcopy(primary_generation(first.state, offline.config))
    second = await worker.run_editorial_pass(offline.config, tmp_path, [], mode="independent", max_calls=20)
    peer_calls = offline.calls[len(primary_calls):]
    assert len(peer_calls) == len(primary_calls) == 6
    assert {call["provider"] for call in peer_calls} == {offline.config.review.secondary.provider}
    assert [call["messages"] for call in primary_calls if call["stage"] == "chunk"] == [
        call["messages"] for call in peer_calls if call["stage"] == "chunk"
    ]
    assert PRIMARY_MARKER not in json.dumps([call["messages"] for call in peer_calls], ensure_ascii=False)
    assert PEER_MARKER not in json.dumps([call["messages"] for call in primary_calls], ensure_ascii=False)
    assert "RSS_POISON" not in json.dumps([call["messages"] for call in offline.calls], ensure_ascii=False)
    assert primary_generation(second.state, offline.config) == primary
    ready = state_api.ready_results(second.state, offline.config)
    assert len(ready) == 1 and ready[0].independent_complete
    assert ready[0].provider == offline.config.review.primary.provider
    assert len(offline.fetches) == 1
    assert len(list((tmp_path / "bodies").iterdir())) == 1


async def test_independent_pass_does_not_create_hidden_primary_prerequisite(
    tmp_path: Path, offline: OfflineProvider,
) -> None:
    result = await worker.run_editorial_pass(offline.config, tmp_path, [make_article()],
                                            mode="independent", max_calls=5)
    assert result.summary.admitted == result.summary.pending == 1
    assert result.summary.acquired == result.summary.calls_this_pass == 0
    assert not offline.calls and not offline.fetches
    assert only_article(result.state).body_sha256 is None


async def test_peer_failure_cannot_revoke_completed_primary(
    tmp_path: Path, offline: OfflineProvider,
) -> None:
    first = await worker.run_editorial_pass(offline.config, tmp_path, [make_article()], max_calls=2)
    primary = deepcopy(primary_generation(first.state, offline.config))
    offline.response = lambda call, answer: RuntimeError("Synthetic peer unavailable")
    failed = await worker.run_editorial_pass(offline.config, tmp_path, [], mode="independent",
                                            deadline_seconds=1, max_calls=5)
    ready = state_api.ready_results(failed.state, offline.config)
    assert len(ready) == 1 and not ready[0].independent_complete
    assert primary_generation(failed.state, offline.config) == primary
    assert failed.summary.ready == failed.summary.fully_analysed == 1
    assert failed.summary.stop_reason == "cooldown"


async def test_technical_primary_failure_can_complete_explicitly_attributed_fallback(
    tmp_path: Path, offline: OfflineProvider,
) -> None:
    offline.response = lambda call, answer: RuntimeError("Synthetic quota failure") if (
        call["provider"] == offline.config.review.primary.provider) else answer
    result = await worker.run_editorial_pass(offline.config, tmp_path, [make_article()], max_calls=10,
                                            deadline_seconds=1)
    primary = primary_generation(result.state, offline.config)
    assert primary.final is None and primary.attempts[0].status == "failed"
    assert primary.blocked_until and primary.last_error
    ready = state_api.ready_results(result.state, offline.config)
    assert len(ready) == 1 and not ready[0].independent_complete
    assert (ready[0].provider, ready[0].model) == (offline.config.review.secondary.provider,
                                                 offline.config.review.secondary.model)
    assert ready[0].provider in ready[0].to_article_summary().summary
    assert [call["provider"] for call in offline.calls] == [offline.config.review.primary.provider,
                                                            offline.config.review.secondary.provider,
                                                            offline.config.review.secondary.provider]
    assert offline.calls[0]["messages"] == offline.calls[1]["messages"]
    assert only_article(result.state).delivery_state == "pending"


@pytest.mark.parametrize("invalid", [
    "not-json", '{"claims":', {"claims": [], "empty_reason": ""}, {"claims": [], "empty_reason": "ok"},
    {"claims": [], "empty_reason": "Сегмент не содержит значимых утверждений.", "extra": True},
])
async def test_malformed_or_partial_chunk_output_remains_pending(
    tmp_path: Path, offline: OfflineProvider, invalid: Any,
) -> None:
    offline.response = lambda call, answer: invalid
    result = await worker.run_editorial_pass(offline.config, tmp_path, [make_article()], max_calls=1)
    generation = primary_generation(result.state, offline.config)
    assert generation.final is None and not generation.nodes
    assert generation.attempts[0].status == "failed"
    assert generation.attempts[0].rejected_output is not None
    assert result.summary.pending == 1 and result.summary.ready == result.summary.rejected == 0
    assert state_api.load_state(tmp_path) == result.state


@pytest.mark.parametrize("damage", ["fabricated_quote", "wrong_chunk", "extra_key", "english"])
async def test_chunk_claims_require_exact_in_bounds_source_evidence_and_russian(
    tmp_path: Path, offline: OfflineProvider, damage: str,
) -> None:
    def corrupt(call: dict[str, Any], answer: dict[str, Any]) -> dict[str, Any]:
        claim = answer["claims"][0]
        if damage == "fabricated_quote":
            claim["quote"] = "Fabricated assertion not in source."
        elif damage == "wrong_chunk":
            claim["quote"] = "This text occurs in a different source chunk only."
            claim["start"] = call["payload"]["end"]
        elif damage == "extra_key":
            claim["unsupported"] = "extra"
        else:
            claim["text"] = "This English language assertion must not pass Russian validation."
        return answer
    offline.response = corrupt
    result = await worker.run_editorial_pass(offline.config, tmp_path, [make_article()], max_calls=1)
    generation = primary_generation(result.state, offline.config)
    assert not generation.nodes and generation.final is None
    assert result.summary.pending == 1 and result.summary.rejected == 0


@pytest.mark.parametrize("damage", ["dropped_claim", "duplicate_claim", "unknown_claim", "qualification_as_fact"])
async def test_reduction_cannot_lose_duplicate_invent_or_upgrade_claim_lineage(
    tmp_path: Path, offline: OfflineProvider, damage: str,
) -> None:
    article = make_article()
    offline.bodies[article.link] = source_body(2)

    def corrupt(call: dict[str, Any], answer: dict[str, Any]) -> dict[str, Any]:
        if call["stage"] != "reduce":
            return answer
        if damage == "dropped_claim":
            answer["claims"][0]["supports"].pop()
        elif damage == "duplicate_claim":
            answer["claims"][0]["supports"] *= 2
        elif damage == "unknown_claim":
            answer["claims"][0]["supports"].append("invented-claim")
        else:
            answer["claims"][-1]["kind"] = "fact"
        return answer
    offline.response = corrupt
    result = await worker.run_editorial_pass(offline.config, tmp_path, [article], max_calls=3)
    generation = primary_generation(result.state, offline.config)
    assert len(generation.nodes) == 2 and all(node.stage == "chunk" for node in generation.nodes.values())
    assert generation.final is None and generation.attempts[-1].status == "failed"
    assert result.summary.pending == 1 and result.summary.completed_chunks == 2
    assert result.summary.ready == result.summary.rejected == 0


@pytest.mark.parametrize("damage", ["missing_qualification", "fact_cites_qualification", "unknown_claim",
                                    "empty_claims", "duplicate_field", "partial_response", "technical_rejection"])
async def test_final_requires_complete_grounded_distinct_fields_without_technical_rejection(
    tmp_path: Path, offline: OfflineProvider, damage: str,
) -> None:
    def corrupt(call: dict[str, Any], answer: dict[str, Any]) -> dict[str, Any]:
        if call["stage"] != "final":
            return answer
        if damage == "missing_qualification":
            answer["limitation"]["claim_ids"] = answer["fact"]["claim_ids"]
        elif damage == "fact_cites_qualification":
            answer["fact"]["claim_ids"] = answer["limitation"]["claim_ids"]
        elif damage == "unknown_claim":
            answer["why_read"]["claim_ids"] = ["invented"]
        elif damage == "empty_claims":
            answer["inference"]["claim_ids"] = []
        elif damage == "duplicate_field":
            answer["why_read"]["text"] = answer["fact"]["text"]
        elif damage == "partial_response":
            del answer["why_read"]
        else:
            answer.update(decision="rejected", reason="Статья слишком длинная и не обработана из-за квоты.")
            for name in ("fact", "inference", "limitation", "why_read"):
                answer[name] = None
        return answer
    offline.response = corrupt
    result = await worker.run_editorial_pass(offline.config, tmp_path, [make_article()], max_calls=2)
    generation = primary_generation(result.state, offline.config)
    assert len(generation.nodes) == 1 and generation.final is None
    assert generation.attempts[-1].stage == "final" and generation.attempts[-1].status == "failed"
    assert result.summary.pending == 1 and result.summary.ready == result.summary.rejected == 0


async def test_opening_claim_and_last_footnote_survive_all_reductions_with_exact_final_spans(
    tmp_path: Path, offline: OfflineProvider,
) -> None:
    article = make_article()
    body = source_body(7)
    offline.bodies[article.link] = body
    result = await worker.run_editorial_pass(offline.config, tmp_path, [article], max_calls=20)
    generation = primary_generation(result.state, offline.config)
    work = only_article(result.state)
    final = generation.final
    assert final is not None and final.fact is not None and final.limitation is not None
    assert final.why_read is not None and final.inference is not None
    nodes = {node.node_id: node for node in generation.nodes.values()}
    footnote_chunk = work.chunks[-1].chunk_id
    for node in generation.nodes.values():
        if node.stage == "reduce":
            children = [nodes[identity] for identity in node.input_node_ids]
            assert node.chunk_ids == tuple(key for child in children for key in child.chunk_ids)
            refs = [ref for claim in node.claims for ref in claim.supports]
            expected = [claim.claim_id for child in children for claim in child.claims]
            assert set(refs) == set(expected) and len(refs) == len(expected)
        if footnote_chunk in node.chunk_ids:
            qualifications = [claim for claim in node.claims if claim.kind == "qualification"]
            assert qualifications and "сноска" in qualifications[0].text.lower()
            spans = [span for claim in qualifications
                     for span in state_api.resolve_claim_spans(generation, claim.claim_id)]
            assert any(span.quote == FOOTNOTE for span in spans)
    fact_spans = [span for ref in final.fact.claim_ids for span in state_api.resolve_claim_spans(generation, ref)]
    limitation_spans = [span for ref in final.limitation.claim_ids
                        for span in state_api.resolve_claim_spans(generation, ref)]
    assert any(span.start == 0 and span.quote == OPENING for span in fact_spans)
    assert len(limitation_spans) == 1
    assert limitation_spans[0].quote == FOOTNOTE and limitation_spans[0].end == len(body)
    assert limitation_spans[0].chunk_id == footnote_chunk
    for final_field in (final.fact, final.inference, final.limitation, final.why_read):
        spans = [span for ref in final_field.claim_ids for span in state_api.resolve_claim_spans(generation, ref)]
        assert spans
        assert all(body[span.start:span.end] == span.quote for span in spans)
    assert state_api.load_state(tmp_path) == result.state


async def test_failed_attempt_cooldown_survives_reload_and_does_not_spend_more_calls(
    tmp_path: Path, offline: OfflineProvider,
) -> None:
    offline.response = lambda call, answer: RuntimeError("Synthetic provider outage")
    first = await worker.run_editorial_pass(offline.config, tmp_path, [make_article()], max_calls=2)
    before = deepcopy(only_article(first.state).generations)
    assert len(offline.calls) == 2
    for generation in before.values():
        assert generation.attempts[0].status == "failed"
        assert generation.attempts[0].retry_at == generation.blocked_until
        assert datetime.fromisoformat(generation.blocked_until or "").timestamp() > time.time()
    second = await worker.run_editorial_pass(offline.config, tmp_path, [], max_calls=20, deadline_seconds=1)
    assert len(offline.calls) == 2
    assert only_article(second.state).generations == before
    assert second.summary.stop_reason == "cooldown" and second.summary.pending == 1


async def test_expired_cooldown_retries_failed_task_without_repeating_completed_chunks(
    tmp_path: Path, offline: OfflineProvider,
) -> None:
    article = make_article()
    offline.bodies[article.link] = source_body(2)
    offline.response = lambda call, answer: RuntimeError("One failed task") if len(offline.calls) == 2 else answer
    first = await worker.run_editorial_pass(offline.config, tmp_path, [article], max_calls=2)
    generation = primary_generation(first.state, offline.config)
    completed = deepcopy(generation.nodes)
    failed_key = generation.attempts[-1].task_key
    generation.blocked_until = NOW
    first.state.provider_next_eligible.clear()
    first.state.provider_unavailable_until.clear()
    state_api.store_state(first.state, tmp_path)
    finished = await worker.run_editorial_pass(offline.config, tmp_path, [], max_calls=10)
    generation = primary_generation(finished.state, offline.config)
    assert finished.summary.ready == 1
    assert generation.attempts[1].status == "failed" and generation.attempts[2].status == "success"
    assert generation.attempts[2].task_key == failed_key
    assert all(generation.nodes[key] == value for key, value in completed.items())
    assert [call["payload"]["start"] for call in offline.calls if call["stage"] == "chunk"] == [0, 7500, 7500]


async def test_zero_request_allowance_admits_every_article_without_fetching_or_dropping(
    tmp_path: Path, offline: OfflineProvider,
) -> None:
    articles = [make_article(title=f"Article {index}", link=f"https://example.com/{index}") for index in range(35)]
    result = await worker.run_editorial_pass(offline.config, tmp_path, articles, max_calls=0)
    assert result.summary.admitted == result.summary.pending == result.summary.admitted_this_pass == 35
    assert result.summary.calls_this_pass == result.summary.acquired == 0
    assert result.summary.stop_reason == "request_allowance"
    assert result.summary.oldest_pending_at == min(item.admitted_at for item in result.state.articles.values())
    assert not offline.calls and not offline.fetches
    assert state_api.load_state(tmp_path) == result.state


async def test_deadline_preserves_successful_progress_for_next_pass(
    tmp_path: Path, offline: OfflineProvider, monkeypatch: pytest.MonkeyPatch,
) -> None:
    article = make_article()
    offline.bodies[article.link] = source_body(2)
    clock = [100.0]
    monkeypatch.setattr(worker, "time", SimpleNamespace(time=time.time, monotonic=lambda: clock[0]))

    def advance_time(call: dict[str, Any], answer: dict[str, Any]) -> dict[str, Any]:
        clock[0] += 2
        return answer
    offline.response = advance_time
    result = await worker.run_editorial_pass(offline.config, tmp_path, [article], deadline_seconds=1, max_calls=10)
    assert result.summary.stop_reason == "deadline"
    assert result.summary.calls_this_pass == result.summary.completed_chunks == 1
    generation = primary_generation(result.state, offline.config)
    saved = deepcopy(generation.nodes)
    assert generation.final is None
    offline.response = None
    finished = await worker.run_editorial_pass(offline.config, tmp_path, [], deadline_seconds=1, max_calls=10)
    assert finished.summary.ready == 1
    assert all(primary_generation(finished.state, offline.config).nodes[key] == value for key, value in saved.items())
    assert len(offline.fetches) == 1


async def test_provider_timeout_records_unknown_and_keeps_completed_progress(
    tmp_path: Path, offline: OfflineProvider, monkeypatch: pytest.MonkeyPatch,
) -> None:
    article = make_article()
    offline.bodies[article.link] = source_body(2)
    first = await worker.run_editorial_pass(offline.config, tmp_path, [article], max_calls=1)
    saved = deepcopy(primary_generation(first.state, offline.config).nodes)

    async def slow_complete(*args: Any, **kwargs: Any) -> tuple[str, dict[str, int]]:
        await asyncio.sleep(1)
        raise AssertionError("Timed-out provider must be cancelled")
    monkeypatch.setattr(worker, "complete", slow_complete)
    failed = await worker.run_editorial_pass(offline.config, tmp_path, [], max_calls=1, deadline_seconds=0.02)
    generation = primary_generation(failed.state, offline.config)
    assert generation.nodes == saved and generation.final is None
    assert generation.attempts[-1].status == "unknown" and generation.attempts[-1].error == "TimeoutError"
    assert generation.blocked_until == generation.attempts[-1].retry_at
    assert state_api.load_state(tmp_path) == failed.state


async def test_interrupted_started_attempts_become_unknown_before_any_new_call(
    tmp_path: Path, offline: OfflineProvider,
) -> None:
    article = make_article()
    first = await worker.run_editorial_pass(offline.config, tmp_path, [article], max_calls=1)
    work = only_article(first.state)
    generation = primary_generation(first.state, offline.config)
    completed = deepcopy(generation.nodes)
    generation.attempts.append(Attempt("interrupted-provider", "final", "task-pending", "a" * 64, NOW))
    second_article = make_article(title="Unacquired", link="https://example.com/unacquired")
    state_api.admit_articles(first.state, [second_article])
    unacquired = first.state.articles[first.state.order[-1]]
    unacquired.acquisition_attempts.append(Attempt("interrupted-fetch", "acquire", unacquired.article_id, "", NOW))
    state_api.store_state(first.state, tmp_path)
    resumed = await worker.run_editorial_pass(offline.config, tmp_path, [], max_calls=0)
    recovered_generation = state_api.current_generation(resumed.state.articles[work.article_id],
                                                        offline.config.review.primary.provider,
                                                        offline.config.review.primary.model)
    assert recovered_generation is not None and recovered_generation.nodes == completed
    interrupted = recovered_generation.attempts[-1]
    assert interrupted.status == "unknown" and interrupted.error == "InterruptedProviderAttempt"
    assert interrupted.retry_at == recovered_generation.blocked_until and interrupted.retry_at
    recovered = resumed.state.articles[unacquired.article_id]
    assert recovered.acquisition_attempts[0].status == "unknown"
    assert recovered.acquisition_attempts[0].error == "InterruptedAcquisition"
    assert recovered.acquisition_attempts[0].retry_at == recovered.acquisition_retry_at
    assert len(offline.calls) == len(offline.fetches) == 1
    assert state_api.load_state(tmp_path) == resumed.state


async def test_usage_is_sanitized_persisted_and_counted_even_for_rejected_outputs(
    tmp_path: Path, offline: OfflineProvider,
) -> None:
    offline.usage = {**USAGE, "total_tokens": 100, "secret": "never persist", "other": True}
    first = await worker.run_editorial_pass(offline.config, tmp_path, [make_article()], max_calls=1)
    generation = primary_generation(first.state, offline.config)
    assert generation.attempts[0].usage == USAGE
    assert next(iter(generation.nodes.values())).usage == USAGE
    offline.response = lambda call, answer: "incomplete final JSON"
    second = await worker.run_editorial_pass(offline.config, tmp_path, [], max_calls=1)
    generation = primary_generation(second.state, offline.config)
    assert generation.attempts[-1].usage == USAGE and generation.attempts[-1].status == "failed"
    assert second.summary.observed_prompt_tokens == 142 and second.summary.observed_completion_tokens == 58
    assert second.summary.attempts == 3  # Source acquisition plus both model attempts.
    assert "never persist" not in (tmp_path / "state.json").read_text()
    assert state_api.load_state(tmp_path) == second.state


@pytest.mark.parametrize("bad_usage", [
    {"prompt_tokens": -1, "completion_tokens": True},
    {"prompt_tokens": "15", "completion_tokens": 1.5},
])
async def test_invalid_usage_values_are_not_reported_as_observed_tokens(
    tmp_path: Path, offline: OfflineProvider, bad_usage: dict[str, Any],
) -> None:
    offline.usage = bad_usage
    result = await worker.run_editorial_pass(offline.config, tmp_path, [make_article()], max_calls=2)
    assert result.summary.ready == 1
    assert result.summary.observed_prompt_tokens == result.summary.observed_completion_tokens == 0
    assert all(attempt.usage == {} for attempt in primary_generation(result.state, offline.config).attempts)


@pytest.mark.parametrize("delivery_state", ["pending", "confirmed_failed", "reserved", "unknown", "delivered"])
async def test_worker_never_mutates_existing_delivery_fields_or_consumes_dedup(
    tmp_path: Path, offline: OfflineProvider, delivery_state: Any,
) -> None:
    first = await worker.run_editorial_pass(offline.config, tmp_path, [make_article()], max_calls=1)
    work = only_article(first.state)
    work.delivery_state = delivery_state
    work.delivery_attempt_id = "transport-attempt-must-survive"
    state_api.store_state(first.state, tmp_path)
    result = await worker.run_editorial_pass(offline.config, tmp_path, [], max_calls=10)
    resumed = only_article(result.state)
    assert (resumed.delivery_state, resumed.delivery_attempt_id) == (delivery_state, "transport-attempt-must-survive")
    assert result.summary.delivered == int(delivery_state == "delivered")
    assert result.summary.unknown_delivery == int(delivery_state == "unknown")
    assert len(offline.calls) == (2 if delivery_state in {"pending", "confirmed_failed"} else 1)
    assert not (tmp_path / "seen_articles.json").exists()


async def test_editorial_rejection_requires_completed_coverage_and_does_not_consume_delivery(
    tmp_path: Path, offline: OfflineProvider,
) -> None:
    article = make_article()
    offline.bodies[article.link] = source_body(2)

    def reject(call: dict[str, Any], answer: dict[str, Any]) -> dict[str, Any]:
        if call["stage"] == "final":
            answer.update(decision="rejected", reason="Источник повторяет известный результат без новых измерений.")
            for name in ("fact", "inference", "limitation", "why_read"):
                answer[name] = None
        return answer
    offline.response = reject
    result = await worker.run_editorial_pass(offline.config, tmp_path, [article], max_calls=10)
    assert result.summary.rejected == result.summary.fully_analysed == 1
    assert result.summary.ready == result.summary.pending == result.summary.delivered == 0
    assert result.summary.completed_chunks == 2
    assert only_article(result.state).delivery_state == "pending"
    assert only_article(result.state).delivery_attempt_id is None
    assert len(offline.calls) == 4


async def test_acquisition_failure_stays_pending_without_rss_substitution_or_editorial_rejection(
    tmp_path: Path, offline: OfflineProvider, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unavailable(url: str) -> FetchedArticle:
        raise ValueError("coverage_incomplete:pagination")
    monkeypatch.setattr(worker, "fetch_article", unavailable)
    result = await worker.run_editorial_pass(offline.config, tmp_path,
                                            [make_article(description="RSS contains persuasive but partial facts")],
                                            max_calls=5, deadline_seconds=1)
    work = only_article(result.state)
    assert work.body_sha256 is None and not work.chunks and not work.generations
    assert work.acquisition_error == "coverage_incomplete:pagination"
    assert work.acquisition_attempts[0].status == "failed" and work.acquisition_retry_at
    assert result.summary.pending == 1 and result.summary.rejected == result.summary.ready == 0
    assert result.summary.stop_reason == "cooldown" and not offline.calls


async def test_single_provider_call_uses_explicit_model_and_bounded_output_without_internal_retries(
    tmp_path: Path, offline: OfflineProvider,
) -> None:
    offline.config.llm.max_retries = 5
    offline.config.review.max_output_tokens = 10000
    await worker.run_editorial_pass(offline.config, tmp_path, [make_article()], max_calls=1)
    call = offline.calls[0]
    assert call["role"] == LLMRole.REVIEW_EVIDENCE
    assert call["model"] == offline.config.review.primary.model
    assert call["config"].llm.max_retries == call["config"].llm.min_request_interval_seconds == 0
    assert call["kwargs"]["max_output_tokens"] == worker.MAX_OUTPUT_TOKENS
    assert call["kwargs"]["provider_override"].role == ["review_evidence"]
    assert offline.config.llm.max_retries == 5
    assert worker.estimate_input_tokens(call["messages"]) <= worker.MAX_INPUT_ESTIMATE


async def test_response_and_prompt_hashes_record_exact_successful_call(
    tmp_path: Path, offline: OfflineProvider,
) -> None:
    result = await worker.run_editorial_pass(offline.config, tmp_path, [make_article()], max_calls=2)
    generation = primary_generation(result.state, offline.config)
    assert generation.final is not None
    assert generation.final.usage == USAGE
    for call, attempt in zip(offline.calls, generation.attempts, strict=True):
        text = json.dumps(synthetic_response(call["payload"], PRIMARY_MARKER), ensure_ascii=False)
        assert attempt.response_sha256 == hashlib.sha256(text.encode()).hexdigest()
        assert attempt.prompt_hash == hashlib.sha256(json.dumps(call["messages"], sort_keys=True).encode()).hexdigest()
        assert attempt.status == "success"
    assert generation.final.response_sha256 == generation.attempts[-1].response_sha256
    assert generation.final.prompt_hash == generation.attempts[-1].prompt_hash


@pytest.mark.parametrize("kwargs", [{"deadline_seconds": 0}, {"max_calls": -1},
                                    {"mode": "unknown"}])
async def test_invalid_worker_budget_or_mode_fails_before_io(
    tmp_path: Path, offline: OfflineProvider, kwargs: dict[str, Any],
) -> None:
    with pytest.raises(ValueError, match="budget or mode"):
        await worker.run_editorial_pass(offline.config, tmp_path, [make_article()], **kwargs)
    assert not offline.calls and not offline.fetches and not list(tmp_path.iterdir())


async def test_provider_pacing_leaves_pending_progress_when_wait_exceeds_deadline(
    tmp_path: Path, offline: OfflineProvider,
) -> None:
    first = await worker.run_editorial_pass(offline.config, tmp_path, [make_article()], max_calls=1)
    first.state.provider_next_eligible[offline.config.review.primary.provider] = (
        datetime.now(UTC) + timedelta(hours=1)).isoformat()
    state_api.store_state(first.state, tmp_path)
    result = await worker.run_editorial_pass(offline.config, tmp_path, [], max_calls=5, deadline_seconds=1)
    assert result.summary.stop_reason == "cooldown" and result.summary.completed_chunks == 1
    assert result.summary.calls_this_pass == 0 and result.summary.pending == 1
    assert len(offline.calls) == 1
    assert only_article(result.state).generations == only_article(first.state).generations


@pytest.mark.parametrize("hint", [None, 0, True])
async def test_quote_only_later_chunk_and_wrong_hints_resolve_unique_exact_offsets(
    tmp_path: Path, offline: OfflineProvider, hint: Any,
) -> None:
    article = make_article()
    body = source_body(2)
    offline.bodies[article.link] = body

    def quote_only(call: dict[str, Any], answer: dict[str, Any]) -> dict[str, Any]:
        if call["stage"] == "chunk":
            if call["payload"]["start"] > 0:
                answer["claims"] = [answer["claims"][-1]]
            for claim in answer["claims"]:
                claim.pop("start")
                if hint is not None:
                    claim["start"] = hint
        return answer
    offline.response = quote_only
    result = await worker.run_editorial_pass(offline.config, tmp_path, [article], max_calls=10)
    assert result.summary.ready == 1
    generation = primary_generation(result.state, offline.config)
    leaf = next(node for node in generation.nodes.values()
                if node.stage == "chunk" and node.chunk_ids == (only_article(result.state).chunks[-1].chunk_id,))
    span = leaf.claims[0].spans[0]
    assert span.start == body.index(FOOTNOTE) and span.end == len(body) and span.quote == FOOTNOTE
    assert span.offset_recovered is (hint is not None)
    assert not span.typography_normalized
    assert state_api.load_state(tmp_path) == result.state


@pytest.mark.parametrize("valid_hint", [False, True])
async def test_repeated_quote_is_rejected_unless_exact_valid_hint_disambiguates(
    tmp_path: Path, offline: OfflineProvider, valid_hint: bool,
) -> None:
    article = make_article()
    body = f"{OPENING} {OPENING} {FOOTNOTE}"
    offline.bodies[article.link] = body

    def repeat_quote(call: dict[str, Any], answer: dict[str, Any]) -> dict[str, Any]:
        if call["stage"] == "chunk":
            if valid_hint:
                answer["claims"][0]["start"] = len(OPENING) + 1
            else:
                answer["claims"][0].pop("start")
        return answer
    offline.response = repeat_quote
    result = await worker.run_editorial_pass(offline.config, tmp_path, [article], max_calls=1)
    generation = primary_generation(result.state, offline.config)
    assert bool(generation.nodes) is valid_hint
    if valid_hint:
        span = next(iter(generation.nodes.values())).claims[0].spans[0]
        assert span.start == len(OPENING) + 1 and span.quote == OPENING
        assert not span.offset_recovered
    else:
        assert generation.attempts[-1].status == "failed"
        assert generation.attempts[-1].error.startswith("ValueError")
        assert result.summary.pending == 1


async def test_hyphen_typography_normalization_preserves_literal_saved_source(
    tmp_path: Path, offline: OfflineProvider,
) -> None:
    article = make_article()
    quote = "A peer\u2011reviewed experiment reports a measurable change."
    offline.bodies[article.link] = quote + " " + FOOTNOTE

    def normalize_hyphen(call: dict[str, Any], answer: dict[str, Any]) -> dict[str, Any]:
        if call["stage"] == "chunk":
            answer["claims"][0]["quote"] = quote.replace("\u2011", "-")
            answer["claims"][0].pop("start")
        return answer
    offline.response = normalize_hyphen
    result = await worker.run_editorial_pass(offline.config, tmp_path, [article], max_calls=2)
    assert result.summary.ready == 1
    generation = primary_generation(result.state, offline.config)
    span = next(iter(generation.nodes.values())).claims[0].spans[0]
    assert span.quote == quote and span.start == 0 and span.end == len(quote)
    assert span.typography_normalized and not span.offset_recovered
    assert state_api.load_state(tmp_path) == result.state


async def test_small_request_budget_finishes_one_card_before_every_candidate_first_chunk(
    tmp_path: Path, offline: OfflineProvider,
) -> None:
    articles = [make_article(title=f"Small article {index}", link=f"https://example.com/{index}") for index in range(8)]
    result = await worker.run_editorial_pass(offline.config, tmp_path, articles, max_calls=2)
    assert result.summary.admitted == 8
    assert result.summary.ready == 1 and result.summary.pending == 7
    assert result.summary.calls_this_pass == 2
    assert [call["stage"] for call in offline.calls] == ["chunk", "final"]
    assert [call["payload"]["title"] for call in offline.calls] == [articles[0].title, articles[0].title]
    assert result.summary.completed_chunks == 1


@pytest.mark.parametrize("stage", ["acquire", "provider"])
async def test_cancellation_leaves_started_attempt_durable_and_resume_recovers_unknown(
    tmp_path: Path, offline: OfflineProvider, monkeypatch: pytest.MonkeyPatch, stage: str,
) -> None:
    async def interrupt(*args: Any, **kwargs: Any) -> Any:
        saved = state_api.load_state(tmp_path)
        work = only_article(saved)
        attempts = (work.acquisition_attempts if stage == "acquire"
                    else primary_generation(saved, offline.config).attempts)
        assert attempts[-1].status == "started"
        raise asyncio.CancelledError
    monkeypatch.setattr(worker, "fetch_article" if stage == "acquire" else "complete", interrupt)
    with pytest.raises(asyncio.CancelledError):
        await worker.run_editorial_pass(offline.config, tmp_path, [make_article()], max_calls=1)
    interrupted = state_api.load_state(tmp_path)
    work = only_article(interrupted)
    attempts = (work.acquisition_attempts if stage == "acquire"
                else primary_generation(interrupted, offline.config).attempts)
    assert attempts[-1].status == "started"
    recovered = await worker.run_editorial_pass(offline.config, tmp_path, [], max_calls=0)
    recovered_work = only_article(recovered.state)
    attempts = (recovered_work.acquisition_attempts if stage == "acquire"
                else primary_generation(recovered.state, offline.config).attempts)
    assert attempts[-1].status == "unknown" and attempts[-1].retry_at
    assert recovered.summary.pending == 1 and recovered.summary.ready == 0


async def test_per_request_input_budget_failure_does_not_drop_or_reject_article(
    tmp_path: Path, offline: OfflineProvider, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(worker, "MAX_INPUT_ESTIMATE", 1)
    result = await worker.run_editorial_pass(offline.config, tmp_path, [make_article()],
                                            max_calls=10, deadline_seconds=1)
    assert result.summary.acquired == result.summary.pending == 1
    assert result.summary.calls_this_pass == result.summary.ready == result.summary.rejected == 0
    assert result.summary.stop_reason == "cooldown"
    assert not offline.calls
    assert all("per_request_budget" in generation.last_error
               for generation in only_article(result.state).generations.values())
    assert state_api.load_state(tmp_path) == result.state


async def test_continuing_short_arrivals_cannot_starve_old_long_article(
    tmp_path: Path, offline: OfflineProvider,
) -> None:
    old = make_article(title="Original long work", link="https://example.com/long")
    offline.bodies[old.link] = source_body(4)
    await worker.run_editorial_pass(offline.config, tmp_path, [old], max_calls=1)
    old_id = article_hash(old.title, old.link)
    completed = False
    for index in range(12):
        new = make_article(title=f"New article {index}", link=f"https://example.com/new-{index}")
        result = await worker.run_editorial_pass(offline.config, tmp_path, [new], max_calls=2)
        original = result.state.articles[old_id]
        generation = state_api.current_generation(original, offline.config.review.primary.provider,
                                                  offline.config.review.primary.model)
        assert generation is not None
        if generation.final is not None:
            completed = True
            break
    assert completed, "Original work must finish despite a continual stream of short arrivals"
    old_calls = [call for call in offline.calls if call["stage"] == "chunk" and call["payload"]["title"] == old.title]
    assert [call["payload"]["start"] for call in old_calls] == [
        chunk.start for chunk in state_api.make_chunks(offline.bodies[old.link])
    ]


async def test_provider_outage_on_one_article_routes_later_article_directly_to_fallback(
    tmp_path: Path, offline: OfflineProvider,
) -> None:
    primary = offline.config.review.primary
    secondary = offline.config.review.secondary
    articles = [make_article(title=f"Outage article {index}", link=f"https://example.com/outage-{index}")
                for index in range(2)]
    offline.response = lambda call, answer: RuntimeError("Provider is unavailable") if (
        call["provider"] == primary.provider) else answer
    result = await worker.run_editorial_pass(offline.config, tmp_path, articles, max_calls=10, deadline_seconds=1)
    assert sum(call["provider"] == primary.provider for call in offline.calls) == 1
    assert result.summary.ready == 2
    for ready in state_api.ready_results(result.state, offline.config):
        assert ready.provider == secondary.provider and not ready.independent_complete
    later = result.state.articles[article_hash(articles[1].title, articles[1].link)]
    generation = state_api.current_generation(later, primary.provider, primary.model)
    assert generation is not None and not generation.attempts
    assert generation.last_error == "ProviderUnavailable"


async def test_malformed_article_does_not_quarantine_healthy_primary_on_other_articles(
    tmp_path: Path, offline: OfflineProvider,
) -> None:
    articles = [make_article(title=f"Schema article {index}", link=f"https://example.com/schema-{index}")
                for index in range(2)]

    def one_invalid(call: dict[str, Any], answer: dict[str, Any]) -> Any:
        if call["provider"] == offline.config.review.primary.provider and (
            call["payload"].get("title") == articles[0].title):
            return "Malformed model response"
        return answer
    offline.response = one_invalid
    result = await worker.run_editorial_pass(offline.config, tmp_path, articles, max_calls=10, deadline_seconds=1)
    second = [call for call in offline.calls if call["payload"].get("title") == articles[1].title]
    assert [call["provider"] for call in second] == [offline.config.review.primary.provider] * 2
    ready = {item.title: item for item in state_api.ready_results(result.state, offline.config)}
    assert ready[articles[1].title].provider == offline.config.review.primary.provider
    assert ready[articles[0].title].provider == offline.config.review.secondary.provider


async def test_ordinary_provider_pacing_does_not_switch_articles_to_fallback(
    tmp_path: Path, offline: OfflineProvider,
) -> None:
    articles = [make_article(title=f"Paced article {index}", link=f"https://example.com/paced-{index}")
                for index in range(2)]
    first = await worker.run_editorial_pass(offline.config, tmp_path, articles, max_calls=1)
    first.state.provider_next_eligible[offline.config.review.primary.provider] = (
        datetime.now(UTC) + timedelta(hours=1)).isoformat()
    state_api.store_state(first.state, tmp_path)
    result = await worker.run_editorial_pass(offline.config, tmp_path, [], max_calls=10, deadline_seconds=1)
    assert result.summary.pending == 2 and result.summary.ready == 0
    assert result.summary.stop_reason == "cooldown" and len(offline.calls) == 1
    assert all(generation.provider == offline.config.review.primary.provider
               for article in result.state.articles.values() for generation in article.generations.values())


async def test_empty_chunks_are_processed_before_whole_article_editorial_rejection(
    tmp_path: Path, offline: OfflineProvider,
) -> None:
    article = make_article()
    offline.bodies[article.link] = source_body(3)

    def no_findings(call: dict[str, Any], answer: dict[str, Any]) -> dict[str, Any]:
        if call["stage"] == "chunk":
            return {"claims": [], "empty_reason": "Сегмент содержит только описание структуры документа."}
        if call["stage"] == "final":
            answer.update(decision="rejected", reason="Источник описывает структуру документа без новых результатов.")
            for name in ("fact", "inference", "limitation", "why_read"):
                answer[name] = None
        return answer
    offline.response = no_findings
    result = await worker.run_editorial_pass(offline.config, tmp_path, [article], max_calls=20)
    assert result.summary.rejected == result.summary.fully_analysed == 1
    assert result.summary.completed_chunks == 3
    assert [call["stage"] for call in offline.calls] == ["chunk"] * 3 + ["reduce"] * 2 + ["final"]
    generation = primary_generation(result.state, offline.config)
    assert all(not node.claims and node.empty_reason for node in generation.nodes.values())


async def test_retry_after_runtime_outage_extends_durable_cooldown(
    tmp_path: Path, offline: OfflineProvider,
) -> None:
    def retry_after(call: dict[str, Any], answer: dict[str, Any]) -> Any:
        call["config"].llm._runtime = SimpleNamespace(unavailable_until={
            (call["provider"], call["model"]): time.monotonic() + 7200,
        })
        return RuntimeError("Synthetic two-hour Retry-After")
    offline.response = retry_after
    started = time.time()
    result = await worker.run_editorial_pass(offline.config, tmp_path, [make_article()], max_calls=1)
    generation = primary_generation(result.state, offline.config)
    assert generation.blocked_until is not None
    assert datetime.fromisoformat(generation.blocked_until).timestamp() >= started + 7200
    assert generation.attempts[0].retry_at == generation.blocked_until
    assert result.state.provider_unavailable_until[generation.provider] == generation.blocked_until


async def test_independent_analysis_cannot_reopen_confirmed_delivery(
    tmp_path: Path, offline: OfflineProvider,
) -> None:
    result = await worker.run_editorial_pass(offline.config, tmp_path, [make_article()], max_calls=2)
    work = only_article(result.state)
    work.delivery_state, work.delivery_attempt_id = "delivered", "confirmed-telegram-receipt"
    primary = deepcopy(primary_generation(result.state, offline.config))
    state_api.store_state(result.state, tmp_path)
    independent = await worker.run_editorial_pass(offline.config, tmp_path, [], mode="independent", max_calls=10)
    work = only_article(independent.state)
    assert (work.delivery_state, work.delivery_attempt_id) == ("delivered", "confirmed-telegram-receipt")
    assert primary_generation(independent.state, offline.config) == primary
    assert state_api.ready_results(independent.state, offline.config) == []
    assert independent.summary.delivered == 1 and independent.summary.independent_complete == 1


def reject_secondary(offline: OfflineProvider) -> None:
    def response(call: dict[str, Any], answer: dict[str, Any]) -> dict[str, Any]:
        secondary = offline.config.review.secondary
        if call["stage"] == "final" and (call["provider"], call["model"]) == (secondary.provider, secondary.model):
            answer.update(decision="rejected",
                          reason="Измерение повторяет известный результат без проверки новых условий.")
            for name in ("fact", "inference", "limitation", "why_read"):
                answer[name] = None
        return answer
    offline.response = response


async def test_disagreement_triggers_bounded_third_opinion_on_same_source_without_opinion_leakage(
    tmp_path: Path, offline: OfflineProvider,
) -> None:
    article = make_article()
    offline.bodies[article.link] = source_body(2)
    reject_secondary(offline)
    primary_result = await worker.run_editorial_pass(offline.config, tmp_path, [article], max_calls=10)
    primary = deepcopy(primary_generation(primary_result.state, offline.config))
    assert primary_result.summary.ready == 1
    assert state_api.ready_results(primary_result.state, offline.config)[0].third_review_status == "awaiting_comparison"
    compared = await worker.run_editorial_pass(offline.config, tmp_path, [], mode="independent", max_calls=4)
    ready = state_api.ready_results(compared.state, offline.config)[0]
    assert ready.third_review_status == "pending" and ready.independent_disagreement
    assert not ready.independent_complete and ready.provider == offline.config.review.primary.provider
    assert compared.summary.third_pending == compared.summary.independent_disagreements == 1
    third = offline.config.review.tie_breaker
    assert third is not None
    assert not [call for call in offline.calls if call["model"] == third.model]
    partial = await worker.run_editorial_pass(offline.config, tmp_path, [], mode="independent", max_calls=1)
    assert partial.summary.calls_this_pass == partial.summary.third_pending == 1
    assert partial.summary.ready == 1
    finished = await worker.run_editorial_pass(offline.config, tmp_path, [], mode="independent", max_calls=10)
    third_calls = [call for call in offline.calls if call["model"] == third.model]
    primary_calls = [call for call in offline.calls if call["model"] == offline.config.review.primary.model]
    assert len(third_calls) == len(primary_calls) == 4
    assert [call["messages"] for call in third_calls if call["stage"] == "chunk"] == [
        call["messages"] for call in primary_calls if call["stage"] == "chunk"
    ]
    prompts = json.dumps([call["messages"] for call in third_calls], ensure_ascii=False)
    assert PRIMARY_MARKER not in prompts and PEER_MARKER not in prompts
    assert THIRD_MARKER in prompts
    assert primary_generation(finished.state, offline.config) == primary
    ready = state_api.ready_results(finished.state, offline.config)[0]
    assert ready.third_review_status == "complete" and ready.independent_disagreement and ready.independent_complete
    assert finished.summary.third_pending == 0 and finished.summary.independent_complete == 1
    assert len(offline.fetches) == 1 and len(list((tmp_path / "bodies").iterdir())) == 1
    assert only_article(finished.state).delivery_state == "pending"
    assert state_api.load_state(tmp_path) == finished.state


async def test_two_ready_judgments_never_invoke_configured_third_model(
    tmp_path: Path, offline: OfflineProvider,
) -> None:
    await worker.run_editorial_pass(offline.config, tmp_path, [make_article()], max_calls=10)
    result = await worker.run_editorial_pass(offline.config, tmp_path, [], mode="independent", max_calls=10)
    third = offline.config.review.tie_breaker
    assert third is not None
    assert len(offline.calls) == 4 and all(call["model"] != third.model for call in offline.calls)
    ready = state_api.ready_results(result.state, offline.config)[0]
    assert ready.independent_complete and not ready.independent_disagreement
    assert ready.third_review_status == "not_required"
    assert result.summary.third_pending == result.summary.independent_disagreements == 0
    assert result.summary.independent_complete == 1
    again = await worker.run_editorial_pass(offline.config, tmp_path, [], mode="independent", max_calls=10)
    assert again.summary.calls_this_pass == 0 and len(offline.calls) == 4


async def test_disagreement_without_third_configuration_is_explicit_and_keeps_primary_ready(
    tmp_path: Path, offline: OfflineProvider,
) -> None:
    offline.config.review.tie_breaker = None
    reject_secondary(offline)
    first = await worker.run_editorial_pass(offline.config, tmp_path, [make_article()], max_calls=10)
    primary = deepcopy(primary_generation(first.state, offline.config))
    result = await worker.run_editorial_pass(offline.config, tmp_path, [], mode="independent", max_calls=10)
    ready = state_api.ready_results(result.state, offline.config)[0]
    assert ready.third_review_status == "not_configured" and ready.independent_disagreement
    assert ready.provider == offline.config.review.primary.provider and ready.independent_complete
    assert result.summary.independent_disagreements == result.summary.ready == 1
    assert result.summary.third_pending == 0
    assert primary_generation(result.state, offline.config) == primary
    assert len(only_article(result.state).generations) == 2 and len(offline.calls) == 4
    assert state_api.independent_status(only_article(result.state), offline.config) == (True, "not_configured", True)


@pytest.mark.parametrize("feed_published,source_published", [
    ("2026-10-01T03:00:00+00:00", "2025-04-09T12:00:00+00:00"),
    (None, "2025-04-09T12:00:00+00:00"),
    ("2026-10-01T03:00:00+00:00", None),
    (None, None),
])
async def test_chunk_and_final_preserve_both_dates_and_prefer_original_source_publication(
    tmp_path: Path, offline: OfflineProvider, monkeypatch: pytest.MonkeyPatch,
    feed_published: str | None, source_published: str | None,
) -> None:
    article = make_article()
    article.pub_date = datetime.fromisoformat(feed_published) if feed_published else None

    async def dated_source(url: str) -> FetchedArticle:
        return FetchedArticle(source_body(), url + "?canonical", NOW, source_published, "article",
                              ("Complete synthetic source with original publication provenance.",))
    monkeypatch.setattr(worker, "fetch_article", dated_source)
    result = await worker.run_editorial_pass(offline.config, tmp_path, [article], max_calls=2)
    assert result.summary.ready == 1
    assert [call["stage"] for call in offline.calls] == ["chunk", "final"]
    for call in offline.calls:
        payload = call["payload"]
        assert payload["category"] == article.category
        assert payload["feed_published"] == feed_published
        assert payload["source_published"] == source_published
        assert payload["published"] == (source_published or feed_published)
        instructions = call["messages"][0]["content"].lower()
        assert "feed" in instructions and "novelty" in instructions
        if call["stage"] == "final":
            assert "technology architect" in instructions and "software and enterprise systems" in instructions
            assert "adjacent subject categories" in instructions and "not architectural value" in instructions
            assert "do not force a banking angle" in instructions
    work = only_article(result.state)
    assert work.published == feed_published and work.source_published == source_published
    ready = state_api.ready_results(result.state, offline.config)[0]
    assert ready.published == feed_published and ready.source_published == source_published
    assert state_api.load_state(tmp_path) == result.state


async def test_permanent_runtime_unavailability_persists_finite_cooldown_and_allows_fallback(
    tmp_path: Path, offline: OfflineProvider,
) -> None:
    primary = offline.config.review.primary

    def permanent_failure(call: dict[str, Any], answer: dict[str, Any]) -> Any:
        if (call["provider"], call["model"]) == (primary.provider, primary.model):
            call["config"].llm._runtime = SimpleNamespace(unavailable_until={
                (primary.provider, primary.model): float("inf"),
            })
            return RuntimeError("Permanent provider authentication or account failure")
        return answer
    offline.response = permanent_failure
    started = time.time()
    result = await worker.run_editorial_pass(offline.config, tmp_path, [make_article()],
                                            max_calls=10, deadline_seconds=1)
    generation = primary_generation(result.state, offline.config)
    assert len(generation.attempts) == 1 and generation.final is None
    attempt = generation.attempts[0]
    assert attempt.status == "failed" and attempt.error == "RuntimeError"
    assert attempt.response_sha256 is None and attempt.rejected_output is None
    assert attempt.retry_at == generation.blocked_until and attempt.retry_at is not None
    retry_timestamp = datetime.fromisoformat(attempt.retry_at).timestamp()
    assert started + 23 * 3600 <= retry_timestamp <= time.time() + 25 * 3600
    assert result.state.provider_unavailable_until[primary.provider] == generation.blocked_until
    ready = state_api.ready_results(result.state, offline.config)
    assert len(ready) == 1 and ready[0].provider == offline.config.review.secondary.provider
    assert not ready[0].independent_complete
    assert result.summary.ready == 1 and result.summary.calls_this_pass == 3
    assert [call["provider"] for call in offline.calls] == [primary.provider,
                                                           offline.config.review.secondary.provider,
                                                           offline.config.review.secondary.provider]
    assert state_api.load_state(tmp_path) == result.state
