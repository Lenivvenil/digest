# Architecture decision records

Decisions use the MADR format and numbered files. Preserve the original decision,
status and rationale; a translation or implementation observation does not change
who approved it.

## Existing decisions

- [0001](0001-adopt-claude-mini-governance.md) — Adopt the claude-mini engineering workflow
- [0002](0002-engine-instance-split.md) — Separate the public engine from runtime instances
- [0003](0003-source-state-split.md) — Separate source configuration from lifecycle state

These historical records retain their original language and decision context. Current
English explanations are in the [architecture guide](../ARCHITECTURE.md) and canonical
[domain overview](../domain/digest/overview.md).

ADR0004 was proposed/implemented in [closed, unmerged PR #93](https://github.com/Lenivvenil/digest/pull/93).
It is not part of main; the proposal's existence is not a release approval.

Choose the next unused sequence number after checking open PRs. If the local claude-mini
skill package is installed, its `next_adr_number.sh` helper can assist; that private tool
installation is not required to read, configure or run Digest.

[0005](0005-optional-presentation-translation.md) records the implemented opt-in
translation of primary and supplementary generated prose under #94. Narrow real
verification and ordinary translated output do not establish universal semantic fidelity.

[0006](0006-batch-message-voting.md) records batch-compatible message voting and the
150-second callback queue limitation. Real message ingestion, persistence, acknowledgement
and computed priority influence are verified; daily retention limitations remain explicit.

[0007](0007-compact-issue-reservation.md) records compact daily presentation and its
coarse pre-publication reservation, followed by the ready-edition amendment and the
#145 application-ownership extraction. No automatic resend lifecycle or exactly-once
guarantee is implied; the dated record preserves its separate acceptance boundaries.


[0008](0008-candidate-selection-progress.md) records accepted candidate accounting,
bounded preparation and evidence storage under #121. Owner approval on 2026-10-04
covered PR #125 and its engine-pin rollout; natural-run, disposition-quality and
throughput acceptance remain separate and open.

[0009](0009-selected-source-admission.md) proposes candidate-bound source admission
and technical handoff under #122; it does not approve semantic publication or runtime activation.

[0010](0010-group-source-points-with-qualifications.md) proposes an offline grouped
source-point representation for #55. It does not approve a new prompt or publication path.

[0011](0011-source-anchored-investigation-queries.md) revises the unaccepted mandatory
literal-query guard into optional provenance diagnostics for bounded RSS and full-source
investigation under #77. Useful grounded searches need no copied phrase; neutrality and
useful retrieval remain empirical gates. The optional-provenance revision is accepted and deployed; full-source activation remains separate.

[0012](0012-private-ranking-evidence.md) records accepted bounded candidate/admission and
validated ranking-decision evidence in the existing private JSON archive under #77.
The private archive trace is deployed; recorded decisions are not semantic acceptance.

[0013](0013-discovery-exploration-state.md) records the accepted first slice: configurable exploration areas,
fair passes with least-recent-offer preference, and finite pending-feed validation
cooldowns in existing discovery metadata under #132. Approval-to-candidate protection
and ordinary recommendation quality remain open; owner approval on 2026-10-06 covered PR #133 and its engine-pin rollout.


[0014](0014-optional-humane-closing-item.md) proposes a disabled-by-default humane
closing designation in the existing primary selection response, strict preparation
v1/v2 compatibility and optional presentation before immutable freeze. Source
activation, translated capacity and semantic acceptance remain open under #127.


[0015](0015-application-workflow-ownership.md) records stage 1 of the structural
migration: explicit prepared application ownership, removal of lower-to-entrypoint
imports, and fail-before-effects preparation/preview validation. Implementation and
rollout are verified through engine PR #149 (`de595797282b7b289561105820a55d467f8379a2`)
and runtime PR #72 under #143; persisted formats and product acceptance remain separate.

[0016](0016-candidate-contracts-and-retirement.md) records the deployed
candidate-ownership slice under #144: pure values/proof validators, storage adapters,
and explicit verified retirement versus persistence without retirement. Engine PR #150
merged at `6c5d7db7a6c0f5520d36f3a89b60fbb8e73fdda6`; runtime PR #73 merged the one-line
pin at `9d529cc84d9c1968feebe25d55da0658767cc3e7`. Existing formats/policies remain;
scheduler ownership, later stages and semantic acceptance are separate.

[0017](0017-confirmed-delivery-application.md) records the implemented #145
confirmed-outcome application, pure coverage projection and strict delivery-state
adapter. Explicit prepared/direct policies preserve accounting, write order and
failure behavior. Successful-repeat counter deduplication does not authorize
automatic reconciliation of interrupted application; release evidence is tracked in #145.
PR #151 and runtime PR #74 verified this scoped rollout on 2026-10-07.

[0018](0018-review-reuse-and-source-attribution.md) records the scoped #146
implementation: shared exact-request review reuse, distinct request validation,
canonical source occurrences with named compatibility types, and general exact-feed
notices separated from optional closing. Immutable main-packet resolution remains an
application boundary; existing hashes, recovery and presentation ordering are preserved.
PR #152 and runtime PR #75 verified the scoped deployment on 2026-10-07;
source activation and editorial acceptance remain separate.

[0019](0019-remaining-application-scenarios.md) records the #147-A execution,
legacy and discovery application boundary with explicit CLI output adapters.
Public entrypoints, guard finalization and the discovery persistence/output/send
barrier remain compatible.

[0020](0020-explicit-model-execution.md) records #147-B separation of provider
settings from explicit lazy model-execution holders, preserved copy/reset and budget
policy, and the deliberate internal helper signature migration. Release evidence
and editorial acceptance remain separately tracked.

[0021](0021-catalog-feedback-boundaries.md) records #147-C proposal/feedback
rules, storage codecs, Telegram protocol and explicit collect/persist/ack ownership.
Exact proposal revalidation, persisted bytes, write order and failure behavior remain
compatible. Its deployed continuations give source quality/trial rules and exploration
policy explicit domain owners, codecs/Telegram transport explicit adapters, and source
scoring/discovery explicit application owners. Telegram presentation/transport is deployed
through engine PR #158/runtime PR #81.
Prepared delivery is deployed through engine PR #159/runtime PR #82; investigation
validation and shared contracts are deployed through engine PR #160/runtime PR #83. These continuations are recorded in ADR0017/ADR0019; source activation and
editorial
acceptance remain separate from these ownership releases.

[0022](0022-versioned-bounded-search-policy.md) records the existing DEV adapter's
Forem V1 repair, unchanged bounded attempt/deadline ceilings, and explicit future-only
search-policy binding. Old attempts and results remain intact; transport reachability
and genuinely useful counter-evidence are separate acceptance facts.

[0023](0023-response-owned-editorial-outcome.md) records #181's response-owned
attempt and single ordinary editorial resolver. Mutable capture outputs and repeated
live slot joins are removed while persisted trust checks, card projection and
historical recovery remain distinct contracts.
