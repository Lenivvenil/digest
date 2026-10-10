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
from digest.adapters.models.execution import ModelExecution
from digest.article_source import FetchedArticle
from digest.config import Config, ReadingBriefConfig
from digest.radar.collector import Article
from digest.reading_brief import _advance, _routes, _validate_progress, ready_brief_evidence
from digest.reading_brief_state import (
    BriefState,
    Selection,
    checksum,
    completed,
    load_source,
    load_state,
    now,
    page_wire,
    save_state,
    state_root,
    state_wire,
)
from digest.reading_reconciliation import build_reconciliation_input
from scripts.review_fixture import fixture_config
from tests.factories import make_article


def config() -> Config:
    result = fixture_config()
    result.reading_brief = ReadingBriefConfig(True, "gemini", "gemini-3.8-flash")
    result.llm.max_retries = 0
    return result


def fetched(text: str) -> FetchedArticle:
    return FetchedArticle(
        text,
        "https://example.com/final",
        "2026-10-02T12:00:00+00:00",
        "2026-09-20T09:00:00+00:00",
        "article",
        ("Textual content only; semantic completeness is not guaranteed.", "Uninspected images."),
    )


def payload(messages: list[dict[str, str]]) -> dict[str, Any]:
    return json.loads(messages[1]["content"])


def response(messages: list[dict[str, str]], *, abstain: bool = False) -> tuple[str, dict[str, Any]]:
    spans = payload(messages)["spans"]
    ids = [span["id"] for span in spans]
    result = {
        "coverage": {"first_span_id": ids[0], "last_span_id": ids[-1]},
        "selected_span_ids": [] if abstain else [ids[0]],
        "qualification_span_ids": [span["id"] for span in spans if "QUALIFICATION" in span["text"]],
        "reading_angle": None
        if abstain
        else {"text": "Read the measured scope before using this result.", "span_ids": [ids[0]]},
        "abstain": abstain,
    }
    return json.dumps(result), {"finish_reason": "STOP"}


def saved_brief_state(tmp_path: Path, cfg: Config, article: Article | None = None) -> BriefState:
    """Persist a caller-owned supported-route fixture; admission belongs to preparation."""
    state = BriefState(Selection.from_article(article or make_article()), _routes(cfg)[0], now(), now())
    save_state(tmp_path, state)
    return state


@pytest.mark.asyncio
async def test_direct_full_body_and_late_qualification_reach_checked_evidence(tmp_path: Path) -> None:
    from digest.reading_preparation import prepare_selected_sources
    from tests.test_main_reading_brief import saved_selection

    execution = ModelExecution()
    cfg, progress, packet, report = await saved_selection(tmp_path, execution=execution)
    identity = report.reviews[0].selections[0].evidence_id
    text = "OPENING CLAIM\n\n" + "\n\n".join(f"Section {i}: " + "context " * 50 for i in range(150))
    text += "\n\nFINAL QUALIFICATION: failed deployments are excluded."
    requests: list[list[dict[str, str]]] = []

    async def generate(_role: Any, messages: list[dict[str, str]], _config: Any, **kwargs: Any) -> Any:
        requests.append(messages)
        assert kwargs["provider_override"].model == "gemini-3.8-flash"
        assert kwargs["max_output_tokens"] == 2048
        raw, usage = response(messages)
        usage.update(prompt_tokens=100, completion_tokens=80, total_tokens=180)
        return raw, usage

    with (
        patch("digest.reading_brief.fetch_article", AsyncMock(return_value=fetched(text))) as fetch,
        patch("digest.llm.count_gemini_tokens", AsyncMock(return_value=150_000)) as count,
        patch("digest.llm.complete", side_effect=generate) as call,
    ):
        prepared = await prepare_selected_sources(
            progress, packet, report, cfg, tmp_path, time.monotonic() + 1000, execution=execution
        )
        assert prepared.technical_complete == 1 and prepared.pending == 0
        state, source = ready_brief_evidence(tmp_path, identity)
        assert state.status == "ready" and len(state.pages) == 1
        assert "".join(span["text"] for span in payload(requests[0])["spans"]) == text
        finished = completed(state.pages[0])
        assert finished is not None
        result = finished.result
        assert result is not None
        assert result.covered_span_ids == tuple(span.id for span in source.spans)
        assert result.qualification_span_ids == (source.spans[-1].id,)
        handoff = json.loads(Path(prepared.handoff_paths[0]).read_text())
        assert handoff["state"] == state_wire(state)
        assert handoff["source_sha256"] == state.source_sha256
        assert handoff["source_body_sha256"] == source.body_sha256
        assert handoff["state"]["pages"][0]["result"]["qualification_span_ids"] == [source.spans[-1].id]
        assert (
            Path(handoff["source_path"]).read_bytes()
            == (state_root(tmp_path) / "sources" / f"{state.source_sha256}.json").read_bytes()
        )
        evidence = build_reconciliation_input(source, state)
        assert evidence.pages[0].qualification_span_ids == (source.spans[-1].id,)
        assert evidence.evidence[-1].text == source.text[source.spans[-1].start : source.spans[-1].end]
        assert "FINAL QUALIFICATION" in evidence.evidence[-1].text
        assert evidence.source_published == "2026-09-20T09:00:00+00:00"
        assert "Uninspected images." in evidence.source_coverage_notes
        assert dict(finished.usage) == {"prompt_tokens": 100, "completion_tokens": 80, "total_tokens": 180}
        state_bytes = (state_root(tmp_path) / f"{identity}.json").read_bytes()
        assert ready_brief_evidence(tmp_path, identity) == (state, source)
        assert (state_root(tmp_path) / f"{identity}.json").read_bytes() == state_bytes
        assert fetch.call_count == count.call_count == call.call_count == 1


