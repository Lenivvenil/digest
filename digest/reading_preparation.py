"""Candidate-bound technical source work before accepted presentation.

This is an admission/handoff adapter, not a second scheduler or editorial gate.
Completed pages remain evidence for #55; they never authorize a publication here.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Literal

from digest._serialization import canonical_json_bytes as _canonical
from digest._serialization import restore_dataclass as _restore
from digest._util import atomic_json_write
from digest.adapters.storage.checkpoints import safe_checkpoint_path as _safe
from digest.candidate_review import (
    Candidate,
    CandidateArticle,
    CandidatePacket,
    CandidateProgress,
)
from digest.config import Config
from digest.domain.editorial.candidates import accepted_empty_packet as _accepted_empty_packet
from digest.domain.editorial.candidates import proof_packets as _proof_packets
from digest.domain.editorial.reviews import BlindReviewReport, delivery_review, validated_cached_selections
from digest.llm import request_budget_remaining, set_request_limit
from digest.reading_brief import _advance, _count_routes_held, _routes, _validate_progress
from digest.reading_brief_state import (
    BriefState,
    Route,
    Selection,
    has_unresolved_generation,
    load_source,
    load_state,
    now,
    save_state,
    state_root,
)


@dataclass(frozen=True)
class ReadingBinding:
    identity: str
    occurrence_sha256: str
    source_url: str
    report_sha256: str
    bundle_id: str
    prompt_sha256: str
    response_sha256: str
    evidence_origin: Literal[
        "current_selection_binding", "legacy_selection_match_historical_feed_binding_unknown",
    ] = "current_selection_binding"


@dataclass(frozen=True)
class ReadingOutcome:
    identity: str
    state: str
    reason: str
    handoff: str | None = None


@dataclass(frozen=True)
class ReadingPreparationResult:
    selected: int
    technical_complete: int
    pending: int
    oldest_pending: str | None
    status: str
    handoff_paths: tuple[str, ...]
    request_budget_remaining: int | None
    request_reservations: int
    elapsed_seconds: float
    account_quota: str
    outcomes: tuple[ReadingOutcome, ...]


def _hash(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def reading_deadline(config: Config, started: float) -> float:
    reserve = config.translation.timeout_seconds if config.translation.enabled else 0.0
    deadline = started + 360.0 - reserve - 45.0
    if managed := os.environ.get("PREPARATION_DEADLINE"):
        if not managed.isdecimal() or len(managed) > 20:
            raise ValueError("Managed preparation deadline must be a Unix timestamp.")
        deadline = min(deadline, time.monotonic() + int(managed) - time.time() - reserve - 45.0)
    return deadline


def validate_reading_mode(config: Config, prepare_only: bool) -> None:
    if getattr(getattr(config, "reading_brief", None), "enabled", False) and not prepare_only:
        raise ValueError("Source reading requires candidate-bound --prepare-edition mode.")


def setup_reading_budget(config: Config) -> None:
    if getattr(getattr(config, "reading_brief", None), "enabled", False):
        config.llm.max_retries = 0
        set_request_limit(config, min(config.reading_brief.max_requests_per_run, 10))


def _binding(packet: CandidatePacket, report: BlindReviewReport, identity: str) -> ReadingBinding:
    review = delivery_review(report)
    if not review.response_sha256:
        raise ValueError("technical_selection_proof_missing")
    from digest.radar.collector import article_hash

    article = next(item for item in packet.articles if article_hash(item.title, item.link) == identity)
    return ReadingBinding(identity, _hash(asdict(article)), article.source_url, _hash(asdict(report)),
                          report.evidence.bundle_id, review.prompt_hash, review.response_sha256)


def _bound_state(
    binding: ReadingBinding, selection: Selection, route: Route, state_dir: Path,
) -> tuple[BriefState, ReadingBinding]:
    root = _safe(state_dir / "reading_bindings")
    root.mkdir(parents=True, exist_ok=True)
    path = _safe(root / f"{binding.identity}.json")
    state_path = state_root(state_dir) / f"{binding.identity}.json"
    previous: ReadingBinding | None = None
    if path.exists():
        raw = json.loads(path.read_text())
        if raw.get("sha256") != _hash(raw.get("binding")):
            raise ValueError("Reading selection binding checksum mismatch.")
        previous = _restore(raw["binding"], ReadingBinding)
        if replace(previous, evidence_origin="current_selection_binding") == binding:
            binding = previous
    if state_path.exists():
        state = load_state(state_dir, binding.identity)
        if previous is not None:
            original = CandidateArticle(state.selection.title, state.selection.link, state.selection.description,
                                        state.selection.source, state.selection.category, state.selection.pub_date,
                                        previous.source_url)
            if _hash(asdict(original)) != previous.occurrence_sha256:
                raise ValueError("Reading state and prior selection binding require integrity recovery.")
        if state.source_sha256 is not None:
            _validate_progress(state, load_source(state_dir, state))
        if previous != binding:
            # A new metadata proof must never reset an uncertain generation.
            if has_unresolved_generation(state):
                raise ValueError("technical_generation_unknown")
            archived = _safe(root / "revisions")
            archived.mkdir(exist_ok=True)
            old = {"binding": asdict(previous) if previous else None, "state": asdict(state)}
            target = _safe(archived / f"{_hash(old)}.json")
            if not target.exists():
                atomic_json_write(target, old)
            if state.selection != selection:
                state = BriefState(selection, route, now(), now())
                save_state(state_dir, state)
            elif previous is None and state.source_sha256 is not None:
                binding = replace(binding, evidence_origin="legacy_selection_match_historical_feed_binding_unknown")
        elif state.selection != selection:
            raise ValueError("Reading selection differs from its bound occurrence.")
    else:
        if previous is not None:
            raise ValueError("technical_missing_reading_state")
        state = BriefState(selection, route, now(), now())
        save_state(state_dir, state)
    atomic_json_write(path, {"binding": asdict(binding), "sha256": _hash(asdict(binding))})
    return state, binding


def _handoff_body(binding: ReadingBinding, state: BriefState, state_dir: Path) -> dict[str, object]:
    source = load_source(state_dir, state)
    _validate_progress(state, source)
    return {"schema_version": 1, "status": "technical_complete_semantic_review_pending",
            "binding": asdict(binding), "state": asdict(state), "source_sha256": state.source_sha256,
            "source_body_sha256": source.body_sha256,
            "source_path": str(state_root(state_dir) / "sources" / f"{state.source_sha256}.json")}


def _freeze_handoff(binding: ReadingBinding, state: BriefState, state_dir: Path) -> Path:
    body = _handoff_body(binding, state, state_dir)
    root = _safe(state_dir / "reading_handoffs")
    root.mkdir(exist_ok=True)
    path = _safe(root / f"{_hash(body)}.json")
    if path.exists():
        if json.loads(path.read_text()) != body:
            raise ValueError("Immutable reading handoff differs from its retained evidence.")
    else:
        atomic_json_write(path, body)
    return path


def _currently_selected(
    candidate: Candidate, progress: CandidateProgress, packet: CandidatePacket, report: BlindReviewReport,
) -> bool:
    if (candidate.status != "selected" or not candidate.eligible
            or candidate.delivery_cache_observed_at is not None or packet not in progress.packets):
        return False
    proof = _proof_packets(candidate, progress.packets)
    return bool(proof and proof[0].report == report and proof[0].articles == packet.articles)


def _deferred_source(
    candidate: Candidate, packet: CandidatePacket, report: BlindReviewReport, state_dir: Path, config: Config,
) -> bool:
    path = _safe(state_dir / "reading_bindings" / f"{candidate.identity}.json")
    state_path = _safe(state_dir / "reading_briefs" / f"{candidate.identity}.json")
    if not path.is_file() or not state_path.is_file():
        return False
    raw = json.loads(path.read_text())
    if not isinstance(raw, dict) or raw.get("sha256") != _hash(raw.get("binding")):
        return False
    actual: ReadingBinding = _restore(raw["binding"], ReadingBinding)
    state = load_state(state_dir, candidate.identity)
    old_occurrence = CandidateArticle(state.selection.title, state.selection.link, state.selection.description,
                                     state.selection.source, state.selection.category, state.selection.pub_date,
                                     actual.source_url)
    if actual.identity != candidate.identity or _hash(asdict(old_occurrence)) != actual.occurrence_sha256:
        return False
    source = load_source(state_dir, state)
    _validate_progress(state, source)
    if has_unresolved_generation(state):
        return True  # Retain the original unknown binding, including across a new RSS occurrence.
    expected = _binding(packet, report, candidate.identity)
    if (replace(actual, evidence_origin="current_selection_binding") != expected
            or state.selection != Selection.from_article(candidate.article.article())):
        return False
    if _count_routes_held(state, source, _routes(config)):
        return True
    if state.status not in {"ready", "abstained"}:
        return False
    body = _handoff_body(actual, state, state_dir)
    handoff = _safe(state_dir / "reading_handoffs" / f"{_hash(body)}.json")
    return handoff.is_file() and json.loads(handoff.read_text()) == body


def deferred_source_reports(progress: CandidateProgress, state_dir: Path, config: Config) -> set[str]:
    """Skip verified technical handoffs and explicitly non-resumable current-route holds."""
    deferred: set[str] = set()
    for packet in progress.packets:
        report = packet.report
        if report is None or not report.reviews:
            continue
        try:
            selections, _ = validated_cached_selections(delivery_review(report), packet.evidence)
            if not selections:
                if _accepted_empty_packet(packet):
                    deferred.add(_hash(asdict(report)))
                continue
            for selected in selections:
                candidate = progress.candidates.get(selected.evidence_id)
                if (candidate is None or not _currently_selected(candidate, progress, packet, report)
                        or _binding(packet, report, selected.evidence_id).occurrence_sha256
                        != _hash(asdict(candidate.article))
                        or not _deferred_source(candidate, packet, report, state_dir, config)):
                    break
            else:
                deferred.add(_hash(asdict(report)))
        except (OSError, ValueError, TypeError, KeyError):
            continue  # Missing/corrupt evidence is never silently promoted to a completed handoff.
    return deferred


async def prepare_selected_sources(
    progress: CandidateProgress | None, packet: CandidatePacket | None, report: BlindReviewReport | None,
    config: Config, state_dir: Path, deadline: float, *, prepare_only: bool = True,
) -> ReadingPreparationResult:
    if not prepare_only or progress is None or packet is None or report is None:
        raise ValueError("Source reading requires candidate-bound --prepare-edition mode.")
    if packet.report != report or packet.evidence != report.evidence:
        raise ValueError("Reading requires the exact saved candidate report.")
    started = time.monotonic()
    review = delivery_review(report)
    selections, _ = validated_cached_selections(review, packet.evidence)
    routes = _routes(config)
    route = routes[0] if routes else Route(config.reading_brief.provider, config.reading_brief.model,
                                         1, config.reading_brief.max_output_tokens)
    complete: list[str] = []
    pending_dates: list[str] = []
    outcomes: list[ReadingOutcome] = []
    for selected in selections:
        candidate = progress.candidates.get(selected.evidence_id)
        if candidate is None or not _currently_selected(candidate, progress, packet, report):
            pending_dates.append(candidate.first_observed_at if candidate else packet.planned_at)
            outcomes.append(ReadingOutcome(selected.evidence_id, "held", "current_occurrence_ineligible"))
            continue
        try:
            binding = _binding(packet, report, selected.evidence_id)
            if binding.occurrence_sha256 != _hash(asdict(candidate.article)):
                pending_dates.append(candidate.first_observed_at)
                outcomes.append(ReadingOutcome(selected.evidence_id, "held", "selected_occurrence_changed"))
                continue
            state, binding = _bound_state(
                binding, Selection.from_article(candidate.article.article()), route, state_dir)
            if routes and state.status == "pending" and time.monotonic() < deadline:
                await _advance(state, config, state_dir, deadline)
            if state.status in {"ready", "abstained"}:
                handoff = str(_freeze_handoff(binding, state, state_dir))
                complete.append(handoff)
                outcomes.append(ReadingOutcome(selected.evidence_id, "technical_complete",
                                               "semantic_reconciliation_pending", handoff))
            else:
                pending_dates.append(state.created_at)
                outcomes.append(ReadingOutcome(selected.evidence_id, "pending",
                                               state.error_class or "technical_profile_or_deadline"))
        except (OSError, ValueError, TypeError, KeyError) as exc:
            pending_dates.append(candidate.first_observed_at)
            known = {"technical_selection_proof_missing", "technical_generation_unknown",
                     "technical_missing_reading_state"}
            reason = str(exc) if str(exc) in known else (
                "technical_persistence" if isinstance(exc, OSError) else "technical_evidence_integrity")
            outcomes.append(ReadingOutcome(selected.evidence_id, "held", reason))
    from digest.llm import _request_state

    result = ReadingPreparationResult(len(selections), len(complete), len(pending_dates),
                                    min(pending_dates) if pending_dates else None,
                                    "semantic_reconciliation_pending" if complete else "technical_pending",
                                    tuple(complete), request_budget_remaining(config),
                                    _request_state(config).requests_attempted, time.monotonic() - started,
                                    "unknown", tuple(outcomes))
    atomic_json_write(_safe(state_dir / "reading_preparation.json"),
                      {"report_sha256": _hash(asdict(report)), "result": asdict(result)})
    return result
