"""Publication-stage recovery and exact-text integrity, with no provider/network work."""
from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from digest.config import EnrichmentConfig, ReviewModelConfig
from digest.editorial_state import EditorialState, admit_articles, load_state, make_chunks, save_body, store_state
from digest.enrichment_tokens import InputCount
from digest.llm import LLMProviderError, ProviderResponseDiagnostics
from digest.publication_contract import parse_claims, parse_verdicts, render_card
from digest.publication_worker import current_work, run_publication_pass
from scripts.review_fixture import fixture_config
from tests.factories import make_article


@pytest.fixture
def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Path, str]:
    config = fixture_config()
    config.radar.language = "en"
    config.enrichment = EnrichmentConfig(ReviewModelConfig("groq", "qwen/qwen3.8-27b"),
                                         ReviewModelConfig("groq", "openai/gpt-oss-120b"))
    state = EditorialState()
    admit_articles(state, [make_article(title="Fixture", link="https://example.com/fixture")])
    article = next(iter(state.articles.values()))
    body = "The service remains a preview. General availability has not been announced."
    article.body_sha256 = save_body(tmp_path, body)
    article.chunks = make_chunks(body)
    article.final_url = article.url
    article.fetched_at = "2026-10-01T00:00:00+00:00"
    article.extraction_status = "article"
    store_state(state, tmp_path)
    monkeypatch.setattr("digest.publication_worker.count_input", lambda *args: InputCount(100, "fixture", "a" * 64))
    monkeypatch.setattr("digest.publication_worker.next_request_time", lambda *args: (time.time(), "fixture"))
    monkeypatch.setattr("httpx.AsyncClient", lambda *args, **kwargs: pytest.fail("No HTTP allowed"))
    return config, tmp_path, article.article_id


def writer(text: str) -> str:
    return json.dumps({"claims": [{"id": "lowercase-id", "text": text, "source_ids": ["S0"],
                                  "kind": "fact", "scope": {"subject": "service", "predicate_object": "preview",
                                  "quantity_population": "", "time_status": "current", "conditions": []}}]})


def verdict(text: str, status: str = "supported") -> str:
    return json.dumps({"verdicts": [{"candidate_id": "A", "claim_id": "C1", "verdict": status,
                                    "reason": "Source states preview", "source_ids": ["S0"],
                                    "scope_checks": {
                                        "actor_population": "preserved",
                                        "time_availability": "preserved" if status == "supported" else "broadened",
                                        "material_conditions": "preserved", "qualification_source_ids": ["S0"],
                                    }}]})


@pytest.mark.asyncio
async def test_one_repair_rechecks_exact_revised_text_and_resumes_without_repeating_calls(
    setup: tuple, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, directory, identity = setup
    seen = []

    async def model(role: Any, messages: list, actual: Any, **kwargs: Any) -> tuple[str, dict]:
        payload = json.loads(messages[1]["content"])
        persisted = load_state(directory).articles[identity]
        work = current_work(identity, load_state(directory), config)
        assert work is not None and work.attempts[-1].status == "started"
        seen.append(payload)
        if len(seen) == 1:
            assert set(payload) == {"source_spans"}
            assert kwargs["provider_override"].model == config.enrichment.writer.model
            text = writer("The service is generally available.")
        elif len(seen) == 2:
            assert kwargs["reasoning_effort"] == "medium"
            assert set(payload) == {"source_spans", "candidates"}
            text = verdict("", "unsupported")
        elif len(seen) == 3:
            assert work.repair_round == 1 and work.repair_binding
            assert persisted.publications[work.binding].repair_round == 1
            assert set(payload) == {"source_spans", "prior_draft", "critic_feedback"}
            assert payload["critic_feedback"][0]["verdict"] == "unsupported"
            text = writer("The service remains a preview.")
        else:
            assert set(payload) == {"source_spans", "candidates"}
            assert payload["candidates"][0]["claims"][0]["text"] == "The service remains a preview."
            text = verdict("")
        return text, {"finish_reason": "stop", "prompt_tokens": 100, "completion_tokens": 50}

    complete = AsyncMock(side_effect=model)
    monkeypatch.setattr("digest.publication_worker.complete", complete)
    first = await run_publication_pass(config, directory, max_calls=2)
    work = current_work(identity, first.state, config)
    assert work is not None and work.outcome == "pending" and work.repair_round == 0
    final = await run_publication_pass(config, directory, max_calls=2)
    work = current_work(identity, final.state, config)
    assert work is not None and work.outcome == "model_checked" and work.repair_round == 1
    assert work.drafts[0].claims[0].writer_id == "lowercase-id"
    assert work.drafts[0].claims[0].claim_id == "C1"
    rendered = render_card(work.drafts[-1], work.audits[-1], source="Fixture", title="Fixture", url="https://example.com",
                           source_published_at="2026-09-29", feed_published_at="2026-09-30", fetched_at="2026-10-01")
    assert "The service remains a preview." in rendered and "generally available" not in rendered
    assert "Source publication date (page metadata): 2026-09-29" in rendered
    assert "Feed publication date (RSS/Atom metadata): 2026-09-30" in rendered
    assert "Source fetched as of: 2026-10-01" in rendered
    assert await run_publication_pass(config, directory, max_calls=4)
    assert complete.await_count == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["second_rejection", "uncertain_repair"])
