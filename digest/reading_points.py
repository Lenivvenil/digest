"""Offline grouped source selections; no reading, publication or model integration.

The model may nominate complete literal units, never write a factual point's text.
Known IDs establish provenance only. They do not establish that an actor, scope,
qualification or contradiction was correctly selected or associated with a point.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, replace
from typing import Any

from digest.reading_brief_state import Source, Span, _validate_source, checksum

UNIT_VIEW_VERSION = "complete-source-units-v1"
_LIMITATION = (
    "Selection provenance only; semantic completeness, qualification associations and "
    "contradictions are unverified. This candidate does not establish publication eligibility."
)


@dataclass(frozen=True)
class PointSelection:
    supporting_unit_ids: tuple[int, ...]
    context_unit_ids: tuple[int, ...]
    qualification_unit_ids: tuple[int, ...]


@dataclass(frozen=True)
class EditorialInterpretation:
    text: str
    unit_ids: tuple[int, ...]


@dataclass(frozen=True)
class PointCandidate:
    unit_view_version: str
    source_sha256: str
    points: tuple[PointSelection, ...]
    editorial_interpretation: EditorialInterpretation | None = None


@dataclass(frozen=True)
class PointProjection:
    candidate: PointCandidate
    # One-based original point positions; each replacement is retained unchanged.
    redundant_points: tuple[tuple[int, int], ...]


def source_units(source: Source) -> tuple[Span, ...]:
    """Cover every original character without changing the paragraph manifest.

    This conservative offset algorithm follows the paused complete-source-units-v1
    experiment. Only candidate sentence boundaries split a paragraph. Detected
    abbreviations, decimals, quotations and bracketed clauses keep it together.
    This is a conservative heuristic, not a general sentence-boundary proof.
    Whitespace belongs to the preceding unit; a long sentence is never cut at a
    length limit. A complete offset view does not prove complete article extraction.
    """
    _validate_source(source)
    units: list[Span] = []
    for paragraph in re.finditer(r"[^\n]+(?:\n+|$)|\n+", source.text):
        text = paragraph.group()
        boundaries = list(re.finditer(r"[.!?]\s+(?=[A-Z])", text))
        ambiguous = (
            re.search(r'["“”«»()\[\]]', text)
            or re.search(r"\b(?:[A-Za-z]|Mr|Mrs|Ms|Dr|Prof|Sr|Jr|St|vs|etc|Fig|No)\.", text)
            or re.search(r"\d\.\d|\.\.\.", text)
        )
        terminal_periods = {match.start() for match in boundaries if text[match.start()] == "."}
        internal_periods = {match.start() for match in re.finditer(r"\.", text.rstrip()[:-1])}
        if ambiguous or internal_periods - terminal_periods:
            boundaries = []
        start = paragraph.start()
        for match in boundaries:
            stop = paragraph.start() + match.end()
            units.append(Span(len(units) + 1, start, stop))
            start = stop
        if start < paragraph.end():
            units.append(Span(len(units) + 1, start, paragraph.end()))
    return tuple(units)


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Do not let duplicate JSON keys silently replace a selection or binding."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate_selection_key: {key}")
        result[key] = value
    return result


def _fields(value: Any, expected: set[str], location: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError(f"invalid_selection_fields: {location}")
    return value


def _ids(value: Any, known: set[int], location: str, *, required: bool = False) -> tuple[int, ...]:
    if not isinstance(value, list) or any(type(item) is not int for item in value):
        raise ValueError(f"invalid_unit_ids: {location}")
    if len(set(value)) != len(value):
        raise ValueError(f"duplicate_unit_ids: {location}")
    if not set(value) <= known:
        raise ValueError(f"unknown_unit_ids: {location}")
    if required and not value:
        raise ValueError(f"missing_unit_ids: {location}")
    # The source's order, never the proposal's ordering, determines excerpt order.
    return tuple(sorted(value))


def _roles(point: PointSelection) -> tuple[tuple[str, tuple[int, ...]], ...]:
    return (
        ("Supporting", point.supporting_unit_ids),
        ("Context", point.context_unit_ids),
        ("Qualification", point.qualification_unit_ids),
    )


def _point(value: Any, known: set[int]) -> PointSelection:
    data = _fields(value, {"supporting_unit_ids", "context_unit_ids", "qualification_unit_ids"}, "point")
    return PointSelection(
        _ids(data["supporting_unit_ids"], known, "supporting", required=True),
        _ids(data["context_unit_ids"], known, "context"),
        _ids(data["qualification_unit_ids"], known, "qualification"),
    )


def parse_point_selection(response: str, source: Source) -> PointCandidate:
    """Validate a proposed JSON selection against one complete source-unit view.

    Required JSON fields: unit_view_version, source_sha256 (checksum(asdict(source))),
    points, editorial_interpretation. Each point has supporting_unit_ids,
    context_unit_ids and qualification_unit_ids. Editorial interpretation is null
    or an object with text and unit_ids. Unknown fields, including generated point
    prose or acceptance claims, fail. Empty points are permitted and do not mean
    editorial rejection. One complete unit can serve several points or roles:
    a finding and its qualification can occur in the same indivisible passage.
    """
    units = source_units(source)
    data = _fields(json.loads(response, object_pairs_hook=_object),
                   {"unit_view_version", "source_sha256", "points", "editorial_interpretation"}, "candidate")
    if data["unit_view_version"] != UNIT_VIEW_VERSION:
        raise ValueError("source_unit_view_version_mismatch")
    if data["source_sha256"] != checksum(asdict(source)):
        raise ValueError("point_selection_source_mismatch")
    if not isinstance(data["points"], list):
        raise ValueError("invalid_selection_points")
    known = {unit.id for unit in units}
    points = tuple(_point(value, known) for value in data["points"])
    if len(set(points)) != len(points):
        raise ValueError("duplicate_points")
    interpretation = None
    if data["editorial_interpretation"] is not None:
        editorial = _fields(data["editorial_interpretation"], {"text", "unit_ids"}, "editorial_interpretation")
        if not isinstance(editorial["text"], str) or not editorial["text"].strip():
            raise ValueError("invalid_editorial_interpretation_text")
        interpretation = EditorialInterpretation(
            editorial["text"], _ids(editorial["unit_ids"], known, "editorial_interpretation", required=True),
        )
    return PointCandidate(UNIT_VIEW_VERSION, data["source_sha256"], points, interpretation)


def _validated_units(candidate: PointCandidate, source: Source) -> tuple[Span, ...]:
    # Rendering also checks manually constructed dataclasses and later source edits.
    parse_point_selection(json.dumps(asdict(candidate), ensure_ascii=False), source)
    return source_units(source)


def project_distinct_points(candidate: PointCandidate, source: Source) -> PointProjection:
    """Offline role-preserving redundancy projection, never semantic equivalence.

    Drop only a group with identical support and strictly contained context or
    qualifications. The original candidate is untouched; replacements refer to
    retained original groups. This cannot repair missing context or infer scope.
    """
    _validated_units(candidate, source)

    def contains(outer: PointSelection, inner: PointSelection) -> bool:
        return (set(outer.supporting_unit_ids) == set(inner.supporting_unit_ids)
                and set(inner.context_unit_ids) <= set(outer.context_unit_ids)
                and set(inner.qualification_unit_ids) <= set(outer.qualification_unit_ids)
                and (set(inner.context_unit_ids) < set(outer.context_unit_ids)
                     or set(inner.qualification_unit_ids) < set(outer.qualification_unit_ids)))

    kept = tuple(index for index, point in enumerate(candidate.points)
                 if not any(contains(other, point) for other in candidate.points))
    replacements = tuple(
        (index + 1, next(other + 1 for other in kept if contains(candidate.points[other], point)))
        for index, point in enumerate(candidate.points) if index not in kept
    )
    return PointProjection(replace(candidate, points=tuple(candidate.points[index] for index in kept)), replacements)


def _literal(source: Source, unit: Span, label: str) -> str:
    return (
        f"[{label}; source unit {unit.id}; characters {unit.start}:{unit.end}]\n"
        + source.text[unit.start:unit.end]
        + f"\n[End source unit {unit.id}]\n"
    )


def _provenance(candidate: PointCandidate, source: Source) -> str:
    notes = "\n".join(f"Source coverage note: {note}" for note in source.coverage_notes)
    return (
        f"Source name (metadata): {source.selection.source}\n"
        f"Title (metadata): {source.selection.title}\n"
        f"Final source URL: {source.final_url}\n"
        f"Source snapshot: {candidate.source_sha256}\nUnit view: {UNIT_VIEW_VERSION}\n"
        + (notes + "\n" if notes else "")
    )


def render_candidate(candidate: PointCandidate, source: Source) -> str:
    """Render each nominated group whole, with its context and qualifications.

    There is deliberately no summary-only or support-only rendering option. This
    output is plain text for inspection, not a Telegram/Markdown publication card.
    No generated factual connective, heading or point text is used.
    """
    units = _validated_units(candidate, source)
    parts = ["Offline grouped-source candidate\n", _LIMITATION + "\n"]
    parts.append(_provenance(candidate, source))
    if not candidate.points:
        parts.append("No points nominated; this is not an editorial rejection.\n")
    for number, point in enumerate(candidate.points, 1):
        parts.append(f"\nPoint {number} (proposed source-unit group)\n")
        roles: dict[int, list[str]] = {}
        for role, ids in _roles(point):
            for unit_id in ids:
                roles.setdefault(unit_id, []).append(role)
        for unit in units:
            if unit.id in roles:
                parts.append(_literal(source, unit, "; ".join(roles[unit.id])))
        if not point.context_unit_ids:
            parts.append("Context: none nominated; this is not a completeness finding.\n")
        if not point.qualification_unit_ids:
            parts.append("Qualifications: none nominated; this is not a completeness finding.\n")
    if candidate.editorial_interpretation is not None:
        editorial = candidate.editorial_interpretation
        editorial_ids = ", ".join(str(unit_id) for unit_id in editorial.unit_ids)
        parts.extend(("\nEditorial interpretation (unverified; cannot certify the source groups)\n",
                      f"Nominated source unit IDs: {editorial_ids}\n", editorial.text,
                      "\nEnd editorial interpretation\n"))
    parts.append("\nUnselected source material is available in the complete source coverage view.\n")
    return "".join(parts)


def render_source_coverage(candidate: PointCandidate, source: Source) -> str:
    """Show every unit verbatim, including unselected material, in source order.

    Coverage describes the derived offset view and proposal membership only. It
    does not claim that a model saw or understood any source text or page.
    """
    units = _validated_units(candidate, source)
    uses: dict[int, list[str]] = {unit.id: [] for unit in units}
    for number, point in enumerate(candidate.points, 1):
        for role, ids in _roles(point):
            for unit_id in ids:
                uses[unit_id].append(f"Point {number}: {role.lower()}")
    if candidate.editorial_interpretation is not None:
        for unit_id in candidate.editorial_interpretation.unit_ids:
            uses[unit_id].append("Editorial interpretation reference (unverified)")
    parts = ["Complete source coverage view (offsets and selection membership only)\n", _LIMITATION + "\n",
             _provenance(candidate, source)]
    for unit in units:
        parts.append(_literal(source, unit, "; ".join(uses[unit.id]) or "Unselected"))
    return "".join(parts)
