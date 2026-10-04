"""Offline contract tests for complete source use, resumability and immutable quotes."""
from __future__ import annotations

import json
import time
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from digest import llm
from digest.article_source import FetchedArticle
from digest.config import Config, ReadingBriefConfig
from digest.reading_brief import enrich_selected_cards, mark_briefs_delivered, ready_brief_evidence
from digest.reading_brief_state import checksum, load_state, state_root
from scripts.review_fixture import fixture_config
from tests.factories import make_article


def config() -> Config:
    result = fixture_config()
    result.reading_brief = ReadingBriefConfig(True, "gemini", "gemini-3.8-flash")
    result.llm.max_retries = 0
    return result


def fetched(text: str) -> FetchedArticle:
    return FetchedArticle(text, "https://example.com/final", "2026-10-02T12:00:00+00:00",
                          "2026-09-20T09:00:00+00:00", "article",
                          ("Textual content only; semantic completeness is not guaranteed.", "Uninspected images."))


def payload(messages: list[dict[str, str]]) -> dict[str, Any]:
    return json.loads(messages[1]["content"])


def response(messages: list[dict[str, str]], *, abstain: bool = False) -> tuple[str, dict[str, Any]]:
    spans = payload(messages)["spans"]
    ids = [span["id"] for span in spans]
    result = {
        "coverage": {"first_span_id": ids[0], "last_span_id": ids[-1]},
        "selected_span_ids": [] if abstain else [ids[0]],
        "qualification_span_ids": [span["id"] for span in spans if "QUALIFICATION" in span["text"]],
        "reading_angle": None if abstain else {"text": "Read the measured scope before using this result.",
                                                "span_ids": [ids[0]]},
        "abstain": abstain,
    }
    return json.dumps(result), {"finish_reason": "STOP"}


@pytest.mark.asyncio
async def test_direct_full_body_and_late_qualification_are_quoted_after_angle(tmp_path: Path) -> None:
    article = make_article()
    text = "OPENING CLAIM\n\n" + "\n\n".join(f"Section {i}: " + "context " * 50 for i in range(150))
    text += "\n\nFINAL QUALIFICATION: failed deployments are excluded."
    requests: list[list[dict[str, str]]] = []

    async def generate(_role: Any, messages: list[dict[str, str]], _config: Any, **kwargs: Any) -> Any:
        requests.append(messages)
        assert kwargs["provider_override"].model == "gemini-3.8-flash"
        assert kwargs["max_output_tokens"] == 2048
        return response(messages)

    with (patch("digest.reading_brief.fetch_article", AsyncMock(return_value=fetched(text))) as fetch,
          patch("digest.llm.count_gemini_tokens", AsyncMock(return_value=150_000)) as count,
          patch("digest.llm.complete", side_effect=generate) as call):
        run = await enrich_selected_cards([article], config(), tmp_path, time.monotonic() + 1000)
        assert len(run.cards) == 1 and run.pending == 0 and run.abstained == 0
        identity = next(iter(run.quotations))
        assert run.cards[0].summary.startswith("Reading brief: ")
        assert "".join(span["text"] for span in payload(requests[0])["spans"]) == text
        assert "FINAL QUALIFICATION" not in run.cards[0].summary
        assert "FINAL QUALIFICATION" in run.quotations[identity]
        assert "Uninspected images." in run.quotations[identity]
        assert "2026-09-20T09:00:00+00:00" in run.quotations[identity]
        assert "source metadata" in run.quotations[identity]
        assert "Conditions/limitations from the source" in run.quotations[identity]
        assert "FINAL QUALIFICATION" not in run.provenance[identity]
        assert "Published: 2026-09-20 (source)" in run.provenance[identity]
        assert "Images not assessed." in run.provenance[identity]
        assert fetch.call_count == count.call_count == call.call_count == 1
        again = await enrich_selected_cards([], config(), tmp_path, time.monotonic() + 1000)
        assert again.quotations == run.quotations and call.call_count == 1
        with pytest.raises(ValueError):
            mark_briefs_delivered(tmp_path, {"0" * 32})
        assert load_state(tmp_path, identity).status == "ready"
        mark_briefs_delivered(tmp_path, {identity})
        assert load_state(tmp_path, identity).status == "delivered"
        assert not (await enrich_selected_cards([], config(), tmp_path, time.monotonic() + 1000)).cards
        assert call.call_count == 1


