"""Offline selection provenance must never become semantic or release approval."""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import asdict, replace
from typing import Any

import pytest

from digest.reading_brief_state import PROMPT_VERSION, Selection, Source, checksum, make_spans
from digest.reading_points import (
    UNIT_VIEW_VERSION,
    PointCandidate,
    PointSelection,
    parse_point_selection,
    render_candidate,
    render_source_coverage,
    source_units,
)


def source(text: str) -> Source:
    return Source(
        Selection("Saved article", "https://example.com/article", "Example", "technology", None),
        text, hashlib.sha256(text.encode()).hexdigest(), "https://example.com/article",
        "2026-10-04T12:00:00+00:00", None, "article", ["Images not inspected."], make_spans(text),
    )


def proposal(article: Source, *, points: list[dict[str, list[int]]] | None = None) -> dict[str, Any]:
    return {
        "unit_view_version": UNIT_VIEW_VERSION,
        "source_sha256": checksum(asdict(article)),
        "points": points if points is not None else [point([1])],
        "editorial_interpretation": None,
    }


def point(supporting: list[int], context: list[int] | None = None,
          qualification: list[int] | None = None) -> dict[str, list[int]]:
    return {"supporting_unit_ids": supporting, "context_unit_ids": context or [],
            "qualification_unit_ids": qualification or []}


def parse(data: dict[str, Any], article: Source) -> PointCandidate:
    return parse_point_selection(json.dumps(data), article)


def test_actor_scope_context_and_late_qualifier_remain_one_literal_group() -> None:
    context = "The pilot belongs to Example Bank and covers its institutional clients only.\n\n"
    supporting = "Its new service uses the existing messaging network for instructions.\n\n"
    unrelated = "Background material remains part of the original article.\n\n" * 400
    qualification = "The offering excludes other banks, and settlement is limited to two supported currencies."
    article = source(context + supporting + unrelated + qualification)
    before = copy.deepcopy(asdict(article))
    units = source_units(article)
    candidate = parse(proposal(article, points=[point([2], [1], [units[-1].id])]), article)
    rendered = render_candidate(candidate, article)
    assert context in rendered and supporting in rendered and qualification in rendered
    assert rendered.index(context) < rendered.index(supporting) < rendered.index(qualification)
    assert "[Context; source unit 1;" in rendered
    assert "[Supporting; source unit 2;" in rendered
    assert f"[Qualification; source unit {units[-1].id};" in rendered
    assert "Background material" not in rendered
    assert "Point 2" not in rendered
    assert asdict(article) == before
    assert PROMPT_VERSION == "source-passages-v4"


@pytest.mark.parametrize("text", [
    "Dr. Green reports a result. Another claim follows.",
    "The U.S. pilot is limited. Another claim follows.",
    "The rate is 3.5 percent. Another claim follows.",
    "It rose... Another claim follows.",
    'The report says "Only pilot users." Another claim follows.',
    "The report says “Only pilot users.” Another claim follows.",
    "The result (with exclusions). Another claim follows.",
    "The result [with exclusions]. Another claim follows.",
    "The example e.g. this one has context. Another claim follows.",
    "The link is example.com. Another claim follows.",
    "Это утверждение. Другое утверждение остаётся в том же абзаце.",
])
def test_ambiguous_paragraphs_and_abbreviations_stay_whole(text: str) -> None:
    article = source(text)
    assert [(unit.start, unit.end) for unit in source_units(article)] == [(0, len(text))]


def test_clear_boundaries_keep_original_whitespace_and_all_source_characters() -> None:
    text = "\n\nAlpha operates locally.  Beta has a narrower scope.\r\n\r\n\tFinal condition applies.  \n"
    article = source(text)
    units = source_units(article)
    assert [unit.id for unit in units] == list(range(1, len(units) + 1))
    assert units[0].start == 0 and units[-1].end == len(text)
    assert all(left.end == right.start for left, right in zip(units, units[1:], strict=False))
    assert "".join(text[unit.start:unit.end] for unit in units) == text
    assert text[units[1].start:units[1].end] == "Alpha operates locally.  "


def test_unknown_abbreviation_still_reconstructs_the_source_without_boundary_certification() -> None:
    text = "Acme Inc. Participates in the pilot with a narrow remit.  Another party has a different role.\n"
    article = source(text)
    units = source_units(article)
    # Inc. is outside the heuristic's recognized abbreviations. Offset fidelity
    # remains testable; whether its boundary is linguistically correct does not.
    assert "".join(text[unit.start:unit.end] for unit in units) == text
    assert all(left.end == right.start for left, right in zip(units, units[1:], strict=False))
    candidate = parse(proposal(article, points=[point([unit.id for unit in units])]), article)
    rendered = render_candidate(candidate, article)
    assert "semantic completeness, qualification associations and contradictions are unverified" in rendered
    for unit in units:
        assert text[unit.start:unit.end] in rendered


