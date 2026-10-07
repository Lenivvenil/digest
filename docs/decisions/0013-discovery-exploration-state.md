# 0013. Keep exploration attempts separate from source offers

Status: accepted for the implemented first slice of
[#132](https://github.com/Lenivvenil/digest/issues/132). Owner approval on 2026-10-06
authorized PR #133 and its engine-pin rollout. Professional-radar protection after
approval and ordinary recommendation quality remain open; this is not semantic
breadth or complete issue acceptance.

## Context

The source-discovery prompt restricted recommendations to professional technology
subjects. URL deduplication did not rotate disciplines, and an invalid old pending
proposal consumed a bounded feed check on every run. Discovery is a separate operator
approval loop from Irritator searches. Fintech, banking and architecture remain the
professional radar; this decision changes proposal generation only.

## Decision

Add `discovery.exploration_areas`, provisionally defaulting to fintech/banking/architecture,
science, society/institutions, history/culture, environment and design. Accept 1–16
distinct trimmed names of 1–80 characters, rejecting case-insensitive duplicates.
The defaults are explicit
starting settings, not historical user preferences or an owner-mandated proportion.
Professional source refresh remains in the default rotation alongside the five
cross-field targets. The generation prompt matches the requested area and requires
no contrived technology or banking connection for another discipline. This does not
reserve daily slots or establish professional protection after source approval.

Within a pass through configured areas, select the least recently offered area among
those not yet attempted; ties preserve configured order. When all current areas have
been attempted, start a new pass. Pure least-recent-offer ordering was rejected because
an empty or invalid area could monopolize future requests. An attempt is saved before
generation and never constitutes an offer, successful classification or coverage.
An interrupted `started` record remains an attempt. This does not introduce a model
request reservation, automatic retry or exactly-once model-call guarantee.

Retain optional additive fields in schema 1 of `discovery_delivery.json`:

| Field | Contract |
| --- | --- |
| `proposal_areas` | Exact SHA-256 proposal binding to requested area; generated, feed-validated proposals only. Legacy pending entries receive no inferred assignment |
| `attempted_areas` | Distinct configured areas attempted in the current pass, at most 16 |
| `area_offers` | At most one latest `{updated_at, status}` per configured area; status is `confirmed` or `unknown` |
| `generation` | Latest requested area, timezone-aware request time, cycle, outcome and up to three validated proposal bindings; outcome is `started`, `failed`, `empty`, `no_valid_proposals` or `proposed` |
| `validation_failures` | Exact pending binding to timezone-aware failure time, failed cycle and nullable skipped cycle |

At prepare, fold final persisted confirmed/unknown receipts into `area_offers` before
30-day receipt pruning. Unknown denotes possible delivery; keep its status distinct
from a confirmed Telegram receipt. Reserved and explicitly API-rejected messages are
not offers. Do not fold pre-POST uncertainty while sending: a final rejection must
not become false exposure. Source approval/rejection does not erase an actual prior
offer. Requested area is provenance for the prompt, not proof of the source's subject.

Latest area summaries survive receipt expiry while their areas stay configured.
Prune summaries and attempted entries for removed configured areas. Keep binding-area
provenance while the binding remains pending, in retained receipts or in recent
decision history. If a removed area returns while a retained receipt still exists,
that real receipt can inform the new pass. Remove failed-validation entries when
their exact pending binding is gone. The latest generation is a single bounded audit
record; it may describe an older request if a full pending batch prevents generation.

Absent optional fields initialize empty. Present malformed fields fail closed; retain
the 256,000-byte metadata read ceiling and enforce it before atomic writes as well.
`PendingSource`, its five serialized identity fields and `proposal_binding` stay
unchanged. Existing persisted decisions still bind to the same proposal. No migration,
new cache file or runtime persistence list is required.

## Pending validation transitions

1. A failed validation consumes its check in cycle A and records `failed_cycle=A`,
   `skipped_cycle=null`; reruns of A defer that exact proposal
2. The next distinct eligible prepare cycle B records `skipped_cycle=B` and skips
   validation; every rerun of B still skips
3. A later eligible cycle C may retry. Failure replaces the marker with C/null;
   success clears it. Expiry remains the original 30 days and records `expired`,
   never editorial `rejected` as a consequence of technical failure

A proposal is eligible only if reached before the existing three-check limit and
it has no delivery receipt or configured URL. Backlogs can delay generation and when
the skip is observed. Managed cycle identity is `GITHUB_RUN_ID`, excluding rerun
attempt; send ownership remains run-plus-attempt. Direct local invocations are separate
cycles. Send-only phases do not change cooldowns. A changed proposal binding receives
no inherited failure marker; expiry/discovery timestamps are never rewritten.

## Preserved boundaries and open work

Keep one logical generation, at most the first two already-configured routes with
zero retries, three total validation attempts/offers, 2,048 output tokens, weekly
runtime cadence and ten-minute job ceiling. Pending failures do not reclaim their
current check. Full legacy offer batches require no model call. Preserve feed/URL
safety, deduplication, Add/Reject, exact remotely persisted pair hashes, attempt-owned
reservations and unknown-send holds. Active sources and daily scheduling are unchanged.

Approval still adds a priority-3 trial source. `adaptive.trial_slots` is not enforced
by the current candidate scheduler and cannot substantiate professional protection.
Busy professional backlog and aging exploratory admission need a separate reviewed
decision. #132 remains open for that path and for ordinary scheduled evidence of
useful cross-field recommendations. Category names, URL counts and offline green
tests do not establish actual novelty, publisher diversity or reduced filter bubbles.

## Validation

Offline cases cover two passes through empty/invalid/failed generation, mixed confirmed
and uncertain sends, explicit API and later editorial rejection, held reservations,
legacy metadata, persisted exact binding, same-run cooldown retries, expiry, retained
area history and malformed/oversized state. Existing feed safety, configured fallback,
pair-barrier and Add/Reject regressions remain required. No live feed/model calls or
feed activation are part of this verification.
