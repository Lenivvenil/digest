"""Bounded, question-specific investigation plans and complete document evidence.

These rules bind acquisition and preserve evidence. They do not certify a model's
interpretation or infer the reader's beliefs from their reading list.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urldefrag

from digest._serialization import extract_json
from digest.domain.investigation.queries import SearchQuery
from digest.domain.investigation.signals import Signal
from digest.irritator.documents import DocumentLink, DocumentSnapshot
from digest.irritator.query_contract import lexical_atoms

PURPOSE = (
    "Choose the most consequential reading-derived claim, decision or interpretation worth testing against "
    "external evidence. The purpose is to challenge the picture created by this reading, not to dispute a "
    "random announcement or collect unrelated interesting news. Prefer a question whose answer could "
    "materially change an interpretation or practical decision. Existing caveats and trivial details are "
    "not new counter-evidence. Do not invent the reader's beliefs, needs or consensus. Keep a publisher's "
    "assertion distinct from the digest's interpretation. Label possible implications and counterhypotheses "
    "as unverified questions in implicit_assumptions; explain the consequential question in "
    "why_worth_challenging. Never strengthen 'could' into 'will', availability into effectiveness, or an "
    "announcement into universal benefit. No worthwhile supported target is a valid empty outcome. "
)

PLAN_INSTRUCTION = (
    "Plan one meaningful external investigation of the exact reading-derived target. Exploratory hypotheses "
    "are unverified questions, never attributed facts or the reader's beliefs. State a neutral question and "
    "the evidence needed to distinguish the possible answers. Choose sources suited to that evidence: "
    "arxiv supplies research abstracts; hackernews and devto supply discovery leads, never counter-evidence "
    "by themselves. Use read_query_index to acquire a returned HN/DEV document if no linked document is selected. "
    "Product "
    "terms, standards or regulatory questions need the relevant document, not generic developer posts. "
    "Return exactly question, evidence_needed, queries, document_ids, read_query_index, limitations. "
    "Use one JSON object; the complete response must not exceed 16000 characters. "
    "question and evidence_needed are each one nonempty JSON string of at most 1000 characters, "
    "never an array or object; combine multiple evidence needs in that single string. "
    "queries, document_ids and limitations are JSON arrays, including when empty. "
    "Every query entry has exactly query, intent, sources: query is a nonempty string of at most "
    "200 characters; intent is a nonempty string of at most 1000 characters; sources is a nonempty "
    "array of distinct allowed_sources strings. Queries must be distinct ignoring case after trimming. "
    "document_ids contains at most one string matching a discovered_links ID. "
    "read_query_index is null or a zero-based integer (not a boolean) within queries, and must be "
    "null when document_ids is nonempty. limitations contains at most five nonempty strings, "
    "each at most 1000 characters. All text strings must contain non-whitespace text. "
    "queries is at most max_queries objects with query (<=200 characters), intent, sources (a nonempty "
    "subset of allowed_sources). Do not fan every question to every source. document_ids is zero or one "
    "exact ID from discovered_links; never invent a URL. A document-only plan is allowed. Alternatively "
    "read_query_index may select one query whose first eligible returned document should be acquired; "
    "otherwise null. Do not combine that with document_ids. Discovery titles and URLs are leads, not "
    "evidence of the proposed relation or independent authority. Use neutral topic/entity searches; "
    "do not require a desired negative answer. If no available route can answer, return no queries or "
    "document_ids with a limitation. limitations is at most five short strings. "
)

RANK_INSTRUCTION = (
    "Evaluate external evidence against both the immutable target and the explicitly labelled research "
    "question. Preserve their difference. Return rankings (at most max_ranked) and limitations. Each "
    "ranking has exactly url, score (integer1-10), relation (contradicts, complicates, supports, context, "
    "insufficient), finding, applicability, material_change, target_quote, evidence_quote_ids. "
    "target_quote is an exact nonempty substring of narrative.claim. evidence_quote_ids selects one to "
    "three exact IDs from that URL's snippet/body segments, never its title alone. finding states what "
    "the external source actually establishes; applicability states the evidenced mechanism/conditions "
    "linking it to the target, distinguishing conditional relevance from known applicability; "
    "material_change states what substantial reading-derived conclusion or decision changes, or explains "
    "why none does. These three fields form the published explanation. A missing applicability bridge, "
    "mere topic overlap, repeated known qualification or trivial detail is not a counter-signal. "
    "Do not force an opposing answer. A body quotation and a score do not establish truth. Abstracts "
    "establish only their scoped reported findings; HN/DEV descriptions are discovery only and cannot be ranked. "
    "Selected sections do not establish whole-document "
    "coverage. Do not claim an independent evaluation from a publisher announcement or republished claim. "
    "Never assume an application has a prerequisite that was not supplied: state 'if' explicitly. "
    "When original_document_context is null, qualifications outside the saved excerpts are unknown. "
    "Never claim the original author omitted or ignored a caveat, or that it is new to the whole article. "
    "State implications for the exact delivered target only; novelty beyond supplied context is unverified. "
    "9-10 is strong contradiction, 7-8 a substantial applicable qualification, 5-6 a limited supported "
    "qualification, 1-4 weak relevance. Use the requested language for finding, applicability, material_change "
    "and limitations; never translate the literal target_quote. Explain empty rankings; at most five limitations. "
)


@dataclass(frozen=True)
class QueryRoute:
    query_index: int
    sources: tuple[str, ...]


@dataclass(frozen=True)
class ResearchPlan:
    question: str
    evidence_needed: str
    routes: tuple[QueryRoute, ...]
    document_ids: tuple[str, ...]
    read_query_index: int | None


@dataclass(frozen=True)
class DocumentAcquisition:
    role: Literal["origin", "external"]
    selection: Literal["origin", "link", "query"]
    query_index: int | None
    snapshot: DocumentSnapshot
    discovery_signal: Signal | None = None


def _text(value: Any, limit: int = 1000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError("Invalid research plan text.")
    return value.strip()


def parse_research_plan(
    text: str, sources: list[str], maximum: int, links: tuple[DocumentLink, ...],
) -> tuple[list[SearchQuery], ResearchPlan, list[str]]:
    if len(text) > 16000:
        raise ValueError("Research plan exceeds the response budget.")
    raw = extract_json(text)
    if not isinstance(raw, dict) or set(raw) != {
        "question", "evidence_needed", "queries", "document_ids", "read_query_index", "limitations",
    }:
        raise ValueError("Invalid research plan fields.")
    entries, ids, limitations = raw["queries"], raw["document_ids"], raw["limitations"]
    if (not isinstance(entries, list) or len(entries) > maximum
            or not isinstance(ids, list) or len(ids) > 1
            or any(not isinstance(item, str) or item not in {link.link_id for link in links} for item in ids)
            or not isinstance(limitations, list) or len(limitations) > 5):
        raise ValueError("Invalid research plan count or document identity.")
    queries, routes = [], []
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict) or set(entry) != {"query", "intent", "sources"}:
            raise ValueError("Invalid routed query fields.")
        query, selected = _text(entry["query"], 200), entry["sources"]
        lexical_atoms(query)
        if (query.casefold() in seen or not isinstance(selected, list) or not selected
                or any(not isinstance(item, str) or item not in sources for item in selected)
                or len(set(selected)) != len(selected)):
            raise ValueError("Invalid routed query sources.")
        seen.add(query.casefold())
        queries.append(SearchQuery(query, _text(entry["intent"])))
        routes.append(QueryRoute(index, tuple(source for source in sources if source in selected)))
    read_index = raw["read_query_index"]
    if read_index is not None and (
        type(read_index) is not int or not 0 <= read_index < len(queries) or ids
    ):
        raise ValueError("Invalid research document selection.")
    limitations = [_text(item) for item in limitations]
    if not queries and not ids and not limitations:
        raise ValueError("An empty research plan requires an explanation.")
    return queries, ResearchPlan(
        _text(raw["question"]), _text(raw["evidence_needed"]), tuple(routes), tuple(ids), read_index,
    ), limitations


def discovery_links(snapshot: DocumentSnapshot, max_chars: int = 6000) -> tuple[DocumentLink, ...]:
    """Admit complete locators, never shortened or model-generated URLs."""
    accepted: list[DocumentLink] = []
    payload: list[dict[str, str]] = []
    for link in snapshot.links:
        entry = {"id": link.link_id, "url": link.url, "title": link.title}
        trial = [*payload, entry]
        if len(json.dumps(trial, ensure_ascii=False)) <= max_chars:
            accepted.append(link)
            payload = trial
    return tuple(accepted)


_STOP = frozenset(
    "the and for with that this from what which where when have into would could should about their "
    "a an of to in is it on as by or be are does can how".split()
)


def _terms(text: str) -> set[str]:
    return {term for term in re.findall(r"[a-z][a-z0-9-]{2,}", text.lower()) if term not in _STOP}


def document_signals(snapshot: DocumentSnapshot, question: str, title: str) -> list[Signal]:
    """Prefer whole natural sections by transparent lexical fit, never prefixes.

    Selection is a bounded retrieval heuristic, not evidence of completeness or
    semantic applicability. The actual ranking-packet owner still admits units.
    """
    if snapshot.status != "complete" or snapshot.final_url is None:
        return []
    sections = snapshot.sections
    if not sections:
        return [Signal(snapshot.final_url, title, snapshot.text, "document", "", 0.0)] if snapshot.text else []
    wanted = _terms(question)
    frequencies = Counter(term for section in sections for term in _terms(section.text))

    def relevance(index: int) -> tuple[float, int]:
        section = sections[index]
        score = sum(1 / frequencies[term] for term in wanted & _terms(section.text))
        score += sum(2 / frequencies.get(term, 1) for term in wanted & _terms(section.title))
        return -score, index

    chosen = [index for index in sorted(range(len(sections)), key=relevance) if relevance(index)[0] < 0][:6]
    return [Signal(sections[index].url, sections[index].title, sections[index].text, "document", "", 0.0)
            for index in chosen]


def signal_identity(signal: Signal) -> str:
    from dataclasses import asdict

    return hashlib.sha256(json.dumps(asdict(signal), ensure_ascii=True, sort_keys=True).encode()).hexdigest()


def document_supports_signal(signal: Signal, acquisition: DocumentAcquisition) -> bool:
    snapshot = acquisition.snapshot
    if (acquisition.role != "external" or snapshot.status != "complete" or signal.source_name != "document"
            or not snapshot.final_url or not snapshot.content_sha256):
        return False
    if snapshot.sections:
        return any(
            signal.url == section.url == urldefrag(snapshot.final_url)[0] + "#" + section.section_id
            and signal.title == section.title and signal.snippet == section.text
            and hashlib.sha256(section.text.encode()).hexdigest() == section.text_sha256
            for section in snapshot.sections
        )
    return (signal.url == snapshot.final_url and signal.snippet == snapshot.text and bool(signal.snippet)
            and hashlib.sha256(snapshot.text.encode()).hexdigest() == snapshot.text_sha256)


def document_is_external(snapshot: DocumentSnapshot, source_urls: set[str]) -> bool:
    """Compare both sides of redirects; fragment sections do not create a new source."""
    excluded = {urldefrag(url)[0] for url in source_urls}
    return bool(snapshot.final_url) and all(
        urldefrag(url)[0] not in excluded for url in (snapshot.requested_url, snapshot.final_url or "")
    )


def valid_document_lineage(
    acquisition: DocumentAcquisition, plan: ResearchPlan, origin: DocumentAcquisition,
    queries: list[SearchQuery],
) -> bool:
    """Check recorded discovery identities without inventing a search occurrence."""
    if acquisition.role != "external" or origin.role != "origin" or origin.selection != "origin":
        return False
    if acquisition.selection == "link":
        if len(plan.document_ids) != 1 or acquisition.query_index is not None:
            return False
        for link in discovery_links(origin.snapshot):
            identity = hashlib.sha256(
                f"{origin.snapshot.final_url}\0{origin.snapshot.content_sha256}\0{link.url}\0{link.title}".encode()
            ).hexdigest()[:24]
            if (link.link_id == plan.document_ids[0] == "link-" + identity
                    and origin.snapshot.final_url and origin.snapshot.content_sha256
                    and acquisition.snapshot.requested_url == link.url
                    and acquisition.discovery_signal is None):
                return True
        return False
    index, signal = acquisition.query_index, acquisition.discovery_signal
    return (acquisition.selection == "query" and not plan.document_ids
            and type(index) is int and index == plan.read_query_index and 0 <= index < len(queries)
            and signal is not None and signal.source_name != "arxiv"
            and acquisition.snapshot.requested_url == signal.url
            and any(route.query_index == index and signal.source_name in route.sources for route in plan.routes))


def document_coverage(snapshot: DocumentSnapshot, max_note_chars: int = 2000) -> dict[str, Any]:
    """Bound whole diagnostic strings; the private snapshot keeps every omission."""
    notes: list[str] = []
    for note in snapshot.coverage_notes:
        if len(json.dumps([*notes, note], ensure_ascii=False)) <= max_note_chars:
            notes.append(note)
    return {
        "url": snapshot.final_url, "extraction_status": snapshot.extraction_status,
        "complete_sections": len(snapshot.sections), "coverage_notes": notes,
        "omitted_coverage_notes": len(snapshot.coverage_notes) - len(notes),
    }