def test_a_long_sentence_ignores_legacy_manifest_character_cuts() -> None:
    text = "The measured scope includes " + "limited participants and " * 300 + "requires human approval."
    article = source(text)
    assert len(article.spans) > 2
    units = source_units(article)
    assert len(units) == 1
    assert article.text[units[0].start:units[0].end] == text
    assert text in render_candidate(parse(proposal(article), article), article)


def test_complete_source_coverage_exposes_unselected_qualifications_without_certifying_them() -> None:
    article = source("Example Bank reports a gain.\n\nOnly successful pilots were counted.\n\nFinal background.\n")
    candidate = parse(proposal(article), article)
    rendered = render_candidate(candidate, article)
    coverage = render_source_coverage(candidate, article)
    assert "Only successful pilots" not in rendered
    assert "Qualifications: none nominated; this is not a completeness finding." in rendered
    assert "[Unselected; source unit 2;" in coverage
    for unit in source_units(article):
        assert article.text[unit.start:unit.end] in coverage
    assert "semantic completeness, qualification associations and contradictions are unverified" in rendered
    assert "does not establish publication eligibility" in rendered
    assert "does not establish publication eligibility" in coverage
    assert set(asdict(candidate)) == {"unit_view_version", "source_sha256", "points", "editorial_interpretation"}


@pytest.mark.parametrize("ids,error", [
    ([999], "unknown_unit_ids"), ([0], "unknown_unit_ids"), ([-1], "unknown_unit_ids"),
    ([1, 1], "duplicate_unit_ids"), ([True], "invalid_unit_ids"), ([1.0], "invalid_unit_ids"),
    (["1"], "invalid_unit_ids"), ("1", "invalid_unit_ids"), ([], "missing_unit_ids"),
])
def test_invalid_supporting_ids_fail(ids: Any, error: str) -> None:
    article = source("Original source text.")
    data = proposal(article)
    data["points"][0]["supporting_unit_ids"] = ids
    with pytest.raises(ValueError, match=error):
        parse(data, article)


@pytest.mark.parametrize("role", ["context_unit_ids", "qualification_unit_ids"])
def test_unknown_and_duplicate_context_and_qualification_ids_fail(role: str) -> None:
    article = source("Original source text.\n\nA condition applies.")
    data = proposal(article)
    for ids, error in [([999], "unknown_unit_ids"), ([2, 2], "duplicate_unit_ids")]:
        data["points"][0][role] = ids
        with pytest.raises(ValueError, match=error):
            parse(data, article)


def test_one_complete_unit_can_supply_multiple_roles_but_is_rendered_once_per_point() -> None:
    text = "Example Bank reports a gain, limited to its pilot clients and subject to human review."
    article = source(text)
    candidate = parse(proposal(article, points=[point([1], [1], [1])]), article)
    rendered = render_candidate(candidate, article)
    assert "[Supporting; Context; Qualification; source unit 1;" in rendered
    assert rendered.count(text) == 1
    assert "Point 1: supporting; Point 1: context; Point 1: qualification" in render_source_coverage(candidate, article)


def test_identical_points_cannot_repeat() -> None:
    article = source("A result was reported.\n\nA condition applies.")
    with pytest.raises(ValueError, match="duplicate_points"):
        parse(proposal(article, points=[point([1]), point([1])]), article)


def test_one_qualification_can_apply_to_multiple_distinct_points() -> None:
    article = source("First result.\n\nSecond result.\n\nBoth results concern successful pilots only.")
    candidate = parse(proposal(article, points=[point([1], qualification=[3]),
                                               point([2], qualification=[3])]), article)
    rendered = render_candidate(candidate, article)
    assert rendered.count("Both results concern successful pilots only.") == 2
    first, second = rendered.split("Point 2 (proposed source-unit group)")
    assert "Both results concern successful pilots only." in first
    assert "Both results concern successful pilots only." in second
    assert "Point 1: qualification; Point 2: qualification" in render_source_coverage(candidate, article)


