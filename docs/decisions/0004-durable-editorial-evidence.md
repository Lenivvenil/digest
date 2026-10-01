# 0004. Complete-source editorial analysis with durable progress

* Status: accepted for implementation under delegated execution; real-output and release gates pending
* Date: 2026-10-01
* Scope: #55; develops the lifecycle debt identified in #39
* Decision authority: operator-directed autonomous rehabilitation; no new spending, credentials or audience
* Review baseline: [Digest domain register](../domain/digest/overview.md), D-01–D-06

## Problem and recovered context

The product is a personal information-intake and learning loop: relevant reading,
source feedback/discovery, differentiated analysis, and external counter-evidence.
It is not satisfied by transporting syntactically valid RSS cards. The April #55
plan already distinguished mechanical brevity from empirically verified information
gain and deferred a second analytical pass. #39 identified missing article lifecycle
state. Free-provider failures, truncation and rate limits predate this work.

The owner rejected both a fixed three-article analysis quota and a shared prompt
budget that favours short articles. Provider throughput limits must constrain when
work finishes, not redefine which material deserves attention. The original domain
excluded full-text storage; replayable processing intentionally changes that boundary.
This ADR makes that change explicit rather than hiding a corpus in a runtime cache.

Review: root technical review and independent read-only editorial review completed
2026-10-01. Their coverage, late-qualification, admission and uncertain-delivery
findings are incorporated. This is not a claim of a separate completed human ADR review.

## Decision

Persist complete extracted source evidence and per-stage results in the private
runtime repository. Process it as resumable article work, with bounded execution
passes. A partially analysed article cannot produce a completed digest card.
No limit on the number or length of editorial candidates is introduced by this ADR.
Network safety limits remain technical acquisition limits, with explicit failure
reasons and no claim that the material was read or judged irrelevant.

### Article lifecycle and identity

1. **Admitted:** every newly eligible collected article enters the work manifest.
   Source enablement, blocklists, confirmed-delivery deduplication and configured
   publication-age filters remain ingestion policy. The new editorial admission path
   runs BEFORE the legacy category/source slot budget: all semantically eligible
   articles are queued, while priorities order work rather than discard candidates.
   Legacy collection remains unchanged when this mode is off. Record raw fetched,
   eligible unique, admitted, legacy-allocated (when applicable), and pending counts;
   do not describe the old first-20 evidence packet as complete coverage. Model quota
   is not an ingestion or relevance filter.
2. **Acquired:** safely fetch readable public HTML, validating every redirect and
   pinning DNS. Preserve original URL, final URL, feed/source dates, extraction method,
   acquisition time, complete normalized body, and SHA-256. Inaccessible/paywalled,
   clipped or ambiguous text stays technically incomplete; never substitute RSS prose.
   Completeness is an assessed extraction property, not HTTP 200: preserve tables and
   footnotes; flag pagination, client-rendered continuation and unread visual material.
   Do not claim a full-material reading where relevant evidence remains inaccessible.
3. **Analysing:** a deterministic chunk manifest covers the entire body in order.
   A chunk has body hash, index, offsets and its own hash. Whitespace normalization
   happens once before hashing. There are no omitted windows. Chunk extraction records
   source claims, limitations and deterministic references to unchanged source spans; it does not reject the article
   because a particular chunk lacks news value.
4. **Ready or editorially rejected:** only after every chunk has valid analysis,
   synthesize the article's concrete change, significance and limitations. The result
   separates source fact, inference, unknowns and why the original is worth reading,
   in Russian. Additional inference is optional: omit it when it would merely invent
   benefits or restate the source. Actual material source qualifications remain
   mandatory; a specific unknown may be reported honestly instead of inventing a
   counterargument. Every final reference resolves to the stored body. A completed, unhelpful
   article may be rejected with a reason; a failed/unfinished one cannot. Preserve
   final claim-to-chunk/span references. Later qualifications or retractions must be
   reconciled with opening claims, not lost because the headline is more attractive.
5. **Delivered:** only confirmed Telegram card delivery consumes dedup identity.
   Ready but undelivered work remains eligible. Failed/uncertain delivery cannot be
   mistaken for editorial rejection or silently replayed as a new article. Record
   confirmed failed and unknown outcomes separately: unknown Telegram outcomes are
   held for reconciliation and are never automatically resent.

When the complete body fits the measured per-request allowance, analyse it directly
with source-backed final fields. Do not force it through a lossy intermediate summary.
Otherwise process every chunk, preserving atomic findings and material qualifications.
If all chunk findings cannot fit one synthesis request, reduce them hierarchically
in bounded groups. Every reduction node records the complete ordered set of input
node hashes and covered source chunk IDs. No first-K truncation is permitted. The
root may synthesize only when its lineage covers every source chunk; otherwise the
article remains pending. Coverage proves that every part was processed, not that
summarization preserved every nuance. The real-output editorial gate must evaluate
that lossiness, including an opening claim qualified by the final footnote.

Analysis generations are keyed by body hash, chunking version, prompt version,
provider/model and stage. Reuse requires all keys to match. A changed source or prompt
cannot reuse an earlier opinion as if it analysed the new evidence. Delivery identity
continues to use the existing article identity; changing analysis does not authorize
sending the same article again.

### Operational budget and backpressure

A run has an explicit execution deadline and request allowance. These bound a worker
pass, not candidate count or article length. Provider-specific pacing and Retry-After
are respected; output allowance, context capacity, RPM/RPD and TPM/TPD are distinct.
No paid fallback, unbounded retry loop, or new recurring schedule is introduced.

