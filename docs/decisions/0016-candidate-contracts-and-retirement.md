# 0016. Separate candidate contracts, storage and retirement

Status: scoped #144 implementation merged and deployed on 2026-10-07.
[PR #150](https://github.com/Lenivvenil/digest/pull/150) merged at engine
`6c5d7db7a6c0f5520d36f3a89b60fbb8e73fdda6`; the one-line engine-pin update in runtime
[PR #73](https://github.com/Lenivvenil/digest-prod/pull/73) merged at
`9d529cc84d9c1968feebe25d55da0658767cc3e7`. Release evidence is tracked in #144;
this record does not establish semantic acceptance.

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

The following records the original extraction-stage decision. The dated RSS bridge
retirement below supersedes its import-path promise for four namespaces only.

Compatibility exports preserve moved value identities and existing import paths.
`candidate_storage.py` delegates to the storage adapter; the legacy
`save_candidate_progress(..., retire=...)` API retains its existing dispatch semantics.
Persisted formats, envelope versions, canonical bytes/hashes, article identities,
limits, eligibility, scheduling and retry policies are unchanged. No new framework,
store, retention period or evidence cleanup is introduced.

`candidate_review.py` still coordinates scheduling and eligibility with configuration,
collector and review code. Confirmed-delivery application is the next implemented
slice, recorded in [ADR0017](0017-confirmed-delivery-application.md), with release
evidence tracked separately in #145. Review prompt/model execution, legacy/discovery
scenarios and the other target domains retain their remaining ownership debt.
Pure candidate contracts do not make all existing domain behavior isolated or complete.

## Retire the RSS import bridges — 2026-10-08

[#194](https://github.com/Lenivvenil/digest/issues/194) retires four root modules after
all production and script consumers moved to their canonical owners. These were
migration bridges, not a second implementation of review or candidate policy.
Direct Python imports of the retired paths now fail; no replacement shim is added.
The pre-retirement sources remain at immutable commit `0b939fb6`:

- [`digest.review`](https://github.com/Lenivvenil/digest/blob/0b939fb6b065723eee278069fec546980788e439/digest/review.py)
- [`digest.candidate_dispositions`](https://github.com/Lenivvenil/digest/blob/0b939fb6b065723eee278069fec546980788e439/digest/candidate_dispositions.py)
- [`digest.candidate_storage`](https://github.com/Lenivvenil/digest/blob/0b939fb6b065723eee278069fec546980788e439/digest/candidate_storage.py)
- [`digest.candidate_review`](https://github.com/Lenivvenil/digest/blob/0b939fb6b065723eee278069fec546980788e439/digest/candidate_review.py)

Import from the owner of the operation rather than another aggregate namespace:

| Former bridge content | Canonical owner |
| --- | --- |
| Disposition records, parsing and validation | `domain.editorial.dispositions` |
| Candidate object reads, writes and hashes | `adapters.storage.candidate_objects` |
| Candidate values / pure accounting and replay policy | `domain.editorial.candidates` / `domain.editorial.candidate_policy` |
| Merge, plan, begin, reconcile and handoff | `application.candidate_review` |
| Active-root loading, archive accounting and source references | `adapters.storage.candidate_progress` |
| Persist, index and checkpoint/retire operations | `application.candidate_lifecycle` |
| Review values, strict/live parsing, quote and cache validation | `domain.editorial.reviews` |
| Review request construction / model execution | `application.review_request` / `application.review` |
| Card and review-notice rendering | `presentation.review` |

The legacy `save_candidate_progress` boolean dispatcher is removed. Its default or
`retire=True` call becomes `checkpoint_candidates`, retaining `skipped_empty_reports`;
`retire=False` becomes `persist_candidates`. These existing operations keep their
write/failure order. The incidental private aliases also retire:
`_ordered_unique_articles` becomes `domain.editorial.evidence.ordered_unique_articles`,
`_groq_review_format` becomes `adapters.models.review.groq_review_response_format`, and
`_validated_cached_selections` becomes `domain.editorial.reviews.validated_cached_selections`.
Configuration, catalog, summary and execution values incidentally re-exported by the
bridges should be imported from their defining modules shown in the retained sources.

Canonical classes, their module identities, codecs, persisted bytes and hashes remain
unchanged. CLI, `main.run` and config entrypoints remain supported. The documented
feedback, discovery and source-scoring compatibility interfaces in ADR0021 remain;
this decision does not withdraw them. Existing behavioral tests use actual owners;
retirement does not make their persistence, budget or validation safeguards redundant.

## Verification and rollback

Review actual imports, including function-local imports, and compare persisted bytes
and effect order against the deployed baseline. Existing candidate storage,
freshness, review and preparation regressions must retain interruption, corruption,
accepted-abstention and bounded-continuation coverage. Verification results belong
to the implementing change; this record does not assert that checks have passed.

A reviewed revert restoring the four bridges, or a compatible prior engine pin,
is the rollback boundary. After full-token vote publication, every sender and
feedback collector must retain the ready-v2 and full-token readers required by
[ADR0024's rollback floor](0024-full-article-vote-identity.md#deployment-and-rollback-floor);
an arbitrary pre-retirement pin is not safe. Retain runtime objects, candidate
history and delivery receipts; no format migration or state reset is required.
Editorial quality and ordinary-runtime acceptance remain separate.