@pytest.mark.asyncio
async def test_only_real_exact_overflow_sweeps_every_page_and_resumes_without_dropping(tmp_path: Path) -> None:
    execution = ModelExecution()
    cfg = config()
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

    with (
        patch("digest.source_admission.INPUT_LIMITS", {("gemini", "gemini-3.8-flash"): 121}),
        patch("digest.reading_brief.fetch_article", AsyncMock(return_value=fetched(text))) as fetch,
        patch("digest.llm.count_gemini_tokens", side_effect=count),
        patch("digest.llm.complete", side_effect=generate),
    ):
        state = saved_brief_state(tmp_path, cfg)
        identity = state.selection.identity
        await _advance(state, cfg, tmp_path, time.monotonic() + 1000, execution=execution)
        state = load_state(tmp_path, identity)
        assert state.status == "pending" and state.error_class == "technical_quota_or_budget" and state.attempts == 1
        assert completed(state.pages[0]) is not None
        completed_page = page_wire(state, state.pages[0])
        source_hash = state.source_sha256
        assert generated == [[1, 2], [3, 4]]
        await _advance(state, cfg, tmp_path, time.monotonic() + 1000, execution=execution)
        state, source = ready_brief_evidence(tmp_path, identity)
        assert state.status == "ready" and state.attempts == 2
        assert page_wire(state, state.pages[0]) == completed_page and state.source_sha256 == source_hash
        assert counted[0] == list(range(1, 9))
        assert generated == [[1, 2], [3, 4], [3, 4], [5, 6], [7, 8]]
        assert counted.count([3, 4]) == 1 and fetch.call_count == 1
        covered = [span for page in state.pages if (value := completed(page)) for span in value.result.covered_span_ids]
        assert covered == list(range(1, 9))
        assert "".join(source.text[s.start : s.end] for s in source.spans) == text
        evidence = build_reconciliation_input(source, state)
        assert [span for page in evidence.pages for span in page.qualification_span_ids] == list(range(1, 9))
        assert "".join(span.text for span in evidence.evidence) == text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "damage", ["unknown_id", "partial_coverage", "no_angle_citations", "missing_brief", "bad_type"]
)
async def test_invalid_or_truncated_output_stays_pending_without_repair(tmp_path: Path, damage: str) -> None:
    execution = ModelExecution()
    cfg = config()
    state = saved_brief_state(tmp_path, cfg)

    async def generate(_role: Any, messages: list[dict[str, str]], *_args: Any, **_kwargs: Any) -> Any:
        text, usage = response(messages)
        data = json.loads(text)
        if damage == "unknown_id":
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

    with (
        patch("digest.reading_brief.fetch_article", AsyncMock(return_value=fetched("Claim.\n\nQualification."))),
        patch("digest.llm.count_gemini_tokens", AsyncMock(return_value=100)),
        patch("digest.llm.complete", side_effect=generate) as call,
    ):
        await _advance(state, cfg, tmp_path, time.monotonic() + 1000, execution=execution)
    state = load_state(tmp_path, state.selection.identity)
    assert state.status == "pending" and completed(state.pages[0]) is None and call.call_count == 1
    assert state.pages[0].request_attempts[-1].status == "accepted"
    with pytest.raises(ValueError, match="brief_not_ready"):
        ready_brief_evidence(tmp_path, state.selection.identity)


