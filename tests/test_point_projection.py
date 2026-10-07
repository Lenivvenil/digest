"""Offline duplicate projection preserves role and immutable source bindings."""
from __future__ import annotations

from dataclasses import asdict, replace

import pytest

from digest.reading_points import EditorialInterpretation, project_distinct_points
from tests.test_reading_points import parse, point, proposal, source


def test_projection_keeps_role_superset_and_original_order_without_mutating_raw() -> None:
    article = source("Mechanism.\nContext.\nCondition.\nOther finding.\n")
    candidate = parse(proposal(article, points=[point([1], [2]), point([4]), point([1], [2], [3])]), article)
    candidate = replace(candidate, editorial_interpretation=EditorialInterpretation("Unverified opinion", (1,)))
    before = asdict(candidate)
    result = project_distinct_points(candidate, article)
    assert result.redundant_points == ((1, 3),)
    assert result.candidate.points == (candidate.points[1], candidate.points[2])
    assert asdict(candidate) == before
    assert result.candidate.source_sha256 == candidate.source_sha256
    assert result.candidate.unit_view_version == candidate.unit_view_version
    assert result.candidate.editorial_interpretation == candidate.editorial_interpretation
    assert not project_distinct_points(result.candidate, article).redundant_points


@pytest.mark.parametrize("other", [
    point([2], [1], [3]),  # Same text union, different support.
    point([1, 2], [], [3]),  # Support expansion is not semantic equivalence.
    point([1], [3], [2]),  # Swapping context and qualification loses the relationship.
    point([1], [], [3]),  # Missing context cannot dominate.
    point([1], [2], []),  # Missing qualification cannot dominate.
])
def test_projection_never_infers_equivalence_from_union_or_loses_roles(other: dict[str, list[int]]) -> None:
    article = source("Mechanism.\nContext.\nCondition.\n")
    original = point([1], [2], [3])
    candidate = parse(proposal(article, points=[original, other]), article)
    result = project_distinct_points(candidate, article)
    assert candidate.points[0] in result.candidate.points
    if other in (point([1], [], [3]), point([1], [2], [])):
        assert result.redundant_points == ((2, 1),)
    else:
        assert result.candidate == candidate and not result.redundant_points


def test_dominance_chain_maps_directly_to_a_retained_original_point() -> None:
    article = source("Mechanism.\nContext.\nCondition.\nOther context.\n")
    candidate = parse(proposal(article, points=[
        point([1]), point([1], [2]), point([1], [2], [3]), point([1], [4], [3]),
    ]), article)
    result = project_distinct_points(candidate, article)
    assert result.redundant_points == ((1, 3), (2, 3))
    assert result.candidate.points == candidate.points[2:]


def test_valid_manual_support_order_does_not_change_role_set_comparison() -> None:
    article = source("First finding.\nSecond finding.\nContext.\nCondition.\n")
    candidate = parse(proposal(article, points=[point([1, 2], [3]), point([1, 2], [3], [4])]), article)
    candidate = replace(candidate, points=(replace(candidate.points[0], supporting_unit_ids=(2, 1)),
                                           candidate.points[1]))
    result = project_distinct_points(candidate, article)
    assert result.redundant_points == ((1, 2),)
    assert result.candidate.points == (candidate.points[1],)
    assert candidate.points[0].supporting_unit_ids == (2, 1)


def test_invalid_source_version_unknown_ids_and_duplicate_groups_still_reject() -> None:
    article = source("Mechanism.\nContext.\n")
    candidate = parse(proposal(article), article)
    for invalid in (
        replace(candidate, source_sha256="0" * 64),
        replace(candidate, unit_view_version="invented"),
        replace(candidate, points=(replace(candidate.points[0], supporting_unit_ids=(999,)),)),
        replace(candidate, points=candidate.points * 2),
    ):
        with pytest.raises(ValueError):
            project_distinct_points(invalid, article)
    with pytest.raises(ValueError):
        project_distinct_points(candidate, source("Different source.\n"))


def test_empty_candidate_stays_empty_without_editorial_verdict() -> None:
    article = source("Mechanism.\n")
    candidate = parse(proposal(article, points=[]), article)
    assert project_distinct_points(candidate, article).candidate == candidate
