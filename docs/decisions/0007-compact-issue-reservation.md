# ADR-0007: Compact daily presentation with a coarse publication reservation

Status: compact transport deployed; the 2026-10-04 ready-edition boundary update is accepted for implementation under #120, pending rollout and natural-release acceptance.
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
and its canonical/translated archives. The original archive-only Irritator decision
is superseded for new target-bound attempts by the #208 policy below. No independent
optional Telegram dispatch is added. An incomplete external stage remains incomplete;
format choice does not prove its factual utility.

## 2026-10-09 later-edition supplementary evidence (#208)

An accepted target-bound investigation can retain one immutable fragment projection
beside its existing result archive. Its existing per-investigation attempt owns the
mutable pending/reserved/consumed/expired disposition in the existing
`supplement_status` field. There is no queue, index, worker,
extra model request, search request, retry, job or billing allowance. An occupied or
unverifiable pending slot skips a new optional investigation while primary delivery
remains independent. Incomplete overall investigations may contribute individually
accepted material counter-signals, with their coverage limits retained visibly.

An origin edition on UTC day D can contribute to an ordinary edition on D+1 through
D+3. The actual UTC clock reaching D+4 expires an unreserved fragment; preparing
a future D+4 edition earlier only makes that destination ineligible and does not
discard remaining D+1..D+3 opportunities. This is an explicit bounded product
tradeoff, not a factual-currentness guarantee; origin, investigation and source dates
remain visible. Expiry never releases a claimed or uncertain dispatch.

The fragment follows main cards and precedes an admitted positive closer. It has no
article identity, vote buttons, deduplication entry or source-statistics entry. The
existing lossless renderer and whole-edition transport deadline remain; there is no
new one-chunk or total chunk-count cap. Required main copy, source evidence, qualifiers
and closer are never removed or truncated to make room. A rendering conflict archives
the fragment with an explicit reason. Its existing translated presentation or canonical
fallback is reused without next-day translation or a second semantic model check.

Main-only readiness retains schema 2. Fragment editions use schema 3, which old
senders reject before claim or POST. New readers accept exactly schemas 1, 2 and 3;
schema 1 retains legacy eight-character votes, schemas 2/3 use full article hashes.
Claims and receipts retain schema 1 because their exact ready hash includes fragment
bytes, immutable references and complete supplement coverage. The trusted renderer
creates the ordered payload and coverage once; the sender validates references and
coverage bounds, then consumes the exact frozen bytes.
Mutable attempt records never enter checkpoint references. Sending uses frozen payloads,
not a second renderer or prose parser.

Schema 3 also names the current publication's review checkpoint explicitly. The
origin review is a separate immutable reference and can never become the next
post-delivery target merely by sorting before the current review filename.

`included_ready` is a reservation, not consumption. An expired unclaimed ready may
release its fragment only after verifying the exact old manifest/hash and absence of
claim, receipt and orphan dispatch state. Only the latest exact release proof is retained.
The old binding and release reason are saved
before ready replacement; the new reservation is saved after ready and before claim.
Missing proof or a crash between writes holds the optional fragment; it is never guessed
or overwritten. Frozen-reference corruption blocks dispatch.

`application.prepared_delivery.send_prepared_edition` owns the required consumption
write before its exact-readback applied marker (ADR0017 receipt-owned amendment).
Every fragment-covering chunk needs a positive owner-matching receipt. A later unrelated
unknown chunk does not undo complete fragment coverage, but the overall issue stays held.
Incomplete fragment coverage or a failed consumption write leaves receipts unapplied and
the fragment unconsumed. These ordered local writes are not a transaction or replay licence.

Deployment requires two changes to existing runtime steps: supply `TELEGRAM_CHAT_ID`
to post-delivery preparation for owner verification, and include `digests/` alongside
`.cache/` in the receipt/application persistence barrier. This persists the consumed
attempt together with applied receipts. No bot/model credential is needed for prepare;
no new job, schedule or supplemental persistence ledger is introduced.

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

## 2026-10-04 amendment: freeze readiness before claiming delivery (#120)

