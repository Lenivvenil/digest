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
The optional closer's existing `closing_contract_invalid` fallback is separate
and is not further diagnosed by this failed-batch field. These reasons identify
structural rejection, not semantic fidelity or the cause of earlier failures.


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
