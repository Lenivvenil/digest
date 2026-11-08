"""Conservative native selection evidence without provider-proof fabrication.

Editorial acceptance is not delivery. Native decisions are retained separately
from provider candidate decisions; all prior candidate work remains recoverable.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from digest.adapters.storage import native as storage
from digest.adapters.storage.candidate_objects import load_candidate, load_candidate_packets, packet_key
from digest.adapters.storage.candidate_progress import load_candidate_progress
from digest.application.candidate_lifecycle import persist_candidates
from digest.domain.catalog.articles import article_hash
from digest.domain.catalog.occurrences import occurrence_sha256
from digest.domain.editorial.candidates import Candidate, CandidateArticle, validate_occurrence
from digest.domain.editorial.native import encode

_STATUSES = {"card", "needs_enrichment", "capacity_deferred", "withheld", "duplicate"}


def build_lifecycle_record(
    *,
    packet_ref: dict[str, str],
    response_ref: dict[str, str],
    review_ref: dict[str, str],
    acceptance_ref: dict[str, str],
    originals: dict[str, dict[str, Any]],
    decisions: list[dict[str, Any]],
    accepted_ids: set[str],
) -> dict[str, Any]:
    """Build deterministic full-pool evidence without reading or writing state."""
    references = {
        "packet": packet_ref,
        "response": response_ref,
        "review": review_ref,
        "acceptance": acceptance_ref,
    }
    for reference in references.values():
        if set(reference) != {"path", "sha256"}:
            raise ValueError("Invalid native lifecycle evidence reference.")
    if not originals or not accepted_ids <= originals.keys():
        raise ValueError("Native lifecycle accepted identities differ from full pool.")
    articles = {}
    for identity, raw in originals.items():
        article = CandidateArticle(**raw)
        validate_occurrence(article)
        if identity != article_hash(article.title, article.link):
            raise ValueError("Native lifecycle source identity mismatch.")
        articles[identity] = article
    by_id = {}
    for decision in decisions:
        if set(decision) != {"id", "status", "text", "reason", "duplicate_of"}:
            raise ValueError("Invalid native lifecycle decision fields.")
        identity = decision["id"]
        if identity not in articles or identity in by_id or decision["status"] not in _STATUSES:
            raise ValueError("Invalid native lifecycle decision membership or status.")
        if not isinstance(decision["reason"], str) or not decision["reason"].strip():
            raise ValueError("Native lifecycle decision requires a reason.")
        if (
            not isinstance(decision["text"], str)
            or (decision["status"] == "card" and not decision["text"].strip())
            or (decision["status"] != "card" and decision["text"] != "")
        ):
            raise ValueError("Invalid native lifecycle card text.")
        target = decision["duplicate_of"]
        if decision["status"] == "duplicate":
            if target not in articles or target == identity:
                raise ValueError("Invalid native lifecycle duplicate target.")
        elif target is not None:
            raise ValueError("Unexpected native lifecycle duplicate target.")
        if identity in accepted_ids and decision["status"] != "card":
            raise ValueError("Only proposed native cards can be source accepted.")
        by_id[identity] = decision
    if set(by_id) != set(articles):
        raise ValueError("Native lifecycle decisions do not cover the full pool.")
    if any(
        item["status"] == "duplicate" and by_id[item["duplicate_of"]]["status"] == "duplicate"
        for item in by_id.values()
    ):
        raise ValueError("Native lifecycle duplicate chains are not allowed.")
    body = {
        "schema": "native-selection-lifecycle/v1",
        "references": references,
        "delivery_accounting": "unchanged; acceptance is not delivery",
        "candidates": [
            {
                "identity": identity,
                "occurrence_sha256": occurrence_sha256(articles[identity]),
                "original": asdict(articles[identity]),
                "decision": by_id[identity],
                "source_accepted": identity in accepted_ids,
            }
            for identity in sorted(articles)
        ],
    }
    return body


def persist_selection_lifecycle(
    cache: Path,
    *,
    packet_ref: dict[str, str],
    response_ref: dict[str, str],
    review_ref: dict[str, str],
    acceptance_ref: dict[str, str],
    originals: dict[str, dict[str, Any]],
    decisions: list[dict[str, Any]],
    accepted_ids: set[str],
    now: datetime,
) -> storage.Reference:
    """Retain all unfinished work before publishing immutable lifecycle evidence."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Native lifecycle observation requires a timezone.")
    body = build_lifecycle_record(
        packet_ref=packet_ref,
        response_ref=response_ref,
        review_ref=review_ref,
        acceptance_ref=acceptance_ref,
        originals=originals,
        decisions=decisions,
        accepted_ids=accepted_ids,
    )
    for reference in body["references"].values():
        storage.read_reference(storage.Reference(**reference))
    encoded = encode(body)
    if not 0 < len(encoded) <= storage.MAX_BYTES:
        raise ValueError("Native lifecycle checkpoint exceeds its byte bound.")
    retain_selection_pool(cache, originals, now=now)
    return storage.put_bytes(encoded, cache, "selection-lifecycle")


def retain_selection_pool(
    cache: Path,
    originals: dict[str, dict[str, Any]],
    *,
    now: datetime,
) -> None:
    """Retain an exported full pool without claiming decisions or delivery."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Native lifecycle observation requires a timezone.")
    if not originals:
        raise ValueError("Native selection pool must not be empty.")
    articles = {identity: CandidateArticle(**raw) for identity, raw in originals.items()}
    for identity, article in articles.items():
        validate_occurrence(article)
        if identity != article_hash(article.title, article.link):
            raise ValueError("Native lifecycle source identity mismatch.")
    progress = load_candidate_progress(cache)
    restored = {}
    for identity, article in articles.items():
        candidate = progress.candidates.get(identity)
        if candidate is None:
            candidate = load_candidate(identity, cache)
            if candidate is not None:
                restored[identity] = candidate
        if candidate is not None and candidate.status != "not_presented":
            raise ValueError("Completed candidate disposition cannot be resurrected by selection export")
        if candidate is not None and article not in (candidate.article, *candidate.occurrences):
            raise ValueError("Native lifecycle source differs from retained occurrence.")
    for identity, candidate in restored.items():
        progress.candidates[identity] = candidate
        existing = {packet_key(packet) for packet in progress.packets}
        progress.packets.extend(
            packet for packet in load_candidate_packets(identity, cache) if packet_key(packet) not in existing
        )
    for identity, article in articles.items():
        if identity not in progress.candidates:
            progress.candidates[identity] = Candidate(identity, article, now.isoformat(), 1)
    persist_candidates(progress, cache)