The previous coarse reservation before preparation is superseded for the managed
workflow by a versioned ready-edition boundary. Preparation saves accepted canonical
cards and review evidence in one publication-day-bound checkpoint before translation/rendering.
Unavailable analysis is not cached as a successful empty decision. The ready manifest
then freezes exact ordered Telegram payloads, buttons, complete article-to-chunk
coverage, canonical/presentation hashes, producing engine provenance, archive hashes,
recipient and UTC publication window/expiry. There is no new editorial certification.

The runtime commits/pushes preparation and readiness before a sender may claim them.
A separate immutable claim binds to the ready-file hash and recipient; its own remote
hash barrier precedes any POST. Sender code never loads feeds, calls a model, translates
or re-renders. Current model/prompt and unrelated config changes do not invalidate a
supported frozen manifest. Recipient, bot username and enabled delivery policy still
apply. Readiness expires at the end of its intended UTC publication day; no silent stale send.

Each accepted chunk must contain Telegram's positive message ID and matching chat.
Exact receipts and complete article coverage are saved. The sender reloads current
feedback/dedup state, merges confirmed attribution, then marks coverage applied.
A failure between those writes leaves an unapplied receipt hold. This deliberately
requires inspection rather than automatic reconciliation of partially saved counters.
The final Git commit/push persists those changes together; a failed/non-fast-forward
push leaves the remote claim held. Never automatically rebase operational state.

The existing two jobs retain their 8/12-minute ceilings and shared concurrency. The
first prepares; the second sends first and may inspect an older eligible ready edition
when preparation failed. Optional work runs only from the confirmed delivery checkpoint
and remains archive-only within the job's remaining time. Failure of an optional stage
must not be described as failure of already-confirmed primary delivery.

Migration preserves legacy confirmed current-day markers and delivered article hashes.
Legacy reserved/sending/partial/unknown markers remain holds. A legacy clean not_sent
record is not evidence of a Telegram attempt; the new protocol may prepare an edition.
No automatic deletion/reset of uncertain state, new cron, service or extra live test
message is introduced. Keep one active ready edition; eligible or held data cannot be
replaced by a newer preparation. Inspect expiry/missing readiness explicitly.

Local failure injection covers the preparation/persistence/claim/transport boundaries.
The existing confirmed daily edition is never replayed as a test. Operational acceptance
requires the next natural new edition's receipts and remote state, separately from #55
and #77 semantic quality. Exact-once delivery remains impossible to promise.

The default intended publication day is today in UTC. `--edition-date YYYY-MM-DD`
explicitly prepares a later UTC day's edition, including after today's delivery is
confirmed. Its creation timestamp is separate from its publication window: it remains
`pending_window` and cannot be claimed or sent before that day's 00:00 UTC. Expiry is
the end of the intended day, not creation plus 24 hours. A current-day edition prepared
at 23:59 expires at 00:00; a tomorrow edition remains eligible through tomorrow. No
new schedule or automatic choice of a future publication day is introduced.

## 2026-10-07 implementation boundary: confirmed-outcome application (#145)

[ADR0017](0017-confirmed-delivery-application.md) records the ownership extraction
without changing this reservation/receipt protocol. `domain/delivery/outcomes.py`
owns the result values and pure article-to-chunk coverage projection shared by
prepared and direct compact sending. `application/delivery.py` applies each
scenario's attribution, deduplication and source accounting through their owners;
`adapters/storage/delivery_state.py` owns strict prepared-state writes. The caller
marks prepared receipts applied only after the application returns successfully.

The [architecture effect matrix](../ARCHITECTURE.md#stage-3-confirmed-delivery-application)
records preserved prepared/direct differences, including Markdown consumption,
source-statistics/lifecycle policy and write failures. Prepared writes remain
feedback → statistics → optional adaptive state → seen articles. A successful
application prevents repeated inclusion counts while hashes remain in the delivered
cache; an interruption before the applied marker is a held inspection boundary,
even if earlier files were already saved. This is neither a multi-file transaction
nor automatic recovery. Partial/unknown transport outcomes remain held after known
coverage is applied. The runtime still owns the remote Git persistence barrier.

Shared projection does not change transport acceptance: prepared receipts require
a positive message ID and matching chat; direct compact retains its existing
HTTP/`ok` check and coarse reservation. Legacy cards retain their retry/fallback
policy. No schema, identity, receipt history or automatic resend policy changes.