async def test_correction_limit_and_uncertain_inference_never_start_another_round(
    setup: tuple, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    config, directory, identity = setup
    responses: list[Any] = [(writer("The service is generally available."), {"finish_reason": "stop"}),
                            (verdict("", "unsupported"), {"finish_reason": "stop"})]
    if failure == "uncertain_repair":
        responses.append(TimeoutError())
    else:
        responses.extend([(writer("Another unproven claim."), {"finish_reason": "stop"}),
                          (verdict("", "unsupported"), {"finish_reason": "stop"})])
    complete = AsyncMock(side_effect=responses)
    monkeypatch.setattr("digest.publication_worker.complete", complete)
    await run_publication_pass(config, directory, max_calls=6)
    work = current_work(identity, load_state(directory), config)
    assert work is not None and work.repair_round == 1
    assert work.outcome == ("unknown" if failure == "uncertain_repair" else "rejected")
    count = complete.await_count
    await run_publication_pass(config, directory, max_calls=6)
    assert complete.await_count == count


@pytest.mark.asyncio
async def test_missing_verdict_is_held_and_old_blind_review_cannot_approve_publication(
    setup: tuple, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, directory, identity = setup
    complete = AsyncMock(side_effect=[(writer("The service remains a preview."), {"finish_reason": "stop"}),
                                      ('{"verdicts": []}', {"finish_reason": "stop"})])
    monkeypatch.setattr("digest.publication_worker.complete", complete)
    await run_publication_pass(config, directory, max_calls=4)
    work = current_work(identity, load_state(directory), config)
    assert work is not None and work.outcome == "pending" and not work.audits[0].verdicts
    await run_publication_pass(config, directory, max_calls=4)
    assert complete.await_count == 2


@pytest.mark.asyncio
async def test_cached_verdict_mutation_is_rejected_and_input_delta_can_be_negative(
    setup: tuple, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, directory, identity = setup
    complete = AsyncMock(side_effect=[
        (writer("The service is generally available."), {"finish_reason": "stop", "prompt_tokens": 80}),
        (verdict("", "unsupported"), {"finish_reason": "stop", "prompt_tokens": 80}),
    ])
    monkeypatch.setattr("digest.publication_worker.complete", complete)
    await run_publication_pass(config, directory, max_calls=2)
    state = load_state(directory)
    work = current_work(identity, state, config)
    assert work is not None and all(item.usage["input_delta"] == -20 for item in work.attempts)
    path = directory / "state.json"
    payload = json.loads(path.read_text())
    cached = payload["articles"][identity]["publications"][work.binding]
    cached["audits"][0]["verdicts"][0]["verdict"] = "supported"
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError):
        load_state(directory)
    cached["audits"][0]["verdicts"][0]["verdict"] = "unsupported"
    cached["audits"][0]["verdicts"][0]["reason"] = "Changed without a new provider result"
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="matching successful bound request"):
        load_state(directory)
    cached['prompt_version'] = 'unrecognized-version'
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match='Unsupported publication prompt version'):
        load_state(directory)
    with pytest.raises(ValueError, match='Unsupported publication prompt version'):
        render_card(work.drafts[-1], work.audits[-1], source='Fixture', title='Fixture',
                    url='https://example.com', prompt_version='unrecognized-version')
    assert complete.await_count == 2