@pytest.mark.asyncio
async def test_abstention_is_semantic_only_after_every_page_completes(tmp_path: Path) -> None:
    execution = ModelExecution()
    cfg = config()
    generated: list[list[int]] = []

    async def count(messages: list[dict[str, str]], *_args: Any, **_kwargs: Any) -> int:
        return 100 + len(payload(messages)["spans"]) * 10

    async def generate(_role: Any, messages: list[dict[str, str]], *_args: Any, **_kwargs: Any) -> Any:
        generated.append(payload(messages)["span_range"])
        persisted = load_state(tmp_path, state.selection.identity)
        assert persisted.status == "pending"
        return response(messages, abstain=True)

    with (
        patch("digest.source_admission.INPUT_LIMITS", {("gemini", "gemini-3.8-flash"): 115}),
        patch("digest.reading_brief.fetch_article", AsyncMock(return_value=fetched("Routine notice.\n\nNo finding."))),
        patch("digest.llm.count_gemini_tokens", side_effect=count),
        patch("digest.llm.complete", side_effect=generate) as call,
    ):
        state = saved_brief_state(tmp_path, cfg)
        await _advance(state, cfg, tmp_path, time.monotonic() + 1000, execution=execution)
        state = load_state(tmp_path, state.selection.identity)
        _validate_progress(state, load_source(tmp_path, state))
        assert state.status == "abstained" and generated == [[1, 1], [2, 2]]
        assert all((value := completed(page)) is not None and value.result.abstain for page in state.pages)
        with pytest.raises(ValueError, match="brief_not_ready"):
            ready_brief_evidence(tmp_path, state.selection.identity)
    assert call.call_count == 2


@pytest.mark.asyncio
async def test_incomplete_fetch_and_exhausted_budget_never_become_editorial_rejections(tmp_path: Path) -> None:
    execution = ModelExecution()
    cfg = config()
    state = saved_brief_state(tmp_path, cfg)
    llm.set_request_limit(cfg, execution, 0)
    with (
        patch("digest.reading_brief.fetch_article", AsyncMock(side_effect=ValueError("coverage_incomplete"))),
        patch("digest.llm.count_gemini_tokens", AsyncMock()) as count,
    ):
        await _advance(state, cfg, tmp_path, time.monotonic() + 1000, execution=execution)
    state = load_state(tmp_path, state.selection.identity)
    assert state.status == "pending" and state.error_class == "technical_fetch_incomplete"
    assert state.source_sha256 is None and count.call_count == 0
    with (
        patch("digest.reading_brief.fetch_article", AsyncMock(return_value=fetched("Complete article text."))),
        patch("digest.llm.count_gemini_tokens", AsyncMock()) as count,
    ):
        await _advance(state, cfg, tmp_path, time.monotonic() + 1000, execution=execution)
    state = load_state(tmp_path, state.selection.identity)
    assert state.status == "pending" and state.error_class == "technical_quota_or_budget"
    assert state.source_sha256 is not None and completed(state.pages[0]) is None and count.call_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("remaining,expected_calls", [(60.0, 1), (34.0, 0)])