@pytest.mark.asyncio
async def test_only_real_exact_overflow_sweeps_every_page_and_resumes_without_dropping(tmp_path: Path) -> None:
    text = "\n\n".join(f"QUALIFICATION {index}: condition {index}." for index in range(8))
    counted: list[list[int]] = []
    generated: list[list[int]] = []
    fail = True

    async def count(messages: list[dict[str, str]], *_args: Any, **_kwargs: Any) -> int:
        ids = [span["id"] for span in payload(messages)["spans"]]
        counted.append(ids)
        return 100 + len(ids) * 10

    async def generate(_role: Any, messages: list[dict[str, str]], *_args: Any, **_kwargs: Any) -> Any:
        nonlocal fail
        ids = [span["id"] for span in payload(messages)["spans"]]
        generated.append(ids)
        if ids == [3, 4] and fail:
            fail = False
            raise RuntimeError("HTTP 429 code=RESOURCE_EXHAUSTED")
        return response(messages)

    with (patch("digest.reading_brief.INPUT_LIMITS", {("gemini", "gemini-3.8-flash"): 121}),
          patch("digest.reading_brief.fetch_article", AsyncMock(return_value=fetched(text))) as fetch,
          patch("digest.llm.count_gemini_tokens", side_effect=count),
          patch("digest.llm.complete", side_effect=generate)):
        first = await enrich_selected_cards([make_article()], config(), tmp_path, time.monotonic() + 1000)
        assert first.pending == 1 and not first.cards and first.abstained == 0
        identity = next(state_root(tmp_path).glob("*.json")).stem
        state = load_state(tmp_path, identity)
        assert state.error_class == "technical_quota_or_budget" and state.attempts == 1
        assert state.pages[0].result is not None
        assert generated == [[1, 2], [3, 4]]
        second = await enrich_selected_cards([], config(), tmp_path, time.monotonic() + 1000)
        assert second.pending == 0 and len(second.cards) == 1
        assert counted[0] == list(range(1, 9))
        assert generated == [[1, 2], [3, 4], [3, 4], [5, 6], [7, 8]]
        assert counted.count([3, 4]) == 1 and fetch.call_count == 1
        assert all(f"QUALIFICATION {i}: condition {i}." in second.quotations[identity] for i in range(8))
        assert load_state(tmp_path, identity).attempts == 2
        state, source = ready_brief_evidence(tmp_path, identity)
        assert [span for page in state.pages for span in page.result.covered_span_ids] == list(range(1, 9))
        assert "".join(source.text[s.start:s.end] for s in source.spans) == text


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["truncated", "unknown_id", "partial_coverage", "no_angle_citations",
                                    "missing_brief", "bad_type"])
async def test_invalid_or_truncated_output_stays_pending_without_repair(tmp_path: Path, damage: str) -> None:
    async def generate(_role: Any, messages: list[dict[str, str]], *_args: Any, **_kwargs: Any) -> Any:
        text, usage = response(messages)
        data = json.loads(text)
        if damage == "truncated":
            usage["finish_reason"] = "MAX_TOKENS"
        elif damage == "unknown_id":
            data["qualification_span_ids"] = [999]
        elif damage == "partial_coverage":
            data["coverage"]["last_span_id"] = 1
        elif damage == "no_angle_citations":
            data["reading_angle"]["span_ids"] = []
        elif damage == "missing_brief":
            data["reading_angle"] = None
        else:
            data["abstain"] = "false"
        return json.dumps(data), usage

    with (patch("digest.reading_brief.fetch_article", AsyncMock(return_value=fetched("Claim.\n\nQualification."))),
          patch("digest.llm.count_gemini_tokens", AsyncMock(return_value=100)),
          patch("digest.llm.complete", side_effect=generate) as call):
        run = await enrich_selected_cards([make_article()], config(), tmp_path, time.monotonic() + 1000)
    assert not run.cards and run.pending == 1 and run.abstained == 0 and call.call_count == 1


