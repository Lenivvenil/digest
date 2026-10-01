# ADR-0005: Opt-in translation of primary publication text

Status: proposed for #94 implementation review; real translated-output verification remains pending.

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
- Translate generated primary card summaries and category-summary prose after analysis.
  Keep original titles, source metadata, URLs, evidence, raw reviews and separate
  Irritator supplements canonical. Presentation targets do not alter review/search input.
- Pin one explicitly configured existing provider/model; do not enter the fallback chain.
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

Real representative translation needs independent source/canonical-text review before
rollout. On failure, retain canonical English with visible degraded status. The #55
factual quality gate remains independent of this presentation step.

## Deliberate remaining scope

Main currently supports en/ru; arbitrary language support is not claimed. Separate
Irritator rendering has its own truncation behavior and must be reconciled under the
existing #94/#77 acceptance before supplementary translation is added. No new delivery
subsystem, inference framework or model experiment is introduced here.


Normal completion includes the existing adapters' stop/STOP and Anthropic end_turn;
max_tokens/tool_use are not normal completion. See the provider's
[stop-reason contract](https://platform.claude.com/docs/en/build-with-claude/handling-stop-reasons).
A known pacing/cooldown wait beyond the remaining translation deadline creates no
attempt record. Operators may explicitly choose a total timeout up to 180 seconds
(default 30); the runtime does not expand it automatically. An attempted translation
failure accepts canonical fallback for that version instead of silently replacing an
already delivered post. New content has its own key; recovery is not a global disable.
