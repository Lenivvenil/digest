"""Candidate values and pure occurrence, packet and decision-proof contracts.

No selection workflow, HTTP client or persistence adapter is imported here.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from typing import Literal

from digest._serialization import canonical_json_bytes
from digest.domain.catalog.articles import Article, article_hash
from digest.domain.editorial.dispositions import (
    CandidateDisposition,
    CandidateDispositionAttempt,
    validate_disposition_attempt,
)
from digest.domain.editorial.reviews import (
    BlindReviewReport,
    EvidenceBundle,
    delivery_review,
    validate_canonical_evidence,
    validate_canonical_report,
    validated_cached_selections,
)

CandidateStatus = Literal[
    "selected", "not_selected", "duplicate", "not_selected_without_editorial_reason",
    "not_presented", "technical_pending",
]


@dataclass(frozen=True)
class CandidateArticle:
    title: str
    link: str
    description: str
    source: str
    category: str
    published: str | None
    source_url: str

    def article(self) -> Article:
        return Article(self.title, self.link, self.description, self.source, self.category,
                       datetime.fromisoformat(self.published) if self.published else None)


@dataclass
class Candidate:
    identity: str
    article: CandidateArticle
    first_observed_at: str
    priority: int
    status: CandidateStatus = "not_presented"
    eligible: bool = True
    eligibility_reason: str = ""
    occurrences: tuple[CandidateArticle, ...] = ()
    delivery_cache_observed_at: str | None = None
    disposition: CandidateDisposition | None = None
    decision_response_sha256: str | None = None
    decision_prompt_hash: str | None = None
    decision_occurrence_sha256: str | None = None


@dataclass
class CandidatePacket:
    evidence: EvidenceBundle
    articles: tuple[CandidateArticle, ...]
    priorities: dict[str, int]
    planned_at: str
    prompt_hash: str = ""
    max_selections: int = 5  # Planned publication cap; retained as frozen provenance, not a relevance bound.
    report: BlindReviewReport | None = None
    handed_to_preparation: bool = False
    disposition_attempts: tuple[CandidateDispositionAttempt, ...] = ()
    collection_json: str = "{}"


@dataclass
class CandidateProgress:
    schema_version: int = 1
    candidates: dict[str, Candidate] = field(default_factory=dict)
    packets: list[CandidatePacket] = field(default_factory=list)
    latest_collection_json: str = "{}"
    policy_sha256: str = ""


def _digest(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def validate_occurrence(article: CandidateArticle) -> None:
    """Validate the original publication-time contract without fabricating a candidate."""
    parsed = article.article()
    if parsed.pub_date is not None and parsed.pub_date.tzinfo is None:
        raise ValueError("Candidate publication time requires a timezone.")


def validate_candidate_decision(candidate: Candidate, packets: Sequence[CandidatePacket]) -> None:
    if candidate.disposition is None:
        if (candidate.status in {"not_selected", "duplicate"}
                or any(value is not None for value in (candidate.decision_response_sha256,
                       candidate.decision_prompt_hash, candidate.decision_occurrence_sha256))):
            raise ValueError("Resolved candidate lacks exact disposition evidence.")
        return
    if (candidate.disposition.evidence_id != candidate.identity
            or candidate.status in {"selected", "not_selected", "duplicate"}
            and candidate.disposition.status != candidate.status
            or candidate.decision_occurrence_sha256 != _digest(asdict(candidate.article))):
        raise ValueError("Candidate decision source occurrence mismatch.")
    for packet in packets:
        if not any(article_hash(article.title, article.link) == candidate.identity
                   and _digest(asdict(article)) == candidate.decision_occurrence_sha256 for article in packet.articles):
            continue
        for attempt in packet.disposition_attempts:
            if (attempt.response_sha256 == candidate.decision_response_sha256
                    and attempt.prompt_hash == candidate.decision_prompt_hash
                    and candidate.disposition in attempt.dispositions):
                return
    raise ValueError("Candidate decision has no exact saved response evidence.")


def validate_candidate(
    candidate: Candidate, packets: Sequence[CandidatePacket], *, identity: str | None = None,
) -> None:
    """Validate one retained candidate and its actual decision-proof packets."""
    if identity is None:
        identity = candidate.identity
    if identity != candidate.identity or identity != article_hash(candidate.article.title, candidate.article.link):
        raise ValueError("Candidate identity mismatch.")
    if datetime.fromisoformat(candidate.first_observed_at).tzinfo is None:
        raise ValueError("Candidate observation requires a timezone.")
    if (candidate.delivery_cache_observed_at is not None
            and datetime.fromisoformat(candidate.delivery_cache_observed_at).tzinfo is None):
        raise ValueError("Candidate delivery-cache evidence requires a timezone.")
    if candidate.article in candidate.occurrences:
        raise ValueError("Candidate repeats its canonical occurrence.")
    if any(article_hash(saved.title, saved.link) != identity for saved in candidate.occurrences):
        raise ValueError("Candidate occurrence identity mismatch.")
    article = candidate.article.article()
    if article.pub_date is not None and article.pub_date.tzinfo is None:
        raise ValueError("Candidate publication time requires a timezone.")
    validate_candidate_decision(candidate, packets)


def validate_packet(packet: CandidatePacket) -> None:
    """Validate packet membership, frozen review and same-response decision bindings."""
    if not isinstance(json.loads(packet.collection_json), dict):
        raise ValueError("Invalid packet collection accounting.")
    if len(packet.prompt_hash) != 64 or packet.max_selections < 1:
        raise ValueError("Invalid frozen candidate prompt contract.")
    if datetime.fromisoformat(packet.planned_at).tzinfo is None:
        raise ValueError("Candidate attempt requires a timezone.")
    known = {article_hash(article.title, article.link): article for article in packet.articles}
    if len(known) != len(packet.articles) or set(known) != {item.evidence_id for item in packet.evidence.items}:
        raise ValueError("Candidate packet membership mismatch.")
    if packet.report is None:
        validate_canonical_evidence(packet.evidence)
    else:
        validate_canonical_report(packet.report)
    if packet.disposition_attempts and packet.report is None:
        raise ValueError("Candidate dispositions lack their saved report.")
    if packet.report is not None:
        for attempt in packet.disposition_attempts:
            matching = next((review for review in packet.report.reviews if review.slot == attempt.slot), None)
            if matching is None:
                raise ValueError("Saved disposition has no matching model review.")
            validate_disposition_attempt(attempt, packet.evidence, matching)
        if packet.report.evidence != packet.evidence:
            raise ValueError("Candidate report evidence mismatch.")
        for review in packet.report.reviews:
            if review.status in {"ok", "partial", "abstained"}:
                validated_cached_selections(review, packet.evidence)
                if review.prompt_hash != packet.prompt_hash:
                    raise ValueError("Candidate report prompt mismatch.")


def validate_progress(progress: CandidateProgress) -> None:
    if not isinstance(json.loads(progress.latest_collection_json), dict):
        raise ValueError("Invalid collection inventory snapshot.")
    if progress.schema_version != 1:
        raise ValueError("Unsupported candidate progress schema.")
    for identity, candidate in progress.candidates.items():
        validate_candidate(candidate, progress.packets, identity=identity)
    for packet in progress.packets:
        validate_packet(packet)


def latest_occurrence_packet(candidate: Candidate, packets: list[CandidatePacket]) -> CandidatePacket | None:
    """A retained plan for this exact source occurrence, never proof of dispatch."""
    matches = [packet for packet in packets if candidate.article in packet.articles
               and any(item.evidence_id == candidate.identity for item in packet.evidence.items)]
    return max(matches, key=lambda packet: datetime.fromisoformat(packet.planned_at)) if matches else None


def proof_packets(candidate: Candidate, packets: list[CandidatePacket]) -> list[CandidatePacket]:
    matches = [packet for packet in packets if any(item.evidence_id == candidate.identity
                                                  for item in packet.evidence.items)]
    if candidate.disposition is not None:
        matches = [packet for packet in matches if any(
            attempt.response_sha256 == candidate.decision_response_sha256
            and attempt.prompt_hash == candidate.decision_prompt_hash
            and candidate.disposition in attempt.dispositions for attempt in packet.disposition_attempts)]
    proof = [max(reversed(matches), key=lambda packet: datetime.fromisoformat(packet.planned_at))] if matches else []
    # Keep existing decision/history proof, plus the current occurrence's latest
    # planning opportunity when rebinding makes them different. At most two.
    if candidate.status == "technical_pending":
        current = latest_occurrence_packet(candidate, packets)
        if current is not None and current not in proof:
            proof.append(current)
    return proof


def accepted_empty_packet(packet: CandidatePacket) -> bool:
    if packet.report is None or not packet.report.reviews:
        return False
    review = delivery_review(packet.report)
    capture = next((item for item in packet.disposition_attempts if item.slot == review.slot), None)
    return review.status == "abstained" and (
        not packet.disposition_attempts or capture is not None and capture.status == "complete")


def packet_key(packet: CandidatePacket) -> str:
    """Address completed reports by report hash and unfinished attempts by packet hash."""
    return _digest(asdict(packet.report)) if packet.report else _digest(asdict(replace(
        packet, handed_to_preparation=False)))