@pytest.mark.asyncio
async def test_abstention_is_semantic_only_after_every_page_completes(tmp_path: Path) -> None:
    async def generate(_role: Any, messages: list[dict[str, str]], *_args: Any, **_kwargs: Any) -> Any:
        return response(messages, abstain=True)

    with (patch("digest.reading_brief.fetch_article", AsyncMock(return_value=fetched("Public routine announcement."))),
          patch("digest.llm.count_gemini_tokens", AsyncMock(return_value=100)),
          patch("digest.llm.complete", side_effect=generate) as call):
        run = await enrich_selected_cards([make_article()], config(), tmp_path, time.monotonic() + 1000)
        again = await enrich_selected_cards([], config(), tmp_path, time.monotonic() + 1000)
    assert not run.cards and run.pending == 0 and run.abstained == again.abstained == 1
    assert call.call_count == 1


@pytest.mark.asyncio
async def test_incomplete_fetch_and_exhausted_budget_never_become_editorial_rejections(tmp_path: Path) -> None:
    cfg = config()
    llm.set_request_limit(cfg, 0)
    with (patch("digest.reading_brief.fetch_article", AsyncMock(side_effect=ValueError("coverage_incomplete"))),
          patch("digest.llm.count_gemini_tokens", AsyncMock()) as count):
        first = await enrich_selected_cards([make_article()], cfg, tmp_path, time.monotonic() + 1000)
    assert first.pending == 1 and first.abstained == 0 and first.oldest_pending
    with (patch("digest.reading_brief.fetch_article", AsyncMock(return_value=fetched("Complete article text."))),
          patch("digest.llm.count_gemini_tokens", AsyncMock()) as count):
        second = await enrich_selected_cards([], cfg, tmp_path, time.monotonic() + 1000)
    assert second.pending == 1 and second.abstained == 0 and count.call_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("remaining,expected_calls", [(60.0, 1), (34.0, 0)])
async def test_deadline_admits_a_clipped_useful_window_after_pacing(
    tmp_path: Path, remaining: float, expected_calls: int,
) -> None:
    async def generate_response(_role: Any, messages: list[dict[str, str]], *_args: Any, **kwargs: Any) -> Any:
        assert 30 <= kwargs["request_timeout_seconds"] <= remaining - 5 - 0.25
        assert kwargs["request_timeout_seconds"] < 120
        return response(messages)

    with (patch("digest.reading_brief.fetch_article", AsyncMock(return_value=fetched("Complete article text."))),
          patch("digest.llm.request_wait_seconds", return_value=5),
          patch("digest.llm.count_gemini_tokens", AsyncMock(return_value=100)) as count,
          patch("digest.llm.complete", side_effect=generate_response) as generate):
        run = await enrich_selected_cards([make_article()], config(), tmp_path, time.monotonic() + remaining)
    assert run.pending == 1 - expected_calls and count.call_count == 1 and generate.call_count == expected_calls


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["result_id", "angle", "ready_before_coverage", "route", "source"])
async def test_completed_state_cannot_reemit_after_evidence_or_result_tampering(tmp_path: Path, damage: str) -> None:
    async def generate(_role: Any, messages: list[dict[str, str]], *_args: Any, **_kwargs: Any) -> Any:
        return response(messages)

    full_source = fetched("First source.\n\nSecond source.")
    with (patch("digest.reading_brief.fetch_article", AsyncMock(return_value=full_source)),
          patch("digest.llm.count_gemini_tokens", AsyncMock(return_value=100)),
          patch("digest.llm.complete", side_effect=generate) as call):
        run = await enrich_selected_cards([make_article()], config(), tmp_path, time.monotonic() + 1000)
        identity = next(iter(run.quotations))
        path = state_root(tmp_path) / f"{identity}.json"
        envelope = json.loads(path.read_text())
        data = envelope["payload"]
        if damage == "result_id":
            data["pages"][0]["result"]["selected_span_ids"] = [2]
        elif damage == "angle":
            data["pages"][0]["result"]["reading_angle"] = "A changed uncited conclusion."
        elif damage == "ready_before_coverage":
            data["pages"][0]["result"] = None
        elif damage == "route":
            data["route"]["model"] = "different-model"
        else:
            source_path = state_root(tmp_path) / "sources" / f"{data['source_sha256']}.json"
            source = json.loads(source_path.read_text())
            source["text"] = "Manufactured source."
            source_path.write_text(json.dumps(source))
        envelope["sha256"] = checksum(data)
        path.write_text(json.dumps(envelope))
        held = await enrich_selected_cards([], config(), tmp_path, time.monotonic() + 1000)
        assert not held.cards and held.pending == 1 and call.call_count == 1
        with pytest.raises(ValueError):
            mark_briefs_delivered(tmp_path, {identity})
        assert load_state(tmp_path, identity).status != "delivered"


