"""Mechanical research-plan contract regression; no workers or acquisition."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from digest.domain.investigation.signals import Signal
from digest.irritator.documents import DocumentLink, DocumentSnapshot, SourceRequestBudget
from digest.irritator.evidence_stage import EvidenceIrritatorResult, _collect_research_evidence
from digest.irritator.research import PLAN_INSTRUCTION, parse_research_plan
from scripts.review_fixture import fixture_config


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


def test_plan_guidance_explains_the_single_consumable_document_route() -> None:
    for text in ('at most one external document acquisition, not one per query',
                 'sole slot before searches, even if the read is held, fails or repeats the original source',
                 'no later document selection or fallback read from HN/DEV results',
                 'only the declared read_query_index can supply the first eligible HN/DEV hit',
                 'no eligible hit means no document read',
                 'Other HN/DEV discovery results cannot enter evidence or guide another phase',
                 'arxiv abstracts can separately supply scoped reported findings',
                 'including alongside a selected link',
                 'Neither a selected route nor a successful read guarantees relevant evidence or an opposing result'):
        assert text in PLAN_INSTRUCTION


@pytest.mark.parametrize('linked', [False, True])
def test_mixed_routes_remain_parser_accepted_without_pruning(linked: bool) -> None:
    result = valid()
    link = DocumentLink('link-synthetic', 'https://evidence.example/report', 'Report')
    result.update(queries=[
        {'query': 'protocol measurements', 'intent': 'Find measurements.', 'sources': ['hackernews']},
        {'query': 'protocol study', 'intent': 'Find scoped research.', 'sources': ['arxiv', 'devto']},
    ], document_ids=[link.link_id] if linked else [], read_query_index=None if linked else 0)
    queries, plan, _ = parse_research_plan(json.dumps(result), ['hackernews', 'arxiv', 'devto'], 3, (link,))
    assert len(queries) == 2
    assert [route.sources for route in plan.routes] == [('hackernews',), ('arxiv', 'devto')]
    assert plan.document_ids == ((link.link_id,) if linked else ())
    assert plan.read_query_index == (None if linked else 0)


@pytest.mark.asyncio
async def test_link_read_precedes_search_without_fallback_but_abstract_remains() -> None:
    link = DocumentLink('link-synthetic', 'https://evidence.example/report', 'Report')
    payload = valid()
    payload.update(document_ids=[link.link_id], queries=[{
        'query': 'protocol measurements', 'intent': 'Find measurements.', 'sources': ['hackernews', 'arxiv'],
    }])
    queries, plan, _ = parse_research_plan(json.dumps(payload), ['hackernews', 'arxiv'], 3, (link,))
    result = EvidenceIrritatorResult(2, 'synthetic', queries=queries, research_plan=plan)
    held = DocumentSnapshot(link.url, link.url, '2026-01-01T00:00:00Z', 'a' * 64, '', 'held', 'synthetic_hold')
    lead = Signal('https://lead.example/report', 'Discovery lead', 'Not acquired evidence.', 'hackernews', '', 0)
    abstract = Signal('https://arxiv.org/abs/0000.00000', 'Study', 'Scoped reported finding.', 'arxiv', '', 0)
    events: list[str] = []

    async def acquire(url: str, *, budget: SourceRequestBudget) -> DocumentSnapshot:
        events.append('document')
        budget.consume()
        assert url == link.url
        return held

    async def search(*args: object, **kwargs: object) -> list[Signal]:
        events.append('search')
        return [lead, abstract]

    budget = SourceRequestBudget()
    with (patch('digest.irritator.evidence_stage.acquire_document', side_effect=acquire) as read,
          patch('digest.irritator.evidence_stage._search', side_effect=search),
          patch('digest.irritator.evidence_stage.complete', new_callable=AsyncMock) as model):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: pytest.fail('No HTTP'))) as client:
            signals, omitted = await _collect_research_evidence(
                result, fixture_config(), client, (link,), {}, budget, {'https://origin.example/article'},
            )
    assert events == ['document', 'search'] and read.call_count == 1
    assert signals == [abstract] and omitted == 1 and budget.requests_started == 1
    model.assert_not_awaited()


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
