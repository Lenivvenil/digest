# 0025. Own ordinary Irritator fan-out results

Status: approved bounded decision for [#204](https://github.com/Lenivvenil/digest/issues/204),
recorded before implementation on 2026-10-09. Verification and rollout are tracked
in the issue. This continues explicit execution ownership in
[ADR0020](0020-explicit-model-execution.md) and the response-owned result principle
in [ADR0023](0023-response-owned-editorial-outcome.md).

## Context and decision

Ordinary query generation and source search returned their content while mutating
optional caller-owned diagnostic objects. The orchestrator joined those two channels
to derive status. Each operation will instead return one concrete batch containing
its payload and the existing diagnostic record: `QueryBatch(queries_by_narrative,
diagnostics)` and `SearchBatch(signals, diagnostics)`. The operation owns its counters;
there is no optional output argument, compatibility wrapper or exception-carried
diagnostics protocol. These records group related results, without claiming deep
immutability or introducing a general execution framework.

## Compatibility and migration

The helpers are named exports and documented flow steps. Their unchanged names and
import paths do not make this return/keyword change backward compatible. Direct
Python callers must migrate:

1. `digest.irritator.query_generator.generate_queries` (also exported from
   `digest.irritator`) returns `QueryBatch`; read `.queries_by_narrative` for the
   former dictionary and `.diagnostics` for per-attempt counts.
2. `digest.irritator.sources.search_all_sources` (also exported from
   `digest.irritator`) returns `SearchBatch`; read `.signals` for the former list
   and `.diagnostics` for source-attempt counts.
3. Remove `diagnostics=...` from both calls. Import the batch types from the helper's
   defining module when constructing substitutes. The existing `QueryDiagnostics`
   and `SearchDiagnostics` fields remain unchanged.

The exact baseline `141edb6d` inventory found only `run_irritator` as a production
consumer, no script/workflow caller, direct calls in `test_query_generator.py` and
`test_sources_init.py`, and patches in `test_irritator_orchestrator.py`. They migrate
together. This inventory cannot rule out external Python consumers.
`run_irritator` retains its public tuple and non-cancelled status text, levels,
precedence and diagnostic shape. CLI, configuration, persisted evidence, delivery
and supported feedback contracts are unchanged.

## Preserved behavior and cancellation correction

Query prompts, request bytes, provider/model order and limits remain unchanged.
Ordered reduction still replaces duplicate claim keys with the last successful
result, while counts describe every attempt, including successful empty results.
Search keeps query-major/sorted-source order, configured-source multiplicity,
flattened result order, semaphore 10, adapter selection and no added retries.
Successful empty, failed and unavailable attempts remain distinct. Safe logs,
partial results, validation and all-pairs ranking keep their existing behavior.

An individually cancelled query child will explicitly propagate `CancelledError`.
Previously `gather(return_exceptions=True)` returned that non-`Exception` object;
the helper counted it successful and either raised an accidental `TypeError` while
logging the total or let a later duplicate claim overwrite and mask cancellation.
The correction deliberately changes that behavior; cancellation is never success,
empty content, ordinary failure or a synthetic status. Whole-query-task and source
adapter cancellation already propagate and continue to do so.

The caller audit finds an `AsyncClient` context in `application/investigation.py`
and outer issue-guard finalization in `application/execution.py`. Legacy execution
awaits investigation before publication; prepared assembly awaits it before archive,
freeze or clearing accepted preparation. Cancellation keeps those boundaries.
The separate bounded `evidence_stage` deadline/partial-evidence and reading-timeout
mutation protocols remain byte-for-byte unchanged.

## Verification and rollback

Retarget the three existing test owners and add only the focused child-cancellation
regression needed for the deliberate correction. Finite offline traces compare
unaffected request/content/count/log/status behavior and record cancellation
separately; independent review precedes the final aggregate checks.
This establishes structural behavior, not #77's useful counter-evidence, #55's
owner-visible receipt or broader #126/#164 acceptance.

Rollback is a reviewed revert or compatible engine pin, retaining saved evidence.
After full-token vote publication, both sender and poller must retain
[ADR0024's compatible-reader floor](0024-full-article-vote-identity.md); an arbitrary
older engine is not a safe rollback. No state migration, reset or resend is needed.
