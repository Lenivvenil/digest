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
