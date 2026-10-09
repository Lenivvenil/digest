# 0010. Group source points with their material qualifications

Status: proposed; offline prototype retired under #207 (2026-10-09)

Refs [#55](https://github.com/Lenivvenil/digest/issues/55),
[ADR0009](0009-selected-source-admission.md), and
[the canonical domain requirements](../domain/digest/overview.md#current-owner-requirements-and-acceptance-traces).

## Context

The retained Citi response selected the relevant footnotes but still generalized
Citi's offering to all 12,500 Swift-connected institutions. The single-page renderer
also omitted those selected qualifications from presentation provenance. Correcting
the latter loss preserves evidence; it does not correct the generated assertion.
Mechanically projecting the old selections into literal source units produced 370
words. Neither a quotation appendix nor typed model-generated claims proves useful,
compact, complete analysis.

D-02 requires a specific useful insight with evidence, limitations and a reason to
read the original, with honest evidence coverage and technical-pending status.
The earlier attribution of mandatory complete-source support to D-03 was corrected
in the [domain provenance record](../domain/digest/overview.md#requirement-provenance-correction--2026-10-05).
The grouped representation below remains an experimental mechanism, not an
owner-approved publication algorithm or a mandatory #55 acceptance gate.

## Proposed decision

Keep the immutable complete source and paragraph manifest. Derive a versioned view
of literal units from original offsets. The offline splitter recognizes conservative
sentence-boundary patterns and retains known ambiguous paragraphs, abbreviations and
quoted clauses intact. It is an experimental heuristic: an unfamiliar abbreviation
can still produce an unsuitable boundary. Exact offsets, no splicing or arbitrary-length
cuts, and complete body reconstruction are enforceable; semantic sentence completeness
is not. Do not cut a long unit to meet a presentation or request allowance.

A candidate factual point selects supporting unit IDs and its own context and
material-qualification IDs. Render the exact units with neutral role labels, keeping
conditions beside their point. Do not generate connective factual prose that transfers
an actor, broadens availability, changes modality or resolves a contradiction.
Editorial interpretation, if retained, must be separately labelled as interpretation;
it is not a source fact or evidence of its own correctness. Literal source units stay
in their original language; this prototype does not demonstrate the complete Russian
reading experience or authorize translating those units as if they were exact quotations.

Validate known IDs and source identity mechanically. The model's selection of context
and qualifications remains a hypothesis: valid IDs cannot prove an omitted condition
irrelevant, or prove a point complete. Preserve full source access for independent
inspection, including unselected footnotes. Do not treat only the compressed candidate
as sufficient input for a later completeness assessment.

## Whole-article boundary

For multiple pages, a later exception may qualify an earlier point. Independent page
selections cannot certify that association through concatenation or an ID union.
A reconciliation operation must consider all retained page findings and their source
context, preserve unresolved contradictions, and fit the existing request/deadline
allowance. Its algorithm and admission behavior remain undecided in this proposal.
Insufficient allowance stays technical pending; it cannot silently omit late pages.
No additional critic request, automatic certification, accepted preparation or ready
edition is introduced by the offline prototype.

## Offline evaluation and limits

Compare readable candidates against the saved complete Citi source and a long-source
case with a late qualification. Report full selected text and length, not only valid
IDs or token counts. Manually supplied selections are labelled manual: they demonstrate
representation/renderer behavior, not model selection capability. The saved-source
manual fixtures retain 135 source words for Citi (scheme-wide context versus client
and account-holder scope) and 70 for one AWS outcome point (document-intake timing
with human verification and the distinct medical-director workflow). The AWS qualifier
occurs after character 15,689 of a 19,747-character source. Full inspection renderings
are longer because they expose provenance and uncertainty. These examples demonstrate
that a bounded point can retain its chosen late condition; they do not establish that
it is the best point, represents the entire article, or meets the Russian digest's
usefulness and compactness requirements. The earlier actual
Citi result remains failed and the AWS generation failure remains technically unresolved.

Keep the current v4 prompt, source-reading state and publication path unchanged.
A future prompt/version or publication change requires review and finite real-source
acceptance before activation. Source reading remains disabled.

One bounded metadata packet per fresh preparation window and page-by-page source work
may process fewer arrivals than the feeds produce. Local checks do not establish
sustainable daily throughput. Pending age, selected-source backlog and actual request
usage must remain visible; this proposal does not increase the ten-request cycle or
8/12-minute job envelopes, and does not impose a new article quota or retention TTL.

## Local role-preserving redundancy projection

The actual grouped-source Citi trial retained exact quotations but repeated one
complete point inside another, while omitting a material launch-versus-existing-
product transition elsewhere. These are different defects. A proposed offline
projection removes a point only when a retained point has identical supporting IDs
and supersets of its context and qualification IDs, with at least one strict
containment. It preserves original order, source/version and every represented
evidence role, and records dropped-to-retained original point positions. The raw
response remains unchanged. Incomparable groups and different support roles remain.

This is representation-level redundancy, not a judgment that the underlying claims
are semantically equivalent or complete. Distinct intended angles within the same
support unit are not represented by this schema. The saved Citi projection removes
one duplicated group (570 to 468 quoted words); it does not repair the missing
product-scope context. The saved AWS selection remains unchanged at 1,668 quoted
words. Blanket original-paragraph expansion would grow those examples to 650 and
2,592 words respectively, and still would not resolve the AWS logging ambiguity.
It is therefore not adopted as an automatic publication correction.

The helper has no model, preparation, sender or runtime integration. A separate
relation-aware selection prompt may be proposed and evaluated locally, without
replacing frozen requests or calling a provider. Adoption remains subject to this
ADR's owner decision and finite source-fidelity/readability acceptance; neither
projection nor a revised instruction establishes that outcome.

## Research disposition and restored brief direction — 2026-10-05

Quote-only publication is not selected following the observed product evidence;
this is not an owner rejection of an accepted architecture. Blind paragraph-view
candidates produced 496, 1,903 and 2,307 words in the Citi, AWS and TigerData reader
views. Exact source text and broader structural context did not establish concise
analytical reading. Preserve the experimental code and evidence; paragraph projection
is not the product correction.

The local successor restores the established e4772dd/README/D-02 distinction: concise
semantic brief and material caveats on the card, complete immutable source and literal
evidence in the archive, optional translation of the generated brief. Three cached
assistant-side candidates were 77, 103 and 172 words. Source review accepted the chosen
AWS/TigerData claims but found a geographic condition omitted from Citi's prose. The
existing full qualification appendage retained that condition at 566 combined words;
neither a detached appendix nor concise text alone demonstrated the joint requirement.
These are diagnostic assistant-side results, not configured-provider release evidence.

A separate untried instruction revision requires geography, eligibility and product
version to survive compression when inherited from surrounding definitions or linked
footnotes. This generic rule does not establish that a model will follow it. No new
prompt, generation, publication mechanism or runtime activation is adopted here.

The proposed offline input binding in ADR0009 distinguishes completed original page
coverage from the sparse evidence supplied for reconciliation. All required nominated
conditions remain bound; their semantic completeness remains unproved. Existing
midpoint admission can process the two larger sources in complete ordered pages,
but concatenating page briefs is not the final article-level reconciliation. Its
output/input coverage contract, source fidelity, translation and useful sustainable
throughput remain acceptance work within existing budgets.

## Retire the unintegrated reading-angle publication protocol — 2026-10-08

The scoped cleanup in [#183](https://github.com/Lenivvenil/digest/issues/183)
retires `BriefRun`, `enrich_selected_cards`, `_render`, `_citation_note`,
`mark_briefs_delivered` and `reconcile_briefs_delivered` from `reading_brief.py`.
These formed a separate article-admission/backlog/render/acknowledgment path with no
repository CLI, application, script or plugin caller. They were not the grouped-point
prototype described above. Shipping the module made direct Python imports possible;
this is an explicit internal-API retirement, not a claim that nobody could import it.

The earlier instruction to preserve experimental code and evidence does not require
this second live publication protocol. Its complete implementation, including the
single-page qualification correction, remains in
[the exact pre-retirement revision](https://github.com/Lenivvenil/digest/blob/82f012f1e6c52a1c0b53496040c5c41dbb2af4e5/digest/reading_brief.py).
The original decisions, failed-result evidence and source fixtures remain. The current
grouped-point experiment, reconciliation and their acceptance limits are unchanged.

The supported experimental path remains candidate-bound preparation: verify the
saved selection, acquire/process the complete source, retain request intent and page
proof, then freeze a technical handoff for reconciliation. It does not publish the
old concatenated page angles. All source/state/page/attempt schemas and hashes remain
unchanged, including readers for historical `delivered` and `delivered_at` values.
No state is rewritten or acknowledged by this retirement.

Selected qualifications remain in page evidence and reconciliation input regardless
of page count. Retained full-source checkpoint builders/readers also preserve exact
qualification roles and excerpts; fresh preparation emits a technical handoff, not
that checkpoint extension. The retained late-qualification checks exercise these
evidence contracts. Removing the old presentation function
does not erase a qualification, rehabilitate the failed Citi claim, or establish
semantic completeness. Any future publication algorithm still needs its own factual
and readability acceptance.

Tests of admission, fallback, pacing, request uncertainty, source integrity and
recovery move from the old wrapper to their live engine/application owner. Only
assertions about the retired renderer, its global backlog scanner and its independent
delivery acknowledgment are retired. No replacement scanner is introduced in test
helpers, and the optional source-reading feature remains disabled by default.

## Retire the unused grouped-point prototype — 2026-10-09

[#207](https://github.com/Lenivvenil/digest/issues/207) explicitly amends the
2026-10-05 instruction to preserve experimental code: preserve its exact historical
implementation and findings, but remove the unintegrated production module and its
two dedicated test files. Repository application, CLI and script reachability found
no callers. Direct Python imports were possible and now break; there is no shim or
replacement grouped-point API.

The immutable baseline keeps the original
[module](https://github.com/Lenivvenil/digest/blob/f8ad6811879c2233c17ece6b3abcdac57056c791/digest/reading_points.py),
[representation tests](https://github.com/Lenivvenil/digest/blob/f8ad6811879c2233c17ece6b3abcdac57056c791/tests/test_reading_points.py)
and [projection tests](https://github.com/Lenivvenil/digest/blob/f8ad6811879c2233c17ece6b3abcdac57056c791/tests/test_point_projection.py).
Their 54 cases retire with that mechanism. The decisions, failed research findings,
word counts, source fixtures and private research archives remain evidence, not a
publication algorithm. This amendment does not erase or rehabilitate any result above.

Candidate-bound full-source acquisition, page reading, qualification retention,
technical handoffs and reconciliation remain supported. Source reading stays optional
and disabled by default; no prompt, persisted schema, runtime activation or semantic
acceptance changes. Reconciliation's own authority migration is recorded in ADR0009.
