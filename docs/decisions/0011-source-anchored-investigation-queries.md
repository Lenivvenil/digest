# 0011. Ground bounded investigation queries with optional literal provenance

Status: revised optional-provenance decision accepted for the engine-only release
on 2026-10-05, following owner approval to merge PR #107 and update digest-prod.
Full-source reading stays disabled; its activation and ADR0009/0010 are not accepted.
The earlier mandatory-anchor proposal was implemented in draft PR #107 but was never
accepted or deployed; it is superseded by this revision.

Refs [#77](https://github.com/Lenivvenil/digest/issues/77),
[the Irritator domain](../domain/irritator/overview.md), and
[ADR0009](0009-selected-source-admission.md).

## 2026-10-09 delivered-card contract (#208)

The compact post-delivery path freezes the exact confirmed and applied origin
edition before creating its attempt. Attempt schema 3 binds ready, claim, receipts,
publication day, original review bytes, canonical and presented cards, and original
source occurrences. It includes every actually delivered card, including an admitted
closer; a selected but omitted closer is not a target. Canonical source identity is
preserved while presented summaries may contain translation and source credit.
The original evidence bundle and hash remain unchanged.

Only this target-bound mode requests `target_card_id` and `delivered_quote` (an exact
nonempty substring of the canonical `title` or `summary` of an actually delivered
card) in the existing narrative
call. The original-source quotation must belong to that same occurrence. Cross-card
or field stitching, paraphrases and typography repairs cannot establish this binding.
No supported assertion means no target. Literal matching proves provenance, not
semantic entailment or factual correctness.

The canonical field is not necessarily the literal translated Telegram wording.
Exact presentation bytes and hashes prove publication association; translation and
appended source credits remain outside analysis inputs, preserving ADR0005.

The target and quotation remain in existing query and ranking contexts, together
with source qualifications. Target-bound narratives allow zero inferred assumptions;
speculative hypotheses and model motivation are excluded from query inputs.
Topic/entity search, lexical validation, optional literal-anchor diagnostics and
existing ranking rules remain. Unbound standalone and full-source callers retain
their previous response/query contracts and cannot imply delivered-fragment eligibility.
Old compact attempt schemas are held unchanged, not reinterpreted or rerun.

No additional model call, source fetch, provider, retry, job or budget is introduced.
The same one-narrative, three-query, nine-source-request maximum and 180-second
investigation deadline apply. Existing optional translation and full-source availability
are unchanged; this does not activate full-source reading. Semantic translation errors,
including changing sharing to use, remain an ordinary editorial acceptance gate.

## Context and existing purpose

Irritator seeks meaningful external contradictions and qualifications, not a forced
opposite opinion. The query contract already asks for topic/entity terms without
requiring a desired conclusion. A preserved ordinary run nevertheless used only
queries conditioned on cancellation, controversy or criticism; all available searches
were empty and ranking never ran. Syntax validity did not establish useful retrieval.

## Superseded initial proposal (unaccepted)

The initial proposal required at least one whole query to match a contiguous phrase
in supplied source evidence, within the existing maximum of three queries. Its rationale
was a source-presence floor after the verdict-conditioned searches above. A nonempty
set without a match retained its queries but stopped as technical incomplete before
source I/O with `MissingSourceQueryAnchor`; no replacement query was synthesized.

The owner declined adopting that mandatory literal-match gate. This revises an
unaccepted proposal, not a previously accepted policy. Literal copying is not necessary
for a useful grounded search, and a match by itself does not establish neutrality,
relevance or useful retrieval.

## Revised accepted decision

Ask for useful topic/entity searches grounded in the supplied evidence and its
qualification context, without requiring a copied phrase. Queries may paraphrase or
combine relevant source terms. Keep exploratory hypotheses and reasons in `intent`,
distinct from source claims; preserve the source's scope and qualifications. Keep the
existing query/intent JSON schema, lexical validation and maximum of three queries.

Retain `find_source_anchor` as optional derived diagnostic metadata. When one WHOLE
query matches a contiguous phrase within one supplied title or excerpt, record the
match. Search quote delimiters, case and whitespace may differ; recorded offsets and
matched text always refer to the original source field.
Technical-word boundaries prevent prefix fragments from posing as full source terms.
The matcher performs no title/excerpt stitching, cross-article joins, synonym repair
or replacement query. Searches preferably use English, retaining source-language names
or phrases when useful. Generated queries are searched unchanged after validation.

The proof records the query index/text, actual evidence bundle and item IDs, field,
offsets and exact source slice. In full-source mode, only the cited passages and their
already-bound qualification context are eligible. The proof refers to that full-source
bundle, not the outer RSS bundle. Negative language can be present in the source, and
a generic phrase can match. Neither neutrality, relevance nor retrieval usefulness is
certified. No keyword blacklist or extra minimum length is introduced.

A nonempty, valid generated set without an anchor proceeds to the existing bounded
search. Missing literal provenance is diagnostic absence only, never an error or
technical incomplete by itself. Existing empty-query handling remains unchanged.
Neither presence nor absence of an anchor relaxes source validation, self-source
exclusion or semantic ranking acceptance.

## Compatibility and rollout scope

This accepted revision permits bounded RSS and full-source Irritator execution without
a mandatory literal-match gate. The deployed RSS behavior is independent of
`reading_brief.enabled`; full-source reading remains disabled. The standalone legacy
query generator remains unchanged.

SearchQuery and model response fields stay unchanged. Results add optional derived
query_anchor metadata; absence in older archives means no recorded proof, never a
retroactively inferred anchor. Original requests, responses and queries are not rewritten.
The existing query/model counts, source adapters, deadlines and shared request allowance
are unchanged. Primary preparation and frozen sending remain independent.

## Verification and remaining acceptance

Offline cases cover the formerly held three-query set reaching mocked search without
an anchor in RSS and full-source modes, unchanged schema/lexical rejection, one literal
anchor plus exploratory queries, uncited evidence excluded from anchor metadata,
actual full-source bundle binding, quoted/case/source-language variants, technical-word
boundaries and genuine source negative terms. Exact self-source exclusions remain
covered. The saved failure is preserved privately; public fixtures are synthetic.
Actual model compliance, search recall and useful sourced relations remain empirical
acceptance gates. Acceptance of this contract does not establish semantic ranking
quality or authorize full-source activation.

## Known self-source hits are not external evidence

Search can return the cited target itself through an index or aggregator. Exclude only
exact URLs already present in the narrative's cited evidence, including a final URL
retained from verified source acquisition. Do not infer redirects, canonicalize unknown
aliases or issue another fetch. Different documents remain eligible even when they have
the same publisher or host; their actual relation still needs assessment.

Results retain the distinct excluded URLs in optional diagnostic metadata. Validation
counts include the exclusion while original source-attempt result counts stay unchanged.
If only self-source hits remain, skip ranking and explain the exclusion; this is not a
claim that the wider web contains no counter-evidence. Older records without this field
contain no retrospective exclusion proof. This enforces the existing external-evidence
boundary without changing source adapters, query counts or primary delivery.
