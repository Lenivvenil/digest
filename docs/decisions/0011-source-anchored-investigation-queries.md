# 0011. Retain a source-anchored topic query within bounded investigation

Status: proposed; local implementation under review

Refs [#77](https://github.com/Lenivvenil/digest/issues/77),
[the Irritator domain](../domain/irritator/overview.md), and
[ADR0009](0009-selected-source-admission.md).

## Context and existing purpose

Irritator seeks meaningful external contradictions and qualifications, not a forced
opposite opinion. The query contract already asks for topic/entity terms without
requiring a desired conclusion. A preserved ordinary run nevertheless used only
queries conditioned on cancellation, controversy or criticism; all available searches
were empty and ranking never ran. Syntax validity did not establish useful retrieval.

## Proposed decision

Within the existing maximum of three queries, ask for at least one useful neutral
topic/entity phrase grounded in the actual supplied source. Keep the model's existing
query/intent JSON schema. Derive proof in code that one WHOLE query matches a contiguous
phrase within one supplied title or excerpt. Search quote delimiters, case and whitespace
may differ; recorded offsets and matched text always refer to the original source field.
Technical-word boundaries prevent prefix fragments from posing as full source terms.
No title/excerpt stitching, cross-article joins, synonym repair or replacement query is
performed. The anchor may remain in the source language; other slots may explore
English hypotheses under the existing query contract.

The proof records the query index/text, actual evidence bundle and item IDs, field,
offsets and exact source slice. In full-source mode, only the cited passages and their
already-bound qualification context are eligible. The proof refers to that full-source
bundle, not the outer RSS bundle. It is a source-presence floor: negative language can
be present in the source, and a generic phrase can match. Neither neutrality, relevance
nor retrieval usefulness is certified. No keyword blacklist or extra minimum length
is introduced.

A nonempty generated set without an anchor is technical incomplete before source I/O.
Retain its queries for diagnosis; do not report that no external evidence exists or
synthesize a replacement. Existing empty-query handling remains unchanged. Successful
anchoring does not relax source validation or semantic ranking acceptance.

## Compatibility and rollout scope

This changes BOTH bounded RSS and full-source Irritator query generation/validation.
It is not gated by reading_brief.enabled. Production remains unchanged while the draft
engine is undeployed; a later rollout would affect RSS investigation even with source
reading disabled. The standalone legacy query generator remains unchanged.

SearchQuery and model response fields stay unchanged. Results add optional derived
query_anchor metadata; absence in older archives means no recorded proof, never a
retroactively inferred anchor. Original requests, responses and queries are not rewritten.
The existing query/model counts, source adapters, deadlines and shared request allowance
are unchanged. Primary preparation and frozen sending remain independent.

## Verification and remaining acceptance

Offline cases cover a whole verdict-seeking query set holding, one literal anchor plus
exploratory queries, uncited-source rejection, actual full-source bundle binding,
quoted/case/source-language variants, technical-word boundaries and genuine source
negative terms. The saved failure is preserved privately; public fixtures are synthetic.
Actual model compliance, search recall and useful sourced relations remain empirical
acceptance gates. No automatic ranking success or runtime activation follows from this
proposed contract.
