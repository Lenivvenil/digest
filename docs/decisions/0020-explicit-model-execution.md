# 0020. Separate model execution ownership from configuration

Status: implementation record for the scoped #147-B migration on 2026-10-07.
Primary-review ownership amended on 2026-10-09 by #215, as recorded below.
Merge and runtime rollout evidence are tracked in #147; editorial acceptance is separate.

Refs [the staged architecture](../ARCHITECTURE.md#stage-5-b-explicit-model-execution),
[ADR0019](0019-remaining-application-scenarios.md),
[ADR0005](0005-optional-presentation-translation.md) and
[ADR0009](0009-selected-source-admission.md).

## Context and decision

`LLMConfig._runtime` mixed provider settings with mutable loop-bound execution.
Dataclass replacement dropped that field while shallow copies retained it, making
request-sharing policy an implicit consequence of the copying operation.

`adapters/models/execution.py` now owns `ModelExecution` and `RequestState`.
The holder is concrete and lazy: construction reads neither the event loop nor
the durable budget/environment. `request_state(settings)` initializes one loop's
semaphore, spacing lock, next/last request times, provider cooldowns, local limit
and attempt count. A loop change replaces only that holder's state slot.
`share_initialized(settings)` initializes the caller and creates a separate
holder referencing its current state. It does not couple their future slot resets.

`Config` and `LLMConfig` contain settings only. No registry keyed by configuration,
context variable, global current execution or optional fresh-execution fallback
substitutes for explicit ownership. `llm.py` continues to own wire protocols,
provider routing, pacing, retry and reservation behavior; this is not a wholesale
provider adapter rewrite or a new dependency-injection framework.

## Entrypoints and copy policy

Applications create an execution once and pass it through model-consuming work.
The following matrix records execution ownership. The #215 amendment supersedes
the original fresh-state branch for primary review with reading disabled;
the remaining fresh/shared policies are preserved:

| Operation | Holder and state policy |
| --- | --- |
| Main prepared/direct run | `application.execution._run` creates a lazy holder after `load_config`; preparation and legacy handoffs explicitly carry it. Public `main.run` and `_run` signatures stay unchanged. |
| Config-only replacement | Pass the identical holder, including source-portfolio and presentation-language variants. |
| Nested LLM replacement | Select a fresh holder unless the specific sharing exception below applies. There is no automatic rule inferred from object identity. |
| Primary review, with or without reading | Initialize caller settings, then give review a separate holder sharing that state. Primary and possible secondary use that same derived holder, with zero-retry settings. |
| Translation | Initialize caller before shallow-copying its LLM settings; give translation a separate sharing holder and keep its zero-retry policy. |
| Reading and reconciliation operations | Call `request_budget_remaining` first, then derive a separate sharing holder and shallow settings copy. Existing route-specific pacing remains dynamic. |
| RSS evidence, translation disabled and no full-source evidence | Fresh holder; initialize from the bounded settings (concurrency 1, interval 65 seconds). |
| Evidence with translation enabled or full-source evidence | Initialize caller settings, then derive a sharing holder. The caller's already-sized semaphore remains authoritative even though bounded settings say concurrency 1. |
| Discovery generation | Fresh holder for the single bounded generation and its existing two-route fallback chain. |
| Review trial/resume, post-delivery and reconciliation CLIs | One fresh lazy holder for each invocation, explicitly passed to the internal operation. Settings load/route restriction precede first state access. |
| Approved-source reload | `apply_pending_approvals` returns both settings and execution. No decisions or disabled application returns both original objects. Any actual `load_config` call returns a fresh holder, even if settings do not change. |

Other initialization timing stays at the existing points. Accepted preparation and
cached review recovery do not acquire a model allowance merely because an owner
exists. Semaphore capacity is sampled on initialization and is not resized after
settings mutation. Same-holder aliases observe the same loop replacement; separate
holders that shared a state replace their own slots independently on a later loop.

### Primary review pacing correction — 2026-10-09

[#215](https://github.com/Lenivvenil/digest/issues/215) replaces the original
reading-disabled fresh execution with `execution.share_initialized(config.llm)`.
Review and later translation now share the caller's same-loop semaphore, spacing
lock, next/last request timestamps, provider-plus-model cooldowns, local request
cap and spent-attempt count. This intentionally changes admission: an existing
caller cap or cooldown may reduce the attempts review can make. It is a correction,
not behavior parity with the old branch. The settings clone keeps `max_retries=0`,
and primary/fallback retain one derived holder without adding a model allowance.

Sharing remains local to one application execution and event loop. Independent
discovery, review-resume and post-delivery invocations keep their own ownership;
shared holders still rebind independently on later loops. Accepted/cache replay
and durable reservation ordering do not change. Local sharing neither enables
an unconfigured durable ledger nor establishes cross-process pacing.

The offline real-owner regression runs primary review followed by translation,
with reading disabled and durable-budget environment absent. A review starting
at synthetic t=100 and finishing at 107.5 previously allowed immediate translation
at 107.5; a configured 20-second interval now requires one 12.5-second wait until
t=120. Both successful runs dispatch exactly two requests, with each request's
payload unchanged by the correction.
This proves the configured pacing boundary, not the cause of an observed HTTP 429
or live provider, translation-fidelity or editorial acceptance.

## Accounting and deadlines

Counting and generation use the same explicit state's semaphore, spacing lock,
provider cooldowns and local cap. The ordering stays: pace, recheck any queued
provider cooldown, check the local cap, reserve through durable `model_budget`,
increment the local attempted count, then inspect credentials and dispatch.
There is no refund or reset after failures, missing credentials or fallback;
each physical retry/fallback/count request reserves its own attempt.

`model_budget.py` remains a separate durable cycle journal and environment-bound
reservation owner. Holder construction does not inspect it or begin a stage.
The remaining-budget helper still combines local and durable allowances and
returns zero on a malformed shared budget while dispatch raises the precise error.
The reconciliation checkpoint retains its explicit bridge: restore the local cap
as already-spent attempts plus saved remaining allowance, and restore the saved
absolute pacing floor after the existing verified checkpoint barrier.

Absolute operation deadlines, pacing from prior durable reservations, stricter
stage intervals, queued cooldown rechecks and generic timeout retries are
unchanged. Uncertain source-generation holds remain separate from generic retry
handling. No provider route, prompt, source setting, persisted schema, journal,
remote checkpoint or source-generation policy changes.

## Internal API compatibility and verification

This is an approved internal Python API break: model-consuming operations require
keyword-only `execution: ModelExecution`. `complete` and `count_gemini_tokens`
require `execution=...`; budget/wait/state helpers take `(config, execution)` and
`set_request_limit` takes `(config, execution, limit)`. All production, script and
test callers migrate explicitly. `apply_pending_approvals` now returns
`tuple[Config, ModelExecution]`; internal selection/collection handoffs carry both.
The removed private `_runtime` field no longer appears in dataclass introspection
or `asdict(LLMConfig)`. It was not a supported persisted configuration schema.
Public CLI/run entrypoints and their results remain compatible.

Existing concurrency, spacing, counting/fallback, primary review, translation,
reading, cycle-budget and reconciliation regressions remain the behavior evidence.
Small ownership checks cover lazy construction, settings serialization, fresh and
shared state, sampled semaphore capacity and independent cross-loop rebinding.
Local checks establish structural behavior only, not real provider or editorial
acceptance. Rollback uses a reviewed code revert or prior engine pin while retaining
all runtime evidence and budgets; no state migration or reset is required.