@pytest.mark.asyncio
async def test_unknown_profile_holds_admitted_selection_without_fetch_or_fallback(tmp_path: Path) -> None:
    cfg = config()
    cfg.reading_brief = replace(cfg.reading_brief, model="unprofiled-model")
    with patch("digest.reading_brief.fetch_article", AsyncMock()) as fetch:
        run = await enrich_selected_cards([make_article()], cfg, tmp_path, time.monotonic() + 1000)
    assert run.pending == 1 and not run.cards and fetch.call_count == 0


@pytest.mark.asyncio
async def test_acknowledgment_persistence_failure_propagates_and_reconciliation_is_exact(tmp_path: Path) -> None:
    from digest.reading_brief import reconcile_briefs_delivered

    async def generate(_role: Any, messages: list[dict[str, str]], *_args: Any, **_kwargs: Any) -> Any:
        text, usage = response(messages)
        usage.update(prompt_tokens=100, completion_tokens=80, total_tokens=180)
        return text, usage

    with (patch("digest.reading_brief.fetch_article", AsyncMock(return_value=fetched("Complete public article."))),
          patch("digest.llm.count_gemini_tokens", AsyncMock(return_value=100)),
          patch("digest.llm.complete", side_effect=generate)):
        run = await enrich_selected_cards([make_article()], config(), tmp_path, time.monotonic() + 1000)
    identity = next(iter(run.quotations))
    assert load_state(tmp_path, identity).pages[0].usage == {
        "prompt_tokens": 100, "completion_tokens": 80, "total_tokens": 180,
    }
    with patch("digest.reading_brief.save_state", side_effect=OSError("disk full")):
        with pytest.raises(OSError, match="disk full"):
            mark_briefs_delivered(tmp_path, {identity})
    assert load_state(tmp_path, identity).status == "ready"
    reconcile_briefs_delivered(tmp_path, {"0" * 32})
    assert load_state(tmp_path, identity).status == "ready"
    reconcile_briefs_delivered(tmp_path, {"0" * 32, identity})
    assert load_state(tmp_path, identity).status == "delivered"
    mark_briefs_delivered(tmp_path, {identity})


@pytest.mark.asyncio
async def test_original_rss_context_survives_metadata_drift_but_does_not_enter_reader_prompt(tmp_path: Path) -> None:
    original = make_article(description="UNVERIFIED RSS ANNOUNCEMENT", category="Original category")
    messages_seen = []

    async def generate(_role: Any, messages: list[dict[str, str]], *_args: Any, **_kwargs: Any) -> Any:
        messages_seen.append(messages)
        return response(messages)

    with (patch("digest.reading_brief.fetch_article", AsyncMock(return_value=fetched("Complete actual source."))),
          patch("digest.llm.count_gemini_tokens", AsyncMock(return_value=100)),
          patch("digest.llm.complete", side_effect=generate) as call):
        first = await enrich_selected_cards([original], config(), tmp_path, time.monotonic() + 1000)
        later = replace(original, description="Changed RSS teaser", category="Changed category", pub_date=None)
        second = await enrich_selected_cards([later], config(), tmp_path, time.monotonic() + 1000)
    assert second.cards == first.cards and second.articles[0] == original and call.call_count == 1
    assert "UNVERIFIED RSS ANNOUNCEMENT" not in json.dumps(messages_seen)