Pending work survives exhausted allowance, rate errors and the next scheduled run.
Advance fairly across ready articles/chunks so one long article cannot monopolize a
pass and newer short articles cannot repeatedly jump ahead. Existing twice-daily
runtime cadence is preserved. It may be insufficient for the arrival rate; expose
counts for admitted/acquired/partially analysed/fully analysed/rejected/ready work,
oldest pending age, completed coverage, attempts and observed token usage
rather than hide backlog by dropping old work. Publication dates remain visible even
when completion is delayed. There is no automatic age-out of unfinished work.

A provider failure is an explicit blocked attempt with retry eligibility, not a
completed empty answer. Provider-reported output exhaustion of a chunk divides only
that failed segment into smaller contiguous work units, with a generation-local
manifest and complete ordered coverage. Successful sibling work remains reusable;
retrying the same oversized request is not progress. A segment that cannot be split
further remains explicitly technically blocked. Auxiliary ranking explanations and
topic labels are optional metadata; their length or language cannot invalidate
otherwise validated substantive card fields. Persist each successful local result atomically. The workflow
must persist worker state before launching delivery and must save bounded partial
progress on a normal deadline exit. Worker failure cannot revoke an already committed
primary delivery. A hard runner loss before git persistence may repeat model work;
that limit must remain visible and must not imply exactly-once provider execution.

### Primary result, second opinion and Irritator

A completed primary article can be delivered without waiting for a second opinion.
If primary processing is unavailable, a completed fallback may lead with explicit
provider attribution; it is still one opinion. Independent analysis uses the same
source body/chunk manifest and its own extraction/synthesis, never the earlier
model's opinions. Missing or incomplete independent analysis is labelled honestly.

The Irritator remains a separate optional stage after committed primary delivery:
original evidence/narratives → external search → validation/ranking → same-destination
supplement. It is not replaced by model agreement. Existing source failures stay
visible as incomplete coverage; #77 is not silently absorbed into this implementation.
Feedback repair stays #48. Existing feedback and discovery capabilities remain product
requirements; this ADR does not redefine their current regression as intended behavior.

### Storage and privacy boundary

The public engine stores code, schemas, synthetic tests and decision records. Complete
article bodies and runtime work records live only in the existing private runtime
repository, under a dedicated editorial-state directory. They contain public source
material and operational provenance, never credentials or user vote identities in
provider prompts. No new service or persistent access grant is required.

Initial implementation does not delete unfinished or delivered evidence automatically.
Retention pruning requires a separately reviewed policy; a disk/response safety limit
is reported as an operational block. Keep one body snapshot per generation, reference
it from chunk results, and avoid duplicated full-text copies in each prompt checkpoint.
Run artifacts hold temporary diagnostic reports; development plans and issue backlog
remain in the engine repository.

## Alternatives and rationale

- **Shared small packet / fixed article quota:** rejected by the owner; creates
  editorial selection by length and repeatedly loses material outside the packet.
- **Unlimited one-shot prompt:** does not solve TPM/TPD or complete-source acquisition;
  context-window size is not free-account throughput.
- **Repeated stateless chunking each run:** wastes quota, loses provenance and can
  present partially processed work as complete. Conflicts with #39 replay needs.
- **Durable complete-source processing:** selected for review because it separates
  editorial value from execution capacity and reuses existing atomic state/receipt
  patterns. It adds real state complexity and may create backlog; neither cost is
  hidden by claiming a fixed number of cards is sufficient.

## Citation and synthesis interface refinement (2026-10-01)

Use deterministic numbered source spans instead of asking the model to reproduce
quotations. Each span resolves to immutable body offsets and text; unknown references
are rejected. Compound factual statements must cite the relevant source spans. The
model must preserve the scope and quantifiers of those spans, and the real-output
review must verify that it did so. This removes transcription and quotation-length
failure modes without relaxing factual grounding. A source reference alone cannot
establish that a factual statement follows from the cited text.

For example, a synthetic claim about a protocol's potential network reach does not
establish that a particular deployment serves that entire network. An unquantified
source statement cannot establish a majority. Such entailment errors require a
semantic quality check even when references are syntactically valid.

A fixed three-finding limit per chunk/reduction is not an editorial requirement and
must not erase source qualifications. Output exhaustion remains explicit incomplete
work, never a completed extraction. Direct whole-body analysis and conditional
hierarchical synthesis reduce unnecessary calls and avoid summarizing a summary when
all original evidence fits. Changed analysis contracts use a new prompt generation;
unchanged acquired bodies remain reusable, while earlier model opinions are retained
as history rather than silently treated as verified under the new contract.

Expected conclusions belong in the evaluator, not the model's source prompt. This
refinement is part of #55; it does not establish successful editorial quality or
sustainable processing capacity before real evaluation. Detailed runtime evaluation
artifacts remain private; public tests use synthetic examples.

## Verification and release gates

- Deterministic tests: complete chunk coverage and boundaries; body/prompt/model hash
  mismatch; interruption/resume; malformed and partial answers; technical versus
  editorial rejection; pending age; fair progress; no model opinion leakage; no
  duplicate delivery consumption; successful primary independent of optional failure;
  an opening claim with a limiting footnote in the final chunk retained through reduction.
- Security tests: public URL validation, redirects, DNS pinning, response/deadline caps,
  incomplete extraction, and no RSS fallback.
- Full lint/type/test checks and independent code review before publishing a release.
- A controlled report-only run using real sources must show completed coverage and
  useful Russian analysis, with a read-only editorial review against #55. Synthetic
  model fixtures prove contracts only, not usefulness. Do not mark #55 done on tests,
  a successful API call or an empty queue alone.
- Deploy only after that gate; verify primary Telegram API acceptance and saved state,
  preserve separate optional-stage diagnostics, and retain the prior pin for rollback.
