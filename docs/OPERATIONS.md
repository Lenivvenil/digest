# Publish and recover an edition

Prepare accepted work, publish its frozen bytes through the managed barriers, and inspect retained evidence if any step is interrupted.

- [Prepare and publish](#ordinary-preparation): acceptance → remote ready proof → claim → remote claim proof → send → persist outcome
- [Inspect an interrupted edition](#inspect-an-interrupted-edition): read-only command, then the complete recovery matrix
- [Collect feedback and decide on sources](#feedback-and-source-decisions), or [discover new sources](#source-discovery)
- [Upgrade without losing evidence](#upgrades-and-retained-state)

Run commands from the private runtime working directory that owns `config.yaml`,
`.cache/` and the output archive. New runtime? Start with [configuration](CONFIGURATION.md#runtime-configuration)
and the [safe quick start](../README.md#try-it-safely).

## Choose the scenario

| Task | Entry | Important boundary |
| --- | --- | --- |
| Prepare the ordinary review-led edition | `python -m digest --prepare-edition` with review enabled, review-led-only mode and compact delivery | Does collection/model/presentation work as needed, but does not send. |
| Inspect, claim or send a frozen edition | `python -m digest --edition-phase inspect\|claim\|send` | Sending consumes exact persisted ready/claim hashes and makes no model call. |
| Preview Radar output | `python -m digest --dry-run --radar-only` | Can fetch sources and call models. It is not a no-network check. |
| Run an independent report-only comparison | `python -m digest.review_trial` | Uses independently pinned review slots; it does not publish a primary edition. |
| Resume an admitted supplementary comparison | `python -m digest.review_resume` | Uses a saved checkpoint and a separately persisted attempt marker. |
| Propose sources | `python -m digest --discover` | Saves proposals and requests approval; it does not activate them. |
| Run legacy direct/category delivery | Ordinary non-prepared invocation with its configured mode | Retains different transport, Markdown and guard rules. Do not mix it into the prepared protocol. |

The `--check` command probes feeds. To validate configuration without external calls,
use the disabled example and commands in the quick start. Model routes, credentials,
source activation and schedules belong to the operator's runtime.

## Ordinary preparation

<a id="review-led-delivery-without-legacy-enrichment"></a>

The ordinary mode uses `review.enabled: true`, `review.review_led_only: true` and
`telegram.delivery_mode: compact`. It does not generate legacy category summaries
or wait for an independent comparison before preparing selected cards.

The managed runtime owns feedback collection, durable persistence and acknowledgement
of the exact saved batch. Pass `--feedback-precollected` whenever that separate stage
owns ingestion, including if its optional collection/persistence/acknowledgement fails,
so preparation cannot consume another uncommitted batch. The flag skips polling; it
does not certify prior success. Without it, preparation uses its normal feedback path.
See the [feedback sequence](#feedback-and-source-decisions); local saving alone is not
a remote persistence guarantee.

```sh
python -m digest --config config.yaml --feedback-precollected --prepare-edition
```

Preparation processes feedback/approved source changes before recovery. It then
prefers an existing ready/claimed edition, or a matching accepted preparation,
before fresh collection. A completed candidate report is a different object: it
is considered after collection and current eligibility reconciliation.

A fresh packet uses one primary attempt and, only if primary output is invalid or
unavailable, at most one configured secondary fallback. The resulting report can
remain `incomplete` for independent comparison even when its cards are usable.

| Editorial result | Preparation outcome |
| --- | --- |
| Valid selected cards, including a validated partial result | Save canonical work, then attempt presentation/archive/freeze. Rejected or unresolved items remain visible in review/candidate evidence. |
| Valid primary abstention with complete required disposition evidence | Save an accepted empty preparation; return no ready edition. Repeating the same publication window does not select another packet. Older packets without disposition capture retain their existing compatibility behavior; fresh candidate packets record attempts. |
| Primary invalid/unavailable; secondary supplies valid selected cards | The permitted fallback supplies cards with its actual provenance; independent comparison is still incomplete. |
| Primary invalid/unavailable; secondary only abstains | Remains incomplete. It is not an accepted no-news result. |
| No usable selection, unfinished output or unresolved empty metadata | Retain unfinished evidence; no ready edition. Ordinary candidate validation failure reports `selection_incomplete`. |
| No eligible candidates | No edition is created. This is not evidence that all sources succeeded or every observed item received an editorial judgment. |

`no_ready` is a publication outcome, not a universal no-news verdict. In particular,
the supported category path also retains its existing `no_ready` result for empty
or failed category analysis. Inspect the saved review/checkpoint and logs before
assigning an editorial meaning to that status.

### What preparation preserves

- `.cache/pending_preparation.json` contains accepted canonical work for the intended
  publication day. It is the recovery point after translation, attribution, archive
  or render failure.
- `.cache/prepared_edition.json` contains the final Telegram payloads and their
  evidence/archive bindings. It is the only input the prepared sender may publish.
- The review archive and candidate proof explain which occurrence and response
  support the accepted cards. A source quote cannot validate unrelated generated claims.

The accepted checkpoint is removed after successful readiness freeze. A valid empty
accepted checkpoint remains until its publication day is past. Corrupt or mismatched
active work raises a failure; it is not silently replaced by another selection.

### Intended publication day

Without `--edition-date`, preparation targets today in UTC. Use
`--prepare-edition --edition-date YYYY-MM-DD` to prepare a later day explicitly.
The ready edition remains `pending_window` until that day's 00:00 UTC and expires at
the end of that day. A preparation made just before midnight does not receive another
24 hours merely because it was created late.

A confirmed/applied edition can permit preparation for a later day. An unresolved
claimed edition blocks replacement, including on a later day. There is one active
record set, not a queue of independently sendable editions.

## Persist, claim and send

The engine writes local files. The managed runtime must establish remote durability
before the next irreversible step. Use its existing persistence workflow rather than
inventing a resend path around a failed push.

| Boundary | Required remote evidence | Next engine operation |
| --- | --- | --- |
| Readiness | Successfully persisted `.cache/prepared_edition.json` and its referenced archive/evidence, with the exact ready-file SHA-256 verified from the remote revision | Create the claim. |
| Claim | Successfully persisted `.cache/prepared_edition_claim.json`, with ready and claim hashes verified from the same remote revision | First send in the continuing managed attempt. |
| Outcome | Persisted receipts and resulting feedback, delivered cache and accounting, including partial/failure states | Inspect completion; optional work must not discard this evidence. |

The workflow-provided `READY_SHA` and `CLAIM_SHA` below are SHA-256 hashes of whole
file bytes. They are not the edition ID or the manifest's internal content hash.
An existing local Git object or locally calculated hash alone does not prove a
successful remote push.

```sh
python -m digest --config config.yaml --edition-phase claim --ready-sha "$READY_SHA"
# Persist the claim and verify both hashes from the successful remote revision.
python -m digest --config config.yaml --edition-phase send --ready-sha "$READY_SHA" --claim-sha "$CLAIM_SHA"
# Persist receipts and resulting operational state, including on partial failure.
```

Claiming validates the edition window and referenced evidence before creating an
immutable claim. Sending validates the exact ready/claim bindings, owner, bot and
checkpoint bytes. It creates `sending` receipts before POST, records attempted count
before each chunk, and records each known positive matching-chat confirmation afterward.
The prepared transport does not retry or switch to plaintext.

Complete article coverage and complete edition transport are different. Every chunk
covering an article must be confirmed before its identity is attributed. A final
notice can fail after all article cards were delivered. After transport, the prepared sender owns exact-receipt application of known
coverage and the final `applied` marker. Reporting results do not authorize replay;
see the [receipt-owned effect contract](STATE_AND_EFFECTS.md#output-effect-matrix).

## Inspect an interrupted edition

Start with the read-only command below. Then match the retained evidence to the
[recovery matrix](#delivery-states-and-recovery); a status or missing receipt alone
cannot establish what reached Telegram.

### Inspect without publishing

Inspection performs no collection, model request or Telegram POST. The command
publishes `edition_status` and `ready_sha256` through `GITHUB_OUTPUT`; its exit code
alone distinguishes neither all normal states nor all failure causes. Outside a
managed job, capture those outputs in a temporary file:

```sh
digest_inspection_output=$(mktemp)
GITHUB_OUTPUT="$digest_inspection_output" python -m digest --config config.yaml --edition-phase inspect
cat "$digest_inspection_output"
rm -f "$digest_inspection_output"
```

A held inspection returns exit code 1; ordinary `missing`, `ready`, `pending_window`,
`confirmed` and `expired` inspection returns 0. Malformed/binding errors fail rather
than inventing a valid state. In a shell using automatic exit-on-error, retain the
inspection output even when the command reports a hold.

## Delivery states and recovery

<a id="prepared-editions-and-delivery-recovery-120"></a>

The inspector keeps its conservative status contract and adds a bounded warning for
valid prepared holds. It distinguishes unresolved dispatch, incomplete application,
and an applied marker with incomplete transport. The warning uses already-read
records: state, persisted attempted/confirmed/total chunks, fully covered articles
and existing evidence filenames. Absent receipt counts are unknown, not zero.
Unrecorded transport effects and the accounting write prefix cannot be inferred.
Inspect the retained files, bindings and workflow attempt; never treat missing
receipts or marker presence as permission to resend or reapply. Legacy compact
holds and malformed-binding errors retain their separate existing behavior.

| Observed evidence | Meaning | Safe next action |
| --- | --- | --- |
| No ready file, claim or receipts; no active accepted checkpoint | No recoverable prepared edition is present. | Use the normal preparation path within its existing budget. Inspect collection/review outcomes if it again returns no ready edition. |
| Valid matching `pending_preparation.json`; no active ready/claimed edition | Canonical work was accepted but presentation/freeze is unfinished, or it is an accepted empty result. | Run preparation for the same intended day. It resumes accepted work; empty acceptance remains no-ready for that day. Do not rerun selection to replace it. |
| Missing, replaced or mismatched accepted preparation reference | The active saved work does not match the supplied reference, or its requested publication day is invalid. | Inspect and preserve the checkpoint and failure evidence. Do not clear/rewrite it to bypass verification. No new ready/receipt hold is created; category fetch statistics/map may already have been written before entry verification failed. |
| `selection_incomplete`, without accepted preparation | A usable editorial result was not accepted. | Retain candidate/review evidence and diagnose the recorded failure. A later admitted preparation may continue; [inspect candidate continuation](ADVANCED_OPERATIONS.md#candidate-continuation). The result must not be reported as editorial rejection or accepted no-news. |
| Unclaimed `pending_window` edition | Frozen content targets a future UTC day. | Preserve it and wait for the publication window. Do not claim or send early. |
| Unclaimed eligible `ready` edition | Content is frozen but not yet reserved. | Verify the remote ready/evidence revision, then use the normal claim/persist/send sequence. Claim validation still checks referenced bytes. |
| A new claim created and persisted by the current uninterrupted managed attempt; no receipts yet | The workflow is between its claim barrier and first send. | Continue that same attempt with the verified ready and claim hashes. This is not recovery of an old unknown attempt. |
| A claim rediscovered after interruption; absent receipts | The earlier process may have sent before its local outcome was retained remotely. Inspection reports held. | Preserve the records and inspect workflow/Telegram evidence. Do not reclaim, regenerate or treat absent receipts as proof of no delivery. |
| Receipts `sending`, `unknown`, `partial` or `failed` | Some or all transport is unfinished, failed or uncertain. Known confirmations remain evidence; later automatic publication is held. | Preserve attempted/confirmed prefixes and matching message IDs. Inspect before any targeted recovery; never rerun the sender blindly, including for a later day. |
| Receipts `confirmed` with `applied: false` | Telegram transport completed, but operational-state writes or the final applied marker did not finish. | Preserve receipts and compare current feedback, statistics, lifecycle and delivered-cache records with the saved write order. There is no general automatic reconciliation command; blind reapplication can double-count a persisted prefix. |
| Matching prepared receipts `confirmed` and `applied: true` within the publication window | The edition and its application completed. | Treat inspection as a no-op. Do not send again. Preparation for a later intended day can proceed through the normal guard. |
| Expired unclaimed edition, or expired confirmed/applied edition | It is no longer eligible for sending. | Prepare a later/current permitted day normally. Do not reset unresolved claims: they remain held rather than becoming safe through expiry. |
| Corrupt/unsupported JSON, wrong owner, orphan claim/receipts, or mismatched hashes/references | The saved objects cannot establish a coherent publication attempt. | Retain the files and restore/repair only from verified matching evidence after checking possible external sends. Do not delete a marker or substitute unrelated older state to bypass validation. |
| Legacy `.cache/compact_issue.json` is reserved/sending/partial/unknown, or already confirmed for the relevant day | A legacy direct-compact attempt also constrains publication. | Inspect that legacy attempt. Switching to prepared mode or deleting the old marker is not a recovery procedure. |

A confirmed legacy marker may also produce the inspector's `confirmed` result;
verify which record supplied it before expecting prepared receipts or a manifest.

### Why an unapplied receipt needs inspection

Prepared application writes feedback, source statistics, optional lifecycle state,
and the delivered cache in that order, then marks receipts applied. If the third
write fails, the first two may already be present. A subsequent unconditional repeat
is not a transaction rollback and can count the same delivery again. The held state
exists to make that uncertainty visible.

Use the [current effect matrix](STATE_AND_EFFECTS.md#output-effect-matrix) and
retained Git/run evidence to establish what actually persisted. Recovery requires a
reviewed resolution of that specific prefix; this guide supplies no marker-reset or
blind resend command.

## Walk through the boundaries

This illustrative walkthrough follows a selected card through the existing contract.
It is a reader aid, not a new live-run or recovery procedure; no receipt or production
success is implied.

1. **Selection is accepted; presentation fails.** Retain `pending_preparation.json`.
   Same-day preparation resumes presentation without selecting again. A missing cache
   may still require the configured presentation route and its request allowance.
2. **Presentation succeeds.** The ready edition freezes exact payloads and their
   evidence/archive references, then clears pending preparation. Local success alone
   does not prove remote durability.
3. **The runtime establishes both barriers.** Persist and prove ready remotely;
   create and persist the claim; prove both whole-file hashes from the same successful
   remote revision. Only that continuing managed attempt proceeds to its one send.
4. **The card spans two chunks; chunk two is uncertain.** The card lacks complete
   delivered coverage. Other completely confirmed cards may have coverage, but the
   edition remains held. Missing receipts are not evidence of zero sends.
5. **Transport confirms; an accounting write fails.** Inspect the retained write
   prefix. Do not reset, resend or blindly reapply. A fully confirmed, complete edition
   with its applied marker is a no-op without collection, generation, rendering,
   transport or repeated accounting. Applied-but-incomplete transport remains held.

**Two empty results have different meanings.** A valid primary abstention with
complete required metadata saves an accepted empty checkpoint; same-window preparation
cannot consume another packet. An abstaining fallback after an invalid/unavailable
primary remains incomplete. See the [preparation outcomes](#ordinary-preparation)
for historical-disposition compatibility and the other no-ready cases.

## Feedback and source decisions

Feedback collection is independent of `adaptive.enabled` and uses no continuously
running bot service. The supported owner is the configured private chat; the sender
must match it. Group and inline callbacks are rejected. Set `telegram.bot_username`
to the plain username of the same bot as the token for 👍/👎 links. Tap a vote and
Telegram's **Start** button; the next eligible run collects the message. Commands
`/vote g <article-code>` and `/vote b <article-code>` also work without a username.
Only recognized vote data and `/status` or `/bubble` tags are retained, not arbitrary
message bodies.

For a managed runtime, keep these three steps in order:

```sh
python -m digest.feedback_poll collect
# Commit/push .cache/feedback.json; verify its SHA256 from the successful remote revision.
python -m digest.feedback_poll ack --expected-sha256 "$FEEDBACK_SHA"
```

`FEEDBACK_SHA` must identify those exact durably persisted file bytes. Pass
`--feedback-precollected` to the digest whenever the managed runtime owns collection,
including if its optional feedback stage fails, so the digest cannot consume another
uncommitted batch. Direct local use saves votes and polling offset together before
acknowledgement; local saving alone does not establish remote durability.

Only `application/feedback.collect_feedback` refreshes `last_successful_poll_at`.
It samples the UTC observation immediately before saving and retains it only if that
local save succeeds, including empty batches. Cursor trust remains separate.
The collect command emits two INFO observations:

- **Previous retained successful local collection**, before polling, shows the
  retained UTC time and age in seconds. It stays historical if this invocation fails.
- **Persisted successful local collection**, after the output-file write, reports
  the observation from the same saved bytes used for the exported SHA-256.

Missing, malformed, naive or future times mean unknown; a skipped invocation emits
nothing. Empty polls count as local success. Recent age proves neither complete vote
capture nor remote persistence/acknowledgement; old age does not count lost votes.
A post-save hash/output failure may leave newer state without the second log line.
Keep these observations private. Stdout stage/counts and `feedback_sha256` output
remain unchanged, but the new field changes saved bytes/hashes: verify the exact
remotely committed bytes before acknowledgement.

See [ADR0021](decisions/0021-catalog-feedback-boundaries.md#retained-successful-local-collection--148-g9-2026-10-10)
for timestamp semantics and older-writer field loss, and preserve the
[full-token rollback floor](decisions/0024-full-article-vote-identity.md#deployment-and-rollback-floor).

Ordinary vote/decision messages remain available for at most 24 hours. Legacy callback
buttons are best effort, with about 150 seconds of server retention. A sleeping or
failed schedule can lose uncollected feedback; see [ADR-0006](decisions/0006-batch-message-voting.md).
Saved votes survive later feed/model/delivery failures. Pending replies are best effort
and can be superseded by later collection. Corrupt state is retained for diagnosis.
Unknown/stale cursor history uses a non-confirming read without an offset before
re-anchoring, not an old high offset that could discard updates.

Only the latest valid vote per exact token is effective within the existing 14-day
feedback window. Repeated taps are not independent evidence. New article tokens use
the full 32-character identity; historical eight-character tokens remain supported
without guessed migration. Mixed old/full tokens may count separately, and old
buttons mean this is not a guaranteed short transition. Raw votes never enter model input.
Even with automatic adaptation disabled, feedback can adjust priorities within existing
bounds; unrated sources retain configured priority. With adaptation enabled, reliability,
productivity, description length and recency also affect source scoring, and trials may
graduate or be demoted. Disabling adaptation also disables automatic trial decisions.
See the [feedback contract](ARCHITECTURE.md#feedback-loop) and
[retained scoring rules](history/architecture-2026-10-08.md#source-quality-scoring).

New main-only ready editions use schema 2; editions carrying a fragment use schema 3.
Readers accept ready schemas 1, 2 and 3. Claims and receipts remain schema 1.
A known short-prefix collision in an unstarted
schema-1 edition blocks claim/send, while confirmed/applied history remains readable
and is never resent. After any full-token publication, both the sender and feedback
collector must stay on a compatible-reader engine. An old poller can discard full
tokens as malformed while advancing its cursor. Roll back using a compatible-reader
build or backport, not an arbitrary previous pin; see [ADR0024](decisions/0024-full-article-vote-identity.md#deployment-and-rollback-floor).
An allocation change does not guarantee article selection.

Source proposals use Add/Reject links followed by Start, or `/source ok HASH` and
`/source no HASH`. A private owner decision must match the exact saved proposal,
URL hash and age of 0–30 days during collection and application. A batch receipt means
**decisions saved**, not sources added. Approved sources are applied before collection,
even if later work is empty or fails. Configuration/backup failures retain the decision
for retry; unbound historical decisions cannot authorize replacement proposals.
[#48](https://github.com/Lenivvenil/digest/issues/48) tracks operational acceptance.

## Source discovery

Discovery is separate from Irritator. `python -m digest --discover` proposes feeds,
validates URLs, saves candidates and requests approval through Telegram when configured.
Configure [exploration areas](CONFIGURATION.md#source-discovery) separately from the
active professional source portfolio. This operation can call models, probe feeds
and send Telegram approval cards; it is not an offline preview.

One logical generation permits at most two configured routes, three feed checks/offers
and a 2,048-token output limit. Invalid pending proposals consume the current check
then skip one later eligible preparation before retry; managed retries do not consume
that skip. Confirmed messages, uncertain possible sends, reservations and explicit
API rejections remain distinct. Follow the [discovery state contract](ARCHITECTURE.md#trial-source-lifecycle-and-discovery)
and [ADR-0013](decisions/0013-discovery-exploration-state.md) for persistence and cooldowns.

Approval adds a priority-3 trial source; configuration and lifecycle state are separate
under [ADR-0003](decisions/0003-source-state-split.md). Discovery does not change engine
source files or install a weekly schedule. The proposal-generation slice of
[#132](https://github.com/Lenivvenil/digest/issues/132) leaves active-feed selection and
daily candidate scheduling unchanged; `adaptive.trial_slots` alone does not protect
important professional work after admission. Delivered recommendations still need
editorial evaluation.

## Upgrades and retained state

Use the package version, immutable commit and [changelog](../CHANGELOG.md) together.
Keep the previous engine pin and compatible runtime configuration for rollback.
The runtime owns its [cache and archived evidence](STATE_AND_EFFECTS.md#publication-records);
no database or always-on service is required.

Before rollback, verify that the chosen engine and configuration can interpret retained
state. Preserve receipts, deduplication/feedback records, translation records and issue
reservations. Hold publication if compatibility or an earlier delivery is uncertain.
Do not reset state or resend an edition merely because the engine was rolled back.
Optional supplementary failures must remain visible without erasing primary receipts.