@pytest.mark.asyncio
async def test_corrupt_backlog_has_unknown_age_and_cannot_be_readmitted(tmp_path: Path) -> None:
    from digest.radar.collector import article_hash

    article = make_article()
    identity = article_hash(article.title, article.link)
    (state_root(tmp_path) / f"{identity}.json").write_text("damaged file")
    with patch("digest.reading_brief.fetch_article", AsyncMock()) as fetch:
        result = await enrich_selected_cards([article], config(), tmp_path, time.monotonic() + 1000)
    assert result.pending == 1 and result.oldest_pending is None and fetch.call_count == 0


@pytest.mark.asyncio
async def test_fetch_transport_failure_is_persisted_as_technical_pending(tmp_path: Path) -> None:
    import httpx

    with patch("digest.reading_brief.fetch_article", AsyncMock(side_effect=httpx.ConnectError("offline"))):
        result = await enrich_selected_cards([make_article()], config(), tmp_path, time.monotonic() + 1000)
    assert result.pending == 1 and not result.cards and result.abstained == 0


@pytest.mark.asyncio
async def test_substantive_brief_keeps_conditions_and_unresolved_conflict_separate_from_quote_archive(
    tmp_path: Path,
) -> None:
    text = (
        "The cache places hot keys in memory to reduce lookup latency.\n\n"
        "The overview says cached values persist across a process restart.\n\n"
        "The recovery section says the volatile cache loses every value on restart.\n\n"
        "The latency measurements apply only to warm reads; rebuild time is excluded."
    )
    brief = (
        "The source reports that the cache speeds warm reads by keeping hot keys in memory. "
        'Its measurements "apply only to warm reads; rebuild time is excluded". '
        'The overview says "cached values persist across a process restart", while the recovery section says '
        '"the volatile cache loses every value on restart". The source leaves restart durability unresolved.'
    )

    async def generate(_role: Any, messages: list[dict[str, str]], *_args: Any, **_kwargs: Any) -> Any:
        instructions = messages[0]["content"]
        assert "concrete mechanism, result or tradeoff supported by the supplied passages" in instructions
        assert "publication brief, not a place for your own architectural interpretation" in instructions
        assert "Every factual clause" in instructions and "be supported by the cited passages" in instructions
        assert "Do not add unstated mechanisms, causal explanations, exclusivity" in instructions
        assert "Describing one route or capability does not establish that alternatives are" in instructions
        assert "Preserve distinctions between related technical concepts" in instructions
        assert "Attribute reported results and assurances to their source" in instructions
        assert "do not fill it with domain knowledge" in instructions
        assert "short attributed quotation of the relevant" in instructions
        assert "preserving its relation verb, modality and negation" in instructions
        assert "Do not re-express that clause" in instructions and "as a stronger restriction" in instructions
        assert "Keep the quotation within the brief; retain full passages" in instructions
        assert "unresolved" in instructions and "invent a resolution" in instructions
        assert "Retain the nominated material conditions in that prose" in instructions
        assert "cache" not in instructions.lower()
        assert "".join(span["text"] for span in payload(messages)["spans"]) == text
        return json.dumps({
            "coverage": {"first_span_id": 1, "last_span_id": 4},
            "selected_span_ids": [1], "qualification_span_ids": [2, 3, 4],
            "reading_angle": {"text": brief, "span_ids": [1, 2, 3, 4]}, "abstain": False,
        }), {"finish_reason": "STOP"}

    with (patch("digest.reading_brief.fetch_article", AsyncMock(return_value=fetched(text))),
          patch("digest.llm.count_gemini_tokens", AsyncMock(return_value=100)) as count,
          patch("digest.llm.complete", side_effect=generate) as call):
        run = await enrich_selected_cards([make_article()], config(), tmp_path, time.monotonic() + 1000)
    identity = next(iter(run.quotations))
    assert run.cards[0].summary == f"Reading brief: {brief}"
    assert all(part in run.quotations[identity] for part in text.split("\n\n"))
    assert "Citations: [S1-S4]" in run.provenance[identity]
    assert not any(part in run.provenance[identity] for part in text.split("\n\n"))
    assert count.call_count == call.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("previous_version", ["source-passages-v1", "source-passages-v2", "source-passages-v3"])
