# 0010. Group source points with their material qualifications

Status: proposed; offline prototype only

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
read the original. D-03 requires complete source support and honest technical pending.
These are established requirements. The grouped representation below is a proposed
implementation, not an owner-approved publication algorithm or an accepted quality gate.

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
