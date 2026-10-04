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


### Local sizing evidence and proposed storage boundary

The current runtime has 54 enabled feeds, each with the existing 200-entry parser
boundary; 10,800 is a theoretical observed population, not measured eligible daily
arrival volume. The old 55-item runtime sample was already source-allocated and
cannot estimate the full pre-slot population. After removing repeated article bodies
from occurrence and observation records, a synthetic 10,800-item population with
500-character descriptions occupies 18,103,615 serialized bytes, including the
decision and historical-cache provenance fields. This leaves 13,896,385 bytes before packet/history
growth under a candidate-only 32,000,000-byte safety
ceiling; the accepted-preparation snapshot's existing 4 MB bound is unchanged.

Record used and remaining storage capacity and fail before model work without
truncating identities. Historical packet and immutable report evidence still consumes
space; this ceiling is not an unlimited-retention or sustainable-throughput solution.
The active-reference encoding below is a reviewed implementation proposal for reducing
duplication. It does not delete archives or supply the still-unresolved retention policy.


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

For active storage, reference encoding v2 replaces only resolved or reversible-policy-
excluded payloads with direct identity/occurrence hashes and verified immutable report
archive references. Materialized v1 remains readable. No reference chains, TTL or
archive deletion are introduced. Write/verify the immutable evidence before atomically
replacing active bytes; missing/tampered references stop explicitly. Hydration restores
exact source occurrences before policy reconciliation. Selected, unknown and pending
work remains materialized; preparation handoff and legacy cache observations do not
constitute confirmed delivery proof. Original timestamps and decisions remain traceable.

Full immutable snapshots remain self-contained and can still grow quadratically with
history. Active compaction alone is not sustainable retention. The candidate-only 32 MB
ceiling applies separately to materialized report evidence; a conservative 2 MiB response
storage reserve before a provider call accounts for the existing two 32K-character
primary/fallback responses, up to 12 JSON bytes per astral Unicode character,
capture/report copies, evidence duplication and metadata. This reserve is
not a provider token estimate or editorial quota. Rollback to an old candidate reader
requires a verified materialized checkpoint; accepted edition/sender schemas are unchanged.

Offline 20-ID/5-card examples count 926 English, 976 Cyrillic and 2991 JSON-escaped
Cyrillic content tokens with the existing official o200k asset. They establish finite
fixture fit only, excluding provider framing/reasoning; they are not a universal 4096-
token guarantee. Truncated output remains incomplete without another repair call.


Metadata completeness is per stable article identity using the exact chosen RSS
occurrence. Retained alternate source memberships are not claimed reviewed; this
does not introduce a pass over every historical RSS variant. New changed evidence
reopens its active metadata decision, while prior bound judgments remain archived.
