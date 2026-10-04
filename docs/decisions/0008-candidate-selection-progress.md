# 0008. Account for candidates across bounded preparation packets

Status: proposed; local implementation and review in progress

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

Offline twenty-window verification with twenty newly resolved identities per window
keeps the active root at 342 bytes and each packet object at 44,034 bytes. All source,
packet, index and history objects total 2,239,214 bytes after window twenty. The prior
full-snapshot design used 20,318,687 bytes for snapshots alone in the matched scenario
shape (response wording differs). Eight revisions of one identity keep its latest
index at 1,248 bytes, with one current proof and no obsolete alternate references;
all original packet evidence remains readable. These fixtures establish removal of
the cumulative-copy defect, not production arrival rates or unlimited archive capacity.

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
The inherited selection response allowance is not an editorial rejection rule:
useful overflow must be deferred. The existing output ceiling and call count remain.
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
