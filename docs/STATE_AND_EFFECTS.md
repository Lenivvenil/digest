# State and effect contracts

Use this reference to establish what a saved result proves, which exact bytes bind it, and which effects may already have happened.

- [Preparation and presentation](#preparation-and-presentation): acceptance, explicit outcomes and ordering
- [Publication records](#publication-records) and [other retained state](#other-retained-state)
- [Send and apply](#send-and-apply), the [output-effect matrix](#output-effect-matrix), and [transport protocols](#transport-protocols)
- [Request identity and reuse](#request-identity-and-reuse), [public acquisition and diagnostics](#public-acquisition-and-diagnostics)

For commands and safe next actions, start with [Operations](OPERATIONS.md#inspect-an-interrupted-edition).
For the implemented lifecycle and code owners, use [Architecture](ARCHITECTURE.md).
These contracts do not establish deployment or observed editorial quality.

## Preparation and presentation

1. **Process independent input.** Load current operational state, collect feedback
   unless the managed runtime already did so, and apply verified source approvals.
   Reloaded source configuration takes effect before inspecting saved preparation.
   Feedback and approved configuration work can occur even when the edition is reused.
2. **Prefer an existing edition.** Ready, future-window, confirmed and held outcomes
   return before fresh collection. A completed edition can permit preparation for a
   later intended day; unresolved claimed work blocks replacement.
3. **Resume accepted work.** A matching `pending_preparation.json` resumes presentation.
   It does not acquire a new review result. Presentation can still call an explicitly
   configured translation route when its cache is absent.
4. **Collect and reconcile candidates.** Retain observed occurrences and current
   eligibility, including saved unfinished work. A feed failure is not a relevance
   judgment. If every feed fails, eligible saved candidates may still form a packet;
   otherwise collection failure remains a failure.
5. **Recover a candidate report or review a packet.** Reusable completed candidate
   reports are considered after collection and eligibility reconciliation. With no
   reusable report, persist the planned attempt before the bounded primary review.
   Planning does not prove that a provider received a request.
6. **Resolve the response, then accept projected cards.** One response-owned result
   binds the chosen primary/fallback review to its dispositions and optional closing
   capture. Fresh enabled v2 validates a separate closing card through the same live
   item owner, appends it after main cards, then derives selected dispositions and
   validates residual accounting. Consumers do not independently reselect a review slot. The comparison
   report remains an audit artifact and can still be incomplete. Valid projected
   cards, including accepted partial results, can advance. A valid primary abstention requires resolved disposition
   evidence when recorded attempts carry it, as fresh candidate packets do. Older
   packets without that capture retain their existing compatibility behavior.
   If the primary is invalid/unavailable and the secondary only abstains, the
   delivery-used result remains incomplete. It does not become accepted no-news.
7. **Save, verify and hand off accepted work.** Candidate and category admission have
   distinct editorial rules, then share `persist_accepted_preparation`: save through
   the existing codec, reload and compare the path and full canonical snapshot. The
   restored reference contains the path and existing canonical envelope-body hash,
   not a hash of the indented file bytes. Candidate handoff precedes fetch statistics;
   category statistics and its source map follow verified acceptance. This is local
   readback, not remote persistence or power-loss durability. A handoff is not delivery.
8. **Present and freeze.** `present_preparation` first reloads the requested publication
   day from the publication-owned `.cache` and compares path, body hash and the full
   snapshot with the supplied reference, even for empty work. It consumes the separately
   restored snapshot. Then validate required immutable source provenance before model
   presentation calls. Translate generated prose, append literal credits, check chunk
   coverage, write the enabled archive and freeze the exact edition. Clear the pending
   preparation only after the ready file is written. An accepted empty result stays
   as its canonical checkpoint and creates no ready edition.

An expired accepted checkpoint does not consume undelivered selection. Candidate
recovery can reuse exact still-eligible selected work after collection, even after
handoff; handed-off empty abstentions stay consumed and mixed eligibility requeues
the eligible subset.

### Outcomes in the application

| Operation | Explicit result | Meaning for the next step |
| --- | --- | --- |
| `recover_preparation` | `ExistingEdition`, `AcceptedPreparation` or `FreshPreparation.REQUIRED` | Return an existing edition outcome, resume locally verified accepted content, or collect fresh work. `held` remains the inspector’s conservative summary, not a detailed receipt diagnosis. |
| Collection/review | `ReviewedCandidates`, `CategoryAnalysis` or `EmptyWork` | Ordinary candidate work carries progress, packet and one resolved review; category and empty outcomes remain separate. |
| Editorial authority | `ReviewAttempt` → `ResolvedReview` | One response owns its review, dispositions and optional closing capture. The resolver chooses authority; the unchanged comparison report is its audit projection. |
| `accept_preparation` | `AcceptedPreparation` or `IncompleteSelection` | Candidate policy binds the resolved review and decides whether work can use shared verified persistence before handoff. |
| `_accept_category_preparation` | `AcceptedPreparation` or `NoEdition` | Category policy retains historical report-order/no-news rules before the same verified persistence operation. |
| `assemble_publication` | `PublicationAssembly` | Resolve required provenance, present main content and decide whether the optional closer can accompany it. Final card order and existing metadata derive from this result. |
| `present_preparation` | `FrozenPreparation` or `NoEdition` | Revalidate the reference against the active checkpoint before any presentation effects; successful freezing returns a required ready-file hash. |

`RunStats` is the terminal public/reporting projection, not the internal work model.
These scenario outcome values add no persisted state machine or new schema. The
separate supplementary-publication ready3 contract is described under [publication records](#publication-records). Supported CLI and run
entrypoints remain stable; deliberate internal API retirements are listed in the
[changelog](../CHANGELOG.md#unreleased--reliability-rehabilitation).

Category admission owns its historical completion and save decisions in
`_accept_category_preparation`; `_prepare_category_edition` sequences the effects.
Shared presentation receives no editorial-completion flag.

| Category result | Save and verify canonical work? | Continue with |
| --- | --- | --- |
| Projected cards exist | Yes, with or without an optional report | Accepted-reference presentation, archive and freeze |
| Empty, no review report | No | `NoEdition("no_ready")` |
| Empty, report-first review abstains | Only if a primary abstention also exists | `NoEdition("no_ready")` |
| Other empty review outcomes | No | `NoEdition("selection_incomplete")` |

The category boundary preserves report ordering, including older reordered reports
and the existing secondary-success fallback from `restore_review`. Summaries or trends
without main cards do not create an edition. A chosen secondary abstention with a later
primary selection remains unsaved no-ready; a chosen selected/partial review with a
later primary abstention remains incomplete. Category acceptance does not require
candidate disposition metadata; fresh candidate abstention still does.

The category effect order is snapshot → admission → save → save readback → fetch
statistics → source map → presentation-entry readback → presentation → archive →
freeze → clear. Unsaved no-edition branches perform no admission read/write and return
after statistics/map. Accepted empty work stays saved and passes the entry check, then
returns no-ready without archive/freeze. Save/readback failure stops later operational
effects; entry validation can fail after statistics/map have already been written.
Missing, replaced or mismatched references, including a UTC day rollover between
operations, stop preparation without deleting or repairing its evidence. Loader errors
also propagate. These are preparation failures, not new ready/receipt hold records.

Reference comparison is not an unforgeable Python capability: an exactly matching
reference has the authority of its valid checkpoint. Frozen dataclasses are shallow;
the freshly restored snapshot isolates caller mutation after verification, but this is
not a lock across awaits or protection against concurrent disk writers. Serialized
runtime writers remain required. Local readback adds no fsync or remote durability.

Publication assembly keeps the canonical snapshot separate from presented main
cards and the optional closing disposition. Required main-card credit failures
hold preparation; optional credit or layout failures omit the closer with its
existing reason. The archive and frozen edition consume the same derived card
sequence. The coordinator then writes the archive, hashes its references, freezes
readiness and clears accepted preparation, in that order. Assembly itself does
not persist a new checkpoint or change translation's request/cache contract.

These are ordinary-path contracts. Category preparation and experimental source work
retain their own terminal behavior; they must not be inferred from a generic success
boolean. The [code owner map](ARCHITECTURE.md#modules-and-responsibilities) identifies the implemented owners.

## Publication records

All paths below are relative to the runtime working directory. These records have
different jobs; a filename's presence alone never proves the later stages happened.

| Record | Meaning and binding | Next boundary |
| --- | --- | --- |
| `.cache/pending_preparation.json` | Canonical `PreparationSnapshot`, intended publication date, creation time and integrity hash. It can contain accepted empty work. | Resume presentation for the same intended day; no selection rerun. It is removed after successful readiness freeze, or becomes ineligible after its publication day. |
| `.cache/prepared_edition.json` | Frozen `Edition`: exact payloads, article chunk ranges, owner/bot binding, UTC window, canonical/presentation hashes and referenced archive/evidence hashes. | The runtime must persist and verify these exact file bytes before claiming. |
| `.cache/prepared_edition_claim.json` | One immutable claim bound to the ready-file SHA-256 and owner. | The runtime must persist and verify the claim and ready file from the same remote revision before first dispatch. |
| `.cache/prepared_edition_receipts.json` | Ready/claim binding, attempted chunk count, known chunk confirmations, terminal transport state and `applied`. | Persist the known send outcome and apply its complete article coverage. Existing uncertain or unapplied state holds later automatic publication. |

Main-only editions retain ready schema 2. A `SupplementEdition` uses schema 3 with
an immutable fragment, separate `SupplementCoverage`,
and an explicit current-review checkpoint. The older origin review remains a reference,
never a filename-order inference for the next optional investigation. Claims and
receipts keep their existing schema because the exact ready hash binds the extension.

The result's `.irritator.fragment.json` freezes presentation and canonical-result/source
references. Its existing `.post-attempt.json` owns pending, reserved, consumed and
expired dispositions; mutable state is not a ready checkpoint reference. This adds no
central queue, index, worker or retry loop. Normal preparation reuses a verified eligible
fragment without model/source work. The shared lossless renderer inserts it before the
closer; sender validation checks frozen hashes, identities and coverage bounds
without rendering or parsing publication prose. See [ADR0007](decisions/0007-compact-issue-reservation.md)
for D+1..D+3 eligibility, actual-clock expiry and proof-bound unclaimed-ready release.

Consumption is an ordered application write, so the runtime receipt barrier must persist
the existing attempt under `digests/` together with `.cache/` applied receipts. The existing
post-prepare step also needs the private owner ID; no bot/model key is needed there.
These are deployment prerequisites, not additional jobs, requests or operating capacity.

Legacy snapshot counters need care: in this preparation path, `article_count` counts
admitted packet articles, while `source_count` is the number of category groups
(`len(articles_by_category)`), not distinct publishers. Main/closing card counts and
`RunStats.feeds_fetched` (attempted feeds) are separate. The retained schema names do
not make those quantities interchangeable.

`READY_SHA` and `CLAIM_SHA` are hashes of the complete persisted file bytes, not an
edition ID or the manifest's internal content hash. Archive references bind the
exact Markdown, review and candidate-evidence files used to prepare the edition.
Reformatting an otherwise equivalent JSON file changes its external hash.

The [managed publication sequence](OPERATIONS.md#persist-claim-and-send) establishes
remote durability for ready evidence, then the claim, then receipts and resulting
state. Preparation failure must retain accepted/candidate work; optional-stage
failure must not discard primary state.

Engine atomic writes and filesystem sync protect local records. They do not make
several files a transaction, publish a Git commit or survive a runner loss by themselves.
The runtime must preserve saved work on failure and serialize writers sharing state.

## Other retained state

| Record | Owner and purpose |
| --- | --- |
| `.cache/candidate_progress.json` | Current eligible/unfinished candidate work and planned packets. |
| `.cache/candidate_sources/`, `candidate_reports/`, `candidate_index/`, `candidate_excluded/` | Exact occurrences and packet proof, indexed historical decisions and reversible policy exclusions. Retirement removes active work only after proof has been written and verified. |
| `.cache/feedback.json` | Votes, polling cursor, replay/acknowledgement data, source decisions and article/source attribution. |
| `.cache/seen_articles.json` | Delivered/consumed identity timestamps under the scenario's deduplication policy. |
| `.cache/source_stats.json`, `source_state.json`, `source_category_map.json` | Source observations, trial lifecycle and category read model. |
| `.cache/pending_sources.json`, `discovery_delivery.json` | Saved proposals and separately bound discovery reservation/delivery history. |
| `.cache/compact_issue.json` | Legacy direct-compact reservation; unresolved legacy sending also blocks prepared publication. |
| `digests/*.review.json`, `*.candidates.json` | Review and candidate-accounting evidence associated with archived output. |

## Send and apply

The sender validates the owner, bot, window, exact ready/claim bytes and referenced
checkpoint files. It uses the frozen payloads; current prompts or translation settings
do not rerender them. Each POST is attempted once in the prepared protocol.

| Step | Ordering and failure meaning |
| --- | --- |
| Claim | Verify an eligible ready edition and its checkpoint bytes, reject existing receipts, then create the claim exclusively. |
| Start send | Require the already persisted ready and claim hashes. Create `sending` receipts exclusively before the first POST. |
| Send a chunk | Persist its attempted count, perform one POST, then persist a positive message ID with a matching chat. A transport uncertainty or lost post-acceptance write can leave the attempt held. |
| Finish transport | Record `confirmed`, `failed`, `partial` or `unknown`. A known complete article needs every covering chunk confirmed; a failed notice can still leave the whole edition incomplete. |
| Apply | Reload current operational state and apply known confirmed coverage. Set `applied` only after those effects complete. Partial/unknown transport remains held even after known coverage is applied. |

A normal confirmed-and-applied edition is a no-op. Confirmed transport with
`applied: false` is a hold: some operational files may already have changed. There
is no automatic multi-file rollback or counter reconciliation. An expired unresolved
claim is still unresolved; expiry does not make a resend safe.

Read-only prepared inspection emits one bounded diagnostic for held work from the
records it already loaded. Claim-without-receipts and sending share an unresolved-
dispatch action; terminal-unapplied work directs inspection to operational write
prefixes; applied-incomplete work identifies marker presence without claiming
accounting proof or replay safety. Missing receipt counts remain unknown. Persisted
coverage does not describe unsaved effects. The status tuple, workflow output keys,
exit codes and legacy compact policy stay unchanged; no new reads or recovery API
are added. See the [recovery guide](OPERATIONS.md#delivery-states-and-recovery).

An uninterrupted managed workflow may send its newly persisted claim when no receipt
exists yet. Rediscovering a claim in a later run is different: missing remote receipts
do not prove the earlier process never sent. The [recovery guide](OPERATIONS.md#delivery-states-and-recovery)
uses the observed records and execution history together.

## Output-effect matrix

Prepared and legacy applications share coverage values while preserving distinct
accounting rules. The following matrix is the current compatibility contract.

The direct-run accounting decision distinguishes confirmed Telegram coverage from
eligible Markdown consumption. Their union determines which collected identities
qualify for deduplication and inclusion statistics. Feedback attribution continues
to use the transport result alone. Separately, an article sent or an archive saved
means that output occurred; this remains true even when the qualifying identity
set is empty. That fact controls cache persistence and adaptive evaluation, not
Telegram success. The decision is an in-memory application value, not a new saved
record or a change to the prepared-edition policy.

| Effect | Prepared edition | Legacy direct run, including direct compact |
| --- | --- | --- |
| Mutable input | Strictly reload current feedback, delivered cache and statistics, plus lifecycle state only when adaptation is enabled, before the first accounting write. Never restore the preparer's mutable snapshot. | Use current-run feedback, collected cache, statistics, lifecycle state and fetch observations supplied by the caller. |
| Delivery identity | Full article hashes enter deduplication only after complete confirmed chunk coverage. Empty coverage returns before state reads or writes. | Confirmed Telegram hashes qualify. Cards mode additionally consumes summarized-category articles and top articles after a saved Markdown output when Telegram is optional or complete. That consumption does not imply Telegram confirmation. Direct compact never treats Markdown as delivered coverage. |
| Attribution | Merge confirmed vote-token/source mappings: full article identities for ready v2/v3, original short tokens for ready v1. Supplement coverage adds no article identity. Update last-digest sources/time only when the whole issue is complete. | New delivery uses full article vote identities. Merge confirmed mappings when any article was sent or Markdown was saved; last-digest metadata still requires complete Telegram output. With neither output, restore only prior attribution, preserving collected votes and polling cursor. |
| Deduplication timestamps/path | Add absent hashes with application-time UTC timestamps; preserve existing timestamps. Write under the supplied cache directory. | Preserve collection timestamps and old entries; filter newly collected entries to qualifying output. Save only when an article was sent or Markdown was saved. Compact uses the supplied cache directory; cards retain `save_dedup_cache`'s default path, ignoring the passed `cache_dir`. |
| Source accounting | Count only confirmed hashes absent from the reloaded delivered cache, and only for existing source-stat entries. Use the intended UTC publication day; a delivery-only snapshot adds no fetch, found-article or HTTP-success observation. No inactive-source pruning. | Record actual fetch observations, including failed feeds, and qualifying output through the existing run-stat operation. Keep current-day fetch history semantics and inactive-source pruning on save. |
| Lifecycle state | When adaptation is enabled and coverage exists, reload/evaluate current state and strictly save it, including an unchanged result. Use application-time UTC day for trial decisions. | Evaluate only when adaptation is enabled and an article was sent or Markdown was saved. Apply changes only when promotion, demotion or trial start is needed; persist lifecycle state on the normal path even without delivery. |
| Write order | Strict feedback → strict source statistics → optional strict lifecycle state → strict seen cache; the prepared sender consumes complete ready3 supplement coverage before its intended-byte-verified applied receipt marker. | Seen cache when output qualifies → usable feedback → lifecycle state → source statistics → source-category map; caller then finalizes the compact guard. |
| Failure policy | All applicable reads fail closed before the first accounting write. Source reads require complete current-writer fields, supported schema, exact types, finite nonnegative numeric values and canonical calendar days. For source statistics/lifecycle files, missing whole files are first-run state; malformed, sparse, unknown-field, duplicate-key, symlinked or unreadable files hold application. All writes propagate failures. | Compact cache/feedback writes propagate failures. Cards cache/feedback and source-state/statistics/category-map writers retain their existing caught-write-error behavior; setup failures outside those handlers can still propagate. |

This preflight runs at accounting time, potentially after confirmed transport. Invalid
source history leaves saved receipts unapplied and holds the edition without changing
accounting files; it does not prove that no message was sent. Default direct/legacy
source readers remain permissive. See the [strict-read amendment](decisions/0017-confirmed-delivery-application.md#strict-prepared-accounting-preflight--2026-10-09)
for the intentional current-writer compatibility boundary and inspection guidance.

The prepared sender owns the active dispatch through application. It compares the
terminal writer's readback hash with the intended canonical receipt bytes, reloads
that exact receipt, and verifies original ready/claim identities before accounting.
Reporting projections do not authorize writes. After accounting it rechecks those
bindings, consumes required supplement coverage, and verifies the applied marker's
readback against its intended canonical bytes before returning success.

Confirmed/applied history returns without dispatch or application. Existing unapplied,
sending, failed, partial or unknown receipts remain held; this owner never replays
accounting. Fresh partial/unknown transport may still apply complete known article
coverage during its uninterrupted invocation. Every failure retains the actual
persisted prefix. A final-write exception after replacement may leave applied bytes;
inspection honors them rather than resetting or resending. These checks are neither
a multi-file transaction nor protection against concurrent state writers.

The Python sender now requires `config`; its old enabled/bot-only signature,
`PreparedOutcomePolicy`, `_merge_delivery`, the prepared apply branch and independent
`mark_applied` are retired. Known engine/configured workflow consumers are migrated
or verified absent; unknown external Python callers must migrate explicitly. CLI,
wire formats, external ready/claim barriers and direct-run policies are unchanged.

## Transport protocols

The transport contracts also differ. Selecting compact formatting alone does not
turn a legacy direct call into the persisted prepared protocol.

| Protocol | Preserved acceptance, retries and effects |
| --- | --- |
| Legacy cards, status and counter-signals | HTTP success is sufficient; no Telegram `ok` or message-ID validation is added. HTTP 400 triggers the existing plaintext fallback. The existing three attempts, Retry-After/backoff, per-card continuation and 0.5-second spacing remain. Supplement dispatch retains its 90-second total bound and status-dependent notification. |
| Direct compact | Render every chunk before `before_send`, which remains immediately before dispatch. A 30-second total bound and per-request timeout remain. HTTP 4xx or explicit `ok: false` fails; other non-200/malformed receipts and transport uncertainty become unknown. HTTP 200 plus `ok: true` confirms without message-ID/chat validation. No retry or fallback; only complete confirmed article coverage is attributed. |
| Legacy standalone post-delivery supplement | Missing/disabled destination returns `not_configured` before rendering/client creation. Each silent chunk gets one POST with a 30-second total bound and per-request timeout. HTTP success plus explicit `ok: true` is required; errors propagate to the existing unknown marker. New compact target-bound material instead enters a later ordinary ready3 edition. |

Prepared transport additionally requires a positive message ID and exact matching
chat, persists chunk receipts and holds uncertain/unapplied outcomes. Its adapter has
no retry or plaintext fallback.

### Legacy delivery caveats

**Cards can duplicate after a timeout or interruption.** Their retries can repeat
a POST, and they have no durable prepared claim/receipt hold. Before considering a
rerun, compare logs and Telegram with retained deduplication and feedback records.
Uncertainty may remain: absent markers or cache entries never prove that nothing
was sent. This is not an automatic resend or marker-reset procedure.

**Telegram success does not verify a direct-run archive.** Both direct and prepared
paths attempt an enabled Markdown archive before sending. An enabled prepared
archive failure blocks readiness; a handled direct archive failure can coexist with
successful Telegram delivery and exit status. Check the archive result and logs
separately. Do not rerun publication merely to recreate a missing archive.

## Request identity and reuse

Declarative settings select provider/model routes and limits. A lazy `ModelExecution`
owns per-loop concurrency, pacing, cooldowns and local attempt accounting. Applications
pass that owner explicitly; construction alone does not inspect a quota or acquire a
request allowance. Accepted work can therefore recover without initializing model work.

Fresh versus shared derived execution, loop rebinding and first-initialization semaphore
capacity follow the [execution contract](decisions/0020-explicit-model-execution.md).
The durable cycle journal is separate from that in-memory holder. Each physical retry,
fallback or counting call retains its own reservation; failures do not refund attempts.
Absolute deadlines and provider/account allowance remain distinct constraints.

Request construction is shared by planning, execution and resume. Cached review reuse
requires the configured slot/provider/model, exact bundle and prompt identity, plus
valid retained selections. Accepted preparation and ready editions are separate
recovery objects and are not revalidated as new model judgments.

Hash encodings retain their specific contracts: prompt JSON uses sorted ASCII-escaped
JSON with spaces; evidence JSON uses sorted non-ASCII JSON with spaces; retained
occurrences/objects use compact sorted UTF-8 JSON. Source attribution resolves the
accepted immutable occurrence before calls, adds literal credits after translation,
and applies identical final cards to archive and frozen payloads.

## Public acquisition and diagnostics

The small [public-fetch operation](../digest/adapters/http/public_fetch.py) owns
untrusted feed, discovery, article and optional signal-liveness acquisition (#218).
Collector and CLI probes use the same operation. Every requested hop rejects unsafe
syntax and every DNS address must be global, non-multicast, non-reserved and not IPv6
site-local. Reserved IPv6 translation prefixes are deliberately excluded. Connections
use a validated IP with the logical origin's HTTP Host and TLS SNI/certificate hostname;
default certificate verification stays enabled. Fresh clients for each address/hop
avoid cross-origin pooling. There is no process-global DNS override or ambient proxy.

GET has at most three redirects and separate 2 MiB raw/decoded caps. One total fetch
budget includes DNS awaits, address attempts, redirects and body consumption: 15 seconds
for collector/probes/discovery and 20 seconds for articles. TCP and TLS establishment each have
at most three seconds (roughly six combined); another validated address is attempted
only if total time remains. Slow DNS can leave no fallback opportunity. Read/status
failures do not trigger address fallback. Collector alone retains its existing second
attempt and bounded backoff, so its whole-source envelope can exceed 15 seconds.
Discovery and article retain their existing outer budgets across fetching and parsing.
Await timeouts cannot stop an OS resolver or parser worker already running; bounded
synchronous decoding/article extraction is checked on return, not preempted mid-call.

Identity, gzip/x-gzip and zlib/raw deflate are supported with bounded decompression.
Unsupported/chained encodings, concatenated compressed members, truncated bodies and
invalid/oversized Content-Length fail technically, without returning partial evidence.
HTTPX may read ahead one bounded raw chunk before the accumulator rejects overflow.
Error statuses and HEAD never consume response bodies. Liveness performs one public-only
HEAD without redirects, drops 404/410 and failed acquisition, and retains other statuses.
Article-specific standard-port, URL-length, content-type and completeness policies stay
in the article caller; feed parsing remains separate. Logical source/final URLs are
preserved rather than replaced with connection IPs.

These deliberate acquisition restrictions and fresh-connection costs are recorded in
[ADR0026](decisions/0026-public-acquisition-boundary.md). No persistence, editorial,
CLI or delivery contract changes. This is not a universal outbound-HTTP guarantee:
fixed-endpoint Hacker News, Reddit, arXiv and other [search clients](../digest/irritator/sources/)
keep their own HTTP policies. Boundary tests use synthetic DNS/transports and the real
HTTPX/httpcore request path, including original-host TLS verification configuration;
they are not evidence of live exploit testing or publisher availability.

All Telegram adapters use the small [diagnostic boundary](../digest/adapters/telegram/diagnostics.py)
for HTTP requests and safe status errors (#219). It adds no retry, receipt or result
policy. Sent URLs and payloads remain unchanged; returned response/exception request
metadata is a separate token-free diagnostic request with no body or original headers.
Retries still use the original local URL, never that redacted metadata. HTTP status,
response body and headers remain available to the existing protocol decisions.

The boundary filters the concrete emitting HTTPX/httpcore 1.0.9 loggers before
handlers retain records, retaining method/status evidence while redacting credentials
in ordinary, DEBUG and exception diagnostics. A per-request context keeps overlapping
requests separate; no temporary process-global logger levels are changed. Safe status
errors do not interpolate response reason, redirect Location or body. This is verified
for the reviewed dependency graph, not a claim about arbitrary future logger names,
third-party log sinks or deliberate logging of raw response objects. Review the
[ADR0017 continuation](decisions/0017-confirmed-delivery-application.md#telegram-diagnostic-boundary--2026-10-09)
when changing the HTTP dependencies or transport entrypoints.

Feed titles/descriptions are untrusted content: sanitization removes HTML, decodes
entities, normalizes whitespace and limits the description supplied to existing RSS prompts. Sanitization does not turn
an excerpt into a full article or guarantee immunity to all malicious instructions.

Keep real keys/tokens in runtime environment variables or Actions secrets. The engine
does not automatically load `.env`. Never commit credentials or source-account details
in public examples. Runtime artifacts can contain source bodies and model outputs;
choose access and retention deliberately rather than copying them into the public repo.
