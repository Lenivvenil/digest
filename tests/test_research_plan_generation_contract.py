"""Mechanical research-plan contract regression; no workers or acquisition."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from digest.irritator.documents import DocumentLink
from digest.irritator.research import PLAN_INSTRUCTION, parse_research_plan


def valid() -> dict[str, object]:
    return {'question': 'What evidence distinguishes the possible answers?',
            'evidence_needed': 'One factual account with scope and limitations.',
            'queries': [], 'document_ids': [], 'read_query_index': None,
            'limitations': ['No suitable route supplied.']}


def test_contract_names_scalar_types_and_every_text_limit() -> None:
    for text in ('question and evidence_needed are each one nonempty JSON string of at most 1000 characters',
                 'never an array or object', 'complete response must not exceed 16000 characters',
                 'intent is a nonempty string of at most 1000 characters',
                 'query is a nonempty string of at most 200 characters',
                 'array of distinct allowed_sources strings', 'zero-based integer (not a boolean)',
                 'limitations contains at most five nonempty strings',
                 'each at most 1000 characters'):
        assert text in PLAN_INSTRUCTION


@pytest.mark.parametrize('field', ['question', 'evidence_needed'])
@pytest.mark.parametrize('value', [[], ['a'], {}, 1, True, None, '', ' ', 'x' * 1001])
def test_scalar_contract_remains_strict_no_coercion(field: str, value: object) -> None:
    result = valid()
    result[field] = value
    with pytest.raises(ValueError, match='Invalid research plan text'):
        parse_research_plan(json.dumps(result), ['arxiv'], 3, ())


def test_exact_string_boundary_and_conforming_control() -> None:
    result = valid()
    result['question'] = result['evidence_needed'] = 'x' * 1000
    _, plan, limitations = parse_research_plan(json.dumps(result), ['arxiv'], 3, ())
    assert plan.evidence_needed == 'x' * 1000 and limitations


def test_actual_immutable_array_failure_remains_rejected() -> None:
    supplied = os.environ.get('ASTRA08_RESPONSE')
    if not supplied:
        pytest.skip('Supply actual retained response explicitly; no synthetic substitute for causal evidence.')
    path = Path(supplied)
    raw = path.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == '33285f8ae5e10116435b37ed109543e49d5fa38c8088b94cfdfd51feb51655c4'
    result = json.loads(raw)['result']
    assert isinstance(result['evidence_needed'], list)
    links = tuple(DocumentLink(item, 'https://example.invalid/causal-test', 'Retained identity only')
                  for item in result['document_ids'])
    with pytest.raises(ValueError, match='Invalid research plan text'):
        parse_research_plan(json.dumps(result), ['hackernews', 'arxiv', 'devto'], 3, links)
    assert path.read_bytes() == raw  # Never repair or retry the consumed actual response.
