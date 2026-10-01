# 0004. Complete-source editorial analysis with durable progress

* Status: accepted for implementation under delegated execution; real-output and release gates pending
* Date: 2026-10-01
* Scope: #55; develops the lifecycle debt identified in #39
* Decision authority: operator-directed autonomous rehabilitation; no new spending, credentials or audience
* Review baseline: [Digest domain register](../domain/digest/overview.md), D-01–D-06

## Current implementation decision — selected publication drafts, 2026-10-01

This section supersedes earlier all-admitted rollout and unverified final-prose proposals
for the selected report-only entry. Historical reasoning below remains a record, not a
second active implementation path. The active work is #55; #94 translation follows it.

The standalone all-admit editorial CLI, its collector extension and only their
specific tests/verification fixture are excluded from the proposed release. Git
history preserves the experiment; source/state compatibility and its unresolved
lifecycle requirements remain. This removes an unshipped execution path, not a
promise that its requirements are satisfied by the selected report-only entry.


- Preserve the saved selection manifest, omitted/unreviewed counts, safe acquisition,
  immutable body/offset provenance, resumable work and strict technical-pending states.
  Do not change normal delivery or admit the whole feed pool through this entry.
- Configure **writer** and **verifier** explicitly for enrichment. Never inherit an
  untested pair from RSS review primary/secondary roles. Roles remain provider-neutral;
  supporting an adapter does not establish free entitlement or semantic suitability.
- A writer returns ordered atomic publication claims with exact text, source references,
  scope annotations and optional interpretation. Each text retains its own material
  qualifications. The renderer uses every checked text in order with source attribution
  and a source link; annotations, a separate summary or manual editing cannot repair
  an inaccurate published sentence.
- A draft-aware factual checker is **not** a blind independent opinion. Store and display
  these separately; factual-check success never changes `independent_complete`.
- Reserve and persist each stage before inference. Bind draft, source, prompts, language,
  routes and checks by hashes. A changed text requires a fresh check of the whole revised
  candidate. Join verdicts against the exact canonical IDs sent to the checker; preserve
  the writer-ID mapping and text hashes instead of normalizing verdicts after the fact.
- Permit at most one correction after a completed nonpassing check. The writer receives
  the complete source, its exact draft and only nonpassing feedback on its own claims.
  It receives no controls, expected labels or prior supported verdicts. A new stateless
  check sees the complete revised draft and source, without prior judgments. A second
  failure leaves an explicit rejected/pending result; no unbounded repair loop.
  The one semantic correction may have one additional identical transport attempt only
  after an explicit HTTP 429/503 without a corrected draft and its persisted cooldown.
  Unknown requests, invalid output and a second transport failure remain held.
- Execution allowance governs progress, not editorial eligibility. Missing source text,
  unavailable media, quota exhaustion or a request envelope that cannot yet be verified
  remain technical pending. Preserve existing long-source progress; do not call a lossy
  reduction fully verified or silently remove an article because it is long.

### Scoped pacing and quota accounting

Enrichment may explicitly select `provider_aware` pacing; legacy calls keep their
existing behavior. The operator's `llm.min_request_interval_seconds` remains a floor.
Only fresh, validated telemetry associated with the observed provider/model may reduce
the enrichment path's additional conservative fallback wait. Reserve estimated input,
maximum output and safety overhead before dispatch, then reconcile measured token use.
Account for RPM separately from RPD/TPM headers, honor server retry/cooldown boundaries,
and do not infer independent organization budgets from different API keys or model names.
Missing, stale or ambiguous telemetry falls back conservatively. No new free allowance,
runtime timeout, recurring schedule or paid fallback is implied.

Source acquisition, candidate selection, checking and the possible repair all count in
full-cycle capacity. Fast isolated API responses do not establish sustainable throughput.
Actual eligibility/arrival observations must be distinguished from one RSS snapshot.

### Optional local tokenizer profiles

The isolated enrichment extra pins tokenizers, tiktoken and Jinja2. Legacy installation
retains its current dependency set. Tokenizer data is prepared explicitly and cached
locally with SHA/revision checks; analysis never fetches assets or model weights.
Profiles are specific to declared provider/model routes. Unsupported profiles and
missing/mismatched data are technical pending, not relevance decisions. Compare actual
reported input with the local count and reserved overhead in diagnostics. See
[asset preparation and third-party licenses](../ENRICHMENT_TOKENIZERS.md).

### State and release boundary

#### Offline scope-check prototype, pending evaluation

A proposed verifier revision records per-claim actor/population, time/availability
and material-condition checks, plus source IDs for relevant qualifications. Explicit
narrowing footnotes take precedence over broad or ambiguous source statements. A
`supported` verdict with missing checks, reported broadening/uncertainty, or uncited
qualification IDs is structurally invalid and held. No second summary or model stage
is added; the writer and exact-text renderer remain unchanged.

