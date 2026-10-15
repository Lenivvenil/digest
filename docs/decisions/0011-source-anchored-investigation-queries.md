# 0011. Ground bounded investigation queries with optional literal provenance

Status: revised optional-provenance decision accepted for the engine-only release
on 2026-10-05, following owner approval to merge PR #107 and update digest-prod.
Full-source reading stays disabled; its activation and ADR0009/0010 are not accepted.
The earlier mandatory-anchor proposal was implemented in draft PR #107 but was never
accepted or deployed; it is superseded by this revision.

Refs [#77](https://github.com/Lenivvenil/digest/issues/77),
[the Irritator domain](../domain/irritator/overview.md), and
[ADR0009](0009-selected-source-admission.md).

<a id="2026-10-15-question-routed-documents"></a>

## 2026-10-15 delivered-only question-routed documents (#77)

Status: local implementation under review. This amendment supersedes the
2026-10-09 target-selection and fixed-fan-out behavior **only for new delivered-bound
attempts**. It does not claim merge, deployment or semantic product acceptance.
The accepted optional-anchor decision and unbound standalone/full-source contracts
below remain in force outside this mode.

### Purpose and immutable target

Investigate a consequential claim, decision or interpretation arising from what was
actually delivered. The useful outcome is external evidence that could materially
change that reading-derived interpretation or a practical decision. Contradicting a
random announcement, repeating a known caveat, or collecting unrelated interesting
news does not meet that purpose. A reading list does not establish the reader's
beliefs, needs or a consensus; the model must not invent them.

The existing first model call selects an actually delivered card and a canonical
`title` or `summary` field with literal card/source provenance. The engine retains
the **whole selected canonical field** as the authoritative target, including its
qualifications; a model-written paraphrase or short quotation cannot replace it.
This remains canonical-language material, not necessarily the translated Telegram
wording. The same occurrence must supply the original evidence.

Delivered narrative category is derived only after all cited IDs, literal quotations,
selected-card and source-occurrence bindings pass. The model's `category` field remains
required nonempty text of at most 200 characters for response compatibility, but its
value is ignored in this mode. The engine preserves the cited source category verbatim:
RSS already retains the validated `occurrence.category[:200]` projection; full-source
passages must all match the selected occurrence's complete category and remain within
the same 200-character narrative bound. Distinct RSS IDs cannot bind one selected card;
multiple full-source spans must share that occurrence and category. Citation order never
chooses among conflicting categories. No cross-source/category fallback is allowed.
New schema-2 result eligibility rechecks this derived value; schema-1 eligibility,
frozen historical artifacts and unbound category validation remain unchanged. The
response schema, prompt, model-call count and request budgets are unchanged. Synthetic
regressions establish this mechanism, not a replay of an unavailable model response.

The second call states a neutral research question and the evidence needed to
answer it. Possible implications and counterhypotheses are explicitly labelled
unverified interpretation, separate from source facts, the immutable target and
any assertion about the reader. No worthwhile supported target, no usable route,
or no useful external finding are valid explained empty outcomes.

### Question-specific routes and original documents

New compact reservations use `question-routed-documents-v2`, with the effective
configured Hacker News/arXiv/DEV subset and at most three queries. Each query names
only the configured sources suited to the question, rather than automatically
fanning out to every source. A document-only plan is allowed. Existing lexical
query checks and optional literal-anchor diagnostics remain; neither establishes
neutrality or useful recall.

After selecting the target, read its original page once for discovery. Only actual
HTML links become selectable, bounded ID/URL/title entries. Link titles and URLs
are discovery leads, not evidence from the linked document. The second model call
may choose at most one supplied link ID, or nominate one query whose first eligible
returned document is read. It cannot invent a URL or combine both selection routes.
A selected direct document is acquired before optional search fan-out. arXiv
abstracts retain their limited reported-findings scope. Raw HN/DEV descriptions
remain discovery only and cannot enter delivered ranking or pass counter-evidence
eligibility, even when a selected document read fails.

At most one external document is acquired in addition to the original discovery
page. Both use the existing public-only HTTP boundary: one 20-second acquisition
and extraction deadline, separate 2 MiB raw/decoded body bounds, at most three
redirects, validated public addresses at every hop, original-host TLS verification,
no ambient proxy/credentials and no arbitrary model-directed network tool.
The [public acquisition decision](0026-public-acquisition-boundary.md) owns those
transport guarantees. This optional investigation read does not activate the
separate primary `reading_brief` workflow.

A snapshot retains requested/final URLs, fetch time, body/text hashes, complete
normalized text or an explicit held/error outcome, and original-page discovery
links. A discovery link is not proof that its destination was read, publicly
resolved or semantically relevant. Partial evidence units are never returned. Original
page text enters planning and ranking only as a whole unit at at most 3,000 characters;
omission retains the saved target excerpts and an explicit limitation. Qualifications
outside those excerpts are then unknown. The model cannot assert that the original
author omitted/ignored a caveat or establish its novelty across the whole article;
recognizing repeated qualifications remains an empirical acceptance risk.

General HTML supplies one complete extracted text unit. Actual RFC headings may
supply complete natural sections, including nested content and visible
cross-references. A fragment must resolve to one unambiguous complete actual
heading section; missing/ambiguous/incomplete sections are held. Without a
fragment, retain complete whole text when available and actual complete section
descriptors, never an invented section. Local unread media or missing section content
holds that section; unrelated independently complete sections may remain available
with explicit omissions and no whole-document coverage claim. Global clipped HTML,
ambiguous identities and pagination still hold. Aggregate section text is bounded
to 2 MiB; normalized section URLs retain the 2,048-character limit.
A deterministic lexical-fit heuristic can propose complete
sections for ranking without a fourth model call. It is a retrieval heuristic,
not proof of applicability or whole-document coverage. Oversized units are omitted
whole rather than cut into a convenient prefix that could hide qualifications.

### Shared bounds and ranking evidence

The stage retains at most three model calls: target selection, routed research
planning, and ranking. Optional translation retains its separate existing contract.
The cumulative investigation deadline remains 180 seconds. A single nine-start
source-request budget covers the original-page read, all routed searches and the
optional external-document read. The callback is consumed immediately before each
actual HTTP start, including every document redirect and address fallback; a tenth
start is refused. Logical document reads and search-attempt diagnostics are not
interchangeable with this physical-start count. No new retry or pagination loop
is introduced.

The existing 8,000-JSON-character limit applies to the serialized ranking
**candidate array**, including its supplied evidence segments. It does not cap the
total prompt at 8,000 characters and is not provider token/quota admission.
Complete document text may exceed it; acquisition preserves that text while the
ranking owner omits an oversized whole unit with a recorded reason. Known target
context, the question, instructions and other request fields remain outside that
candidate-array measurement.

Ranking keeps target and question distinct. It asks what the external evidence
establishes, the evidenced conditions/mechanism connecting it to the target, and
what material interpretation or decision changes. The response binds an exact
target quotation and source-body/abstract segment IDs; a title alone cannot support
a counter-signal. Conditions not established for the target remain explicitly
conditional. Topic overlap, a familiar caveat or a high score cannot substitute for
an applicability bridge. Discovery lineage and acquired-document provenance stay
separate, including source/final-URL self-source exclusion.

### Compatibility, evidence and remaining acceptance

Before any model/source work, compact execution requires its exact versioned policy
binding. An unexecuted old compact checkpoint is held unchanged rather than upgraded,
reinterpreted or rerun. Preserve old markers, completed results, frozen fragments,
primary receipts and historical hashes; do not delete artifacts to obtain another
attempt. Unbound legacy/standalone behavior continues to use its existing query,
source and ranking contracts. Primary delivery and the existing later-edition
fragment lifecycle are unchanged.

Offline synthetic fixtures can establish request counts, exact text/quote bindings,
whole-unit admission, safe acquisition, routing and historical-policy holds. They
cannot establish that a live model asks a worthwhile question, understands a
standard's scope, finds relevant evidence or produces a faithful useful conclusion.
Actual model/search quality, ordinary-run acceptance under #77, exact-main checks
and runtime rollout require their own observed evidence. Do not claim that this
implementation closes #77 on the strength of valid JSON or passing mocks.

## 2026-10-09 delivered-card contract (#208)

The 2026-10-15 amendment above replaces the target/query behavior in this historical
contract for new delivered-bound attempts. Its origin and publication bindings remain.

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
