"""Source-bound native closing/comparison, without provider or English-twin records."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from digest._serialization import canonical_json_bytes, restore_dataclass
from digest.domain.catalog.articles import article_hash
from digest.domain.catalog.occurrences import occurrence_sha256
from digest.domain.editorial.candidates import CandidatePacket
from digest.domain.editorial.summaries import ArticleSummary
from digest.domain.editorial.synthesis import (
    MAX_INTERPRETATION_CHARS,
    MAX_OBSERVATION_CHARS,
    MAX_QUALIFICATION_CHARS,
    EvidenceBinding,
    _plain,
    validate_bindings,
)


@dataclass(frozen=True)
class NativePolicy:
    closing_enabled: bool
    closing_ids: tuple[str, ...]
    comparison_enabled: bool
    comparison_publish: bool


@dataclass(frozen=True)
class NativeClosing:
    evidence_id: str
    occurrence_sha256: str
    text: str


@dataclass(frozen=True)
class NativeObservation:
    evidence_id: str
    occurrence_sha256: str
    observation: str


@dataclass(frozen=True)
class NativeComparison:
    supports: tuple[NativeObservation, ...]
    interpretation: str
    qualification: str | None


def policy(value: Any, known_ids: set[str]) -> NativePolicy:
    restored: NativePolicy = restore_dataclass(value, NativePolicy)
    if (len(restored.closing_ids) != len(set(restored.closing_ids))
            or not set(restored.closing_ids) <= known_ids
            or not restored.closing_enabled and restored.closing_ids):
        raise ValueError("Invalid native optional publication policy.")
    return restored


def optional_verdict(kind: str, value: Any, review: dict[str, Any]) -> str:
    """An absent/ambiguous optional verdict withholds optional prose, not main cards."""
    from hashlib import sha256

    verdicts = review.get("optional_verdicts")
    if not isinstance(verdicts, list):
        return "incomplete_optional_review"
    known = {"closing", "comparison"}
    if any(not isinstance(item, dict) or set(item) != {"kind", "value_sha256", "verdict", "notes"}
           or not isinstance(item.get("kind"), str) or item["kind"] not in known for item in verdicts):
        return "invalid_optional_review"
    matches = [item for item in verdicts if item["kind"] == kind]
    if len(matches) != 1:
        return "incomplete_optional_review"
    verdict = matches[0]
    if (verdict["value_sha256"] != sha256(canonical_json_bytes(value)).hexdigest()
            or any(not isinstance(verdict[key], str) or not verdict[key].strip() for key in ("verdict", "notes"))):
        return "invalid_optional_review"
    return "accepted" if verdict["verdict"] == "accept" else "rejected_optional_review"


def closing_card(
    value: Any, packet: CandidatePacket, proposed_main_ids: set[str], allowed_ids: set[str],
) -> ArticleSummary:
    closing: NativeClosing = restore_dataclass(value, NativeClosing)
    if closing.evidence_id not in allowed_ids or closing.evidence_id in proposed_main_ids:
        raise ValueError("Native closing is not a distinct approved non-main source item.")
    original = next(item for item in packet.articles if article_hash(item.title, item.link) == closing.evidence_id)
    if occurrence_sha256(original) != closing.occurrence_sha256:
        raise ValueError("Native closing occurrence changed.")
    _plain(closing.text, 600)
    return ArticleSummary(original.title, original.link, original.source, original.category, closing.text)


def comparison(
    value: Any, packet: CandidatePacket, retained_main_ids: list[str],
) -> tuple[NativeComparison, tuple[EvidenceBinding, ...]]:
    result: NativeComparison = restore_dataclass(value, NativeComparison)
    if len(result.supports) != 2:
        raise ValueError("Native comparison requires two source-specific supports.")
    ids = [item.evidence_id for item in result.supports]
    if len(set(ids)) != 2 or ids != [identity for identity in retained_main_ids if identity in ids]:
        raise ValueError("Native comparison depends on rejected/non-main items or changed source order.")
    originals = {article_hash(item.title, item.link): item for item in packet.articles}
    evidence = {item.evidence_id: item for item in packet.evidence.items}
    bindings = []
    for support in result.supports:
        original = originals[support.evidence_id]
        if support.occurrence_sha256 != occurrence_sha256(original):
            raise ValueError("Native comparison occurrence changed.")
        _plain(support.observation, MAX_OBSERVATION_CHARS)
        binding = EvidenceBinding(evidence[support.evidence_id], support.occurrence_sha256,
                                  original.source, original.source_url)
        if not binding.evidence.excerpt.strip():
            raise ValueError("Native comparison lacks complete excerpt support.")
        bindings.append(binding)
    validate_bindings(tuple(bindings))
    _plain(result.interpretation, MAX_INTERPRETATION_CHARS)
    if result.qualification is not None:
        _plain(result.qualification, MAX_QUALIFICATION_CHARS)
    return result, tuple(bindings)


def comparison_metadata(result: NativeComparison, bindings: tuple[EvidenceBinding, ...]) -> dict[str, Any]:
    """Complete exact source evidence is attached by code, never copied from output."""
    return {"value": {"supports": [asdict(item) for item in result.supports],
                      "interpretation": result.interpretation, "qualification": result.qualification},
            "bindings": [asdict(binding) for binding in bindings]}
