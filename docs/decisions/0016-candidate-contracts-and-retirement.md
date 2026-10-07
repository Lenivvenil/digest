# 0016. Separate candidate contracts, storage and retirement

Status: implementation record for the scoped #144 structural migration.
Merge and runtime rollout evidence is tracked in #144; this record does not establish
semantic acceptance.

Refs [#144](https://github.com/Lenivvenil/digest/issues/144),
[the staged architecture](../ARCHITECTURE.md#structural-migration-current-stage-and-target),
[ADR0008](0008-candidate-selection-progress.md) and
[the deployed stage-1 boundary](0015-application-workflow-ownership.md).

## Context

Candidate values, review validation, scheduling and checkpoint I/O shared modules.
Storage depended on selection/preparation code and constructed temporary aggregates
to validate individual retained objects. The general save operation also selected
and retired active work, making its lifecycle effects difficult to see at callers.

The existing requirements remain: exact source and decision evidence, recoverable
unfinished work, and verified retained objects before removing active records.
This slice changes their owners, not the accepted accounting or scheduling policy.

## Boundary and implementation

- `domain/catalog/articles.py` owns `Article` and its existing title/link identity.
  `domain/editorial/` owns review/evidence, disposition and candidate values and
  pure validators. Occurrences, packets, canonical evidence/reports and candidate
  decisions are validated directly against their actual proof objects.
- `_serialization.py` contains stdlib-only JSON encoding/restoration. Domain code
  imports no HTTP client, filesystem adapter or application coordinator.
- `adapters/storage/candidate_objects.py` owns object envelopes, hashes, source and
  packet references, retained indexes and verified writes.
  `candidate_progress.py` owns the active-checkpoint codec and archive references;
  `checkpoints.py` owns filesystem path guards. These adapters do not decide which
  work retires.
- `application/candidate_lifecycle.py` coordinates the effect. `checkpoint_candidates`
  materializes source references, writes and verifies retirement proofs/indexes,
  removes retired in-memory work, then writes the active checkpoint.
  `persist_candidates` materializes and writes without retiring candidates or
  proof packets. Reconciliation uses that persistence boundary before freezing
  report accounting; preparation handoff explicitly checkpoints with retirement.

Retirement is removal from the active working set, not deletion of retained evidence.
Existing proof checks preserve unconsumed genuine-abstention reports and recoverable
unsent selections. Policy-excluded work remains separately recoverable; exclusion
does not become an editorial rejection. If an interruption leaves both old active
state and an index, active state retains precedence. The policy acknowledgement
follows the active checkpoint write. Multiple files are not an atomic transaction;
remote persistence remains the runtime's responsibility.

## Compatibility and remaining work

Compatibility exports preserve moved value identities and existing import paths.
`candidate_storage.py` delegates to the storage adapter; the legacy
`save_candidate_progress(..., retire=...)` API retains its existing dispatch semantics.
Persisted formats, envelope versions, canonical bytes/hashes, article identities,
limits, eligibility, scheduling and retry policies are unchanged. No new framework,
store, retention period or evidence cleanup is introduced.

`candidate_review.py` still coordinates scheduling and eligibility with configuration,
collector and review code. Review prompt/model execution, legacy/discovery scenarios,
confirmed-delivery application and the other target domains remain later work.
Pure candidate contracts do not make all existing domain behavior isolated or complete.

## Verification and rollback

Review actual imports, including function-local imports, and compare persisted bytes
and effect order against the deployed baseline. Existing candidate storage,
freshness, review and preparation regressions must retain interruption, corruption,
accepted-abstention and bounded-continuation coverage. Verification results belong
to the implementing change; this record does not assert that checks have passed.

A compatible code revert or prior engine pin is the rollback boundary. Retain runtime
objects, candidate history and delivery receipts; no format migration or state reset
is required. Editorial quality and ordinary-runtime acceptance remain separate.
