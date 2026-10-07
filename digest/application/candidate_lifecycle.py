"""Verified candidate retirement and checkpoint application operations."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from digest._util import utc_instant as _instant
from digest.adapters.storage import candidate_objects as storage
from digest.adapters.storage.candidate_progress import MAX_BYTES, materialize_progress, progress_size, write_progress
from digest.domain.editorial.candidates import Candidate, CandidateProgress, accepted_empty_packet, proof_packets
from digest.domain.editorial.reviews import BlindReviewReport, delivery_review


def index_candidate(candidate: Candidate, progress: CandidateProgress, cache_dir: str | Path) -> None:
    keys = []
    handoffs = {}
    for packet in proof_packets(candidate, progress.packets):
        storage.freeze_packet(packet, {}, cache_dir)
        key = storage.packet_key(packet)
        keys.append(key)
        handoffs[key] = packet.handed_to_preparation
    storage.save_candidate(candidate, tuple(keys), cache_dir, report_handoffs=handoffs)


def retire_indexed_work(
    progress: CandidateProgress,
    cache_dir: str | Path,
    skipped_empty_reports: set[str] | None = None,
) -> None:
    """Verified index first, active removal last; excluded work is not completed work."""

    retiring = []
    for identity, candidate in progress.candidates.items():
        proof = proof_packets(candidate, progress.packets)
        unconsumed = any(
            packet.report is not None
            and accepted_empty_packet(packet)
            and not packet.handed_to_preparation
            and storage.digest(asdict(packet.report)) not in (skipped_empty_reports or set())
            for packet in proof
        )
        if candidate.eligible and (candidate.status not in {"not_selected", "duplicate"} or unconsumed):
            continue
        index_candidate(candidate, progress, cache_dir)
        retiring.append(identity)
    for identity in retiring:
        del progress.candidates[identity]
    required = {
        storage.packet_key(packet)
        for candidate in progress.candidates.values()
        for packet in proof_packets(candidate, progress.packets)
    }
    retained = []
    for packet in progress.packets:
        if storage.packet_key(packet) in required:
            retained.append(packet)
        else:
            storage.freeze_packet(packet, {}, cache_dir)
    progress.packets = retained


def ensure_report_accounting(
    progress: CandidateProgress,
    report: BlindReviewReport,
    cache_dir: str | Path = ".cache",
) -> Path:
    """Freeze only this saved report's packet; current counts are bounded as-of data."""

    packet = next((item for item in reversed(progress.packets) if item.report == report), None)
    if packet is None:
        raise ValueError("Candidate accounting cannot freeze an unrelated report.")
    instant = _instant(None)
    counts = {
        status: sum(candidate.status == status for candidate in progress.candidates.values())
        for status in (
            "selected",
            "not_selected",
            "duplicate",
            "not_selected_without_editorial_reason",
            "not_presented",
            "technical_pending",
        )
    }
    unfinished = [
        datetime.fromisoformat(candidate.first_observed_at)
        for candidate in progress.candidates.values()
        if candidate.eligible and candidate.status in {"not_presented", "technical_pending"}
    ]
    inventory = json.loads(packet.collection_json)
    serialized_bytes = progress_size(progress, cache_dir)
    delivery = delivery_review(report)
    attempt = next((item for item in packet.disposition_attempts if item.slot == delivery.slot), None)
    packet_ids = {item.evidence_id for item in packet.evidence.items}
    summary = {
        "accounted_at": instant.isoformat(),
        "serialized_bytes": serialized_bytes,
        "remaining_capacity_bytes": MAX_BYTES - serialized_bytes,
        "capacity_bytes": MAX_BYTES,
        "capacity_scope": "current active working set; immutable source and packet objects are stored separately",
        "packet_response_coverage_complete": bool(
            attempt and not attempt.errors and {item.evidence_id for item in attempt.dispositions} == packet_ids
        ),
        "packet_metadata_decisions_complete": bool(attempt and attempt.status == "complete"),
        "eligible_metadata_decisions_complete": all(
            item.disposition is not None and item.status in {"selected", "not_selected", "duplicate"}
            for item in progress.candidates.values()
            if item.eligible
        ),
        "registered_identities": len(progress.candidates),
        "eligible_identities": sum(candidate.eligible for candidate in progress.candidates.values()),
        "statuses": counts,
        "oldest_eligible_unfinished_observed_at": min(unfinished).isoformat() if unfinished else None,
        "oldest_eligible_unfinished_age_hours": max(0, (instant - min(unfinished)).total_seconds() / 3600)
        if unfinished
        else None,
        "latest_observed_occurrences": len(inventory.get("observations", [])),
        "current_collection": inventory,
        "limits": "Model judgments over RSS metadata; not semantic correctness or full-source reading",
    }
    return storage.freeze_packet(packet, summary, cache_dir)


def checkpoint_candidates(
    progress: CandidateProgress,
    cache_dir: str | Path = ".cache",
    *,
    skipped_empty_reports: set[str] | None = None,
) -> Path:
    """Verify objects, apply retirement, then persist the resulting working set."""
    materialize_progress(progress, cache_dir)
    retire_indexed_work(progress, cache_dir, skipped_empty_reports)
    return write_progress(progress, cache_dir)


def persist_candidates(progress: CandidateProgress, cache_dir: str | Path = ".cache") -> Path:
    """Persist unfinished work without retiring any candidate or proof packet."""
    materialize_progress(progress, cache_dir)
    return write_progress(progress, cache_dir)
