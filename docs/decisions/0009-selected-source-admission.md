# 0009. Bind resumable source admission to validated candidate work

Status: proposed; local integration under review

Refs [#122](https://github.com/Lenivvenil/digest/issues/122),
[#55](https://github.com/Lenivvenil/digest/issues/55),
[ADR0007](0007-compact-issue-reservation.md), and
[ADR0008](0008-candidate-selection-progress.md).

## Scope of this proposed mechanism

Mandatory complete-source processing was an assistant-introduced interpretation,
not an established owner requirement or a prerequisite for closing #55. The
[domain provenance correction](../domain/digest/overview.md#requirement-provenance-correction--2026-10-05)
preserves that distinction. This experimental mechanism remains unaccepted and disabled.

This proposal reuses draft #107's acquisition, exact/conservative request admission and contiguous
page progress. Preserve the current candidate scheduler, accepted preparation and
model-free sender. Technical limits must remain technical pending, and uncertainty
must not cause an automatic repeated generation. Complete pages are not a semantic
quality verdict. These requirements do not authorize a provider, allowance or schedule
change, or activation of the draft reading path.

## Proposed integration

The current eligible chosen occurrence and its exact saved delivery-used selection
report authorize source work. Bind the stable article identity to occurrence/feed URL,
bundle, prompt and response hashes. A changed policy alone does not discard compatible
work; current source, blocklist, age and delivery boundaries are checked by candidate
reconciliation. A different occurrence cannot silently inherit an old selection.
This uses the existing candidate scheduler, without a second independently scanned queue.

Source work runs before accepted preparation. A completed source record and every page
result are frozen with their original source reference and selection binding as a
technical handoff for #55. They do not create a PreparationSnapshot, ready edition,
editorial acceptance or delivered marker. Accepted presentation retains precedence and does not repeat source acquisition,
source counting or source generation. It may still use the existing presentation
translator if its cache is missing. The sender performs no model calls. The draft concatenated
reading-angle renderer is not used by ordinary publication; #55 still owns global
reconciliation and useful factual presentation.

Write a request intent before each counting/generation adapter invocation. This is
not proof of a POST: pacing or local checks may stop dispatch. Bind each record to the
actual route, request hash, source and page range; retain only safe completion/usage
metadata. An unresolved generation intent, ambiguous failure or accepted invalid output
holds later invocation and route changes. A known explicit rejection may use the
existing configured fallback within the same allowance. An unknown count is not an
unknown generation: its identical remote count is not repeated, but a supported
configured local-admission route can proceed without claiming that count succeeded.

Old completed evidence can be reused when the full saved Selection exactly matches
the current proved occurrence and source/page/response checks pass. Record this as
current adoption with unknown historical feed binding, not retrospective proof that
the new selection authorized the old request. Preserve previous bindings and state
before a new revision. An unresolved generation cannot be bypassed by changing only
RSS description or fetch timestamp. Untouched old pages remain resumable; old pending
pages lacking outcome tracking are conservatively held where dispatch is uncertain.

The actual supplying page route labels quoted source evidence, including fallback;
the configured first route must not be presented as the actual source of all results.

## Compatibility and budget

Keep deployed ModelReview, BlindReviewReport, PreparationSnapshot and ready-edition
schemas unchanged. Preserve #125's valid-selection salvage and typed disposition
capture. The superseded #107 tests required every selection to be discarded when
finish reason was absent or MAX_TOKENS. #125 instead retains individually validated
selections while missing/truncated dispositions remain incomplete and cannot create
editorial rejection. Reading mode therefore retains that contract and the existing
selection-response allowance; useful overflow remains deferred. No additional model
call or prompt-repair cycle follows an incomplete disposition.

Replace the draft default of twelve with an in-process ceiling of at most ten request
reservations, shared by selection, counting, fallback and reading without resetting
spent requests. Reservation counts are not provider receipts or account quota. Keep
the 360-second application window less presentation reserve and 45 seconds for
persistence; each call still checks pacing and remaining useful time. Exact Gemini
counts and the pinned GPT estimate remain distinct accounting methods.

### One allowance across separate processes

The whole-cycle allowance is ten model-service request reservations, addressed by
immutable GitHub run ID rather than publication date. A rerun cannot initialize or
regrant it. The three fixed stages are preparation, Irritator and independent
comparison. A remotely persisted stage claim exclusively holds the current remainder;
no next stage can start while prior usage is active or unknown.

After the existing Git/hash barrier, a one-use local begin marker binds an execution
nonce and initially empty journal. The common LLM reservation point locks and atomically
persists each count/generation/fallback attempt before adapter dispatch. A credential
failure may conservatively consume a slot; the record never claims a POST receipt.
Finalization after the original process exits requires its exact claim/attempt/nonce
and journal. A fresh checkout of the initial remote claim, missing usage or corruption
cannot be finalized as zero. There are no per-request Git pushes. Existing final stage
persistence exposes only the verified remaining allowance to the next model process.

One small file per run contains at most three stage records and ten attempts; prior
cycles are not scanned or copied into new admission. Sequential model stages restore
the preceding reservation time for pacing. This is not a general parallel-process
rate limiter or an assertion of provider/account quota. An in-process cap remains a
second bound. A typed shared-budget failure occurs before HTTP and therefore leaves
source generation resumable; it is not confused with an ambiguous provider outcome.

The runtime proposal reserves only after feedback. Failed budget setup still allows
compatible preparation to reuse accepted/cached work with model dispatch denied.
Frozen sending is independent, including when preparation fails. Optional claims share
their existing marker pushes. Initial control commands must fit before the original
preparation cutoff, and optional execution rechecks its remaining envelope after the
barrier. Command timeouts include kill-after; no 8/12-minute job or monthly allocation
is enlarged. Primary journal upload is best-effort only when its bounded upload window
fits; otherwise the remote reservation remains held rather than guessing lost usage.

Engine pin and workflow must deploy atomically. A small explicit protocol capability
check prevents an old engine from ignoring the new model-budget environment. A missing
capability blocks model-capable preparation/optional work while feedback and the
independent sender remain available. This workflow is a reviewed proposal, not a
current activation. Reading remains off and #55 acceptance is still required.

### Continue other candidate work after a technical handoff

The existing scheduler may skip an exact saved report only when all relevant selected
source work has a verified immutable technical handoff or retained generation-unknown
evidence that prohibits an automatic attempt. A genuinely resumable member keeps its
packet eligible. Missing/corrupt proof or a missing handoff never implies completion.
The filter does not change candidate status, mark preparation/delivery, or introduce
another queue. It lets later unseen/technical candidates advance while preserved source
work awaits #55 reconciliation or uncertainty recovery.

Complete empty metadata packets have no source work and can release their temporary
empty-result protection in reading mode. Resolved peers move through the existing
verified index/archive path. Rehydration preserves actual recorded preparation handoff
flags; retirement alone is not evidence that #120 accepted anything. Older index records
without handoff provenance remain readable but do not gain a fabricated accepted flag.


## Verification and remaining acceptance

Offline checks cover exact saved selection reuse, current occurrence/policy rejection,
full-source technical handoff without presentation, accepted-state precedence, actual
fallback provenance, unknown generation across invocations/config changes, count-only
uncertainty, legacy evidence and shared limits. Preserve actual source/response records;
mock success is not semantic fidelity, quota availability or sustainable throughput.
Owner decision and review apply before merging this proposed cross-context contract.
#55 remains open for global qualification/contradiction reconciliation and useful output.

## Known publication failure retained for #55

The saved Citi result selected the scope footnotes but the single-page renderer omitted
them from presentation provenance. Retain every selected qualification there regardless
of page count. This is an evidence-preservation correction, not semantic acceptance:
the same generated prose still incorrectly generalized Citi's offering to the entire
Swift network. Adding the correct footnote does not make that assertion correct.

Multi-page completion remains a technical handoff. Neither concatenated page summaries
nor appended quotations establish an accurate whole-article brief. A changed publication
algorithm or source-unit selection prompt requires its own proposed decision and real-source
acceptance; this integration does not authorize it or activate source reading.

## Preserve known source qualifications in optional investigation

The full-source Irritator input previously narrowed every later stage to the narrative's
chosen citation IDs. That could discard a qualification already retained from the same
source, even though extraction had seen it. Query and ranking now carry those known
qualification passages separately from the unchanged narrative citations. Matching
requires the exact article identity, source snapshot hash and body hash; another article
or revision cannot supply implicit context. This is still selected evidence, not every
condition in the original source or a semantic-completeness verdict.

The full serialized contextual evidence envelope, including IDs and provenance, must
fit the existing 16,000-character evidence bound. An oversized envelope holds optional
query/search/ranking as technical incomplete without truncating known conditions. This
is a conservative transport bound, not token counting or provider quota admission.
The shared request-admission integration below supplies route-specific input checks;
account quota and operational acceptance remain unverified. Existing RSS input,
request ceilings and primary delivery remain unchanged. This correction does not repair the saved RSS-only Progent applicability
error or establish automatic counter-signal quality.

### Reuse admission for complete optional-stage requests

The source reader and full-source Irritator now share the same profile lookup, wire
serialization and local estimate formula. For each full-source extraction, query and
ranking request, admission binds the actual provider/model, complete serialized wire
body, temperature and output reserve. Dispatch checks that binding again and uses the
same explicit provider override. It cannot count one route and silently dispatch another.
The source reader's configured fallback continues to admit each actual route separately.

The supported Groq route retains its pinned tokenizer estimate, 20% margin, 256 framing
reserve and 8,000-token local allowance. Gemini retains its exact matching-request count
API and input profile. These methods remain distinct and neither proves remaining
account quota. Unsupported routes, unavailable accounting, unknown counts and oversized
requests hold optional analysis without removing source context. Counting shares the
existing request counter/pacing and the same absolute stage deadline; it reserves room
for a generation request instead of spending the last slot on an unusable count.
No additional retry, fallback, provider or allowance is introduced.

Stage diagnostics gain optional admission evidence with a request hash and safe
exact/estimated values; old diagnostic records without it remain readable. Actual
provider usage remains separate. The source reader's stored admission dictionaries,
request hashes, completed page evidence and #120 accepted/ready schemas retain their
existing format. Tests patch the newly shared helper owner rather than changing their
expected reading behavior. The legacy RSS Irritator path adds no count/admission calls.

This is local implementation under proposed ADR0009, not runtime activation, semantic
acceptance or proof of free account capacity. The historical optional-stage 65-second override originated in the
[primary-first implementation](https://github.com/Lenivvenil/digest/commit/d8b6250cc5a9444971af5f122f92e2c14c49118e)
and is documented as conservative Groq spacing in [the review protocol](../BLIND_REVIEW.md).
It is not a Gemini quota or an owner requirement for every provider. The legacy RSS
path retains that policy. The full-source path uses the explicitly selected route:
Gemini respects the configured interval; Groq uses the greater of that interval and
65 seconds. Source-reader fallback rebinds both admission and pacing to each actual
route, preserving the shared counter and prior request timing.

A nonempty Gemini extraction/query/ranking path needs six physical count/generation
requests. At a configured 20-second interval their start-time floor is 100 seconds;
blindly applying the old Groq 65-second floor would make it 325 seconds. The supported
Groq route uses local admission and three generation requests. Neither arithmetic
establishes provider/account RPM or token quota, and count/generation entitlements must
not be conflated. Actual latency or a slower configured interval can still leave the
unchanged 180-second window incomplete. The primary sender remains independent.

## Local reconciliation input binding — proposed

A completed source handoff can supply an offline, immutable reconciliation input.
It must bind the complete original Source hash and body hash to every admitted,
completed page, retaining each actual route, request/admission and response proof.
The existing page validator remains authoritative; an unfinished page, unresolved
generation or mismatched source cannot be represented as completed reconciliation
input. Legacy evidence keeps its original provenance rather than acquiring invented
request-attempt history.

The reconciliation payload has explicitly **sparse original-source evidence**: ordered
original span IDs, offsets and exact text for every span referenced by retained page
findings, selected passages or nominated conditions, including conditions from
abstaining pages. It also retains those page findings and their ID associations.
The immutable input hash binds both the evidence and page proofs. Dropping a required
condition or changing a page/source requires rejection, not a silent new interpretation.

Complete technical page coverage is a separate fact from sparse reconciliation input
coverage. Neither proves that every material condition was identified or that the
page prose is faithful. Do not reuse PageResult's contiguous original-range coverage
field for this new sparse payload or call it a whole-source reconciliation read.
The final semantic brief/output contract remains proposed under #55.

A prospective request binds the exact messages, actual supported provider/model,
output reserve and serialized wire through existing source-admission helpers. Local
Groq estimates keep their current margin; a Gemini request requiring remote counting
remains unverified in offline planning. Oversized/unverified input stays intact and
held. The plan adds no dispatch, retries, queue, request allowance or deadline. Any
later execution must use the existing shared per-cycle counter, count/generation
admission, pacing and absolute deadline; no local sizing result grants quota.

### Local response binding — proposed, no dispatcher

A future reconciliation response uses the existing selection/qualification IDs,
reading-angle prose and abstention semantics, without a contiguous-source coverage
field. Its separate machine envelope binds the verified sparse input, versioned
protocol instruction, exact provider/model/output-bound request and raw response hash.
A local estimated-fit plan is insufficient: parsing requires separately supplied,
consistent admission and terminal-completion metadata. Those records are assertions
from the caller, not cryptographic evidence that a physical request occurred. This
slice neither creates such a receipt from a plan nor dispatches a request.

The planner may size arbitrary proposed instructions, but this response version binds
only the exact fixed protocol instruction. A different editorial instruction needs
an explicit versioned response design; hash matching is not relaxed. Saved Groq
accounting is checked for declared-count arithmetic/profile consistency without
retokenizing the wire, and saved Gemini counts are not independently recounted.
Both remain trusted-caller admission assertions, not newly measured facts.

Original sparse excerpts are source evidence; retained page prose is a prior model
assertion. Every nominated input qualification ID remains retained in the response
and immutable archive, including conditions irrelevant to a selected final finding.
The final prose must preserve the conditions affecting its retained claims, but ID
membership or echoing cannot establish that relationship. Unknown IDs, missing
required IDs, malformed/empty non-abstention, unfinished output and inconsistent
input/request/response records fail validation. No truncation or repair is performed.

The generated brief and complete evidence archive remain separate. Validation proves
schema, identity and citation membership only, not source-wide reading, truthful
paraphrase, omitted-condition safety or publication eligibility. A later operation
still needs the existing cycle budget, actual-route admission, deadline and durable
request-outcome integration; #55's semantic release evaluation remains open. The
protocol does not adopt the separate experimental semantic prompt or activate any
publication path.

### Local computation driver and persistence preconditions — proposed

A local driver may prepare an exact fixed-protocol operation, then execute only an
existing, matching operation record. The immutable prepared intent binds its originating
reserved cycle/stage claim before checkpointing; later execution cannot add a new
origin to a ready-looking record restored after runner loss. Its caller must attest
that the exact immutable
intent has crossed a verified persistence checkpoint; a local atomic write or a
well-formed commit/hash string does not establish remote durability. Uncached work
also requires the existing active shared-budget execution. Neither missing state nor
a different input/route authorizes recreating an uncertain operation.

Persist local adapter intent before counting or generation. Preserve completed,
definite-failure, unknown and accepted-invalid outcomes separately; acceptance of a
response precedes parsing, so an invalid result does not buy an automatic retry.
Count uncertainty is not generation uncertainty: a previously bound configured local
counter route can remain distinct from repeating an uncertain remote count. Unknown
generation conservatively holds replay across inputs/routes in this draft. This is
not an accepted perpetual owner prohibition: the historical domain restricted timeout
retry "in that pass", and bounded later-cycle recovery remains a separate proposal.
Foreign cycle/nonce execution requires
a separately reviewed checkpoint transition; this slice does not establish a new
permanent count-only rejection or adopt the pending later-cycle recovery proposal.
Compatible completed cache reuse needs exact input/request/admission/response binding
and no new model allowance. Every candidate remains semantically unverified.

The existing remote stage reservation protects its cycle allowance after local usage
is lost. It does not identify an exact reconciliation request created later in that
stage. Local operation files survive ordinary retained-workspace recovery, not runner
loss. There are three future orchestration choices, none activated here:

- Include exact already-known operation intents in the initial remote stage checkpoint.
  Newly generated page results cannot be bound before they exist.
- Batch newly ready exact intents into one additional checkpoint before reconciliation,
  using remaining time inside the same absolute deadline. This is not one Git push
  per request and grants no extra model allowance; checkpoint failure holds dispatch.
- Consume previously persisted inputs in a later admitted invocation, still checkpointing
  its operation intent before dispatch. This is an option, not mandatory next-day latency.

The earlier shared-budget workflow proposal persists only the budget reservation
at its initial preparation barrier. Selecting and adopting a reconciliation-intent
barrier remains explicit runtime/ADR work; the paired local prototype is described
below. The local driver's caller precondition cannot substitute
for that verification. Telegram unknown-send replay rules and #120 preparation/sender
isolation are unchanged. No live request, workflow, runtime pin or publication path
is enabled by this proposed driver.

### Paired preparation/checkpoint prototype — local, not deployed

The proposed engine/runtime path connects the driver to the existing preparation
stage. After #120's accepted/ready precedence, reading-enabled managed preparation
uses current candidate proofs to find eligible frozen handoffs, including saved
work when no new packet exists. It prepares one exact batch only while the original
reading cutoff leaves a usable checkpoint/execution window. Ordinary reading-disabled
preparation and the sender remain unchanged.

The batch binds source, selection, operation and eligibility references, the original
cutoff, the actually remaining local/shared allowance and the prior pacing floor.
A separate bounded phase stages only its intended files, commits/pushes once, fetches
the remote tip, verifies commit reachability and exact current referenced bytes,
then invokes the driver. A rejected/uncertain push, changed input/history or expired
window prevents dispatch. A capability check requires the paired engine interface.
No protocol instruction or semantic publication algorithm is adopted by this wiring.

Crucially, the active nonce-bearing model-budget journal is excluded from the
mid-stage checkpoint. The remote retains the original unbegun reservation while the
original workspace retains its active journal. Both are checked separately. Thus a
fresh checkout cannot execute or finalize the reservation using the original nonce
through the existing budget APIs. No second workspace-witness authorization layer is
introduced. The later ordinary persistence step records finalized usage and outcomes;
local writes are not described as surviving runner loss.

Persisted per-item outcomes distinguish genuinely unattempted work from unknown or
accepted-invalid computation. A new claim may rebind only proved-unattempted work,
retaining its exact prior record; another operation's budget use is not evidence
that this item dispatched. Missing proof or any uncertain attempt remains held.
Outcome persistence after stage finalization is idempotently repairable without
releasing allowance again or repeating model calls. This does not adopt the separate
later-cycle unknown-compute recovery proposal.

The proposed runtime keeps its existing triggers, permissions, job deadlines,
feedback ordering and independent frozen sender. All outputs remain technical
candidates. Local bare-Git and fake-transport verification can establish orchestration
invariants, but does not establish live latency, account quota, semantic quality or
production acceptance. The prior frozen runtime proposal and deployed pins remain
unchanged until explicit adoption.