def test_selection_order_is_canonical_but_point_grouping_is_preserved() -> None:
    article = source("First detail.\n\nSecond detail.\n\nThird detail.")
    candidate = parse(proposal(article, points=[point([3, 1], [2])]), article)
    assert candidate.points[0].supporting_unit_ids == (1, 3)
    rendered = render_candidate(candidate, article)
    assert rendered.index("First detail.") < rendered.index("Second detail.") < rendered.index("Third detail.")
    assert rendered == render_candidate(candidate, article)


def test_editorial_prose_is_explicitly_unverified_and_cannot_write_a_factual_point() -> None:
    article = source("A limited pilot achieved a result.")
    data = proposal(article)
    data["editorial_interpretation"] = {"text": "This changes every network participant's service.", "unit_ids": [1]}
    candidate = parse(data, article)
    rendered = render_candidate(candidate, article)
    point_render, editorial = rendered.split("Editorial interpretation (unverified; cannot certify the source groups)")
    assert data["editorial_interpretation"]["text"] not in point_render
    assert data["editorial_interpretation"]["text"] in editorial
    assert "does not establish publication eligibility" in point_render
    data["points"][0]["text"] = "A model-written factual assertion."
    with pytest.raises(ValueError, match="invalid_selection_fields"):
        parse(data, article)


@pytest.mark.parametrize("value", [{"text": "Interpretation", "unit_ids": [999]},
                                   {"text": "Interpretation", "unit_ids": [1, 1]},
                                   {"text": "Interpretation", "unit_ids": []},
                                   {"text": " ", "unit_ids": [1]}, "Interpretation"])
def test_editorial_interpretation_also_requires_valid_exact_ids(value: Any) -> None:
    article = source("Original source text.")
    data = proposal(article)
    data["editorial_interpretation"] = value
    with pytest.raises(ValueError):
        parse(data, article)


@pytest.mark.parametrize("field,value,error", [
    ("unit_view_version", "legacy-paragraphs", "source_unit_view_version_mismatch"),
    ("source_sha256", "0" * 64, "point_selection_source_mismatch"),
    ("points", {}, "invalid_selection_points"),
])
def test_view_source_and_shape_must_match(field: str, value: Any, error: str) -> None:
    article = source("Original source text.")
    data = proposal(article)
    data[field] = value
    with pytest.raises(ValueError, match=error):
        parse(data, article)


def test_unknown_fields_duplicate_json_keys_and_incomplete_json_fail() -> None:
    article = source("Original source text.")
    data = proposal(article)
    data["semantic_complete"] = True
    with pytest.raises(ValueError, match="invalid_selection_fields"):
        parse(data, article)
    with pytest.raises(ValueError, match="duplicate_selection_key"):
        parse_point_selection('{"points":[],"points":[]}', article)
    with pytest.raises(ValueError):
        parse_point_selection('{"points":[', article)


def test_mutated_source_invalid_manifest_and_forged_dataclass_fail_at_render() -> None:
    article = source("Original source text.")
    candidate = parse(proposal(article), article)
    with pytest.raises(ValueError, match="source_body_checksum_mismatch"):
        render_candidate(candidate, replace(article, text="Altered source text."))
    with pytest.raises(ValueError, match="point_selection_source_mismatch"):
        render_candidate(candidate, replace(article, final_url="https://example.com/other"))
    with pytest.raises(ValueError, match="source_span_manifest_mismatch"):
        source_units(replace(article, spans=[]))
    forged = replace(candidate, points=(PointSelection((999,), (), ()),))
    with pytest.raises(ValueError, match="unknown_unit_ids"):
        render_candidate(forged, article)
    with pytest.raises(ValueError, match="unknown_unit_ids"):
        render_source_coverage(forged, article)


def test_empty_selection_is_not_a_model_read_or_editorial_rejection() -> None:
    article = source("Original source text.")
    candidate = parse(proposal(article, points=[]), article)
    assert "No points nominated; this is not an editorial rejection." in render_candidate(candidate, article)
    assert "[Unselected; source unit 1;" in render_source_coverage(candidate, article)


def test_provenance_retains_inspectable_source_metadata_and_extraction_limitations() -> None:
    article = source("Original source text.")
    candidate = parse(proposal(article), article)
    for rendered in (render_candidate(candidate, article), render_source_coverage(candidate, article)):
        assert "Source name (metadata): Example" in rendered
        assert "Title (metadata): Saved article" in rendered
        assert "Final source URL: https://example.com/article" in rendered
        assert f"Source snapshot: {checksum(asdict(article))}" in rendered
        assert "Source coverage note: Images not inspected." in rendered
