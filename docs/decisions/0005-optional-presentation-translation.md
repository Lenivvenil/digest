# ADR-0005: Opt-in translation of publication text

Status: the original translation slice is accepted, implemented and deployed. Finite runtime acceptance under #94 passed on 2026-10-03: independently reviewed ordinary archived narrative translation using prompt v2, exact canonical fallback on a rate limit, and confirmed compact delivery/archive state. The bounded recovery amendment below is tracked separately under #55; it does not extend that runtime evidence. General semantic fidelity is not claimed.

## Context and authority

[#94](https://github.com/Lenivvenil/digest/issues/94) requests English product defaults
and separately configurable post translation. Existing `radar.language` controls
analysis, review prompts and Irritator queries, so it cannot become a presentation
language alias. Existing explicit Russian configurations must not acquire another
model call implicitly. Article titles also participate in deduplication and voting
identities and must remain unchanged.

## Decision for this bounded slice

- An absent `translation` section preserves legacy behavior, including the omitted
  language default and number of model calls. An explicit new translation section
  defaults canonical generation to English only when no legacy language is specified.
- Enabled translation requires canonical `radar.language: en`. A conflicting explicit
  Russian generation setting is rejected with migration guidance, not silently changed.
- Translate generated card summaries, category-summary prose, and published Irritator
  narrative/assessment prose after analysis. Keep original titles, source metadata,
  URLs, literal quotations, evidence and raw reviews canonical. Presentation targets do not alter review/search input.
- Pin one explicitly configured existing provider/model from `llm.providers` or an
  explicit `review.primary`, `review.secondary` or `review.tie_breaker` mapping. Implicit
  review defaults do not authorize reuse. Ordinary role routing is unchanged; do not
  enter the fallback chain.
  Bound calls, total translation time, input allowance and output tokens. Configuration
  does not prove account pricing: the operator must choose an entitled free route for a
  free-only runtime. No new credentials or provider is configured automatically.
- Retain canonical text and cache compatible translated batches by canonical content,
  target, provider/model and prompt identity. Reserve before a call; incomplete/unknown
  attempts do not automatically repeat. Only the [bounded prepared-main recovery](#bounded-prepared-main-429-recovery-55)
  below permits an in-process extra attempt on a newly reserved batch. A cache failure
  prevents the call or falls back to canonical text. A partial translated batch is never a mixed-language publication.
- Use the same presentation for Telegram and Markdown; never change delivery accounting
  or resend policy. Dry-run translation uses temporary storage only.

## What validation does and does not establish

Require complete field-ID coverage, unchanged URLs/numeric literals and recognized
literal quotation/code spans, and a provider-reported normal completion. These are
structural checks. They do not prove preservation of every actor, condition, negation
or qualification. The output identifies machine translation and its unverified
semantic fidelity. Translation must never upgrade the verification status of a draft.

The observed prompt-v2 narrative preserves its canonical meaning. The primary
publication hit a provider rate limit and retained all canonical fields with one
visible fallback notice; that Telegram edition was English. This is finite observed
coverage, not proof of every technical term, the earlier ingest wording, or a Russian
Telegram delivery. Existing interruption/cache/version tests complement that runtime
evidence. The #55 factual-quality gate remains independent of presentation.

## Deliberate remaining scope

Main currently supports en/ru; arbitrary language support is not claimed. Canonical
supplementary rendering was made lossless in #98, and the separate primary prefix
repetition defect was fixed in #99. The same translation setting covers generated
Irritator prose; raw reviews, literal source quotations, source titles and evidence
remain canonical. Broader corpus fidelity is not claimed by the finite acceptance; the documented
failures retain canonical text. No new delivery subsystem or inference framework is implied.


Normal completion includes the existing adapters' stop/STOP and Anthropic end_turn;
max_tokens/tool_use are not normal completion. See the provider's
[stop-reason contract](https://platform.claude.com/docs/en/build-with-claude/handling-stop-reasons).
A known pacing/cooldown wait beyond the remaining translation deadline creates no
attempt record. Operators may explicitly choose a total timeout up to 180 seconds
(default 90); existing explicit values, including 30, remain unchanged. An attempted translation
failure accepts canonical fallback for that version instead of silently replacing an
already delivered post. New content has its own key; recovery is not a global disable.


## Bounded supplementary presentation

Legacy primary and synchronous counter-signal prose share one translation invocation
and its call/time allowance. The separate post-delivery process first saves the
canonical analysis JSON, then translates copies for both Markdown and Telegram.
Markdown diagnostics remain canonical. The translation cache retains canonical text;
source titles, literal quotes, evidence IDs and delivery markers are not translated.

For enabled translation only, processing receives 180 + min(configured translation
seconds, 45) seconds from the start of analysis. Analysis retains its 180-second cap.
Translation is clipped to its own deadline and the remaining shared processing time.
The existing 30-second overall dispatch allowance stays separate. With a 65-second
shared interval, a three-call analysis finishing near 132 seconds can begin translation
near 195 seconds and complete before 225 seconds; the default 90-second helper budget
permits that wait. An explicitly configured 30-second budget may fall back instead.
This is headroom, not a delivery or provider-latency guarantee.

The evidence-stage configuration clone explicitly shares its pacing/cooldown runtime
with presentation when translation is enabled. No quota independence is assumed for
different models or providers. This supplementary path adds no retries, workflow
timeout increases, new feature flags or scheduling changes. With the existing
65-second shell delay, the post-delivery step is bounded by 320 seconds including dispatch, within its 360-second
step limit. A full 360-second comparison step leaves 40 seconds of the 720-second job
for setup/persistence; the hard job timeout remains the final bound, not a guarantee
that arbitrary runner/install latency will fit. Absent/disabled translation preserves
the existing analysis runtime and call count.

## Source-attribution ownership amendment (#146)

[ADR0018](0018-review-reuse-and-source-attribution.md) places reviewed exact-feed
notices in `presentation/source_attribution.py` and immutable main-occurrence lookup
in `application/source_attribution.py`. Lookup validates required provenance before
calls; literal notices are appended after translation and before rendering/split
checks, archive and freeze. Canonical prose, translation request/cache bindings,
fallback and allowances remain unchanged. This ownership extraction neither
activates sources nor extends the finite semantic acceptance recorded above.

## Optional HTTP rejection facts (#55 / G5)

New failed translation records may include `http_rejection`, the bounded safe
response facts described in [ADR0020](0020-explicit-model-execution.md#optional-translation-http-rejection-facts--55--g5).
Its optional [named Groq quota detail](0020-explicit-model-execution.md#optional-named-groq-quota-facts-55)
retains only a recognized dimension and bounded numeric values; it does not
change retry policy or establish historical quota cause or translation success.
The existing route, binding, `error` and `error_kind` keep their meaning. Optional
facts must fit after existing business sizing; otherwise they are omitted without
changing cache bytes, fallback or allowance. Absent facts mean unknown. Successful,
reserved and historical cache records are unchanged and are never rerun to collect
metadata. This establishes neither complete request accounting nor quota cause,
translation fidelity or broader #55 runtime acceptance.

## Optional validation reason (#55)

Failed required translation batches may retain `validation_reason`: one of
`extraction`, `envelope`, `field_shape`, `identity_or_text`, `protected_content`, or
`coverage`, owned by the existing parser. It is present only for that parser's
controlled error during `contract_invalid`; legacy `error="ValueError"` remains.
The optional field is added after normal record sizing and omitted if it cannot
fit. Absence means unavailable, including historical records. Old readers ignore
it and failed reservations remain terminal; this diagnostic addition changes neither
retry policy nor prompt binding.

Only the closed code is retained, never response excerpts or exception text.
These reasons identify structural rejection, not semantic fidelity or the cause
of earlier failures.


### Optional protected-content attribution (#55)

The same parser may add `validation_detail` for a required field's protected-content
mismatch: its already validated canonical `field_id` and a fixed-order `changed`
subset of `url`, `numeric_literal`, `quotation`. It records no tokens, translated text
or general-log detail. Field IDs are bounded to 256 UTF-8 bytes; optional detail is
at most 1024 serialized bytes and must fit the existing cache ceiling after base sizing.
Invalid/oversized detail is omitted without removing other accepted diagnostics.
Old readers ignore it; saved failure records stay terminal and are never rewritten.
This attributes a later structural failure only. Missing details remain unknown;
neither semantic fidelity nor a historical failure's exact cause is established.

### Optional closer rejection diagnostics (#55)

New combined records with valid main translation and a rejected optional closer
may retain the same top-level `validation_reason` and, for protected content,
`validation_detail`. The optional parser's existing controlled error supplies the
reason and canonical `closing.summary` identity with the same fixed categories;
generic errors add no invented detail. The nested optional result keeps its exact
six-field shape and `closing_contract_invalid` fallback. No rejected text, malformed
identity or exception text is retained.

Business-record sizing runs first. Diagnostics are projected only while the final
optional outcome is still `closing_contract_invalid`; a final
`closing_output_allowance` outcome carries no stale validation reason. Projection
is best-effort within the existing ceiling and cannot itself omit the closer,
discard valid main prose, change fallback or add a write. Persistence failure keeps
the existing terminal reservation and in-memory main result. Required-failure
diagnostics retain their existing ordering and meaning.

Readers, schema and prompt identities, request/selection bindings, nested hashes,
reservation and replay rules are unchanged. Historical records are never enriched,
rewritten or retried. A missing reason or detail remains unknown, including earlier
rejected closers. This improves visibility of future structural rejection; it does
not establish numeric equivalence, semantic corruption or closer quality.

## Attached numeric literals and saved-cache compatibility (#55)

Numeric extraction retains the complete recognized literal when a suffix touches
the digits: `20ms`, `1.5ms` and `100MB/s` protect `20`, `1.5` and `100` respectively.
Faithful spacing or localized suffixes can therefore pass, while changed attached
values fail. The leading word boundary, signs, separators, percent and exact literal
spelling remain significant; there is no unit catalog or numeric normalization.
Punctuation can begin a recognized digit run even in identifier-like text. This
lexical check does not validate unit meaning, mathematical equivalence, scientific
notation or semantic fidelity.

Prompt/request identities, cache schemas and bindings are unchanged. Successful
main-only and combined main/optional records are already revalidated on read:
unchanged valid records still reuse, while previously accepted changed attached
values now take the existing canonical fallback without rewriting bytes or making
another call. A newly invalid cached optional result retains the existing whole-batch
fallback; fresh optional parsing still isolates an invalid closer from valid main
prose. Failed/reserved records stay terminal. On rollback, an old reader can reject
newly accepted unit-spacing translations through its existing fallback, also without
rewrite or retry. Frozen ready payloads remain authoritative and are not reparsed.
These synthetic compatibility checks do not identify a historical failure's cause
or establish broader #55 runtime acceptance.

## Bounded prepared-main 429 recovery (#55)

Ordinary prepared publication (`review.enabled` and `review.review_led_only`) opts
both main-only and selected-closing presentation into keyword-only
`retry_explicit_429`. Translation helpers and the application wrapper default it to
false. Manual prepared commands and new translation while resuming accepted work
share this owner policy, without scheduler detection. Category, standalone, legacy,
dry-run and supplementary callers keep their false default; frozen editions bypass
translation.

`translation.max_calls` bounds newly attempted base batches; cache hits are free.
Opted-in invocations allow at most one extra `complete()` call in total, not per
batch: at most `max_calls + 1`, or two when configured as one. `TranslationResult.calls`
includes the extra but is not an exact transport ledger: shared admission can hold a
call before dispatch. Each physical attempt retains its existing model-budget
reservation without refund. Other callers retain the existing call ceiling.

Only a first completion raising `RuntimeError` with exactly one call-local rejection
whose integer HTTP status is 429 permits the extra. Missing diagnostics fail closed;
exception text, historical metadata and optional provider error codes cannot admit
it. Route, messages, output limit and provider `max_retries=0` stay unchanged. A second
429, timeout, cancellation, 5xx, credential failure, unknown/incomplete completion,
validation failure or persistence failure receives no retry.

The existing shared cooldown, including raw `Retry-After` handling and its 60-second
fallback, is authoritative; longer waits are not shortened. Bounded diagnostic header
values do not drive waiting. Before waiting and again before the extra completion,
admission checks route cooldown and existing pacing against the same absolute
deadline. At or beyond that deadline, fallback retains the first rejection without
a second call. Shared cooldown extensions are rechecked after sleeping; waiting and
completion stay inside one deadline, never renewed per batch or attempt.

The configured timeout and any earlier caller/closing deadline remain authoritative.
Default 90 and the supported range are unchanged; a separately reviewed setting
change from 90 to 150 supplies 150, without a hard-coded engine clamp. That shared
setting also lengthens standalone and legacy deadlines without enabling retry. The
[post-delivery extension](#bounded-supplementary-presentation) remains
`min(configured translation seconds, 45)`. Independent runner deadlines still apply;
translation headroom does not guarantee archive or persistence completion.

Both calls use the same exclusive, flushed/fsynced cache reservation, with no
intermediate write permitting another process to retry. Concurrent access or a crash
leaves the reservation terminal. Valid successful records reuse; saved failed,
reserved, malformed or newly invalid records stay held without rewrite or calls.
Final atomic replacement gains no directory-fsync guarantee. Existing combined-record
persistence failure can retain validated main prose in memory while omitting the
closer and leaving the reservation terminal; it never grants another call.

Failed-record diagnostics describe only the final attempted completion, or the first
rejection if no extra call was admitted. Success retains no rejection metadata or
first-attempt history. Cache schemas, prompt/request hashes, canonical text and
validators are unchanged; retry/time allowance is not request identity. Rollback
restores the previous engine pin and timeout while preserving caches, accepted
preparations, markers, archives, ready editions, claims, receipts and coverage.
Never delete state to unlock recovery.

The paired [one-attempt report-only resume](../ADVANCED_OPERATIONS.md#independent-comparison-and-resume)
receives no unused translation allowance. Primary fallback and investigation
generation remain unchanged. This does not identify the limiting provider quota
or establish semantic fidelity or broader #55 runtime acceptance.

## Optional selected-source comparison (#55)

A single `synthesis.enabled` boolean, off by default, allocates one optional
comparison after ordinary main presentation and pending-supplement preflight.
It unconditionally surrenders report-only resume generation at both resume
entrypoints, including old markers. Investigation retains its three stages.
Unsupported modes and omitted/failed synthesis do not refund that allocation.
This is selected-main-subset analysis, not a category summary, independent model
review, historical trend or comprehensive source survey. #55 remains open for
ordinary editorial and runtime acceptance.

Only a review executed in the current ordinary preparation can dispatch new
synthesis after acceptance. Capture that fact before candidate reconciliation
populates the packet report. Restored reports, accepted v1/v2 checkpoints and
direct callers cannot create a new attempt. Current ready/claimed/held work wins
first. A matching retained valid synthesis may be reused during accepted recovery;
disabling synthesis produces baseline presentation even if that capture exists.

Resolve all selected main RSS EvidenceItems, in accepted order, from the immutable
report/occurrence graph. No closer, generated card reason, translated card,
unselected item, full source or newly fetched page enters the request. Missing or
ambiguous bindings omit the stage. Two distinct retained source identities are
required, but do not prove independent ownership or reporting.

One controlled Groq `openai/gpt-oss-120b` secondary-route completion returns
canonical English and, for RU publication, paired Russian prose. It has no
independent translation or repair stage. The original v1 JSON bound exactly two support IDs,
occurrence hashes and exact 1–160-character excerpt quotations; relationship and
qualification are mandatory bounded fields. Both language fields must preserve
the existing URL, numeric-literal and literal-quotation multisets. Missing/unknown
completion, malformed output, unsupported support or invalid presentation omits
the whole comparison. A successful RU comparison cannot repair failed main
translation or upgrade its existing fallback/verification metadata.

The original v1 complete escaped localized block included qualifications, both original
quotes, source labels/links and interpretation/coverage notices, within 1,000
UTF-16 units. It appears after main cards, before any existing supplement and the
closer. The full-publication gate preserves chunk count, every article's values,
identity and covering chunks, supplement identity/coverage, vote keyboards and
required source-credit completeness. The comparison must occupy one chunk.
Otherwise use baseline presentation without trimming primary content or producing
an additional message. Markdown and frozen delivery receive identical selected
comparison text. Synthesis has no article identity, votes or source/delivery credit.

This is structural/layout isolation with bounded transport exposure: visible text
changes bytes of a shared Telegram chunk and can affect that request's acceptance
or latency. Same chunk count is not literal payload isolation. Field/quote checks
also cannot prove entailment, semantic equivalence, relevance or useful analysis.
A positive ordinary capture requires independent excerpt → canonical comparison →
presentation review; abstention is safe degradation, not positive acceptance.
No live call or runtime activation follows from offline test success.

Fresh opted-in accepted snapshots use the existing deterministic deferred-status
owner to say only that counter-signal investigation follows primary delivery.
The normal main translator handles that sentence. Its category-digest request/cache
binding changes intentionally; card/closer fields, routes, retries and validation
remain unchanged. All newly created snapshots get the truthful status even when
the report was restored, while new synthesis dispatch remains fresh-review-only.
Historical accepted/frozen text is never rewritten or patched in Russian. The
archive's status-only projection follows the retained accepted sentence.

See [execution/deadline ownership](0020-explicit-model-execution.md#optional-synthesis-allocation-and-deadline-55)
and [retained attempt authority](../STATE_AND_EFFECTS.md#optional-synthesis-attempt).

### Publication containment for unaccepted comparison prose

The strict `synthesis.publish` boolean defaults to `true`, preserving existing
configuration behavior. An explicit `false` with synthesis still enabled omits
only the generated comparison from newly prepared reader output. It does not
alter request construction, quota allocation, completion persistence or reuse.
The existing bounded attempt still runs when eligible, and report-only resume
remains surrendered. This setting is not a way to save or refund that call.

After normal preparation, a valid result receives presentation metadata status
`withheld` and reason `publication_disabled`; its result, evidence bindings and
request hash remain present. Canonical review reports and synthesis sidecars are
not rewritten. Main cards, supplements, closer, attribution and receipt coverage
keep their existing owners and validation. Existing ready payloads are replayed
exactly even if the current setting changes; publication containment is not a
retroactive edit or retraction.

Exact quotations and distinct source IDs validate provenance, not entailment of
the relationship. Containment is therefore not useful-analysis acceptance under
#55. Restoring accepted comparison publication requires source-specific support
for each claimed property and independent review of actual canonical and
translated output. No new verifier call or acceptance waiver is implied.


### Draft source-specific v2 amendment (#55)

**Status: local candidate for owner review, not released or editorially accepted.**
The prior global relationship could assign source A's property and source B's
property to both systems even when both quotations were exact. The proposed
change makes source observations explicit before the model relates them. It
reduces that generation/attribution risk; no deterministic entailment claim is made.

The response and prompt become `selected-source-comparison-v2` / schema 2, within
the same one completion. A comparison has exactly two supports in accepted source
order, `premise_ids` repeating those two IDs, canonical prose and optional RU prose.
There is no shared-mechanism classification or property matrix whose partial
supports could be mistaken for proof of a shared compound property.

Each wire support contains only its source ID, occurrence hash and paired
observations. After parsing, code resolves that ID/hash and attaches `field=excerpt`
and the **complete exact retained excerpt** to the normalized result. The model
is not asked to copy quotes or choose spans, saving schema/output verbosity.
The request already contains the full evidence, and the existing sidecar retains
its immutable bindings. This keeps all supplied actors, negation, conditions and
qualifications available for review. It
does not claim the RSS excerpt is the complete article or that source claims are
true. Each support also contains a 1–180-character canonical observation and,
when RU is selected, its paired observation. Each observation must be supported
in full by its own source, including every clause; the prompt forbids borrowing a
property from the other source through compound wording.

The former global factual `relationship` field becomes an explicit
`interpretation` (1–240 characters) over those two observation IDs. The model is
instructed to explain a useful comparison of design questions, trade-offs or
implications without adding implementation facts about either or both systems.
A qualification is null or 1–160 characters and included only when material to
that comparison. Missing implementation evidence remains unknown, not evidence
of absence or disagreement. A material caveat cannot be dropped merely to fit.

The renderer revalidates result IDs/hashes, complete excerpt context, order, field
bounds and paired protected content against caller-supplied retained bindings.
The application still owns provenance of those bindings, including source names
and URLs; the renderer does not independently reconstruct the occurrence graph.
It supplies each immutable source name and article link beside the selected-language
observation, followed by the interpretation
and any material qualification. It does not place full English source excerpts,
canonical observations or routine translation diagnostics into RU reader text.
Complete inputs, quotes, paired observations and response remain in existing
private sidecar/ready metadata. The same source IDs own both languages; numeric,
URL and literal-quotation invariants are checked per observation, not only as an
aggregate that could move a number between sources.

The complete escaped reader block still must fit 1,000 UTF-16 units and all
existing publication-layout/coverage gates. Overflow omits the optional block;
there is no shortening of its caveat, trimming of main content or extra message.
The existing publication containment setting remains the evaluation mechanism:
use `synthesis.publish: false` while assessing this candidate. No new approval
record, token, per-edition workflow or persistent permission is introduced.

Storage retains the same schema-1 attempt envelope and filename solely keyed by
accepted preparation. Prompt/schema changes do **not** create a new attempt key.
Existing v1 valid, failed and reserved records remain byte-for-byte preserved and
are non-reusable by v2; load rejection takes the existing omission path and cannot
dispatch again. Do not delete/migrate a record to obtain another attempt. Existing
ready payloads, including v1 result metadata, remain opaque frozen bytes for the
sender and do not acquire a v2 parser or sidecar dependency. A new eligible
ordinary preparation may use v2 only after separately authorized adoption.

#### Finite acceptance and residual semantic failures

Synthetic tests reject source-ID replacement, model-supplied quote/field overrides, reordered
premises, lost paired fields and per-source protected-content swaps. They retain
six counterexamples that can still be structurally accepted in generated prose:
unrelated support, changed negation, changed actor, invented condition, compound
property transfer and RU scope widening. An unsupported global factual union in
`interpretation` also remains an explicit editorial failure. Naming a field
interpretation is not permission to publish such an error, and exact excerpts
are not a semantic validator. There is no automatic shared-property certificate.

A hand-reviewed useful target distinguishes two separate design questions, such
as data transfer and collection/inference separation, assigning each stated
mechanism only to its own source. A list of quotes, a generic question or mere
abstention is not positive analytical acceptance. Before owner-authorized
restoration of publication, review the finite source/canonical/RU/rendered corpus
and an actual ordinary newly generated candidate from the final request version.
Review every source observation, the connection, material qualifications and
language attribution. Preserve that evidence privately. This finite acceptance
path does not guarantee all future model output, add calls/retries or authorize
a live request, release, runtime change or #55 closure from offline tests.