async def test_cached_previous_brief_cannot_be_reused_or_silently_rewritten(
    tmp_path: Path, previous_version: str,
) -> None:
    async def generate(_role: Any, messages: list[dict[str, str]], *_args: Any, **_kwargs: Any) -> Any:
        return response(messages)

    with (patch("digest.reading_brief.fetch_article", AsyncMock(return_value=fetched("Complete public article."))),
          patch("digest.llm.count_gemini_tokens", AsyncMock(return_value=100)),
          patch("digest.llm.complete", side_effect=generate) as call):
        ready = await enrich_selected_cards([make_article()], config(), tmp_path, time.monotonic() + 1000)
        identity = next(iter(ready.quotations))
        path = state_root(tmp_path) / f"{identity}.json"
        envelope = json.loads(path.read_text())
        assert envelope["payload"]["route"]["prompt_version"] == "source-passages-v4"
        envelope["payload"]["route"]["prompt_version"] = previous_version
        envelope["sha256"] = checksum(envelope["payload"])
        legacy_bytes = json.dumps(envelope).encode()
        path.write_bytes(legacy_bytes)
        held = await enrich_selected_cards([], config(), tmp_path, time.monotonic() + 1000)
    assert held.pending == 1 and not held.cards and not held.provenance and call.call_count == 1
    assert path.read_bytes() == legacy_bytes


@pytest.mark.asyncio
async def test_abstaining_later_page_qualification_remains_literal_in_retained_evidence(tmp_path: Path) -> None:
    claim = "The cache accelerates every read."
    condition = "The result applies only to the pilot deployment; production traffic was not evaluated."
    text = claim + "\n\n" + condition
    generated_pages = []

    async def count(messages: list[dict[str, str]], *_args: Any, **_kwargs: Any) -> int:
        return 100 + 10 * len(payload(messages)["spans"])

    async def generate(_role: Any, messages: list[dict[str, str]], *_args: Any, **_kwargs: Any) -> Any:
        ids = [span["id"] for span in payload(messages)["spans"]]
        generated_pages.append(ids)
        is_qualification = ids == [2]
        return json.dumps({
            "coverage": {"first_span_id": ids[0], "last_span_id": ids[-1]},
            "selected_span_ids": [] if is_qualification else [1],
            "qualification_span_ids": [2] if is_qualification else [],
            "reading_angle": None if is_qualification else {"text": claim, "span_ids": [1]},
            "abstain": is_qualification,
        }), {"finish_reason": "STOP"}

    cfg = config()
    with (patch("digest.reading_brief.INPUT_LIMITS", {("gemini", "gemini-3.8-flash"): 115}),
          patch("digest.reading_brief.fetch_article", AsyncMock(return_value=fetched(text))),
          patch("digest.llm.count_gemini_tokens", side_effect=count),
          patch("digest.llm.complete", side_effect=generate)):
        run = await enrich_selected_cards([make_article()], cfg, tmp_path, time.monotonic() + 1000)
    identity = next(iter(run.quotations))
    assert generated_pages == [[1], [2]] and run.pending == 0 and len(run.cards) == 1
    assert load_state(tmp_path, identity).pages[1].result.abstain is True
    assert "Conditions/limitations from the source (original text)" in run.provenance[identity]
    assert f"[S2]\n{condition}" in run.provenance[identity]
    assert claim in run.quotations[identity] and condition in run.quotations[identity]
