"""Small shared lexical query contract, before source-specific serialization."""

from __future__ import annotations

import re

QUERY_CONTRACT = (
    'The query must be 1–8 search words (at most 200 characters), not a sentence or '
    'research instruction. Use topic and caveat keywords; put the explanation in intent. '
    'Double quotes group an exact phrase and its words still count toward eight. '
    'Use no Boolean operators, field prefixes, parentheses, exclusions or wildcards. '
    'Keep technical punctuation within words (for example GPT-4 or C++).'
)
QUERY_ERROR = "Invalid lexical query contract."
_WORD = r"[^\W_][\w.+/#'’\-‑]*"
_ATOM = re.compile(rf'(?:"{_WORD}(?: +{_WORD})*"|{_WORD})', re.UNICODE)


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
