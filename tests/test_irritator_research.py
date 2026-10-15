"""Synthetic offline contracts for question-specific counter-evidence research.

Mock judgments test validation and carry, not a model's semantic accuracy.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, replace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from digest.adapters.models.execution import ModelExecution
from digest.domain.investigation.signals import Signal
from digest.irritator.documents import DocumentLink, DocumentSection, DocumentSnapshot, SourceRequestBudget
from digest.irritator.evidence_stage import (
    EvidenceIrritatorResult,
    GroundedRankingDecision,
    _acquire_research_evidence,
    _admit_ranking,
    _parse_narrative,
    _parse_rankings,
    _ranking_signal_payload,
    _search,
    eligible_delivered_result,
    run_evidence_irritator,
)
from digest.irritator.research import (
    DocumentAcquisition,
    QueryRoute,
    ResearchPlan,
    discovery_links,
    document_coverage,
    document_is_external,
    document_signals,
    document_supports_signal,
    parse_research_plan,
    signal_identity,
)
from scripts.review_fixture import fixture_config
from tests.factories import make_signal
from tests.test_irritator_evidence import _bundle, _narrative, _offline_client, delivered_input

LINK = DocumentLink("link-verified", "https://standards.example/protocol", "Protocol specification")
TARGET = "The managed service suits compatible clients; separate operators remain required."
BODY = (
    "Clients authenticate the intermediary. If the application requires direct target authentication, "
    "this protocol cannot preserve that policy. Other applications retain the stated privacy benefit."
)


def _plan(**changes: Any) -> dict[str, Any]:
    return {
        "question": "Which client authentication policies can the protocol preserve?",
        "evidence_needed": "The protocol's documented client authentication requirements.",
        "queries": [], "document_ids": [LINK.link_id], "read_query_index": None,
        "limitations": [], **changes,
    }


def _section(name: str, text: str, title: str = "Client authentication") -> DocumentSection:
    return DocumentSection(name, title, text, LINK.url + "#" + name, hashlib.sha256(text.encode()).hexdigest())


def _snapshot(*, text: str = BODY, sections: tuple[DocumentSection, ...] = (),
              links: tuple[DocumentLink, ...] = (), url: str = LINK.url) -> DocumentSnapshot:
    digest = hashlib.sha256(text.encode()).hexdigest()
    return DocumentSnapshot(url, url, "2026-10-15T00:00:00+00:00", digest, text, "complete",
                            links=links, sections=sections, text_sha256=digest, extraction_status="article")


def _target() -> tuple[Any, Any, dict[str, Any]]:
    bundle = _bundle(fixture_config())
    origin = delivered_input(bundle)
    card = origin.cards[0]
    origin = replace(origin, cards=(replace(card, canonical=replace(card.canonical, summary=TARGET)),))
    response = _narrative(bundle)
    response["narratives"][0].update(
        claim="The service works universally.", implicit_assumptions=[],
        target_card_id=card.card_id, delivered_quote={"field": "summary", "text": "The managed service"},
    )
    return bundle, origin, response


def _entry(signal: Signal, *, relation: str = "complicates", score: int = 8) -> dict[str, Any]:
    return {
        "url": signal.url, "score": score, "relation": relation,
        "finding": "The specification limits direct target authentication.",
        "applicability": "If the application requires that policy, this protocol is incompatible.",
        "material_change": "Check that requirement before choosing the managed service.",
        "target_quote": "compatible clients",
        "evidence_quote_ids": [_ranking_signal_payload(signal)["snippet"][0]["id"]],
    }


def test_document_only_plan_uses_a_verified_link_without_requiring_a_search() -> None:
    queries, plan, limitations = parse_research_plan(json.dumps(_plan()), [], 3, (LINK,))
    assert queries == [] and plan.routes == () and plan.document_ids == (LINK.link_id,)
    assert plan.read_query_index is None and limitations == []


@pytest.mark.parametrize("mutation", [
    "invented_link", "extra_field", "missing_field", "empty_unexplained", "blank_question",
    "duplicate_query", "unavailable_source", "duplicate_source", "empty_sources", "extra_query_field",
    "query_too_long", "too_many_queries", "boolean_read_index", "out_of_range_read_index",
    "competing_document_routes", "bad_limitation", "too_many_links", "not_an_object",
])
def test_research_plan_is_strict_and_does_not_invent_acquisition_authority(mutation: str) -> None:
    query = {"query": "protocol client authentication", "intent": "Find the relevant specification.",
             "sources": ["arxiv"]}
    payload = _plan(queries=[query], document_ids=[], read_query_index=0)
    if mutation == "invented_link":
        payload.update(document_ids=["https://invented.example/report"], read_query_index=None)
    elif mutation == "extra_field":
        payload["url"] = LINK.url
    elif mutation == "missing_field":
        del payload["evidence_needed"]
    elif mutation == "empty_unexplained":
        payload.update(queries=[], read_query_index=None)
    elif mutation == "blank_question":
        payload["question"] = " "
    elif mutation == "duplicate_query":
        payload["queries"].append({**query, "query": query["query"].upper()})
    elif mutation == "unavailable_source":
        query["sources"] = ["unconfigured"]
    elif mutation == "duplicate_source":
        query["sources"] = ["arxiv", "arxiv"]
    elif mutation == "empty_sources":
        query["sources"] = []
    elif mutation == "extra_query_field":
        query["url"] = LINK.url
    elif mutation == "query_too_long":
        query["query"] = "protocol " * 30
    elif mutation == "too_many_queries":
        payload["queries"] = [query] * 4
    elif mutation == "boolean_read_index":
        payload["read_query_index"] = True
    elif mutation == "out_of_range_read_index":
        payload["read_query_index"] = 1
    elif mutation == "competing_document_routes":
        payload["document_ids"] = [LINK.link_id]
    elif mutation == "bad_limitation":
        payload["limitations"] = [False]
    elif mutation == "too_many_links":
        payload.update(document_ids=[LINK.link_id] * 2, read_query_index=None)
    text = json.dumps([] if mutation == "not_an_object" else payload)
    with pytest.raises(ValueError):
        parse_research_plan(text, ["hackernews", "arxiv"], 3, (LINK,))


def test_absent_route_requires_an_explicit_evidence_gap() -> None:
    queries, plan, limitations = parse_research_plan(
        json.dumps(_plan(document_ids=[], limitations=["No applicable primary document was discovered."])),
        [], 3, (),
    )
    assert not queries and not plan.document_ids and limitations


async def test_search_dispatches_selected_routes_not_a_cartesian_fanout() -> None:
    payload = _plan(document_ids=[], queries=[
        {"query": "protocol client authentication", "intent": "Find a study.", "sources": ["arxiv"]},
        {"query": "protocol deployment", "intent": "Find original document leads.", "sources": ["devto"]},
    ])
    queries, plan, _ = parse_research_plan(json.dumps(payload), ["hackernews", "arxiv", "devto"], 3, ())
    result = EvidenceIrritatorResult(2, "synthetic", queries=queries, research_plan=plan)
    config, budget = fixture_config(), SourceRequestBudget()
    config.irritator.sources = ["hackernews", "arxiv", "devto"]

    async def search(*args: Any, **kwargs: Any) -> list[Signal]:
        kwargs["before_request"]()
        return []

    with (patch("digest.irritator.evidence_stage.search_hackernews", AsyncMock()) as hn,
          patch("digest.irritator.evidence_stage.search_arxiv", AsyncMock(side_effect=search)) as arxiv,
          patch("digest.irritator.evidence_stage.search_devto", AsyncMock(side_effect=search)) as devto):
        async with _offline_client() as client:
            assert await _search(result, config, client, budget=budget) == []
    hn.assert_not_awaited()
    assert arxiv.await_count == devto.await_count == 1
    assert arxiv.await_args.args[0] == queries[0].query and devto.await_args.args[0] == queries[1].query
    assert budget.requests_started == result.source_request_starts == 2


async def test_query_led_search_reserves_one_physical_start_for_the_document() -> None:
    payload = _plan(document_ids=[], read_query_index=0, queries=[
        {"query": "protocol client authentication", "intent": "Find primary document leads.",
         "sources": ["devto"]},
    ])
    queries, plan, _ = parse_research_plan(json.dumps(payload), ["devto"], 3, ())
    result = EvidenceIrritatorResult(2, "synthetic", queries=queries, research_plan=plan)
    config, budget = fixture_config(), SourceRequestBudget()
    config.irritator.sources = ["devto"]
    budget.consume()  # The original-document acquisition has already started.
    observed: list[int] = []

    async def search(*args: Any, **kwargs: Any) -> list[Signal]:
        for _ in range(9):
            kwargs["before_request"]()
            observed.append(budget.requests_started)
        raise AssertionError("The reserved document start must not be consumed by search.")

    with patch("digest.irritator.evidence_stage.search_devto", AsyncMock(side_effect=search)):
        async with _offline_client() as client:
            assert await _search(result, config, client, budget=budget) == []
    assert observed == list(range(2, 9)) and budget.requests_started == 8
    assert result.source_attempts[0].error == "SourceRequestBudgetExceeded"
    budget.consume()  # The promised external document can still start once.
    assert budget.requests_started == 9


def test_discovery_omits_whole_links_without_shortening_identity() -> None:
    snapshot = _snapshot(links=(replace(LINK, title="long " * 100), LINK))
    admitted = discovery_links(snapshot, max_chars=200)
    assert admitted == (LINK,) and admitted[0] is LINK


def test_section_priority_preserves_whole_text_including_terminal_qualification() -> None:
    other = _section("network", "Network operators route authentication packets.", "Routing")
    selected = _section("client", BODY + " Terminal qualification: this excludes only affected clients.")
    snapshot = _snapshot(sections=(other, selected))
    signals = document_signals(snapshot, "client authentication requirements", "Document")
    assert signals[0].url == selected.url and signals[0].snippet == selected.text
    assert signals[1].snippet == other.text
    assert document_signals(snapshot, "unrelated astronomy", "Document") == []
    assert document_signals(replace(snapshot, status="held"), "client authentication", "Document") == []
    assert _admit_ranking(signals, 5, 3, {}).signals[0].snippet.endswith("only affected clients.")
    huge = replace(signals[0], snippet=signals[0].snippet * 100)
    admission = _admit_ranking([huge, signals[1]], 5, 3, {})
    assert admission.signals == (signals[1],) and admission.audit.candidates[0].admission == "evidence_budget"


def test_document_binding_rejects_altered_sections_and_original_source_redirects() -> None:
    snapshot = _snapshot(sections=(_section("client", BODY),))
    signal = document_signals(snapshot, "client authentication", "Document")[0]
    acquired = DocumentAcquisition("external", "link", None, snapshot)
    assert document_supports_signal(signal, acquired)
    assert not document_supports_signal(replace(signal, snippet=BODY + " Invented caveat."), acquired)
    assert not document_supports_signal(signal, replace(acquired, role="origin"))
    assert not document_supports_signal(signal, replace(acquired, snapshot=replace(snapshot, content_sha256=None)))
    assert not document_supports_signal(signal, replace(acquired, snapshot=replace(
        snapshot, final_url="https://different.example/document",
    )))
    assert not document_supports_signal(signal, replace(acquired, snapshot=replace(snapshot, status="held")))
    assert document_is_external(snapshot, {"https://publisher.example/article"})
    assert not document_is_external(snapshot, {LINK.url + "#other-section"})
    assert not document_is_external(replace(snapshot, final_url="https://publisher.example/article#part"),
                                    {"https://publisher.example/article"})


def test_whole_canonical_target_keeps_qualification_despite_model_rewrite() -> None:
    bundle, origin, response = _target()
    original = asdict(bundle), asdict(origin), json.dumps(response)
    narrative = _parse_narrative(json.dumps(response), bundle, delivered=origin, whole_target=True)[0][0]
    assert narrative.claim == TARGET
    assert asdict(narrative)["delivered_quote"] == {"field": "summary", "text": TARGET}
    assert (asdict(bundle), asdict(origin), json.dumps(response)) == original


def test_grounded_fields_and_body_quotes_form_the_visible_explanation() -> None:
    bundle, origin, response = _target()
    narrative = _parse_narrative(json.dumps(response), bundle, delivered=origin, whole_target=True)[0][0]
    signal = make_signal(url=LINK.url, title="Appealing title alone proves nothing", snippet=BODY)
    entry = _entry(signal)
    admission = _admit_ranking([signal], 5, 3, {})
    ranked = _parse_rankings(json.dumps({"rankings": [entry], "limitations": []}),
                             admission, narrative, research=True)
    decision = ranked.audit.candidates[0].decision
    assert isinstance(decision, GroundedRankingDecision)
    assert (decision.finding, decision.applicability, decision.material_change) == (
        entry["finding"], entry["applicability"], entry["material_change"],
    )
    assert decision.reasoning == " ".join(entry[key] for key in ("finding", "applicability", "material_change"))
    assert ranked.ranked_signals[0].reasoning == decision.reasoning
    assert decision.quote in BODY and decision.quote != signal.title


@pytest.mark.parametrize("mutation", [
    "title_only", "invented_id", "cross_source_id", "duplicate_id", "missing_applicability",
    "empty_applicability", "empty_change", "empty_finding", "invented_target", "extra_field",
])
def test_invalid_grounded_judgments_do_not_partially_mutate_the_audit(mutation: str) -> None:
    bundle, origin, response = _target()
    narrative = _parse_narrative(json.dumps(response), bundle, delivered=origin, whole_target=True)[0][0]
    signal = make_signal(url=LINK.url, title="Claim", snippet=BODY)
    entry = _entry(signal)
    if mutation == "title_only":
        entry["evidence_quote_ids"] = [_ranking_signal_payload(signal)["title"][0]["id"]]
    elif mutation == "invented_id":
        entry["evidence_quote_ids"] = ["fabricated"]
    elif mutation == "cross_source_id":
        other = replace(signal, url="https://elsewhere.example/report")
        entry["evidence_quote_ids"] = [_ranking_signal_payload(other)["snippet"][0]["id"]]
    elif mutation == "duplicate_id":
        entry["evidence_quote_ids"] *= 2
    elif mutation == "missing_applicability":
        del entry["applicability"]
    elif mutation.startswith("empty_"):
        entry[{"empty_change": "material_change", "empty_finding": "finding",
               "empty_applicability": "applicability"}[mutation]] = " "
    elif mutation == "invented_target":
        entry["target_quote"] = "This service is universally compatible."
    else:
        entry["reasoning"] = "Unvalidated replacement explanation."
    admission = _admit_ranking([signal], 5, 3, {})
    before = asdict(admission.audit)
    with pytest.raises(ValueError):
        _parse_rankings(json.dumps({"rankings": [entry], "limitations": []}),
                        admission, narrative, research=True)
    assert asdict(admission.audit) == before


@pytest.mark.parametrize("relation", ["supports", "context", "insufficient"])
def test_negative_relations_remain_negative_even_with_quotes_and_high_score(relation: str) -> None:
    bundle, origin, response = _target()
    narrative = _parse_narrative(json.dumps(response), bundle, delivered=origin, whole_target=True)[0][0]
    signal = make_signal(url=LINK.url, snippet=BODY)
    admission = _admit_ranking([signal], 5, 3, {})
    ranking = _parse_rankings(json.dumps({"rankings": [_entry(signal, relation=relation, score=10)],
                                        "limitations": []}), admission, narrative, research=True)
    assert ranking.ranked_signals == [] and ranking.audit.candidates[0].disposition == "non_counter"


async def test_query_document_read_uses_only_selected_query_lineage() -> None:
    signals = [make_signal(url=f"https://external.example/{index}", source_name="devto") for index in range(2)]
    result = EvidenceIrritatorResult(2, "synthetic", research_plan=ResearchPlan(
        "Client authentication", "Original requirements", (QueryRoute(0, ("devto",)),), (), 0,
    ))
    lineage = {signal_identity(signals[0]): {1}, signal_identity(signals[1]): {0}}
    with patch("digest.irritator.evidence_stage.acquire_document", AsyncMock(return_value=_snapshot())) as read:
        acquired = await _acquire_research_evidence(result, signals, (), lineage, SourceRequestBudget(), set())
    assert read.await_count == 1 and read.await_args.args[0] == signals[1].url
    assert acquired and lineage[signal_identity(acquired[0])] == {0}


async def test_direct_document_path_keeps_three_models_two_reads_and_is_eligible() -> None:
    bundle, origin, narrative_response = _target()
    external = _snapshot(sections=(_section("client", BODY),))
    source = _snapshot(url=bundle.items[0].url, text="Original source context.")
    link_id = "link-" + hashlib.sha256(
        f"{source.final_url}\0{source.content_sha256}\0{LINK.url}\0{LINK.title}".encode(),
    ).hexdigest()[:24]
    source = replace(source, links=(replace(LINK, link_id=link_id),))
    signal = document_signals(external, "client authentication", "Specification")[0]
    model = AsyncMock(side_effect=[
        (json.dumps(narrative_response), {}), (json.dumps(_plan(document_ids=[link_id])), {}),
        (json.dumps({"rankings": [_entry(signal)], "limitations": []}), {}),
    ])
    reads: list[str] = []

    async def acquire(url: str, *, budget: SourceRequestBudget) -> DocumentSnapshot:
        reads.append(url)
        budget.consume()
        return source if len(reads) == 1 else external

    config = fixture_config()
    config.irritator.sources = []
    with (patch("digest.irritator.evidence_stage.complete", model),
          patch("digest.irritator.evidence_stage.acquire_document", side_effect=acquire),
          patch("digest.irritator.evidence_stage.search_hackernews", AsyncMock()) as hn,
          patch("digest.irritator.evidence_stage.search_arxiv", AsyncMock()) as arxiv,
          patch("digest.irritator.evidence_stage.search_devto", AsyncMock()) as devto):
        async with _offline_client() as client:
            result = await run_evidence_irritator(bundle, config, client, execution=ModelExecution(), delivered=origin)
    assert result.status == "complete", result.limitations
    assert model.await_count == 3 and reads == [bundle.items[0].url, LINK.url]
    assert result.source_request_starts == 2 and len(result.documents) == 2
    for search in (hn, arxiv, devto):
        search.assert_not_awaited()
    assert result.narratives[0].claim == TARGET and result.ranked_signals[0].signal.snippet == BODY
    assert result.queries == [] and result.research_plan is not None
    assert eligible_delivered_result(result, origin, bundle, None)
    original, acquired = result.documents
    altered_documents = {
        "missing_original": [acquired],
        "missing_external": [original],
        "third_read": [original, acquired, acquired],
        "unbound_original": [replace(original, snapshot=replace(source, requested_url="https://other.example/")),
                             acquired],
        "forged_link": [replace(original, snapshot=replace(source, links=(LINK,))), acquired],
        "missing_content_identity": [replace(original, snapshot=replace(source, content_sha256=None)), acquired],
        "changed_section": [original, replace(acquired, snapshot=replace(external, sections=(
            replace(external.sections[0], text=BODY + " Fabricated qualification."),
        )))],
        "wrong_section_hash": [original, replace(acquired, snapshot=replace(external, sections=(
            replace(external.sections[0], text_sha256="0" * 64),
        )))],
        "invented_external_request": [original, replace(acquired, snapshot=replace(
            external, requested_url="https://invented.example/report",
        ))],
        "redirect_to_original": [original, replace(acquired, snapshot=replace(
            external, final_url=bundle.items[0].url + "#apparently-external",
        ))],
    }
    for name, documents in altered_documents.items():
        assert not eligible_delivered_result(replace(result, documents=documents), origin, bundle, None), name
    assert not eligible_delivered_result(replace(result, source_request_starts=10), origin, bundle, None)
    changed_target = replace(result.narratives[0], claim="The managed service")
    assert not eligible_delivered_result(replace(result, narratives=[changed_target]), origin, bundle, None)


def test_coverage_metadata_admits_whole_notes_under_bound() -> None:
    snapshot = replace(_snapshot(), coverage_notes=("First complete warning.", "x" * 3000, "Last complete warning."))
    payload = document_coverage(snapshot)
    assert payload["coverage_notes"] == ["First complete warning.", "Last complete warning."]
    assert payload["omitted_coverage_notes"] == 1
    assert len(snapshot.coverage_notes[1]) == 3000


@pytest.mark.asyncio
@pytest.mark.parametrize("source_name", ["hackernews", "devto"])
async def test_held_document_cannot_promote_discovery_description(source_name: str) -> None:
    bundle, origin, narrative_response = _target()
    plan = _plan(document_ids=[], read_query_index=0, queries=[{
        "query": "client authentication", "intent": "Check client policies", "sources": [source_name],
    }])
    model = AsyncMock(side_effect=[(json.dumps(narrative_response), {}), (json.dumps(plan), {})])
    original = _snapshot(url=bundle.items[0].url)
    held = replace(_snapshot(), status="held", text="", text_sha256=None, error="unsupported_content_type")
    captures = iter((original, held))

    async def acquire(url: str, *, budget: SourceRequestBudget) -> DocumentSnapshot:
        budget.consume()
        return next(captures)

    async def discover(*args: Any, **kwargs: Any) -> list[Signal]:
        kwargs["before_request"]()
        return [Signal(LINK.url, "Promising discovery", BODY, source_name, "", 0)]

    config = fixture_config()
    config.irritator.sources = [source_name]
    with (patch("digest.irritator.evidence_stage.complete", model),
          patch("digest.irritator.evidence_stage.acquire_document", side_effect=acquire),
          patch("digest.irritator.evidence_stage.search_" + source_name, side_effect=discover)):
        async with _offline_client() as client:
            result = await run_evidence_irritator(bundle, config, client, execution=ModelExecution(), delivered=origin)
    assert result.status == "incomplete" and not result.ranked_signals
    assert model.await_count == 2 and result.source_request_starts == 3
    assert not eligible_delivered_result(result, origin, bundle, None)
    assert any("discovery descriptions were not admitted" in item for item in result.limitations)


def test_discovery_link_bound_counts_actual_json_separators() -> None:
    links = tuple(DocumentLink(f"link-{index}", f"https://public.example/{index}", "x" * 95) for index in range(40))
    chosen = discovery_links(_snapshot(links=links))
    def serialize(values: tuple[DocumentLink, ...]) -> str:
        return json.dumps(
            [{"id": link.link_id, "url": link.url, "title": link.title} for link in values], ensure_ascii=False,
        )
    assert len(serialize(links)) > 6000
    assert len(serialize(chosen)) <= 6000
    assert len(chosen) < len(links)