async def test_deadline_admits_a_clipped_useful_window_after_pacing(
    tmp_path: Path,
    remaining: float,
    expected_calls: int,
) -> None:
    execution = ModelExecution()
    cfg = config()
    state = saved_brief_state(tmp_path, cfg)

    async def generate_response(_role: Any, messages: list[dict[str, str]], *_args: Any, **kwargs: Any) -> Any:
        assert 30 <= kwargs["request_timeout_seconds"] <= remaining - 5 - 0.25
        assert kwargs["request_timeout_seconds"] < 120
        return response(messages)

    with (
        patch("digest.reading_brief.fetch_article", AsyncMock(return_value=fetched("Complete article text."))),
        patch("digest.llm.request_wait_seconds", return_value=5),
        patch("digest.llm.count_gemini_tokens", AsyncMock(return_value=100)) as count,
        patch("digest.llm.complete", side_effect=generate_response) as generate,
    ):
        await _advance(state, cfg, tmp_path, time.monotonic() + remaining, execution=execution)
    state = load_state(tmp_path, state.selection.identity)
    assert state.status == ("ready" if expected_calls else "pending")
    assert count.call_count == 1 and generate.call_count == expected_calls


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["result_id", "angle", "ready_before_coverage", "route", "source"])
async def test_completed_state_cannot_validate_after_evidence_or_result_tampering(tmp_path: Path, damage: str) -> None:
    execution = ModelExecution()
    cfg = config()
    state = saved_brief_state(tmp_path, cfg)
    identity = state.selection.identity

    async def generate(_role: Any, messages: list[dict[str, str]], *_args: Any, **_kwargs: Any) -> Any:
        return response(messages)

    with (
        patch("digest.reading_brief.fetch_article", AsyncMock(return_value=fetched("First source.\n\nSecond source."))),
        patch("digest.llm.count_gemini_tokens", AsyncMock(return_value=100)),
        patch("digest.llm.complete", side_effect=generate) as call,
    ):
        await _advance(state, cfg, tmp_path, time.monotonic() + 1000, execution=execution)
        ready_brief_evidence(tmp_path, identity)
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
        saved_bytes = {p: p.read_bytes() for p in state_root(tmp_path).rglob("*.json")}
        with pytest.raises(ValueError):
            ready_brief_evidence(tmp_path, identity)
        assert saved_bytes == {p: p.read_bytes() for p in state_root(tmp_path).rglob("*.json")}
        assert call.call_count == 1


@pytest.mark.asyncio
async def test_unknown_profile_holds_admitted_selection_without_fetch_or_fallback(tmp_path: Path) -> None:
    from digest.reading_preparation import prepare_selected_sources
    from tests.test_main_reading_brief import saved_selection

    execution = ModelExecution()
    cfg, progress, packet, report = await saved_selection(tmp_path, execution=execution)
    cfg.reading_brief = replace(cfg.reading_brief, model="unprofiled-model")
    with (
        patch("digest.reading_brief.fetch_article", AsyncMock()) as fetch,
        patch("digest.llm.count_gemini_tokens", AsyncMock()) as count,
        patch("digest.llm.complete", AsyncMock()) as generate,
    ):
        result = await prepare_selected_sources(
            progress, packet, report, cfg, tmp_path, time.monotonic() + 1000, execution=execution
        )
    assert result.pending == 1 and result.technical_complete == 0
    assert fetch.call_count == count.call_count == generate.call_count == 0
    state = load_state(tmp_path, result.outcomes[0].identity)
    assert state.status == "pending" and state.source_sha256 is None


