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
   Disposition contradiction and duplicate-entry diagnostics identify known evidence
   IDs within their existing error bound. Saved captures retain their original text;
   new diagnostic text changes packet/envelope hashes, not the standalone review or
   raw-response identity. Recovery reads the saved capture rather than regenerating it.
7. **Save, verify and hand off accepted work.** Candidate and category admission have
   distinct editorial rules, then share `persist_accepted_preparation`: save through
   the existing codec, reload and compare the path and full canonical snapshot. The
   restored reference contains the path and existing canonical envelope-body hash,
   not a hash of the indented file bytes. Candidate handoff precedes fetch statistics;
   category statistics and its source map follow verified acceptance. This is local
   readback, not remote persistence or power-loss durability. A handoff is not delivery.
8. **Present and freeze.** `present_preparation` binds an omitted publication date to
   the UTC day at entry, then reloads that day from the publication-owned `.cache`
   and compares path, body hash and the full
   snapshot with the supplied reference, even for empty work. It consumes the separately
   restored snapshot. Then validate required immutable source provenance before model
   presentation calls. Translate generated prose, qualify future source-feed timestamps,
   append literal credits, check chunk coverage, write the enabled archive and freeze
   the exact edition. Clear the pending
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
[changelog](CHANGELOG.md#unreleased--reliability-rehabilitation).

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

For ordinary prepared review-backed main cards, the pure review-presentation owner
matches exact article identities to the accepted report's evidence. An aware feed
timestamp later than the intended UTC calendar day receives a static source-feed
prefix with a readable UTC source-feed date and edition date; the exact timestamp
remains in evidence. This does not verify
publication/event timing. Missing, invalid, naive or UTC-unrepresentable timestamps,
absent evidence and same-day/past timestamps receive no prefix. The configured output
language selects Russian for `ru` and English otherwise: `translation.target_language`
when enabled, `radar.language` otherwise. Added after translation, a Russian notice
may accompany canonical English prose; the existing notice still discloses fallback.

The prefix changes copied presentation cards only; canonical cards/review/evidence,
model inputs, translation request/cache hashes and source eligibility stay unchanged.
Optional closing cards, direct/legacy/preview and no-review output retain their prior
behavior. Normal lossless chunk splitting and archive bounds apply; the date alone
adds no whole-card hold. Existing required source-credit rules still apply. Newly
rendered archives, presentation/content hashes and chunk boundaries may change;
previously frozen payloads, receipts and saved archives are never rendered again.

The same intended date reaches accepted-reference loading, assembly, pending-fragment
lookup, archive and ready freeze. An explicit future day remains exact. If implicit
presentation crosses UTC midnight, the existing past-date freeze guard rejects it
instead of rolling it to a new edition day. Translation/cache writes and the archive
with review/candidate evidence may already exist. Accepted canonical work remains
uncleared; no ready edition, claim or send is created. This is a preparation failure,
not a new durable hold record or clock schema.

These are ordinary-path contracts. Category preparation and experimental source work
retain their own terminal behavior; they must not be inferred from a generic success
boolean. The [code owner map](ARCHITECTURE.md#modules-and-responsibilities) identifies the implemented owners.

## External editorial candidate

This explicit local candidate is separate from ordinary provider preparation; see
the [draft ADR0005 amendment](decisions/0005-optional-presentation-translation.md#draft-source-authoritative-external-editorial-path-55).
It does not change or migrate previously saved provider reviews or ready editions.

- **Export is not an attempt or a decision.** Shared planning stops before
  `begin_packet`; immutable source occurrences and exact evidence projections are
  retained without clearing dispositions, marking selected work delivered or
  invoking a model. Current effective sources include runtime lifecycle exclusions.
- **One pending handoff owns continuation.** Ordinary fresh provider preparation
  cannot replace it. Exact hashes resume; incompatible, missing or corrupt active
  evidence holds. This is a bounded preparation checkpoint, not another work queue.
  An explicit exact-packet-hash release is limited to all-rejected or expired work;
  it retains an immutable release record and all evidence without resolving candidates
  editorially, starting another generation or bypassing active delivery state.
- **Projection is not publication approval.** Complete per-card source reviews
  retain only literal acceptance of unchanged text. Rejected work remains unresolved.
  The original whole-response verdict stays visible, including failure. Supplied
  optional evidence is accepted only through the versioned native optional contract;
  historical unvalidated closing/comparison blobs remain withheld. Source credits,
  date notices, optional eligibility, ordering and rendering are settled before hashing.
- **Acceptance binds the final composition.** An external machine-readable verdict
  must accept the exact projected canonical/presented cards, notices, optional text,
  payloads/coverage and owner/date/source/response/review bindings. A card pass, a
  differing reviewer-reference string or a boolean provenance flag cannot substitute.
  No ready file exists merely because projection succeeded.
- **Import revalidates before freezing.** Current exact source policy, recency,
  blocklist and delivered-history constraints must still permit the accepted work.
  A changed composition needs a new explicit acceptance, not silent filtering under
  an old hash. Enabled archive failure prevents readiness and retains pending work.
- **Optional content preserves the accepted baseline.** At least one accepted main
  card is required. Closing needs a separate identity, approved exact feed binding,
  supported source credit and its own exact review. Comparison needs two distinct
  retained main sources, source-specific observations and a separately reviewed
  interpretation/qualification. Current activation and publication settings still
  apply. Optional layout failure omits that optional content before final acceptance,
  not accepted main cards; comparison never acquires article/vote/delivery identity.
- **Native ready is a distinct version.** Schema 4 requires its editorial acceptance
  and exact composition bindings during restoration, claim and send. Older engines
  reject this version rather than sending it without the gate. Genuine schemas 1–3
  remain compatible; unapproved schema-2 native drafts from the local prototype do
  not become publishable legacy records. Do not downgrade or strip native metadata.

The managed runtime remains responsible for trustworthy handoff, serialized access
and remote persistence of all required ready/checkpoint/claim bytes. Hashes establish
integrity, not identity, independence, semantic correctness, provider entitlement or
zero incremental spend. Sender recovery uses the frozen accepted evidence, not a
fresh model request, rendering pass or current source-policy reinterpretation.

### Native handoff formats

All documents use exact-byte SHA-256 bindings. JSON object keys are unique; the
bounded storage reader rejects oversized inputs. Keep these artifacts in the private
runtime, not public examples or PRs.

- `native-source-packet/v1` retains the full planned packet, its exact occurrence
  hashes, owner hash, publication date, publication language, excerpt limit and
  editorial context. The planning prompt hash is historical planning metadata,
  not a native-model request or dispatch receipt.
- `native-source-packet/v2` additionally captures the optional policy and exact
  eligible closing identities. It does not change current source-policy checks or
  turn historical v1 optional blobs into valid new proposals.
  `optional_policy` records `closing_enabled`, ordered `closing_ids`,
  `comparison_enabled` and `comparison_publish`.
- `native-editorial-response/v1` binds `packet_sha256`, `execution_ref`, `language`
  and `items`. Every exported evidence ID appears exactly once. An item has
  `evidence_id`, `status` and `text`; statuses are `card`, `withheld`, or
  `insufficient_evidence`. Only cards have text, bounded to 600 characters. The
  captured packet's main-card ceiling still applies. Historical optional
  closing/comparison blobs are retained but cannot activate optional publication.
- `native-editorial-response/v2` explicitly supplies the new optional contract:
  at most one separate source-bound closing and one two-source comparison. Closing
  prose and each comparison observation bind their own occurrence; the interpretation
  and material qualification remain distinct from the per-source observations.
  A closing has `evidence_id`, `occurrence_sha256` and `text`. A comparison has two
  ordered `supports` (`evidence_id`, `occurrence_sha256`, `observation`),
  `interpretation` and nullable `qualification`. The engine attaches the exact
  complete source excerpts rather than accepting writer-supplied evidence quotes.
- `external-source-review/v2` binds `packet_sha256`, `response_sha256`, the writer
  and reviewer execution references, original whole-response `verdict`/`notes`,
  and complete `card_verdicts`. Each generated main card needs its evidence ID,
  exact UTF-8 `text_sha256`, verdict and notes. Only literal `accept` retains it.
  The known writer reference cannot stand in for an independent reviewer.
- `external-source-review/v3` adds explicit optional verdicts bound to the exact
  proposed optional objects. A verdict on a main card cannot substitute for a
  closing/comparison verdict. Absence, rejection, ineligibility or unsafe optional
  layout withholds that content before the final accepted subset is frozen.
  Each `optional_verdicts` entry has `kind` (`closing` or `comparison`),
  `value_sha256`, `verdict` and `notes`. Its hash binds the canonical JSON bytes of
  the complete proposed optional value; only literal `accept` can admit it.
- `native-composition/v1` is produced by the engine, not edited by the writer. It
  binds source/response/review references and exact final publication fields,
  including ordered canonical/presented cards and rendered Telegram payloads.
- `native-composition/v2` additionally fixes optional dispositions, the main-card
  boundary and any admitted closing/comparison placement. Legacy v1 projection
  construction remains unchanged; accepting a v1 packet does not upgrade its
  optional content. Both versions require exact final-composition acceptance.
  Native optional evidence lives under `canonical_metadata.native_optional`, not
  a provider-owned closing record. Canonical cards preserve the complete delivery
  order; `main_count` identifies the main prefix before a possible closer.
- `native-final-acceptance/v1` contains `projection_sha256`, `packet_sha256`,
  `response_sha256`, `review_sha256`, `reviewer_execution_ref`, `verdict` and `notes`
  alongside its schema. Its verdict must be exactly `accept` on those exact bytes;
  the known writer cannot approve its own final composition. The same independent
  source reviewer may perform this final review; a third reviewer is not required.
- `native-archive-evidence/v1` links the retained source, response, review,
  composition and final acceptance from the Markdown archive. Archive counts keep
  their existing planned category-group/article meaning, explicitly labeled; they
  are not published-card counts or a claim about every live feed.
- A packet-keyed release record retains the complete pending record, its reason
  and release time. Release is a continuation-state transition, not an editorial
  rejection or delivery receipt. Repeated/interrupted release validates the same
  retained evidence before clearing only that active pending pointer.

Per-card acceptance and final-composition acceptance are supplied editorial evidence,
not an engine-generated semantic certificate. Missing/contradictory evidence fails
closed, and preserved failed or incomplete work never acquires a retrospective pass.

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

Native-origin investigations use a separate attempt schema 4 (not the primary-ready
schema discriminator): `native-delivered-card-v1` contains a
`NativeDeliveredInvestigationInput`. Common edition/claim/receipt/owner/day/card
fields retain their meaning; `native` carries six typed path/hash references for the
packet, response, source review, composition, final acceptance and archive index.
There is no `report_sha256`, fabricated `ModelReview` or legacy candidate-report.
Exact-field-set union restoration preserves old `DeliveredInvestigationInput` and
`SupplementFragment` serializations unchanged. Native and legacy variants share
source-occurrence and delivered-quote validation, research, ranking and fragment
eligibility. Native RSS evidence is not promoted to full-source evidence.

`<archive-stem>.native.post-attempt.json` and `.native.irritator.*` remain separate
from legacy `.post-attempt.json`/`.irritator.*`. Native execute additionally creates an
exclusive `.native.post-attempt.execute.json` claim before external work; a crash
before updating the main attempt still consumes that claim. Primary state is never
mutated by this adapter. Prepare and execute reread all exact origin anchors, and
execute requires equality with the prepared origin. The runtime still serializes
writers and supplies remote durability; rereads are not a cross-file transaction.
See [native-origin operation and rollback](ADVANCED_OPERATIONS.md#native-delivered-origins).

Explicit native research additionally owns immutable `.phases.json`/`.phases/`
artifacts bound to the existing native attempt and execution claim. Requests bind the
session and all prior artifacts; generation and each acquisition have separate
exclusive intents. The controller proves remote persistence before external work;
a started marker precedes source I/O. Missing result after intent is held, not repeated.
Completed acquired snapshots restore cumulative physical starts and elapsed source
time, never reset either bound. Native worker pauses do not alter request hashes.
Native result schema 3 is a distinct `NativeResearchResult` subclass with typed
`native-irritator-provenance/v1` references; old `EvidenceIrritatorResult` fields are
unchanged. Existing fragment storage rechecks every native reference hash. Exact
completion validates mandatory artifacts against the existing attempt's final result
and Markdown references. Interrupted finalization is held without a second attempt.
See [native phase operations](ADVANCED_OPERATIONS.md#native-research-phases).

A native destination carrying an existing fragment is `NativeSupplementEdition`,
ready schema 5. It extends the native exact projection/acceptance fields with the
existing `PreparedSupplement`, not a fake `current_review_checkpoint`. Schema-3/4
records and `PendingNative` keep their exact field sets. `native-composition/v3`
freezes the whole supplemental value and covering chunks inside `publication`;
final acceptance binds it together with canonical/presentation cards and payloads.
The native archive index and ready also retain the fragment projection, canonical
result and original archive hashes. The mutable attempt is still only a reservation,
never a ready checkpoint reference.

Projection discovers at most one existing eligible fragment; import rehydrates only
that frozen choice and verifies it is still available. New late fragments do not
change a no-fragment projection. Import reserves the ready hash before cleanup; a
retry can finish an exact unclaimed reservation prefix without recomposition. Conflicting
old dispatch remnants remain a manual-proof hold. Receipt-owned consumption and
expired-unclaimed release share the existing supplement owners across schema 3 and 5.
See [native destination and recovery](ADVANCED_OPERATIONS.md#native-fragment-destinations).

The supplement archive frames its exact serialized stage diagnostics in a JSON code
fence. Its companion JSON and surrounding formatted prose are unchanged; the fence
only describes the diagnostic block, not a whole-document Markdown safety boundary.
Previously saved archives and frozen payloads are not rewritten.

Delivered-bound extraction, query and ranking requests share the validated origin
`publication_day` as the edition's UTC reference, separate from the investigation
clock, source publication time and evidence of event occurrence. Historical quotes
remain literal; unbound requests and retained result/fragment readers are unchanged.

New delivered-bound compact reservations bind `question-routed-documents-v2`.
The exact whole selected canonical title/summary remains the target; a separately
labelled research question routes queries and one optional external document read.
Original-page discovery, searches and external acquisition share nine actual HTTP
starts, including redirects/address fallbacks, with at most three model calls in
the existing 180-second stage. The 8,000-JSON-character ranking limit measures only
the candidate array; complete oversized evidence is omitted whole. Old unexecuted
compact policies are held unchanged before model/source work. Unbound legacy
contracts remain separate. See the [ADR0011 amendment](decisions/0011-source-anchored-investigation-queries.md#2026-10-15-question-routed-documents)
for the implementation-under-review and semantic-acceptance boundary.

New non-RFC document snapshots may carry versioned `extraction_diagnostics` bound
by the snapshot's existing `content_sha256` and immutable acquisition reference.
These are content-free observations: fixed candidate-kind/first-failing-predicate
counts, at most 16 indexed region metric records, explicit omitted-region count,
and fixed critical-media reason counts for the selected region plus appended
footnotes. Integers saturate at 2,147,483,647 with an explicit saturation flag.
Skipped unclosed/footnote candidate records use zero character and letter metrics
as unmeasured sentinels, not evidence of empty text. Preselection failures can have
no region observations; absence is not proof that an article body was absent. No HTML, attributes, URLs, headers or exception text
is added to this metadata. RFC extraction can use transformed section HTML, so
this diagnostic slice deliberately leaves RFC metadata absent rather than
mislabel transformed-region observations as an original-response extraction.

HTML extraction keeps media coverage aligned with its selected text. Ordinary
HTML template content supplies neither article text nor critical-media triggers.
Active or ambiguous template content (including shadow-root, marker-patching,
foreign-namespace and malformed/unclosed templates) remains an explicit `unread_template_content` hold.
Explicitly suppressed decorative-SVG descendants do not create a text-triggered
media hold; extraction's automatic SVG text skip is not treated as invisibility.
Only isolated supported inline hiding declarations are recognized for this
correction; conflicting/unknown styles and visible SVG text remain conservative.
ARIA-only, bare SVG `hidden`, or semantic class hints do not establish suppression
under this new descendant rule; explicit SVG visibility overrides remain held.
When the existing substantial-main rule removes linked teaser articles, their media
observations are excluded with their text. Selected-main and appended-footnote
media, global/continuation guards and explicit figure-reference holds remain.
These structural corrections do not identify the causes of historical held pages.

Legacy snapshot field absence restores only as an in-memory default; retained
bytes and their hashes are never rewritten. New diagnostics are strictly validated
on restoration. Stats do not enter discovery leads, signals, model messages,
ranking admission or presentation. New acquisition artifact hashes (and native
request envelopes that bind those artifacts) reflect the added metadata; semantic
message/prompt hashes for otherwise identical evidence remain unchanged. HTTP
source-attempt details retain only `http_status_NNN` for actual integer 100–599
`HTTPStatusError` responses, not exception bodies or headers. These observations
change no extraction rule, source activation, request budget or retry behavior,
and do not establish useful counter-evidence. See [ADR0011](decisions/0011-source-anchored-investigation-queries.md#bounded-acquisition-diagnostics).

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
| `.cache/native_discovery/`, referenced `.cache/native/` records | Opt-in immutable discovery request/raw prefill, same-run reservation and exclusive consumption evidence; no changes to legacy identity schemas. |
| `.cache/compact_issue.json` | Legacy direct-compact reservation; unresolved legacy sending also blocks prepared publication. |
| `digests/*.review.json`, `*.candidates.json` | Review and candidate-accounting evidence associated with archived output. |

Discovery checkpoints each validated, deduplicated generated proposal before the next
validation await: strict pending replacement, then delivery-metadata replacement.
Pending failure preserves the prior disk prefix; metadata failure can leave valid
pending work without its new area/generation attribution. Both errors and cancellation
propagate without rollback or provenance reconstruction. An interrupted prepare adds
no reservation/batch or prepare output and does not send; earlier receipt holds remain.
Normal preparation revalidates retained proposals and may generate for remaining slots.
These are local writes, not proof that the runtime persisted them remotely; cancellation
or runner loss may prevent its existing persistence step. Schema and budgets are
unchanged. See the [validated-prefix amendment](decisions/0013-discovery-exploration-state.md#validated-prefix-recovery--132-2026-10-12).

The [native discovery adapter](NATIVE_DISCOVERY.md) requires its slot reservation
to be externally persisted/read back before new candidate admission. Its locally
exclusive consumption record is not itself remote durability or a live-run guarantee.
Missing/invalid native input never selects a provider fallback.

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
keep their own HTTP policies. Delivered-only investigation document reads use this
public-fetch operation, while the admitted HN/arXiv/DEV searches retain their adapter
transport rules. Both consume the same optional investigation request-start budget
at their actual request owner; document redirects/address fallbacks each count. Boundary tests use synthetic DNS/transports and the real
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

Telegram literal text escapes backslashes before MarkdownV2 interpretation. Newly
rendered payload lengths and chunk boundaries can change; saved ready payloads
remain authoritative and are sent byte-for-byte without re-rendering.

Markdown article-link title labels are literal source text at the card, quotation
heading and review-preview sites. This changes new Markdown/CLI escape spelling,
not canonical titles, link destinations, review JSON or intentionally formatted model
prose. It does not constrain other Markdown fields or URL targets; saved output is
never rewritten by this presentation rule.

Feed titles/descriptions are untrusted content: sanitization removes HTML, decodes
entities, normalizes whitespace and limits the description supplied to existing RSS prompts. Sanitization does not turn
an excerpt into a full article or guarantee immunity to all malicious instructions.

Keep real keys/tokens in runtime environment variables or Actions secrets. The engine
does not automatically load `.env`. Never commit credentials or source-account details
in public examples. Runtime artifacts can contain source bodies and model outputs;
choose access and retention deliberately rather than copying them into the public repo.

### Optional synthesis attempt

When enabled, `.cache/synthesis/<accepted_sha256>.json` is a preparation-local,
versioned attempt authority, retained after accepted preparation is cleared. The
filename does not change with language, model or prompt. Records reject symlinks,
duplicate keys, unsupported shapes/hashes and sizes over 128 KiB. They retain
accepted/date/report/bundle bindings, ordered original evidence and occurrences,
exact messages/full controlled request and hashes, fixed dispositions/timestamps,
bounded response facts, allowlisted usage and validated paired-result identity.
No arbitrary exception text or credentials are recorded.

Exclusive creation, flush/fsync and exact readback precede dispatch. A retained
reserved, failed, invalid, abstained or skipped attempt never generates again.
Malformed/mismatched state is preserved and omitted. Terminal atomic replacement
and readback must succeed before optional prose is eligible; failure preserves
local attempt authority and uses baseline publication. A valid record is reparsed
against current exact bindings before recovery reuse. This is local retained-state
durability, not an account-wide journal or cross-run guarantee: runner loss before
the runtime's existing persistence barrier can lose local evidence.

The draft source-specific v2 result keeps the schema-1 attempt envelope and the
same accepted-preparation filename. Its wire observations carry source IDs/hashes;
the validator attaches complete excerpt context from immutable input bindings to
the normalized result, and retained-response revalidation recreates that same
result without another model call. Its prompt/result shape changes, so v1
success, failure and reserved records are preserved but not reused. Unsupported
retained state takes the existing omission path; it is never absence and never
permits a new request. No migration, deletion or prompt-suffixed cache key is
introduced. Frozen v1 editions keep their exact old payloads and do not call the
current synthesis parser. See the [draft amendment](decisions/0005-optional-presentation-translation.md#draft-source-specific-v2-amendment-55)
for the source-specific observation and finite-acceptance contract.

New v2 requests bind the strict provider schema to the captured EN/RU language.
Stored controls and the complete physical request use the same specialized
format. Exact historical generic-v2 captures remain readable, including reserved,
invalid, abstained and valid records; their bytes and disposition are unchanged.
They do not match the new complete request and therefore take the existing
`cache_mismatch` omission path without another call. New records cannot write the
generic format, and the shared generation owner rejects it. Prompt/result and
attempt-envelope versions remain unchanged; no record is migrated or removed.

The complete baseline, including an eligible pending supplement, is preflighted
before optional work. Admitted result, source bindings and disposition are additive
hash-bound ready metadata; the sender acquires no synthesis parser or mandatory
sidecar dependency and sends frozen bytes unchanged. Accepted preparation v1/v2,
ready schemas, claims, receipts, article/vote/source accounting remain unchanged.
Per-record limits do not bound total retained storage or Git-history growth; these
sidecars are outside candidate accounting's 32 MB limit. There is no new cleanup.
See [the decision and recovery limits](decisions/0005-optional-presentation-translation.md#optional-selected-source-comparison-55).