These checks establish only completeness and consistency of the **reported assessment**.
The model can still overlook a qualification or falsely report preserved scope; listing
restrictions is not proof of source coverage or entailment. Real-output and held-out
evaluation remain required. The new prompt version cannot reuse prior approvals;
archived v2 request/result hashes remain readable and validated under their original
representation. This offline prototype is not approval for automatic delivery.

Older experimental generations remain inspectable but cannot inherit new factual-check
status. New publication attempts use a versioned binding and durable one-correction
limit. The entry writes internal model-checked drafts and explicit incomplete outcomes,
not delivery authorization. Normal primary-first delivery, separate external Irritator,
feedback behavior and production model configuration remain untouched.

Before any rollout: independently review automatically acquired real-source output,
verify complete claim coverage and recoverability, measure full-cycle quota/runtime
capacity and candidate coverage, and preserve a state-safe rollback. No empirical
result from a manually assessed source or a development example alone closes these gates.

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

## Selection obligation clarification — proposal, not implemented

The original [README](../../README.md#адаптивное-управление-источниками) describes
source allocation and a learning information diet. The
[completed product plan](../plans/completed/digest.md#task-3-llm-summarization-with-multi-provider-support) calls for
selecting significant news and skipping irrelevant items; the later
[#55 plan](../../plan.md) distinguishes category analysis from selected article
cards. These sources establish editorial selection, not an obligation to perform
exhaustive full-text LLM analysis of every fetched RSS entry. Their historical
fixed item counts do not supersede the owner's subsequent rejection of artificial
article-count or article-length selection limits.

The initial worker implementation routes every admitted candidate toward complete
analysis. That is an implementation assumption, not an additional owner requirement.
A durable queue alone does not make that assumption sustainable: if admitted work
arrives faster than complete analysis, its age grows indefinitely.

The proposed clarification is to separate a visible candidate inventory from a
traceable content-based reading decision. Metadata/RSS may support prioritisation,
obvious topic mismatch or duplicate-event detection, but cannot prove the quality
or factual content of an unread original. Such a decision must identify its evidence
basis and uncertainty; it must not masquerade as a completed full-text rejection.
Sparse descriptions or ambiguous relevance need further source evidence rather than
an automatic negative verdict. The decision criteria must distinguish relevance and
significance, novelty relative to already delivered material, and source diversity.
A shared topic alone is not a duplicate event or evidence that a contrary account
adds no information. Evaluation must audit missed valuable items as well as selected
items, including long material and low-volume sources.

Every published substantive card still requires complete source analysis and a
faithfulness check. No first-K packet, fixed card quota, article-length preference,
or quota-derived irrelevance label is permitted. A budget delay remains pending;
it cannot be renamed as content-based exclusion. Selection decisions do not consume
delivery deduplication and must remain reconsiderable when their evidence or policy
changes. Existing source-feedback and discovery intent remains relevant, without
silently implementing their separately queued repairs here.

Before adopting this clarification, review its concrete decision criteria and
traceability against D-01–D-06, then measure total work: candidate selection, selected
full-text processing, verification, independent views and external counter-signals.
Provider-specific request limits and actual wall time must support the observed
workload within the existing zero-incremental-spend environment. A single feed-date
snapshot is not a measured arrival rate. Neither more runner time nor a larger
context window alone establishes sustainable free execution. This section authorizes
no schedule change or automatic-card rollout by itself.

## Candidate reading decisions — proposal, not implemented

The smallest extension uses the existing candidate record and review evidence IDs,
not another queue or service. Each actually reviewed candidate needs one explicit
outcome: `read`, `needs_source`, `outside_scope`, or `already_covered`. `outside_scope`
requires positive evidence of a mismatch with configured interests. `already_covered`
requires a confirmed delivered reference and evidence that the development adds no
material change, qualification or contrary account. Shared topics, sparse RSS, source
scores, article length and exhausted execution budgets are not exclusion grounds.

Record evidence fingerprint, policy version, reason and any covered-delivery reference.
Reconsider when source evidence, relevant delivered context or policy changes. Unchanged
uncertain RSS should proceed to the existing source-reading path, rather than repeated
metadata-only model calls. Failed acquisition and unprocessed IDs remain pending.
Request batches are technical boundaries, not editorial quotas. Source/category
alternation improves access to processing, not processing capacity.

Before using decisions to exclude work, a report-only evaluation must account for every
supplied ID, distinguish omitted IDs from decisions, and check missed valuable items
among sparse descriptions, long articles, low-volume sources and contrary accounts.
Measure triage cost and accepted-work cost as well as coverage. No observed selection
rate currently establishes sustainability. Full-source claims still require complete
reading and factual verification; metadata relevance is not proof of article quality.

## Release-gate status — 2026-10-01

**Not met.** Mechanical evidence integrity and successful workflow execution do not
establish faithful editorial output. A writer plus a verifier is still an unproven
execution path; an incomplete verifier response supplies no verdict. Work remains
under #55, and this ADR does not authorize treating the draft as a restored product.

The all-admitted-candidates processing strategy is not acceptable for rollout without
a sustainable workload/capacity result. Persisting a growing backlog does not resolve
that failure. The selection clarification above remains a proposal; it must not be
implemented as the old slot budget or an article-length limit under a new name.

The next bounded investigation is model-specific reasoning/output controls and
numeric token accounting, followed by a source-faithfulness check. Effort controls,
reasoning visibility and total completion limits are different API concepts; supported
values must be checked against current provider documentation. A missing numeric
breakdown is unknown, not evidence that all output tokens were reasoning. Do not
increase limits blindly or retain private reasoning text to diagnose the budget.
Detailed evaluation artifacts remain in the private verification environment.

## Claim-level publication contract — proposal, not implemented

The current final prose fields can each contain several independently falsifiable
statements. Resolving their source IDs does not establish that every statement was
checked. A verifier that rewrites the prose while checking it can silently omit the
very qualifier that needs verification. Its self-reported coverage is insufficient.

A smaller publication surface should consist of identified source-backed assertions,
with optional interpretation rather than mandatory benefit, caveat or reading advice.
Each assertion keeps its exact publication text, source references and explicit scope:
subject, predicate/object, quantities or population, time/status, and applicable
conditions. Unknown components remain unknown; the schema must not force inventions.
Source-reported assertions remain attributed. A condition needed to make a claim true
belongs with that claim, not in optional decorative prose elsewhere.

Verification must return one result for every exact assertion ID and input-text hash,
without substituting a paraphrase. The engine can deterministically verify complete
ID coverage, unchanged input hashes, valid source references and non-passing unresolved
results. It cannot mechanically prove semantic equivalence, atomicity or discovery of
all relevant source qualifications. A factual check therefore retains the complete
available source context and tests actor, scope, quantity, timing and necessary
conditions; its judgment remains fallible and must pass independent real-output
assessment and negative controls before becoming a release gate.

Rendering should introduce no new model-written title, summary, benefit or explanation
outside the checked assertion set. Optional interpretation is also checked if published.
Translation changes the publication text and must preserve the same proposition and
qualifiers; a verdict bound to different text is not reusable as proof of the translation.
This proposal changes neither production delivery nor the configured models. It is a
bounded experimental contract, not another implemented verifier framework.

## Publication language clarification — 2026-10-01

Owner direction recorded in #94 makes English the product default and post translation
an explicit setting with a target language. Earlier Russian wording in this ADR records
the deployment and verification cohort used when the decision was drafted; it is not
a universal editorial invariant. Source evidence remains unchanged by publication
language. The current deployment keeps Russian through configuration. Language changes
must preserve factual scope and material qualifications; optional translation failure
may fall back to the verified canonical English card with a visible degraded status.
This clarification does not claim that the draft implementation already supports the
new configuration or that its factual release gate has passed.

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
  useful analysis in the configured publication language, with a read-only editorial review against #55. Synthetic
  model fixtures prove contracts only, not usefulness. Do not mark #55 done on tests,
  a successful API call or an empty queue alone.
- Deploy only after that gate; verify primary Telegram API acceptance and saved state,
  preserve separate optional-stage diagnostics, and retain the prior pin for rollback.


## Implementation slice — selected-source report enrichment, 2026-10-01

Current #55 implementation scope is a report-only enrichment of an existing saved
RSS selection. It reuses acquisition, immutable source storage and resumable article
work; it does not admit every feed item, alter normal delivery, or add an automatic
factual-approval service. The broader selection strategy above remains a proposal.

A validated primary selection (or the existing secondary fallback) provides candidate
IDs. The report distinguishes selected IDs, items present in the RSS packet but not
selected, and items omitted before selection. Neither of the latter groups is a
full-source editorial rejection. The shortlist coverage gate remains open.

Only selected sources enter the separate enrichment state. An execution allowance
limits a pass, not article eligibility; unfinished long sources retain their complete
body and prior work for resume. Full-text acquisition failure cannot fall back to an
RSS-written substantive card. Existing source qualification references remain attached
to generated facts. Interpretation, reading advice and extra limitations are optional;
the schema must not invent them merely to populate fields.

Reports identify generated text as an unverified draft. Automatic factual-verification
experiments have not passed acceptance; no automatic approver is enabled by this slice.
Source-reference validation proves provenance integrity, not factual equivalence. A saved experimental claim ledger
may be replayed offline to inspect provenance and rendering; that replay is not a new
model evaluation or an approval of its claims. A manually edited reference is labeled
separately and cannot count as automatic quality evidence.

Following #94, the isolated report entry defaults to English when no publication
language is configured, and honors an explicitly configured language. Language is
bound into generation identity to prevent reuse of a draft in a different language.
This slice does not implement the later optional translation/fallback product flow.

The original selected-source slice used a conservative character estimate. The newer
optional local-profile decision above supersedes that estimator for publication work;
legacy experimental generations retain their recorded method. No analysis-time asset
download is introduced, and unknown profiles remain explicit technical pending.

Release still requires independent review of real automatically generated cards and
selection coverage/capacity evidence. Offline contracts and a useful manually edited
reference are insufficient to enable automatic publication.
