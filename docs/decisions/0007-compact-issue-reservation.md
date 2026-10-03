# ADR-0007: Compact daily presentation with a coarse publication reservation

Status: accepted and implemented; runtime activation is deployed. Ordinary-output acceptance remains open.
Date: 2026-10-02. Work order: #91; presentation scope: #94.

## Context

The owner requests a simple daily digest within existing GitHub Actions/free-provider
limits. A separate Telegram post, vote instructions and repeated review/translation
notice for every article creates unnecessary noise. The legacy primary sender also
retries timeouts, even though Telegram has no sendMessage idempotency key. Changing
format alone does not make uncertain delivery replay-safe.

The [canonical operating envelope](../domain/digest/overview.md#operating-envelope-and-daily-edition-decision--2026-10-02)
owns the budget. Keep the existing primary/optional job ceilings and execution
isolation. No new service, queue, provider route, model request or polling schedule
is part of this presentation change. Full-source quality and genuine external
counter-evidence requirements remain open in #55/#77.

## Decision

Add one explicit `telegram.delivery_mode: compact` opt-in. Omitted configuration
keeps legacy `cards` behavior. Compact mode assembles all selected article texts,
original source URLs and indexed vote buttons into one logical issue, with one
publication-level attribution/translation notice. Numbering is presentation only;
vote identity remains the existing title/link hash. No first-N subset or text
truncation is introduced. Necessary transport chunks preserve complete text and
URL boundaries, measured in escaped UTF-16 units.

Only an article whose entire covering chunk set is confirmed contributes its
delivered hash and feedback attribution. The issue itself is complete only after
all chunks, including notice-only chunks, succeed. Compact POSTs have no retry or
plaintext fallback. A failed or unknown request stops subsequent chunks. The full
Markdown archive remains available.

Before the primary process, the managed runtime reserves one **whole issue** locally,
commits/pushes that marker through its existing Git boundary, verifies its exact hash,
and passes that hash to the primary CLI. The marker is bound to configuration and
private owner hashes. It is not a payload queue or automatic resend mechanism.

Immediately before the first POST the local marker moves to `sending`. Confirmation
is recorded only after transport results and strict local dedup/feedback persistence.
The existing runtime output commit makes those files durable together. A process or
runner crash leaves the earlier durable reservation unresolved. `reserved`, `sending`,
`partial` and `unknown` hold subsequent automatic publication, including the next day,
until inspection. Confirmed or proven no-delivery terminal states permit a later UTC
date; no automatic same-day repetition is allowed.

Feedback collection/persistence/acknowledgement remains before this publication guard.
It can continue even while publication is held. A clean exit without a POST records
`not_sent`; an exception after dispatch starts records `unknown`, with unknown counters
represented as unknown rather than zero. Missing/corrupt/mismatched state fails closed.

Compact presentation retains bounded Irritator and independent-comparison processing
and its canonical/translated archives, but sends no additional optional Telegram
supplement. `archive_only` is explicit and is never labelled `sent`. An incomplete
external stage remains incomplete; format choice does not prove its factual utility.

## Consequences and recovery limits

This deliberately trades availability for duplicate resistance: a crash before any
POST can still leave a reserved issue held until inspection. Do not delete or reset
the marker blindly, infer failure from a timeout, or replay an unknown send. Inspect
Telegram and retained state, preserve the evidence, and authorize any recovery
explicitly. Exactly-once delivery is not promised.

The engine verifies a supplied persisted hash; only the managed runtime can establish
the remote Git barrier. Calling the CLI with a locally computed hash alone does not
prove durability. Existing serialized runtime jobs and the unchanged hard job limits
remain necessary. Private verification is not automatically authorized by this ADR.

## Verification

Local checks cover exact content/URL/UTF-16 splitting, per-article chunk coverage,
indexed vote identities, final-notice failure, no retry after uncertainty, and
accepted POST followed by local persistence failure. Guard checks cover hash/owner/
config integrity, pre-POST write failure, clean no-POST exit and next-day unresolved
holds. The actual managed reserve→push→dispatch boundary must be reviewed and replayed
offline before rollout. Public CI is required; transport success alone does not close
the full-source, translation-fidelity, feedback-application or counter-evidence gates.