@pytest.mark.asyncio
async def test_two_batch_check_finishes_before_repair_and_terminal_cache_needs_no_tokenizer(
    setup: tuple, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, directory, identity = setup
    original = json.loads(writer("The service is generally available."))
    second = json.loads(writer("It is released."))["claims"][0]
    second["id"] = "other-writer-id"
    original["claims"].append(second)
    monkeypatch.setattr("digest.publication_worker._plan", lambda claims, *args:
                        tuple((claim.claim_id,) for claim in claims))
    responses = [json.dumps(original), verdict("", "unsupported"),
                 verdict("", "contradicted").replace('"C1"', '"C2"'),
                 writer("The service remains a preview."), verdict("")]
    complete = AsyncMock(side_effect=[(text, {"finish_reason": "stop"}) for text in responses])
    monkeypatch.setattr("digest.publication_worker.complete", complete)
    await run_publication_pass(config, directory, max_calls=2)
    work = current_work(identity, load_state(directory), config)
    assert work is not None and work.repair_round == 0 and work.audits[0].completed_batches == 1
    await run_publication_pass(config, directory, max_calls=3)
    work = current_work(identity, load_state(directory), config)
    assert work is not None and work.outcome == "model_checked" and work.audits[0].completed_batches == 2
    rendered = render_card(work.drafts[-1], work.audits[-1], source="Fixture", title="Fixture", url="https://example.com")
    assert "Source publication date (page metadata): unknown" in rendered
    assert "Feed publication date: unknown" in rendered
    monkeypatch.setattr("digest.publication_worker.count_input", lambda *args: pytest.fail("Terminal cache"))
    await run_publication_pass(config, directory, max_calls=4)
    assert complete.await_count == 5


@pytest.mark.asyncio
@pytest.mark.parametrize("second_transport", ["success", "failed"])
async def test_one_correction_transport_retry_requires_cooldown_and_never_repeats_a_third_time(
    setup: tuple, monkeypatch: pytest.MonkeyPatch, second_transport: str,
) -> None:
    config, directory, identity = setup
    now = time.time()

    def failure(status: int) -> LLMProviderError:
        return LLMProviderError("fixture", ProviderResponseDiagnostics(
            status, "unknown", "unknown", "unknown", datetime.fromtimestamp(now, UTC).isoformat()))

    responses: list[Any] = [(writer("The service is generally available."), {"finish_reason": "stop"}),
                           (verdict("", "unsupported"), {"finish_reason": "stop"}), failure(429)]
    if second_transport == "success":
        responses.extend([(writer("The service remains a preview."), {"finish_reason": "stop"}),
                          (verdict(""), {"finish_reason": "stop"})])
    else:
        responses.append(failure(503))
    complete = AsyncMock(side_effect=responses)
    monkeypatch.setattr("digest.publication_worker.complete", complete)
    await run_publication_pass(config, directory, max_calls=3)
    await run_publication_pass(config, directory, max_calls=2, deadline_seconds=2)
    assert complete.await_count == 3
    monkeypatch.setattr("digest.publication_worker.time.time", lambda: now + 3601)
    await run_publication_pass(config, directory, max_calls=2, deadline_seconds=2)
    work = current_work(identity, load_state(directory), config)
    assert work is not None and work.repair_round == 1
    repairs = [attempt for attempt in work.attempts if attempt.stage == "repair"]
    assert len(repairs) == 2 and repairs[0].prompt_hash == repairs[1].prompt_hash
    assert repairs[0].task_key == repairs[1].task_key
    assert work.outcome == ("model_checked" if second_transport == "success" else "pending")
    count = complete.await_count
    monkeypatch.setattr("digest.publication_worker.time.time", lambda: now + 7202)
    await run_publication_pass(config, directory, max_calls=2)
    assert complete.await_count == count


def test_scope_fixture_checks_reported_constraints_but_cannot_prove_model_judgment() -> None:
    # Generic source fixture: a network has 100 members; a footnote limits the
    # vendor offering to enrolled customers. References do not settle entailment.
    ids = {'S0', 'S1'}
    claims = parse_claims(writer('The vendor service is available to all 100 network members.'), ids)
    raw = json.loads(verdict(''))
    item = raw['verdicts'][0]
    item['source_ids'] = ['S0', 'S1']
    checks = item['scope_checks']
    checks.update(actor_population='broadened', qualification_source_ids=['S1'])
    with pytest.raises(ValueError, match='contradicts its scope'):
        parse_verdicts(json.dumps(raw), claims, ids)
    checks['actor_population'] = 'unknown'
    with pytest.raises(ValueError):
        parse_verdicts(json.dumps(raw), claims, ids)
    checks['actor_population'] = 'preserved'
    item['source_ids'] = ['S0']
    with pytest.raises(ValueError, match='qualifications must be included'):
        parse_verdicts(json.dumps(raw), claims, ids)
    item['source_ids'] = ['S0', 'S1']
    item.pop('scope_checks')
    with pytest.raises(ValueError):
        parse_verdicts(json.dumps(raw), claims, ids)
    item['scope_checks'] = checks
    # A model can still falsely report preserved. The deterministic validator
    # must not be described as an independent semantic truth/coverage checker.
    assert parse_verdicts(json.dumps(raw), claims, ids)[0].verdict == 'supported'