@pytest.mark.asyncio
async def test_rss_description_does_not_enter_reader_prompt(tmp_path: Path) -> None:
    execution = ModelExecution()
    cfg = config()
    original = make_article(description="UNVERIFIED RSS ANNOUNCEMENT", category="Original category")
    state = saved_brief_state(tmp_path, cfg, original)
    messages_seen = []

    async def generate(_role: Any, messages: list[dict[str, str]], *_args: Any, **_kwargs: Any) -> Any:
        messages_seen.append(messages)
        return response(messages)

    with (
        patch("digest.reading_brief.fetch_article", AsyncMock(return_value=fetched("Complete actual source."))),
        patch("digest.llm.count_gemini_tokens", AsyncMock(return_value=100)),
        patch("digest.llm.complete", side_effect=generate) as call,
    ):
        await _advance(state, cfg, tmp_path, time.monotonic() + 1000, execution=execution)
    state, _ = ready_brief_evidence(tmp_path, state.selection.identity)
    assert state.selection.article() == original and call.call_count == 1
    assert "description" not in payload(messages_seen[0])["article"]
    assert "UNVERIFIED RSS ANNOUNCEMENT" not in json.dumps(messages_seen)


@pytest.mark.asyncio
async def test_fetch_transport_failure_is_persisted_as_technical_pending(tmp_path: Path) -> None:
    import httpx

    execution = ModelExecution()
    cfg = config()
    state = saved_brief_state(tmp_path, cfg)
    with patch("digest.reading_brief.fetch_article", AsyncMock(side_effect=httpx.ConnectError("offline"))):
        await _advance(state, cfg, tmp_path, time.monotonic() + 1000, execution=execution)
    state = load_state(tmp_path, state.selection.identity)
    assert state.status == "pending" and state.error_class == "technical_fetch_failed"
    assert state.source_sha256 is None


@pytest.mark.asyncio
async def test_substantive_brief_retains_conditions_and_unresolved_conflict_in_source_evidence(
    tmp_path: Path,
) -> None:
    execution = ModelExecution()
    cfg = config()
    state = saved_brief_state(tmp_path, cfg)
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
        return json.dumps(
            {
                "coverage": {"first_span_id": 1, "last_span_id": 4},
                "selected_span_ids": [1],
                "qualification_span_ids": [2, 3, 4],
                "reading_angle": {"text": brief, "span_ids": [1, 2, 3, 4]},
                "abstain": False,
            }
        ), {"finish_reason": "STOP"}

    with (
        patch("digest.reading_brief.fetch_article", AsyncMock(return_value=fetched(text))),
        patch("digest.llm.count_gemini_tokens", AsyncMock(return_value=100)) as count,
        patch("digest.llm.complete", side_effect=generate) as call,
    ):
        await _advance(state, cfg, tmp_path, time.monotonic() + 1000, execution=execution)
    state, source = ready_brief_evidence(tmp_path, state.selection.identity)
    evidence = build_reconciliation_input(source, state)
    assert evidence.pages[0].reading_angle == brief
    assert evidence.pages[0].angle_span_ids == (1, 2, 3, 4)
    assert evidence.pages[0].qualification_span_ids == (2, 3, 4)
    assert "".join(span.text for span in evidence.evidence) == text
    assert count.call_count == call.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("previous_version", ["source-passages-v1", "source-passages-v2", "source-passages-v3"])
async def test_cached_previous_brief_cannot_be_reused_or_silently_rewritten(
    tmp_path: Path,
    previous_version: str,
) -> None:
    execution = ModelExecution()
    cfg = config()
    state = saved_brief_state(tmp_path, cfg)
    identity = state.selection.identity

    async def generate(_role: Any, messages: list[dict[str, str]], *_args: Any, **_kwargs: Any) -> Any:
        return response(messages)

    with (
        patch("digest.reading_brief.fetch_article", AsyncMock(return_value=fetched("Complete public article."))),
        patch("digest.llm.count_gemini_tokens", AsyncMock(return_value=100)),
        patch("digest.llm.complete", side_effect=generate) as call,
    ):
        await _advance(state, cfg, tmp_path, time.monotonic() + 1000, execution=execution)
        ready_brief_evidence(tmp_path, identity)
        path = state_root(tmp_path) / f"{identity}.json"
        envelope = json.loads(path.read_text())
        assert envelope["payload"]["route"]["prompt_version"] == "source-passages-v4"
        envelope["payload"]["route"]["prompt_version"] = previous_version
        envelope["sha256"] = checksum(envelope["payload"])
        legacy_bytes = json.dumps(envelope).encode()
        path.write_bytes(legacy_bytes)
        with pytest.raises(ValueError):
            ready_brief_evidence(tmp_path, identity)
    assert call.call_count == 1 and path.read_bytes() == legacy_bytes


