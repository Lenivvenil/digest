# ADR-0005: Opt-in translation of publication text

Status: accepted, implemented and deployed. Finite runtime acceptance under #94 passed on 2026-10-03: independently reviewed ordinary archived narrative translation using prompt v2, exact canonical fallback on a rate limit, and confirmed compact delivery/archive state. General semantic fidelity is not claimed.

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
  attempts do not automatically repeat. A cache failure prevents the call or falls back
  to canonical text. A partial translated batch is never a mixed-language publication.
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
different models or providers. There are no extra retries, workflow timeout increases,
new feature flags or scheduling changes. With the existing 65-second shell delay, the
post-delivery step is bounded by 320 seconds including dispatch, within its 360-second
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
