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
publication as distinct facts. A valid response that does not select an input gives
no per-item rejection reason: record that limitation, not an editorial rejection.
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
500-character descriptions occupies 16,451,215 serialized bytes, including the
historical-cache provenance field. This leaves 15,548,785 bytes before packet/history
growth under a candidate-only 32,000,000-byte safety
ceiling; the accepted-preparation snapshot's existing 4 MB bound is unchanged.

Record used and remaining storage capacity and fail before model work without
truncating identities. Historical packet and immutable report evidence still consumes
space; this ceiling is not an unlimited-retention or sustainable-throughput solution.
Compacting resolved records into verified archive references needs a separate reviewed
lifecycle choice rather than ad-hoc deletion or an invented TTL in this patch.


Previously observed delivery-cache membership is retained as cache evidence, not
retrospective proof of a Telegram receipt. It prevents recycling an old accepted
report after normal cache pruning. Handoff into preparation alone is not delivery:
still-eligible unsent selected work can recover after its publication window expires.
If its selected occurrences no longer all meet current policy, remaining eligible
work is explicitly returned to a bounded technical attempt rather than stranded.


A valid primary abstention retains #120's accepted empty snapshot for its publication
day. Repeating preparation in that window returns no ready edition; it does not
advance another packet. Unseen work can advance in a later fresh preparation window.
This inherited limit is part of the remaining throughput acceptance, not a claim
that all observed candidates received an editorial decision.
