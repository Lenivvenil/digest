"""Offline input binding for a future whole-article reconciliation operation.

Complete page reading is technical provenance, not semantic completeness. The
reconciliation evidence is explicitly sparse and cannot certify omitted context.
Nothing here calls a model, admits dispatch, or creates accepted publication work.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, replace
from typing import Literal

import httpx

from digest import source_admission
from digest.reading_brief import _validate_progress
from digest.reading_brief_state import (
    BriefState,
    Page,
    Route,
    Selection,
    Source,
    _validate_source,
    _validate_state,
    checksum,
    has_unresolved_generation,
)
from digest.reading_brief_tokens import TokenProfileUnavailable

INPUT_VERSION = "article-reconciliation-input-v1"


@dataclass(frozen=True)
class EvidenceSpan:
    id: int
    start: int
    end: int
    text: str


@dataclass(frozen=True)
class CompletedPage:
    """Original page indices, supplying route, proof hashes and retained findings.

    The page hash covers its entire saved record, including request history. Old
    completed responses remain distinguishable from modern accepted-attempt proof.
    """

    start: int
    stop: int
    page_sha256: str
    route: Route
    request_sha256: str
    response_sha256: str
    history_evidence: Literal["accepted_attempt", "legacy_completed_response"]
    exact_count: int | None
    admission: tuple[tuple[str, int | str], ...]
    selected_span_ids: tuple[int, ...]
    qualification_span_ids: tuple[int, ...]
    reading_angle: str | None
    angle_span_ids: tuple[int, ...]
    abstain: bool


@dataclass(frozen=True)
class SparseCoverage:
    included_span_ids: tuple[int, ...]
    omitted_span_ids: tuple[int, ...]
    included_characters: int
    kind: Literal["selected_evidence"] = "selected_evidence"


@dataclass(frozen=True)
class ReconciliationInput:
    version: str
    source_sha256: str
    body_sha256: str
    selection: Selection
    final_url: str
    fetched_at: str
    source_published: str | None
    extraction_status: str
    source_coverage_notes: tuple[str, ...]
    source_characters: int
    source_span_count: int
    pages: tuple[CompletedPage, ...]
    evidence: tuple[EvidenceSpan, ...]
    sparse_coverage: SparseCoverage
    extra_context_span_ids: tuple[int, ...]
    input_sha256: str
    technical_page_coverage: Literal["complete"] = "complete"
    semantic_completeness: Literal["unverified"] = "unverified"


@dataclass(frozen=True)
class LocalRequestPlan:
    """Prospective wire binding only, with no quota/deadline/dispatch approval."""

    input_sha256: str
    provider: str
    model: str
    temperature: float
    output_reserve: int
    input_limit: int
    request_sha256: str
    wire_json: str
    status: Literal["unverified", "estimated_fit", "oversized"]
    accounting: tuple[tuple[str, int | str], ...] = ()
    error_class: str | None = None
    remote_count_performed: Literal[False] = False


def _completed_page(state: BriefState, page: Page) -> CompletedPage:
    result = page.result
    assert result is not None  # Validated before copying into immutable tuples.
    route = page.route or state.route
    legacy = page.request_history_version == 0 and not page.request_attempts
    return CompletedPage(
        page.start,
        page.stop,
        checksum(asdict(page)),
        route,
        page.prompt_sha256,
        page.response_sha256,
        "legacy_completed_response" if legacy else "accepted_attempt",
        state.exact_counts.get(page.prompt_sha256) if route.provider == "gemini" else None,
        tuple(sorted(state.admissions.get(page.prompt_sha256, {}).items())) if route.provider == "groq" else (),
        tuple(result.selected_span_ids),
        tuple(result.qualification_span_ids),
        result.reading_angle,
        tuple(result.angle_span_ids),
        result.abstain,
    )


def build_reconciliation_input(
    source: Source,
    state: BriefState,
    *,
    extra_context_span_ids: tuple[int, ...] = (),
) -> ReconciliationInput:
    """Bind all completed findings and nominated conditions to original offsets.

    Additional context may expand the union; callers cannot replace or narrow it.
    Every original page must have checked admission and response evidence, including
    abstaining pages. Sparse context still needs inspection against the full source.
    """
    _validate_source(source)
    _validate_state(state)
    if source.selection != state.selection or state.source_sha256 != checksum(asdict(source)):
        raise ValueError("reconciliation_source_mismatch")
    if has_unresolved_generation(state):
        raise ValueError("reconciliation_generation_unresolved")
    _validate_progress(state, source)
    if state.status not in {"ready", "abstained", "delivered"} or any(page.result is None for page in state.pages):
        raise ValueError("reconciliation_pages_incomplete")
    known = {span.id for span in source.spans}
    if any(type(span_id) is not int or span_id not in known for span_id in extra_context_span_ids) or len(
        set(extra_context_span_ids)
    ) != len(extra_context_span_ids):
        raise ValueError("invalid_reconciliation_context_ids")
    pages = tuple(_completed_page(state, page) for page in state.pages)
    required = set(extra_context_span_ids)
    for page in pages:
        required.update(page.selected_span_ids)
        required.update(page.angle_span_ids)
        required.update(page.qualification_span_ids)
    evidence = tuple(
        EvidenceSpan(span.id, span.start, span.end, source.text[span.start : span.end])
        for span in source.spans
        if span.id in required
    )
    coverage = SparseCoverage(
        tuple(span.id for span in evidence),
        tuple(span.id for span in source.spans if span.id not in required),
        sum(span.end - span.start for span in evidence),
    )
    result = ReconciliationInput(
        INPUT_VERSION,
        checksum(asdict(source)),
        source.body_sha256,
        source.selection,
        source.final_url,
        source.fetched_at,
        source.source_published,
        source.extraction_status,
        tuple(source.coverage_notes),
        len(source.text),
        len(source.spans),
        pages,
        evidence,
        coverage,
        tuple(sorted(extra_context_span_ids)),
        "",
    )
    return replace(result, input_sha256=checksum(asdict(result)))


def verify_reconciliation_input(value: ReconciliationInput, source: Source, state: BriefState) -> None:
    """Recompute the exact binding; checksums establish identity, not truth."""
    expected = build_reconciliation_input(source, state, extra_context_span_ids=value.extra_context_span_ids)
    if value != expected or checksum(asdict(value)) != checksum(asdict(expected)):
        raise ValueError("reconciliation_input_mismatch")


def plan_reconciliation_request(
    value: ReconciliationInput,
    source: Source,
    state: BriefState,
    *,
    instruction: str,
    provider: str,
    model: str,
    max_output_tokens: int,
    temperature: float = 0.1,
) -> LocalRequestPlan:
    """Serialize the complete sparse envelope for one explicit prospective route.

    The caller supplies the proposed instruction; this helper chooses no editorial
    algorithm. Gemini's matching remote count remains unperformed/unverified.
    Groq may be estimated locally with the existing pinned method. A local fit is
    not permission to dispatch: actual shared allowance, deadline, admission and
    quota must still be checked by a future separately authorized integration.
    Oversized evidence is retained intact, never truncated or page-reinterpreted.
    """
    verify_reconciliation_input(value, source, state)
    if (
        not isinstance(instruction, str)
        or not instruction.strip()
        or type(max_output_tokens) is not int
        or max_output_tokens <= 0
        or not isinstance(temperature, (int, float))
        or isinstance(temperature, bool)
        or not math.isfinite(temperature)
    ):
        raise ValueError("invalid_reconciliation_request")
    route = source_admission.route_profile(provider, model, max_output_tokens)
    if route is None:
        raise ValueError("unknown_reconciliation_profile")
    messages = [
        {"role": "system", "content": instruction},
        {"role": "user", "content": json.dumps(asdict(value), ensure_ascii=False, sort_keys=True)},
    ]
    wire = source_admission.wire_request(route, messages, temperature)
    result = LocalRequestPlan(
        value.input_sha256,
        provider,
        model,
        temperature,
        max_output_tokens,
        route.input_tokens,
        source_admission.request_sha256(route, messages, temperature),
        httpx.Request("POST", "https://request.invalid", json=wire).content.decode(),
        "unverified",
        error_class="technical_remote_count_unperformed",
    )
    if provider == "gemini":
        return result
    try:
        record = source_admission.estimate_request(route, messages)
    except TokenProfileUnavailable:
        return replace(result, error_class="technical_tokenizer_profile")
    oversized = int(record["input_estimate"]) > route.input_tokens
    return replace(
        result,
        status="oversized" if oversized else "estimated_fit",
        accounting=tuple(sorted(record.items())),
        error_class="technical_admission_capacity" if oversized else None,
    )
