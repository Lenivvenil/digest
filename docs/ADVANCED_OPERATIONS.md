# Continue or investigate saved work

Inspect unfinished candidate work or choose an optional investigation; each task has its own evidence, allowance and hold rules.

- [Inspect candidate continuation](#candidate-continuation) after incomplete ordinary selection
- Optional: [Compare independent reviews or resume a report](#independent-comparison-and-resume)
- Optional: [Investigate delivered claims](#supplementary-investigation) after durable primary publication
- Optional: [Experiment with source reading](#experimental-source-reading), off by default

For ordinary preparation, publishing and interruption recovery, use [Operations](OPERATIONS.md).
These tasks never authorize a resend or a reset of retained delivery state.

## Candidate continuation

<a id="ordinary-preparation-candidate-accounting"></a>

Keep `.cache/candidate_progress.json` with its immutable source/report objects and
indexed decisions. The archive's `.candidates.json` file is a bounded as-of account
of one report, not a continuously rewritten inventory of every historical body.
Missing required objects fail explicitly. Do not clear progress or rewrite old
rejections to claim complete coverage.

The review instruction evaluates substantive supplied information before reader
relevance: a concrete development, finding, explanation or usable resource. A
relevant question or promised discussion alone is insufficient. Concrete future
announcements remain eligible, with attributed claims and plans distinguished from
achieved outcomes. Every reason must preserve attribution, uncertainty, negation,
scope and material caveats, including the ordinary reason of an enabled closer.
Do not upgrade a reported allegation to an established event or introduce unsupported
specificity; omit a claim if its meaning-changing qualifiers cannot fit. `deferred`
is reserved for otherwise useful items exceeding the detail budget; insufficient
substance or relevance is an editorial `not_selected` judgment about the supplied
metadata, not the unread full article.

These are model instructions, not semantic guarantees enforced by quote validation.
The [#55 clarification](https://github.com/Lenivvenil/digest/issues/55) is prospective;
generated quality remains unverified until a new ordinary output is inspected.
It neither reopens prior decisions nor proves improved selection or faithful prose.

Candidate progress is saved before a review call. Admission works within the configured
count, character, retry-opportunity and storage bounds; it does not promise to drain
an entire feed cohort in one run. Missing/invalid dispositions and capacity-only
omissions remain unfinished. Policy exclusions, duplicates and editorial not-selection
retain distinct evidence.

The protected first-unseen opportunity uses original identity observation age,
then existing priority/source/identity ties, and skips evidence that cannot fit.
Technical retries and the remaining fresh/age source turns follow within the same
packet. At a one-item cap this deliberately favors age over freshness; a larger
old excerpt can leave less room for later items. Planning is only an opportunity,
not a completed review. See the [oldest-unseen amendment](decisions/0008-candidate-selection-progress.md#oldest-unseen-opportunity-amendment-196).

With optional closing enabled, an otherwise absent approved source can receive
one fitting review opportunity. Spare capacity is used first; a full packet may
defer only its final ordinary backfill item while preserving the first unseen
opportunity and reserved technical retries. The deferred item stays pending.
Count and character limits do not increase, and no fit means no substitution.
An already admitted approved occurrence leaves the packet unchanged. This can
postpone a professional item and does not promise a suitable positive story;
see [the bounded admission decision](decisions/0014-optional-humane-closing-item.md#bounded-closing-source-opportunity).

## Independent comparison and resume

<a id="contract"></a>
<a id="resume-a-report-only-comparison"></a>

Independent comparison differs from ordinary primary/fallback preparation. Primary
and secondary slots receive the same frozen evidence and messages and keep their
configured provider/model identities. A failed slot stays failed; another review is
not relabelled as its opinion. Valid complete results may trigger one configured third
review according to overlap. Partial results remain incomplete and do not establish
comparison agreement.

**This manual command can call configured providers.** Use a fresh output path; the
original report remains intact.

```sh
python -m digest.review_trial --config config.yaml --resume previous/review.json --output fresh-output
```

Reuse requires matching slot/provider/model, bundle and current prompt identity
plus valid retained selections. The shared reason-qualifier instruction changes future
prompt hashes and input request size even for unchanged evidence, across planning,
primary/fallback execution, independent comparison and resume. Existing
[exact-identity rules](STATE_AND_EFFECTS.md#request-identity-and-reuse) refuse
old-prompt slot reuse; subsequent work still follows current admission, allowance,
reservation and disabled-path rules.

Accepted canonical preparation, frozen editions, old reports and retained decisions
remain readable. They are not rewritten, relabelled, reopened or silently regenerated
under this new-request policy. Preserve old-prompt evidence without purging caches
or introducing a migration.

`digest.review_resume` keeps separate prepare/execute phases and its persisted
one-time execution marker, validated and atomically marked started before requests.
It now permits at most one physical attempt, preserving stricter limits and spent
attempts. Manual invocations share this policy; `digest.review_trial` and ordinary
independent comparison retain their existing defaults.

Reusable valid slots are free. Resume admits at most one newly needed configured
slot in primary, secondary, then third order; exhausted remaining allowance admits
none. The third still requires valid disagreement. Held slots are `unavailable` with
`new_slot_allowance_exhausted`, no attempt/generation timestamp or response identity,
and no provider call or pacing wait. Reports render `request allowance exhausted
(not attempted)`. Incomplete results do not establish comparison agreement.

Results remain report-only supplements, with unchanged marker/output schemas and
no new primary edition or deduplication effect. The paired
[prepared-main recovery](decisions/0005-optional-presentation-translation.md#bounded-prepared-main-429-recovery-55)
transfers no unused allowance to resume. Primary fallback and investigation
generation remain unchanged.

When `synthesis.enabled` is true, both report-only resume entrypoints instead
return an explicit zero-call skip before creating or mutating a marker. An old
marker cannot spend the surrendered allocation. Investigation still has all three
stages; unused synthesis time or calls are never transferred. This fixed allocation
also applies when synthesis is unsupported or omitted. Preserve old reports for
inspection and follow the [current-edition rollback boundary](OPERATIONS.md#retaining-optional-synthesis-across-recovery).

## Supplementary investigation

<a id="primary-first-runtime-with-preserved-irritator"></a>

After confirmed, durably persisted primary delivery, supplementary work can use the
saved evidence checkpoint under a separate reservation. New compact schema-3
attempts bind canonical cards from the exact confirmed/applied edition and their
original source occurrences. The delivered-only `question-routed-documents-v2`
implementation selects a meaningful reading-derived target, preserves the whole
selected canonical title/summary, and plans a neutral research question. Possible
implications remain labelled unverified interpretation, never source facts or
invented reader beliefs. Unbound standalone contracts remain available separately.
This policy is under review; this guide is not proof of runtime deployment.

### Native delivered origins

Native primary archives do not contain a provider `.review.json`, `BlindReviewReport`
or candidate-report hash. Use the separate native entrypoints with the exact delivered
Markdown archive, not its source-review or final-acceptance file:

```sh
python -m digest.post_delivery native-prepare --config config.yaml --checkpoint digests/2026-10-25.md
# Persist and verify the returned marker and its referenced evidence before execute.
python -m digest.post_delivery native-execute --config config.yaml --checkpoint digests/2026-10-25.md
```

`native-prepare` is read-only with respect to the primary edition and source/archive
files. It makes no model, search or transport request. It requires the configured
compact archive directory, an archive-enabled accepted native schema-4/5 ready edition, its
exact owner-matching claim, and complete confirmed/applied receipts. It rechecks the
native packet, response, source review, projection, final acceptance and archive
index, including original source occurrences, accepted canonical card order and
presentation/coverage. All anchors are reread before reserving work. Origin day D+4,
a future origin, missing/changed evidence, partial/unknown delivery or unapplied
receipts blocks preparation; none is repaired by inventing delivery evidence.

The new attempt is `<archive-stem>.native.post-attempt.json` (attempt schema 4,
`native-delivered-card-v1`). Its `.native.irritator.json`, `.native.irritator.md` and
`.native.irritator.fragment.json` use the existing result/fragment contracts with an
explicit native-origin variant. Legacy `prepare`/`execute`, schema-2/3 attempts,
`DeliveredInvestigationInput` fields and retained fragment bytes are unchanged.
Native source evidence remains labelled RSS excerpts; it is not a full-source review.
The existing delivered-target research/quote/ranking validators remain authoritative.

**`native-execute` can call the configured optional model and source adapters.** Native
origin compatibility does not establish their free entitlement, replace them with
native semantic stages, or authorize their execution. For the separate no-API route,
use [native research phases](#native-research-phases), never this provider entrypoint.
The runtime must explicitly
admit that route, serialize writers, and retain its existing optional eligibility,
timeout and durability barriers. These commands add no scheduler, automatic retry,
primary send or automatic semantic-stage route. Both the ordinary prepared path
and the native destination below can carry an eligible retained fragment, without
repeating the investigation.

Execution refreezes the current origin and compares it to the saved attempt before
any model/source work. An exclusive `.native.post-attempt.execute.json` claim prevents
concurrent or interrupted execution from starting twice. Preserve that claim even if
`execute_started` is still null after a crash; that state is spent/uncertain, not a
retry invitation. Never delete/rename attempt, claim or output files to force a rerun.
If validation fails before the claim, restore only independently verified original
bytes; never recompute hashes to bless changed evidence. If optional execution fails
after the claim, keep its diagnostics and continue the primary lifecycle unchanged.

For rollback, disable the runtime's optional invocation before reverting code. Keep
native-origin attempts/fragments and all referenced evidence intact: older readers do
not understand the new variant. Do not let an older worker silently overlook a pending
schema-4 attempt and create another investigation; keep optional investigation held
until a compatible reader is restored or the retained work is explicitly reconciled.
The primary ready/claim/receipts and all already-sent messages stay unchanged.

### Native research phases

`python -m digest.application.native_irritator` is the explicit native-only compact
research route. It does not call `ModelExecution`, a model provider API, translation
API, Telegram, discovery activation or the primary sender. Do not confuse it with
`digest.post_delivery native-execute`, which retains the provider-backed route above.
The runtime must adopt and persist this route explicitly; these commands do not
install or activate a scheduler.

The sequence preserves the existing delivered target, original evidence and exact
narrative/research-plan/grounded-ranking validators:

1. `begin --config config.yaml --archive digests/EDITION.md` reserves the existing
   optional slot and emits a session path, or null when the slot is unavailable.
   Persist the native attempt, execution claim and session before continuing.
2. `advance --config config.yaml --session SESSION` returns `native_pending` with
   `phase`, `request` and `request_sha256`. The three semantic phases are `narrative`,
   `research_plan` and `ranking`; configured presentation adds `presentation`.
3. `claim --request REQUEST --expected-sha256 HASH --controller-ref CONTROLLER_REF`
   exclusively reserves one generation. The controller must persist/prove this
   intent remotely **before** launching its one native worker. Supply the exact
   `request.input.messages`, with no worker tools or external acquisition. Source
   text is untrusted data, never instruction. The controller reference is the actual admitted owner/task known before launch;
   the actual child reference and raw terminal output are retained separately in its
   runtime receipt afterward. Neither is model-authored proof or an observed provider/
   token-usage record.
4. `accept --request REQUEST --expected-sha256 HASH --response RESPONSE` freezes one
   exact response. Its JSON envelope has exactly `schema` (`native-irritator-response/v1`),
   `phase`, `request_sha256`, `intent_sha256`, `controller_ref` and `result` (JSON object).
   Persist it, then advance. Invalid semantics hold this spent result; do not replace
   it or try a provider. A retained intent without result is unknown, not retryable.

After narrative and research plan, advance instead returns `acquisition_pending`
for `original_document` or `research_evidence`. Use `claim-acquisition --request
REQUEST --expected-sha256 HASH`, persist/prove the exact intent remotely, and only
in that same uninterrupted job run `advance --config config.yaml --session SESSION
--acquisition-intent-sha256 INTENT_HASH`. An exclusive started record precedes I/O.
A later process encountering an unfinished intent must hold; the hash flag is not a
license to reconstruct a lost execution. Engine-local fsync does not prove a remote
commit; that proof remains the controller's responsibility.

Original-page acquisition occurs only after a validated target. Declared source
searches and at most one external document occur after a validated research plan.
The existing public-fetch safety checks, query routing, complete-section evidence,
source exclusion and quotation/ranking rules remain authoritative. Both acquisitions
share at most **nine physical request starts** and **180 seconds of measured source
acquisition**, including redirects and address fallbacks. Native worker waiting is
separately bounded by its controller, not disguised as source acquisition time.
Completed snapshots, elapsed time and counts replay exactly with no HTTP; incomplete
acquisition is held, never refetched or reset to a fresh budget.

Configured non-English presentation uses one bounded native presentation phase after
canonical analysis. Generated research follows configured English, while delivered
targets retain their original language. Only narrative claim and ranked narrative-claim/reasoning
fields can change. Existing URL, numeric, literal quotation, identity and coverage
checks apply; evidence and ranking membership stay canonical. The native route does
not silently use the configured translation API. If the existing input allowance
cannot hold the whole presentation, canonical text is retained with explicit
localized fallback notice/status, not reported as successful Russian translation.
Structurally accepted translation still carries the existing fidelity notice; these
checks alone are not a semantic-equivalence claim.

All artifacts live beside the archived edition: `.native.post-attempt.phases.json`
and its `.phases/` directory contain session, request, intent, started, response and
content-addressed acquisition records. Persist the complete subtree, including nested
acquired bytes, plus the existing attempt/result/Markdown/fragment files. Native
result schema 3 (`NativeResearchResult`) is a distinct variant whose provenance binds
the exact session and all phase references. Older result dataclasses and provider
bytes are unchanged. The existing fragment result hash and projection reader retain
and revalidate these native provenance bytes; no second fragment journal is added.

Finalization returns `native-irritator-complete/v1` with honest complete/empty/incomplete/
error status and exact result references. A qualifying result becomes the existing
pending compact fragment, not a new send. Native abstention can finish empty with no
acquisition or fragment. Exact completed sessions can be read back without work.
Interrupted finalization preserves all files and holds without rewriting output or
restarting generation/acquisition. Preserve the original claim and intent even when
an attempt marker appears unfinished. Repair requires separately reviewed exact-byte
proof; never delete records or recompute hashes to force another attempt.

Rollback: stop the runtime's optional continuation, preserve the entire native session,
result schema 3 and fragment references, and restore only a reader that understands
these variants. Older workers must not overlook a spent native claim or pending
fragment. Primary ready/claim/receipts and delivered messages remain unchanged.

### Native fragment destinations

The existing native export → project → accepted import sequence can include one
already-accepted, eligible pending counterevidence fragment. Projection selects it
once; `native-composition/v3` binds its exact immutable projection, full text, origin
and result references, placement and independently computed covering chunks.
The normal final acceptance must approve those exact bytes before import. There is
no post-acceptance append, fresh investigation, invented provider review, extra
article/vote identity or new fragment journal.

The block follows accepted main cards and any admitted comparison, before closing.
Main/closing/comparison prose stays unchanged, and mandatory source credits remain
intact. If initial optional selection is absent, expired, conflicting, wrong-owner,
reserved elsewhere or unverified, only the supplement is omitted. A proved layout
failure archives that fragment under the existing `supplement_rendering_failed`
disposition; it does not discard accepted mains. Once a fragment is in a projection,
changed evidence, disposition, owner or eligibility before import **holds** that
projection. It is never silently omitted under its old acceptance. Conversely, a
fragment arriving after a no-fragment projection cannot change that accepted edition.

Native fragment editions use `NativeSupplementEdition` ready schema 5, preserving
schema-3 provider and schema-4 native field sets and serialized bytes. Their archive
and Telegram payload use the same complete fragment; ready checkpoint references
include its projection, result and origin archive. The mutable attempt is not an
immutable checkpoint. Import reserves the existing attempt for the exact new ready
hash; claim and send require that reservation. The sender never rerenders or searches.

Consumption uses actual confirmed fragment-chunk coverage, not a new requirement
that every unrelated later primary chunk succeed. If fragment coverage is incomplete,
receipts remain unapplied/held and the fragment stays reserved. If all fragment chunks
are confirmed but a later chunk is unknown, the fragment is consumed exactly once,
while the edition remains honestly partial/unknown and cannot be resent. Failed
consumption persistence leaves the edition unapplied. A fully confirmed schema-5
edition remains a valid next native investigation origin; its carried fragment is
not treated as a newly selected source article.

**Interrupted import:** retry only the same retained projection and acceptance.
If the exact ready already exists, no dispatch evidence conflicts and the fragment
is still the same pending slot, import can finish its reservation and clear the native
pointer without rewriting ready bytes or recomposing. An already matching reservation
is idempotent. A claim, unknown/partial receipt, changed pointer or foreign reservation
is not a retry opportunity. Retain all files and inspect the actual state.

The inherited ready-before-prior-dispatch-cleanup write order can leave old claim or
receipt bytes mismatched to the new ready after interruption. This remains a closed
hold, not an automatic cleanup success. Preserve current and old hashes; locate the
exact predecessor ready/claim/receipts in immutable Git history and verify matching
owner, complete confirmed coverage and applied accounting. Only a separately reviewed
exact-state repair may retire those proved predecessor files. Do not send again or
remove a claim based only on a positive status field. Without predecessor proof, stay
held.

Only an expired **unclaimed** ready with no claim/receipt evidence can release its
reservation. Release is bound to that exact old ready hash and retained before a
later edition replaces it. Native schema-5 release also revalidates final acceptance
and immutable supplement references. A subsequent eligible D+1..D+3 native edition
can then project the same fragment anew and obtain its own final acceptance. Claims,
uncertain delivery and consumed fragments are never released for replay.
If reservation persistence failed, the same exact-current-ready proof can establish
release without inventing a reservation. A stale earlier release proof is not proof
for this ready. A genuinely D+4-expired fragment stays expired throughout recovery;
it is not resurrected merely to let a later primary edition proceed.

Rollback must preserve schema-5 ready, final projection/acceptance, reservation and
source/result files. An older schema-4-only engine/controller cannot adopt or downgrade
them; keep publication held until compatible readers are restored. Runtime checkpoint
allowlists, remote barriers and schema-5-aware validation must be reviewed together
with the engine pin. This engine patch alone does not activate those controllers.

### Outcomes and later-edition fragments

Empty search results, unavailable/unsupported adapters, rejected evidence and failed
model stages are different outcomes. Optional translation failure retains canonical
fallback according to its existing contract. An accepted compact result can freeze one
fragment for an ordinary edition on origin day D+1..D+3, after main cards and before
the closer. No extra dispatch or model work is created. Unusable material remains
explicitly archive-only; partial searches retain their limitations. Actual UTC D+4
expires unused material; a future ineligible edition does not expire it early.
The legacy standalone transport retains its separately documented behavior.

Fragment editions use ready schema 3 with independent supplement chunk coverage and
an explicit current-review checkpoint. Complete owner-matching coverage must be
consumed before the applied receipt marker. Claimed/unknown publication is never
automatically released or replayed. The runtime must persist the existing consumed
attempt under `digests/` with `.cache/` receipts and provide the private owner ID to
post-prepare. See [ADR0007](decisions/0007-compact-issue-reservation.md) for the complete
rollout and unclaimed-ready release contract.

New compact markers bind `question-routed-documents-v2`, the effective configured
Hacker News/arXiv/DEV subset and the query ceiling. Each of at most three queries
chooses suitable configured sources rather than automatically using all of them.
A document-only plan is allowed. DEV remains its unauthenticated first-page search
adapter; operators enable it explicitly in `irritator.sources`.

The target's original page is read for actual discovery links; the plan may select
one supplied link ID or one query's first eligible returned document. At most one
external document is read. Discovery snippets/titles remain leads, not verified
article evidence and cannot enter delivered ranking or pass counter-evidence
eligibility after a failed read. General HTML supplies complete extracted text; RFC HTML can
supply actual complete natural sections with qualifications and cross-references.
Missing, ambiguous or incomplete requested sections are held. No model-generated
URL or shortened evidence prefix is substituted.

A shared maximum of nine actual source HTTP starts includes the original page,
searches, the external document and every document redirect/address fallback.
Document reads therefore use capacity that would otherwise be available to searches.
Each document is bounded by 20 seconds, 2 MiB raw/decoded bodies and three redirects;
all work remains under the existing 180-second investigation deadline. There are
at most three model calls; optional translation retains its separate contract.
The 8,000-JSON-character cap covers the ranking candidate array, not the complete
prompt or provider token admission. Oversized complete evidence is omitted whole
with a limitation. This does not enable primary `reading_brief` processing.
See the [ADR0011 amendment](decisions/0011-source-anchored-investigation-queries.md#2026-10-15-question-routed-documents)
for this delivered-only contract and [ADR0022](decisions/0022-versioned-bounded-search-policy.md)
for the retained unbound baseline.

An older compact attempt marker or a changed source-policy binding is held before
execution. Preserve it and inspect its original engine/configuration; do not
remove markers or archives to rerun an old edition. Completed attempts and saved
results remain protected. Confirm no pending old-policy attempt crosses a rollout.

Independent comparison and genuine external counter-evidence are separate operations.
Neither matching quotations, successful transport nor offline synthetic fixtures
establish useful model judgment, semantic applicability or faithful conclusions.
The [Irritator model](domain/irritator/overview.md#current-domain-model) describes the
source/evidence relationship; [transport compatibility](STATE_AND_EFFECTS.md#transport-protocols)
describes the different sending protocols.

## Experimental source reading

`reading_brief` is off by default and requires English canonical text, review-led
selection and an explicit model route. It runs only through `--prepare-edition`;
unsupported preview/direct-publish modes stop before source/model work. Completed
source pages are a technical evidence handoff, not an accepted or published edition.
The former standalone reading-angle renderer and delivered-marking Python helpers
have been retired; they were not a supported CLI path. Existing source snapshots and
historical delivered records remain readable. See the
[internal API compatibility note](CHANGELOG.md#unreleased--reliability-rehabilitation).
For the immutable reconciliation input, saved operation proof and parser limits,
see [Reconciliation ownership](#reconciliation-ownership).
The unused `digest.reading_points` grouped-point prototype is retired with immutable
historical links in [ADR0010](decisions/0010-group-source-points-with-qualifications.md#retire-the-unused-grouped-point-prototype--2026-10-09).
Direct Python callers must follow the [ADR0009 migration](decisions/0009-selected-source-admission.md#one-immutable-reconciliation-authority--2026-10-09).
Unknown generation outcomes remain held across invocations and route changes.
The [accounting guide](reading-brief-accounting.md) describes verified profiles,
optional offline tokenizer preparation and the bounded configured fallback. Unknown
profiles remain technical pending; advertised context does not establish free quota.
Proposed [ADR0009](decisions/0009-selected-source-admission.md) retains the open
factual-quality, reconciliation and throughput gates. The separate
[closed, unmerged PR #93](https://github.com/Lenivvenil/digest/pull/93) is not on main.

### Reconciliation ownership

Experimental source preparation reloads the current handoff, source, page state and
eligibility through [`source_reading.checkpoint`](../digest/source_reading/checkpoint.py).
[`build_reconciliation_input`](../digest/source_reading/reconciliation.py) then validates and
freezes one evidence input for offline planning and execution. The
[`source_reading.operation`](../digest/source_reading/operation.py) owns its exact
request, admission, attempt history and completion envelope; its decoder verifies
persisted records against the fresh input before cache reuse, uncertainty holds or
fallback. Fresh generation saves accepted metadata before validating terminal output.
The content parser only checks response schema, citation membership and retained
qualifications. Complete source access and sparse evidence remain distinct; none of
these checks establishes semantic completeness or publication acceptance. Reading
remains optional and off by default. [ADR0009](decisions/0009-selected-source-admission.md#one-immutable-reconciliation-authority--2026-10-09)
records the direct-call migration and retained trust boundaries.

The supported `python -m digest.reconciliation_checkpoint` command remains a thin
[CLI entrypoint](../digest/reconciliation_checkpoint.py) to that checkpoint owner,
with the same arguments, help and exit behavior. Other old direct reader/reconciliation
Python imports are retired; use the
[package migration map](decisions/0009-selected-source-admission.md#source-reading-package-layout--2026-10-11).
This layout change does not activate reading or change retained state and request hashes.