@pytest.mark.asyncio
async def test_abstaining_later_page_qualification_remains_literal_in_retained_evidence(tmp_path: Path) -> None:
    execution = ModelExecution()
    cfg = config()
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
        return json.dumps(
            {
                "coverage": {"first_span_id": ids[0], "last_span_id": ids[-1]},
                "selected_span_ids": [] if is_qualification else [1],
                "qualification_span_ids": [2] if is_qualification else [],
                "reading_angle": None if is_qualification else {"text": claim, "span_ids": [1]},
                "abstain": is_qualification,
            }
        ), {"finish_reason": "STOP"}

    with (
        patch("digest.source_admission.INPUT_LIMITS", {("gemini", "gemini-3.8-flash"): 115}),
        patch("digest.reading_brief.fetch_article", AsyncMock(return_value=fetched(text))),
        patch("digest.llm.count_gemini_tokens", side_effect=count),
        patch("digest.llm.complete", side_effect=generate),
    ):
        state = saved_brief_state(tmp_path, cfg)
        await _advance(state, cfg, tmp_path, time.monotonic() + 1000, execution=execution)
    state, source = ready_brief_evidence(tmp_path, state.selection.identity)
    assert generated_pages == [[1], [2]] and state.status == "ready"
    finished = completed(state.pages[1])
    assert finished is not None and finished.result.abstain is True
    evidence = build_reconciliation_input(source, state)
    assert evidence.pages[1].abstain and evidence.pages[1].qualification_span_ids == (2,)
    assert evidence.pages[1].route == state.route
    assert evidence.pages[1].request_sha256 == finished.request_sha256
    assert evidence.evidence[1].id == 2 and evidence.evidence[1].text == condition
    assert "".join(span.text for span in evidence.evidence) == text


@pytest.mark.asyncio
async def test_historical_delivered_record_remains_readable_without_writes(tmp_path: Path) -> None:
    execution = ModelExecution()
    cfg = config()
    state = saved_brief_state(tmp_path, cfg)
    identity = state.selection.identity

    async def generate(_role: Any, messages: list[dict[str, str]], *_args: Any, **_kwargs: Any) -> Any:
        return response(messages)

    with (
        patch("digest.reading_brief.fetch_article", AsyncMock(return_value=fetched("Retained original source."))),
        patch("digest.llm.count_gemini_tokens", AsyncMock(return_value=100)),
        patch("digest.llm.complete", side_effect=generate),
    ):
        await _advance(state, cfg, tmp_path, time.monotonic() + 1000, execution=execution)
    state_path = state_root(tmp_path) / f"{identity}.json"
    envelope = json.loads(state_path.read_text())
    envelope["payload"].update(status="delivered", delivered_at="2026-10-02T13:00:00+00:00")
    envelope["sha256"] = checksum(envelope["payload"])
    state_path.write_text(json.dumps(envelope))
    original = {path: path.read_bytes() for path in state_root(tmp_path).rglob("*.json")}
    with (
        patch("digest.reading_brief.save_state", side_effect=AssertionError("No delivery-state writes")),
        patch("digest.llm.complete", side_effect=AssertionError("No repeated generation")),
        patch("digest.llm.count_gemini_tokens", side_effect=AssertionError("No repeated count")),
        patch("digest.reading_brief.fetch_article", side_effect=AssertionError("No repeated fetch")),
    ):
        restored = load_state(tmp_path, identity)
        checked, source = ready_brief_evidence(tmp_path, identity)
        evidence = build_reconciliation_input(source, checked)
    assert checked == restored and restored.status == "delivered"
    assert restored.delivered_at == "2026-10-02T13:00:00+00:00"
    assert evidence.source_sha256 == restored.source_sha256 and evidence.evidence[0].text == source.text
    assert original == {path: path.read_bytes() for path in state_root(tmp_path).rglob("*.json")}
