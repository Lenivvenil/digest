"""Small shared lexical query contract, before source-specific serialization."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

QUERY_CONTRACT = (
    'The query must be 1–8 search words (at most 200 characters), not a sentence or '
    'research instruction. Prefer 2–4 core topic/entity terms or one exact entity phrase. '
    'Search for relevant material without requiring a desired conclusion in the query. '
    'Keep the counter-hypothesis and why it matters in intent; ranking assesses the actual relation. '
    'Use different topic angles across queries, not longer conjunctions of all narrative details. '
    'Double quotes group an exact phrase and its words still count toward eight. '
    'Use no Boolean operators, field prefixes, parentheses, exclusions or wildcards. '
    'Keep technical punctuation within words (for example GPT-4 or C++).'
)
GROUNDED_QUERY_CONTRACT = (
    'Use the supplied evidence and qualification_context to choose useful topic/entity searches '
    'within the existing query slots. Queries may paraphrase or combine relevant source terms; '
    'no query has to copy a literal source phrase. Prefer English searches, retaining source-language '
    'names or phrases when useful. Keep exploratory hypotheses and why they matter in intent, '
    'distinct from what the source actually claims. Preserve source scope and qualifications; '
    'do not turn a possible outcome into an asserted source fact or require a desired conclusion. '
    'A literal source match alone does not establish neutrality, relevance or retrieval usefulness.'
)
QUERY_ERROR = "Invalid lexical query contract."
_WORD = r"[^\W_][\w.+/#'’\-‑]*"
_ATOM = re.compile(rf'(?:"{_WORD}(?: +{_WORD})*"|{_WORD})', re.UNICODE)
_WORD_CONTINUATION = r"[\w.+/#\-‑]"
# Surrounding quotes delimit prose; an apostrophe within a word does not.
_WORD_START_BOUNDARY = rf"(?<!{_WORD_CONTINUATION})(?<!{_WORD_CONTINUATION}['’])"
# A terminal period ends prose; dots followed by a word still join domains/versions.
_WORD_END_CONTINUATION = r"(?:[\w+/#\-‑]|\.+\w|['’]\w)"


@dataclass(frozen=True)
class SourceQueryAnchor:
    """Literal provenance of a whole query, not a neutrality/usefulness judgment."""

    query_index: int
    query: str
    evidence_bundle_id: str
    evidence_id: str
    field: Literal["title", "excerpt"]
    start: int
    end: int
    matched_text: str


def lexical_atoms(query: str) -> tuple[str, ...]:
    """Preserve words/quoted phrases; reject rather than truncate or infer intent.

    This validates syntax and size only. A short sentence can still pass; relevance
    and useful keyword choice remain model responsibilities.
    """
    if not isinstance(query, str) or not query or len(query) > 200 or query != query.strip():
        raise ValueError(QUERY_ERROR)
    atoms: list[str] = []
    position = 0
    while position < len(query):
        match = _ATOM.match(query, position)
        if match is None:
            raise ValueError(QUERY_ERROR)
        atom = match.group()
        if atom.upper() in {"AND", "OR", "NOT", "ANDNOT"}:
            raise ValueError(QUERY_ERROR)
        atoms.append(atom)
        position = match.end()
        if position == len(query):
            break
        if query[position] != " ":
            raise ValueError(QUERY_ERROR)
        position += 1
    if not atoms or sum(len(atom.strip('"').split()) for atom in atoms) > 8:
        raise ValueError(QUERY_ERROR)
    return tuple(atoms)


def find_source_anchor(queries: list[str], evidence: dict[str, Any]) -> SourceQueryAnchor | None:
    """Find a whole lexical query in one field of already-validated source evidence.

    Inspect only the supplied items and qualification context, in that order. Query
    order wins, followed by evidence order, then title before excerpt. Syntax errors
    still raise; an empty query list simply provides no proof. No input is repaired.
    """
    phrases = [
        r"\s+".join(re.escape(word) for atom in lexical_atoms(query) for word in atom.strip('"').split())
        for query in queries
    ]
    items = [*evidence.get("items", []), *evidence.get("qualification_context", [])]
    fields: tuple[Literal["title", "excerpt"], ...] = ("title", "excerpt")
    for query_index, phrase in enumerate(phrases):
        pattern = re.compile(rf"{_WORD_START_BOUNDARY}{phrase}(?!{_WORD_END_CONTINUATION})", re.IGNORECASE)
        for item in items:
            for field in fields:
                source_text = item.get(field, "")
                match = pattern.search(source_text)
                if match is not None:
                    return SourceQueryAnchor(
                        query_index=query_index,
                        query=queries[query_index],
                        evidence_bundle_id=evidence["bundle_id"],
                        evidence_id=item["evidence_id"],
                        field=field,
                        start=match.start(),
                        end=match.end(),
                        matched_text=source_text[match.start():match.end()],
                    )
    return None
