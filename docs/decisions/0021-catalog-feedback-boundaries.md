# 0021. Separate proposal and feedback rules from persistence and Telegram

Status: #147-C deployed through engine PR #155 and runtime PR #78 on 2026-10-07.
The source-catalog continuation deployed through engine PR #156 and runtime PR #79.
The discovery continuation deployed through engine PR #157 and runtime PR #80.
These ownership releases do not establish source activation or editorial acceptance.

[ADR0024](0024-full-article-vote-identity.md) subsequently versions future article
vote tokens and ready editions. The original extraction below preserved the short
identity contract; historical records and source-proposal identities remain unchanged.

Refs [the staged architecture](../ARCHITECTURE.md#stage-5-c-catalog-and-feedback-boundaries),
[ADR0003](0003-source-state-split.md), [ADR0006](0006-batch-message-voting.md),
[ADR0013](0013-discovery-exploration-state.md) and
[ADR0020](0020-explicit-model-execution.md).

## Decision and owners

`domain/catalog/proposals.py` owns the mutable five-field `PendingSource`, URL hash,
exact proposal binding and decision-time resolver. `domain/feedback/values.py` owns
the existing feedback records and retained reply/replay vocabulary. Its `rules.py`
owns known-article votes, source decision capture and application eligibility,
separate replay namespaces, reply-state transitions, confirmed delivery attribution
and latest-vote scoring. Domain rules receive explicit decision times and perform
no clock, environment, filesystem, HTTP or application operations.

`adapters/storage/feedback.py` owns strict/permissive decoding, file-existence checks,
exact-byte hashing/verified batch reads, local atomic persistence and its post-success
caller mutation. `feedback_exists`, `feedback_sha256` and `read_exact_batch` keep
filesystem access in storage while application ordering remains explicit. The adjacent
`pending_sources.py` owns proposal file loading and pruning. Neither codec calls
the decision-time resolver: stored proposals can be future-dated or expired while
being valid storage records.

`adapters/telegram/feedback.py` owns private-owner validation, Telegram envelopes,
commands and payload parsing, cursor freshness, webhook checks, one bounded poll,
and bounded terminal reply transport. It delegates state transitions to feedback
rules and never reads feedback or proposal files. The command-reply function passed
by the application resolves `/status` and `/bubble` at the existing dispatch point;
this is a single concrete boundary, not a general callback/event framework.

`application/feedback.py` owns collection, local persistence and exact-byte
acknowledgement. `application/run_state.py` retains source application, config
reload and fresh `ModelExecution` ownership. Existing public `feedback.py` exports
and proposal/storage exports from `discovery.py` remain compatible. Public scoring
and resolver wrappers retain their clock-sampling signatures; production callers
use actual owners and pass an independently sampled time at each decision.
Discovery generation, delivery metadata, approval-card transport and YAML editing
remain in their existing owners. Adaptive scoring and lifecycle are not redesigned.

## Preserved identities and rules

- URL identity remains eight MD5 hex characters with `usedforsecurity=False`.
  Proposal SHA-256 remains compact, sorted, non-ASCII-escaped UTF-8 JSON over all
  five fields, including the exact `discovered_at` string. Equivalent instants
  spelled differently do not share a binding.
- Resolution requires exactly one matching hash across the whole pending list,
  valid nonempty string fields, a matching URL hash, and age from zero through
  30 days. A collision is not resolved by filtering invalid or expired entries.
  Strict storage accepts valid future/expired timestamps; save keeps future
  entries, drops old ones, and retains unparsable timestamps as before.
- Collection binds a decision to the current exact proposal. Application calls
  `applicable_source_decision` against a freshly loaded pending list and a new
  decision time immediately before acting. The two checks protect distinct trust
  boundaries; one does not replace the other.
- Votes need known article attribution. Scoring selects the latest timestamp per
  article, the later stored entry wins equal timestamps, and the window uses
  whole-day `age.days`. `/bubble` continues to count raw events separately.
- Callback and owner-message replay ledgers remain separate and capped at 1,000.
  Cursor freshness remains six days with the existing old-generation reanchoring.
  Ratings retain the existing 30-day save window; attribution keeps the last 1,000
  insertion-ordered entries. A new valid poll supersedes old pending replies.

## Ordered effects and failures

Collection orders owner validation → strict feedback reload/stale-store check →
webhook check → one `getUpdates(limit=100)` → envelope validation → strict pending
read only for owned source input → copy/reduce → strict feedback save → optional
exact-byte acknowledgement → strict feedback reload. Corrupt pending state blocks
a relevant batch before any event is consumed or feedback written. A malformed
envelope blocks the entire batch. Saving prunes a copy and changes caller fields
only after successful replacement.

Acknowledgement reads the exact file bytes, verifies SHA-256, strictly decodes them
and verifies the private-owner binding before sending. It sends at most one vote
summary and one source summary, with five seconds per request and a 30-second total
budget. Callback IDs, raw messages, vote values, article identities and owner IDs
remain absent from logs and ordinary summaries. Terminal reply state is cleared
and strictly saved even after bounded reply failure; save failures still propagate.
The empty-reply path still returns without rewriting the file.

Source application preserves each approved config addition → history write →
pending write → feedback write. Rejected decisions skip config addition. Config
failures retain that decision and continue; later write failures can leave a
committed prefix. Existing idempotent config additions support retry without new
rollback. The caller still catches persistence errors, reloads config and returns
a fresh execution holder whenever it invokes config reload.

Local atomic replacement is not remote durability or a multi-file transaction.
The managed feedback collect/commit/ack barrier and discovery pair barrier remain
runtime responsibilities. No schema, remote state list, migration, source setting,
model route, live-call policy or operational schedule changes.

## Validation and rollback

Existing feedback, discovery, main, bubble, delivery and source-state regressions
cover behavioral compatibility. Bounded synthetic before/after fixtures compare
exact proposal/feedback bytes, collection/replay/ack state and request traces, and
source-application write order including an interrupted feedback-write prefix.
Boundary checks cover effect-free domain imports, adapter independence from
storage/application, compatibility value identity and explicit-time rules.
These are offline structural checks, not real runtime or editorial acceptance.
Rollback is a compatible reviewed code revert or engine pin retaining every
runtime file, decision and receipt; no state reset or automatic replay is needed.


## Source-catalog ownership continuation — 2026-10-07

Before this slice, `config.py` defined source/adaptive declarations and
`source_scorer.py` combined source values, implicit clock reads, source rules,
JSON codecs and `/bubble` rendering. Prepared delivery separately encoded the
same statistics/lifecycle JSON. After this slice:

- `domain/catalog/sources.py` owns the unchanged declaration, statistics and
  lifecycle dataclasses. `config.SourceConfig`, `config.AdaptiveConfig` and the
  source-scoring value exports alias those exact classes. Their field order,
  defaults, mutability, runtime type hints and dataclass restoration are retained.
- `domain/catalog/source_rules.py` owns quality factors and weights, trending,
  effective/feedback-only priority arithmetic, fetch and confirmed-inclusion
  accounting, trial eligibility/thresholds and lifecycle mutations. Pure score
  preparation computes existing factors and parses recency before the time
  observation; final scoring receives the observation explicitly.
- `application/source_scoring.py` composes those rules and retains public
  signatures and sampling points: one sample per fetch; one sample per scored
  source only for nonzero fetches and valid `last_seen`; separate eligible-source
  observations in priorities and trials; no sample for skipped trials. Trial
  eligibility still uses the supplied `today`, while recency uses its independently
  observed clock. It does not reuse the run start or publication timestamp.
- `presentation/bubble.py` renders unchanged text from an explicit report time.
  The application samples once at the former entry point after the caller's reads.
- `adapters/storage/sources.py` owns permissive loaders/writers and shared encoders
  for source statistics, lifecycle state and category mapping. Prepared delivery
  shares the encoders without adopting legacy setup, pruning or error handling.
  Production imports use these owners; `source_scorer.py` retains aliases.

Legacy save ordering is intentionally observable: create parent directory → prune
inactive statistics in the caller → encode → try atomic replacement. A directory
failure precedes pruning; an encoding failure propagates after pruning; a write
failure is logged and can leave the caller pruned. State and category-map saves
likewise keep setup and encoding outside their caught-write blocks. Prepared source
writes do not create directories, prune sources or swallow failures. Encoding keeps
field/source insertion order, default JSON escaping/indentation and absent trailing
newline. Strict and permissive loading policies are not unified by sharing encoders.

Existing source, configuration, bubble, delivery, main and restoration regressions
remain the first validation. Focused additions cover exact alias/restoration identity,
per-source clock sampling across date boundaries, byte parity, source-save setup and
encoding failures, mutation on failed legacy writes, and strict prepared failures.
The existing effect-free import check also covers both catalog source modules.

At the end of the source-only slice, discovery generation, exploration rotation/
cooldown, delivery metadata, approval transport, source history and YAML editing
remained in the mixed `discovery.py` owner. The continuation below extracts them.
Broader delivery codec/transport separation and historical domain documentation
reconciliation also remain. No source activation, quota, algorithm, clock policy,
provider call, runtime setting, persisted schema or deployment changes are authorized
by this source ownership slice. #147 remains open.


## Discovery ownership continuation — 2026-10-07

`domain/catalog/exploration.py` owns existing area rotation, offer retention, expiry,
validation retry eligibility, reservation and batch/proposal checks over the unchanged
metadata. `ProposalDelivery` is the same frozen result contract re-exported by the
compatibility facade. Rules receive decision times and perform no I/O or clock reads.
This is a bounded extraction, not a general rewrite of schema-1 state into new types.

`adapters/storage/discovery.py` owns the metadata codec, optional-field defaults,
256,000-byte read/write bound, atomic writes and exact pending/delivery file hashes.
Unknown fields, key order and JSON encoding remain intact. Legacy receipt and history
timestamps still accept naive ISO values; exploration timestamps still require a
timezone. Pruning does not normalize legacy dates or repair their comparison behavior.
`adapters/storage/source_config.py` owns the comment-preserving YAML insertion,
validation and backup/temporary-file replacement. Additions still use trial_days 14,
independently of the SourceConfig declaration default of 7, and are idempotent by exact
URL. Backup creation failure aborts; replacement failure keeps the original/backup
and cleans the temporary file; backup cleanup failure after success remains a warning.

`adapters/telegram/discovery.py` owns exact approval-card text/buttons, the bounded
POST and explicit rejection versus uncertain receipt interpretation. It never reads
or writes discovery state. `discovery_feed.py` stays the existing cohesive URL/DNS,
redirect and feed-content validation adapter.

`application/discovery.py` now owns history, prune, pending validation and persisted
batch sending as well as generation. Preparation retains the original run-time sample;
every non-expired pending identity gets its own subsequent decision-time observation.
Every reserved proposal is independently observed while validating the complete batch,
before any send. Each pre-POST unknown transition samples again. History samples only
for a new binding/decision entry. Pending-file save retention retains its own original
clock observation in its existing adapter. No shared timestamp replaces these checks.

Ordered effects remain explicit: prune metadata/history write → pending write; pending
reservation write → delivery reservation write → caller's hash output barrier. Sending
verifies both supplied exact file hashes before loading, checks owner/target/pending
hash and unique bounded bindings, validates every proposal/reservation, then persists
unknown before each POST and the final receipt afterward. A failed final write leaves
the persisted unknown hold and stops later sends; it does not count confirmation.
Any existing receipt prevents reoffer. Pending revalidation deliberately ignores the
redirect destination, preserving the original Add identity; generated proposals store
the validated final URL. Attempts still persist before generation, and failure does
not reclaim its validation budget or become editorial rejection.

Production callers use the actual owners; `discovery.py` retains public aliases and
its original clock-sampling resolver wrapper. Existing tests run first, with only
private patch targets moved. Additional critical cases cover byte/default preservation,
legacy versus exploration dates, whole-batch clock boundaries, ordered partial writes,
unknown receipt failure, redirect identity and effect-free policy/transport ownership.
A synthetic before/after probe matches 30 scenarios and 49 exact byte snapshots,
including YAML, Telegram payloads and clock/effect traces. This proves local structural
parity, not runtime durability, deployment, source activation or editorial acceptance.

Broader delivery codec/transport separation and historical domain-documentation
reconciliation remain open. No source policy, model route, call budget, schedule,
schema or state migration changed. #147 remains open.

## Retained successful local collection — #148 G9, 2026-10-10

`FeedbackStore.last_successful_poll_at` is one appended optional string, defaulting
to empty for legacy files. Only `application/feedback.collect_feedback` refreshes
it: sample timezone-aware UTC after validation/reduction, immediately before the
existing strict candidate save. Successful local persistence makes that sample
retained evidence; it is not the precise completion time of atomic replacement.
Empty and duplicate-only batches qualify. Cursor observation still requires a
newly observed update ID and retains its independent six-day freshness rule.

Pre-save failures preserve the prior bytes and input store. Subsequent acknowledgement
or output failure does not undo local persistence. Generic saves, acknowledgement,
source application and prepared delivery attribution preserve this observation
without refreshing it. No additional write, request, retry or ledger is introduced.

The existing `feedback_poll collect` CLI logs the previous retained successful local
collection and its age after strict loading, before polling. It then reuses its one
saved-file read for both exact-byte SHA-256 and strict decoding. After the existing
`GITHUB_OUTPUT` write succeeds, it logs the persisted successful local collection
before the unchanged stdout stage/counts JSON. The logs render canonical UTC and
nonnegative integer age seconds; missing, malformed, naive or future times yield a
bounded unknown reason, never the raw unusable string. Failure adds no reread or
post-success claim; the pre-poll line remains explicitly historical.

This intentionally changes saved bytes and their hashes, while preserving stdout,
`GITHUB_OUTPUT` and exact committed-byte acknowledgement contracts. Present non-string
values follow the existing metadata error; unusable strings stay readable. There is
no eager migration or backfill from cursors, counts, file/Git times or intended cadence.
An older compatible reader accepts the new field but drops it on its next write;
returning to a new reader then truthfully reports unknown. Rollback must still retain
the [full-vote-token compatibility floor](0024-full-article-vote-identity.md#deployment-and-rollback-floor).

One locally saved bounded batch does not prove remote durability, acknowledgement,
a drained queue, complete historical feedback or digest delivery. A skipped invocation
emits no evidence; age cannot reconstruct missed invocations or lost votes. No alarm,
threshold, schedule, activation or recovery policy follows. Keep observations in the
existing private state/logs; public evidence uses synthetic cases and aggregate results.
This is partial G9 progress under #148; release, rollout and natural-run observation
remain separate gates, not full reliability or editorial acceptance.
