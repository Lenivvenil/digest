# 0008. Account for candidates across bounded preparation packets

Status: accepted

Owner approval, 2026-10-04: accepted the bounded preparation-packet and evidence-storage
decision and authorized PR #125 merge and engine-pin rollout. Natural-run and
disposition-quality acceptance remain open under #121.

Issue: [#121](https://github.com/Lenivvenil/digest/issues/121)

## Context and established requirements

The owner requires every eligible observed candidate to remain traceable. Source
allocation, a bounded evidence packet, model failure and exhausted processing time
are technical constraints, not evidence that an article is uninteresting. Existing
article identities, feedback priorities, source security and confirmed-delivery
history must remain intact. [Domain D-03/D-06](../domain/digest/overview.md#current-owner-requirements-and-acceptance-traces)
and [ADR0007](0007-compact-issue-reservation.md) remain authoritative.

The prior local report-only proposal did not advance ordinary preparation. This
proposal instead connects accounting to the existing primary preparation call.
The mechanism below is a proposed implementation choice, not an earlier owner rule.

## Proposed decision

Capture typed collection observations before source-slot allocation, including
eligibility exclusions, exact-identity repetitions, source memberships and known
feed collection limits. Preserve the collector's legacy allocated return API.
Persist separate candidate-selection progress before invoking the existing model
selection. Keep accepted preparation snapshots and frozen editions unchanged.

One fresh ordinary preparation window processes at most one existing bounded primary
packet. Prefer eligible candidates never previously included in an attempted packet;
then allow technically unfinished attempts to progress. Admit whole evidence items
within the existing packet limits. Invalid or oversized items remain explicitly
unfinished. There is no additional model call, automatic drain loop, fixed number
of published cards or requirement to finish the cohort before preparing selected
cards. Current primary/fallback routing and job limits still apply.

Track registration, planned/uncertain attempt, validated model output and confirmed
publication as distinct facts. The same bounded response records one per-ID disposition: selected, metadata
not-selected with a reason, duplicate with a retained selected identity, or deferred.
Selected reasons stay in their existing selection record. Missing/invalid or capacity-
only omissions remain technical work; old outputs retain their missing-reason limit.
Output validation failures are not editorial decisions. Exact repeated identities
may share attribution; different reports and contrary accounts remain distinct.

Persist the validated report before presentation, and retain it until handed to
accepted preparation so a crash cannot silently skip selected work. Existing ready,
held and accepted preparation takes precedence over fresh candidate work. On the
next eligible ordinary preparation, reconcile saved work with current source,
blocklist, recency and delivered-history boundaries before planning a later packet.
Candidate age remains visible. No new TTL or permanently active queue is established.

**Deployed #132 admission-order correction.** Owner approval on 2026-10-06
authorized PR [#134](https://github.com/Lenivvenil/digest/pull/134) (`ef68d46e`) and
runtime [#63](https://github.com/Lenivvenil/digest-prod/pull/63) (`e1e7a133`). This
accepted removal of alphabetical admission bias, not the unresolved professional-core
protection policy. The original ordering below was subsequently adjusted by the
October 7 freshness/continuation correction recorded later in this decision.
Within each unseen/technical
status class, admit one candidate per source per round.
Keep each source in first-observed order and re-order its remaining head each round:
oldest observation first, effective priority for coeval heads, then stable source
name and identity. Category names do not grant earlier admission. This uses the
original observation timestamp, including after restart or repeated observation;
it does not change publication age, eligibility, evidence bounds or editorial status.
These are packet-admission preferences, not relevance or publication guarantees.
An older backlog can delay fresh urgent work, and unseen work still precedes technical
retries. Professional-core protection and sustained throughput remain open under
[#132](https://github.com/Lenivvenil/digest/issues/132); categories, source priorities
and trial status do not independently establish a professional-core role.

Archive candidate accounting separately beside the ordinary review report and bind
it into the ready edition's existing archive hashes. Model-review messages do not
contain accounting decisions or another model's choices. Existing report and strict
preparation dataclass schemas remain unchanged. Runtime persistence uses the existing
`.cache` and archive commit steps; a local pre-call save alone does not establish
remote runner-crash durability.

## Consequences and finite verification

This removes first-packet position as a permanent ordinary-path exclusion, but one
packet per day is not proof that processing keeps up with incoming material. Saved
technical work, oldest age, coverage and unselected-without-reason totals must remain
visible. Full-source quality and reconciliation remain #55; provider admission is
#122. Do not claim either from a successful RSS selection.

Offline verification must show a useful identity beyond the first packet reaching
real preparation on a later invocation; interruption and empty/new feed observations
preserve prior evidence; existing accepted snapshots still resume presentation
without selection; and no candidate accounting updates delivery state. Exact
source repetitions, a contrary report, category/source coverage and current
eligibility changes need explicit checks. Final review, ordinary-runtime acceptance
and sustainable throughput remain separate from these local fixtures.


### Current-work storage boundary

The candidate-only 32,000,000-byte safety ceiling applies to the current working
set and current collection accounting, not accumulated historical source bodies.
The accepted-preparation snapshot's existing 4 MB bound remains unchanged. Record
used and remaining capacity before model work, with a 2 MiB reserve for the existing
primary/fallback responses, escaped Unicode, captures and report metadata. This is
storage admission, not a token estimate or editorial quota. No identities are
silently truncated when capacity is exhausted.

Each exact source occurrence is stored once by content hash. Each immutable report
contains only its own packet, bound decisions, source references and the current
collection's fetch/exclusion observations. It never embeds prior packets or the
whole candidate ledger. The ready edition binds all referenced source objects through
its existing persistence/hash barrier.

The active root retains current pending work and recoverable results. Resolved
metadata decisions move into separately addressed per-identity records, after their
source and packet objects have been written and verified. Active entries take
precedence after an interrupted retirement. Policy-excluded saved work remains
enumerable and can return after source reapproval or unblocking even when RSS has
rotated it out. Historical resolved bodies are read only for identities being
reconsidered. Current alternates retain the latest occurrence per source binding;
older versions remain in immutable evidence rather than being re-expanded each day.

Total archive storage still grows with new evidence. This proposal introduces no TTL,
archive deletion or unlimited-retention promise. Finite verification must show that
current-work admission and individual report size do not grow with resolved historical
windows, including repeated revisions of one identity. The current 54-feed, 200-entry
parser bounds describe a theoretical maximum, not measured eligible daily arrivals.

At the accepted PR #125 storage baseline, offline twenty-window verification with
twenty newly resolved identities per window
keeps the active root at 342 bytes and each packet object at 44,034 bytes. All source,
packet, index and history objects total 2,239,214 bytes after window twenty. The prior
full-snapshot design used 20,318,687 bytes for snapshots alone in the matched scenario
shape (response wording differs). Eight revisions of one identity keep its latest
index at 1,248 bytes, with one current proof and no obsolete alternate references;
all original packet evidence remains readable. These baseline fixtures establish removal of
the cumulative-copy defect, not production arrival rates or unlimited archive capacity.
Later explicit handoff provenance adds fixed per-identity metadata; the current growth
regression still requires constant active/per-packet size and linear total storage.

Previously observed delivery-cache membership is retained as cache evidence, not
retrospective proof of a Telegram receipt. It prevents recycling an old accepted
report after normal cache pruning. Handoff into preparation alone is not delivery:
still-eligible unsent selected work can recover after its publication window expires.
If its selected occurrences no longer all meet current policy, remaining eligible
work is explicitly returned to a bounded technical attempt rather than stranded.


A valid primary abstention retains #120's accepted empty snapshot for its publication
day. New candidate responses with only deferred/missing/invalid dispositions do not
qualify as accepted empty decisions; existing legacy snapshots remain compatible. Repeating preparation in that window returns no ready edition; it does not
advance another packet. Unseen work can advance in a later fresh preparation window.
This inherited limit is part of the remaining throughput acceptance, not a claim
that all observed candidates received an editorial decision.


### Typed decisions and verified active compaction

Per-ID capture is separate from ModelReview/BlindReviewReport/PreparationSnapshot.
Bind each attempt to exact slot/provider/model, bundle, prompt and raw response hash;
only the delivery-used primary/fallback may change candidate state. Unknown, missing,
conflicting or malformed entries remain unresolved. Duplicate targets are one-hop
validated selected IDs in the same request; no topic-only equivalence is inferred.
The exact evidence packet bounds relevance selections; publication capacity is not
part of the model request. The reviewer considers all supplied items for relevance and returns at most
`review.max_detailed_selections` detailed selections (default 5, independently
configurable from evidence and publication limits). Other useful items must receive
concise `deferred` response-capacity dispositions, never `not_selected`. Live parsing
enforces this detail limit; saved-report validation retains the original packet
membership ceiling, including partial-result rejection indices. Explicit unfinished
provider responses are rejected before parsing, even when their JSON is closed.
Absent provider finish metadata remains compatible. Quote, identity and provenance checks,
the 32,000-character response limit, configured output tokens and call count remain.

`review.max_selections` caps publication cards locally, after the complete validated
report is saved. Card formation preserves the review's order and does not trim its
selections, quotes or dispositions, or establish delivery. The existing packet field
of that name remains frozen publication-cap provenance; it is not a cached relevance
validation ceiling. Prior packet/report objects, hashes and accepted schemas remain
unchanged. A changed publication cap alone does not change the relevance prompt.

Overflow remains selected and eligible until ordinary preparation rechecks current
policy and delivery history. Once some members have confirmed delivery/cache
evidence, the existing mixed-eligibility recovery returns unsent eligible selections
to technical pending for a later bounded packet. No automatic additional request,
new queue or whole-report replay is introduced. Ready/accepted preparation and
same-window no-op precedence remain unchanged. An offline synthetic eight-useful-item
fixture preserves all eight in the archive, emits five cards, confirms those five
through the mocked delivery flow and prepares the remaining three in the next
window; snapshot failure also recovers the full saved report without another call.
Reasons are RSS metadata judgments, not proof of full-source irrelevance or fidelity.

The new draft codec replaces the undeployed prototype formats; unsupported prototype
files fail explicitly rather than invoking a migration framework. Deployed accepted
preparation, ready edition and model-review schemas remain unchanged. Old accepted
reports without candidate sidecars continue to work. A completed result is persisted
in the active root before immutable freezing, so an interrupted archive write can
resume without repeating its model call. Direct references are verified before active
replacement; unknown or undelivered work is not retired merely because preparation
accepted it. Cache membership remains cache evidence, never a fabricated receipt.

Offline 20-ID/5-card examples count 926 English, 976 Cyrillic and 2991 JSON-escaped
Cyrillic content tokens with the existing official o200k asset. They establish finite
fixture fit only, excluding provider framing/reasoning; they are not a universal 4096-
token guarantee. Truncated output remains incomplete without another repair call.


Metadata completeness is per stable article identity using the exact chosen RSS
occurrence. Retained alternate source memberships are not claimed reviewed; this
does not introduce a pass over every historical RSS variant. New changed evidence
reopens its active metadata decision, while prior bound judgments remain archived.

### October 7 response-capacity and outcome correction

A production response exhausted 4096 completion tokens during its fourteenth
detailed selection, before limitations and dispositions. No partial JSON is
accepted or reconstructed. Bounding detailed entries reduces output demand without
changing the 20-item evidence default, five-card publication default, output token
allowance or request count. It is not a guarantee that every configured packet
fits every provider's token allowance. Deferred entries remain unresolved with
their exact response provenance; valid detailed selections can still be published.
An explicit response-capacity disposition is mechanically retained as pending.
Whether a free-text non-selection reason improperly disguises capacity remains a
semantic audit question; no keyword classifier or fidelity claim is introduced.

Technical empty selection yields `selection_incomplete` and a nonzero preparation
exit after candidate persistence. Complete editorial abstention and no eligible
input remain ordinary no-ready outcomes. The paired workflow deployed by runtime
[#64](https://github.com/Lenivvenil/digest-prod/pull/64) (`dbc474cb`, engine `af34f802`)
carries this status to its final outcome check while preserving persistence and
independent sender recovery. Historical workflow reruns still use historical YAML;
engine status alone cannot override that workflow's continue-on-error.

PR #135's optional closing item is not deployed. Five detailed entries cannot
guarantee five main cards plus one closing item: integration must explicitly review
the shared detail budget and retest main preservation before closing activation.

## Deployed freshness and technical continuation correction (#121 / #132)

The first-unseen choice below records the October 7 policy. The
[oldest-unseen amendment](#oldest-unseen-opportunity-amendment-196) supersedes that
choice while preserving the remaining fresh/age source queue and retry policy.

The October 7 private-state replay exposed two limits in the deployed ordering:
new same-source entries wait behind that source's entire unseen inventory, and
unseen work can exclude every technical retry. Owner approval on 2026-10-07
authorized engine [#139](https://github.com/Lenivvenil/digest/pull/139) (`e0740534`)
and runtime [#67](https://github.com/Lenivvenil/digest-prod/pull/67) (`50e2768d`).
The deployed correction changes admission only; it is not evidence of better
editorial relevance.

Source turns are class-local: unseen and technical work have separate source
rounds, not a global one-item-per-source quota. Within unseen work, turns alternate between
the newest usable supplied timestamp and the oldest original observation. A
usable freshness timestamp must be no later than both planning time and the
identity's first observation. A future-at-observation timestamp receives no
freshness boost even after its advertised date passes. Missing dates stay unknown.
Neither case is excluded, rewritten to “now,” or classified by event/marketing
keywords. A source with no usable dated item takes its oldest head. Source-head
ordering remains observation age, effective-priority ties, then stable identity;
priority is an allocation weight, not a professional-core label. Freshness/age
turns restart each packet. With many sources the packet can end before any age
turn, so this does not guarantee old or undated work against sustained arrivals.

`review.max_technical_retry_articles` is a strict integer from 0 to 100, default 4.
It reserves up to that many **fitting** technical entries inside the existing
packet count and character bounds. When unseen work exists, the allowance is
capped below the packet count and the first fitting unseen item is admitted before
retry reservation. Remaining places backfill with unseen work, then further
technical work; unused reservation adds no call or fictitious reviewed count.
A one-item packet retains unseen preference and cannot serve both classes in one
packet. An unseen item that cannot fit does not prevent fitting technical work.
Byte pressure can reduce either class's actual admissions; a reservation is not a
guarantee of four retries. Individually unrepresentable items remain technical,
and aggregate byte skips preserve their prior status. No skipped item is an
editorial rejection. The 20-item evidence, 16,000-character evidence and 4,096-token
response limits in the deployed configuration are unchanged.

Retries use the least recent retained planning opportunity for the exact current
source occurrence, with source round robin and effective-priority ties. A packet's
`planned_at` does not establish a physical HTTP attempt or success. Missing exact
proof falls back to original observation age without inventing an attempt. Existing
decision proof remains retained; if different, the latest exact-occurrence plan is
also retained through active-work retirement and reload (at most two proof packets
per current technical candidate). Old immutable packet/source records are not
rewritten. This does not change provider uncertainty handling or refund budget.

The measured private snapshot had 145 unseen and 40 technical identities. Existing
recency revalidation removed four expired unseen entries. A synthetic freshly
observed Finextra item moved from ordering position 135 to admitted position 14
with four reserved retries. The actual old/new planner replay admitted 20 unseen items (12,396 evidence
characters) versus 16 unseen plus four retries (12,460 characters). After real
begin/save/load in a temporary copy, the next packet used four different retry IDs.
These are technical probes, not provider or editorial acceptance. Important older stories did not uniformly improve: the digital-gilt
story moved 49 to 84, Stripe/FedEx 58 to 57, and Plaid risk models 130 to 70 in the
unreserved unseen ordering. The four-retry replay packet displaces four unseen
opportunities and still contains eligible future-dated events through ordinary
age/retry turns. No source is automatically accepted, and no fintech quota is
introduced. With 96 new entries and at most 20 admissions per ordinary window,
ordering alone cannot establish sustainable coverage before source recency expiry.
The remaining outcomes stay open in #121 and #132.

## Oldest-unseen opportunity amendment (#196)

The fresh source round can consume every unseen place before an age turn. A
finite eight-window probe of the existing planner and checkpoint reload reproduced
that documented limitation: stable eligible old identities remained unseen while
new source heads filled each packet. This is a fairness-policy amendment, not a
claim that candidate evidence was lost or that the previous guarantee was broken.

The existing protected first-unseen opportunity now chooses the oldest **fitting**
eligible identity by parsed `first_observed_at`, then descending effective priority,
source name and stable identity. Publication dates and category do not break these
ties. Original identity observation age survives a changed source occurrence; no
cursor or new persisted field is introduced. Existing timestamp validation remains
unchanged.

After that opportunity, technical reservation and the precomputed fresh/age unseen
queue continue unchanged, with attempted protected candidates removed. The queue
is not rebuilt after promotion. Closing-source ordering also keeps the original
queues, and the optional closing opportunity cannot displace the protected item.
Individually unrepresentable evidence remains technical pending; source-recency
exclusions remain recorded exclusions rather than editorial rejection.

At a one-item cap, age deliberately takes precedence over freshness. The protected
identity and a fresh backfill item may come from the same source. One scheduling
opportunity changes, but a larger old item can consume more of the existing
character budget and reduce later admissions or distinct-source coverage by more
than one. Technical reservations still mean fitting opportunities, not a promise
of four admissions. There is no added evidence slot, detail allowance, model call
or execution budget.

Beginning and saving a packet advances its admitted identity to technical pending;
repeated planning alone does not. This bounded admission rule establishes neither
editorial suitability nor delivery, and does not guarantee drainage when arrivals
exceed capacity or progress before every source's recency expiry. Future packet
membership and its request hashes intentionally change. Stored packet bytes,
historical decisions, formats and accepted-preparation precedence do not.

## Review and occurrence ownership amendment (#146)

[ADR0018](0018-review-reuse-and-source-attribution.md) records shared pure
exact-request reuse and strict request-evidence validation in the editorial domain.
Request budgets, live response-detail limits, canonical stored integrity and cached
packet-membership validation remain distinct; accepted recovery is unchanged.
The catalog owns the seven-field `SourceOccurrence`; the named `CandidateArticle`
compatibility subclass preserves its Python and serialized contracts. Occurrence,
packet, report and decision hashes, immutable storage, scheduling and retirement
policy remain unchanged. This does not establish natural-run or disposition quality.

## Candidate admission ownership (#211)

[#211](https://github.com/Lenivvenil/digest/issues/211) shares canonical RSS item
preparation, exact per-item JSON measurement and bundle serialization in
`domain/editorial/evidence.py`. Candidate admission lazily caches evidence by exact
occurrence and checks complete fit without repeatedly rebuilding proposed bundles.
Its existing scheduling order remains separate from category-round-robin evidence
order; both the generic builder's duplicate/omission rules and admitted packet bytes
remain unchanged.

The internal `candidate_policy.plan_articles` now returns `AdmittedCandidates | None`
instead of an article list. Its `.articles` and `.evidence` supply the sole production
caller, `application.candidate_review.plan_packet`, directly. Private
`_admit_candidate` and `_closing_opportunity` helpers retire; the existing fit test
exercises the planner. The two clock samples and application effect order remain.
Oldest-unseen/retry protection, intrinsic technical-pending versus aggregate skips,
closing replacement, laziness and all size limits retain their existing rules.
Strict request and persisted-proof validators stay separate and unchanged; no schema
migration, scheduling-policy change or test-count reduction is implied.
