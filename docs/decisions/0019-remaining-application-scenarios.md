# 0019. Give direct execution and discovery explicit application owners

Status: implementation record for the scoped #147-A application migration.
PR #153 merged at `f6204418167ae9c9a82a6b82863c3c5f78173cf2`; runtime PR #76
deployed it at `89baf5a625cbd582617c54651ada6556bfb21e77` on 2026-10-07.
Remaining #147 work and editorial acceptance are separate. The preceding #145
change was verified through [PR #151](https://github.com/Lenivvenil/digest/pull/151)
and runtime [PR #74](https://github.com/Lenivvenil/digest-prod/pull/74).
Provider runtime/configuration separation is the #147-B follow-on recorded in
[ADR0020](0020-explicit-model-execution.md).

Refs [the staged architecture](../ARCHITECTURE.md#stage-5-a-remaining-application-scenarios),
[ADR0007](0007-compact-issue-reservation.md),
[ADR0015](0015-application-workflow-ownership.md),
[ADR0017](0017-confirmed-delivery-application.md) and
[ADR0018](0018-review-reuse-and-source-attribution.md).

## Decision

`application/execution.py` owns validation, prepared/direct scenario selection and
outer execution cleanup. `application/legacy.py` coordinates approved-portfolio
collection, canonical analysis/presentation, and direct publication. Its collection
and publication records are in-memory phase handoffs. Publication visibly orders
Markdown archive → Telegram delivery → confirmed-outcome application → coarse guard
completion. A single typed `emit_preview` callback hands radar/full preview content
to CLI reporting without giving the application a dependency on stdout or the CLI.

`application/discovery.py` owns one session identity and explicit preparation/
reservation and sending operations. `main.discover_sources` orders preparation →
CLI hash output → sending. Strict pending and reservation writes finish before
`GITHUB_OUTPUT` is opened; output failure prevents any send, including local `all`.
Both local phases use the same owner and target. The existing discovery owner still
checks persisted hashes, reservations and bindings, and persists `unknown` before
POST. The managed runtime alone supplies the remote persistence barrier.

`cli/` owns argument parsing, explicit configuration/feed diagnostics, previews and
result/runtime-output reporting. `main.py` retains public compatibility wrappers and
command dispatch through its own `run`, `check_config` and `discover_sources` names.
Lower modules never import `main` or `cli`, including function-local and type-only
imports. Private tests now patch their actual owners rather than preserving reverse
imports. Legacy status HTTP is in the existing Telegram adapter; the application
retains best-effort notice handling and the broader publication-warning boundary
for footer failures.

## Preserved contracts and limits

- Public `run`, `_run`, `check_config`, `discover_sources` and `RunStats` retain their
  signatures/results. Only outer `run` finalizes an unresolved supplied guard on
  early return or error: `reserved` → `not_sent`, `sending` → `unknown`.
- Preparation/preview rejection remains before configuration effects. Other flag
  precedence is unchanged. Radar-only still permits feedback and approved-source
  effects unless combined with dry-run; it is not a promise of read-only execution.
- Cards retain retries, plaintext fallback and optional supplements; compact keeps
  its no-retry transport and coarse guard. Markdown consumption, source accounting,
  attribution and confirmed-application order remain ADR0017's distinct policies.
- Discovery retains three offers/validations, two existing configured routes,
  2,048 output tokens and zero retries per route. Managed `all` fails before discovery
  effects. Durable `prepare` returns zero despite generation failure counters;
  send/all preserve their existing failure mapping and JSON/stdout content.
- Existing localized digest/preview/footer text is preserved. No persisted schema,
  hash, receipt, runtime state, provider route or configuration-runtime behavior is
  changed. No new workflow engine, dependency framework or generic reporting bus is
  introduced.

`LegacyCollection` and `LegacyOutcomePolicy` still carry mutable feedback/scoring
state across the direct run. This slice makes its effect owner explicit, but does
not complete feedback/catalog separation or reduce that existing state coupling.
At the initial #147-A boundary, discovery codecs/approval transport and Telegram
rendering/transport remained combined. [ADR0021](0021-catalog-feedback-boundaries.md)
records the deployed catalog/discovery continuations; [ADR0017](0017-confirmed-delivery-application.md)
records the local delivery continuation. ADR0020 records the follow-on replacement
of `LLMConfig._runtime` copy/sharing with explicit holders.

## Verification and rollback

Existing affected regressions cover previews, approvals, failed collection/analysis,
prepared selection, compact/card differences, confirmed writes and guard cleanup.
Additional offline checks enforce the strict persistence → output → send barrier
at each failure point and reject lower-to-main/CLI imports. No live model, feed or
Telegram calls are used. Local check evidence is not release or editorial acceptance.
Rollback is a compatible reviewed code revert or prior engine pin, retaining all
operational state and receipts without migration, reset or automatic replay.

## Signal validation continuation

The scoped local #147 continuation gives raw signal values and URL validation explicit
owners without changing the deployed #147-A scenario or claiming completion of #147.
`domain/investigation/signals.py` owns the existing six-field `Signal` dataclass and
`domain/investigation/validation.py` owns the unchanged ordered URL deduplication and
blocklist operation. Both import independently of the source registry and HTTP.
`application/signal_validation.py` calls the domain operation before optional bounded
HEAD checks in `adapters/http/signal_liveness.py`. The old sources class export and
validator function exports resolve to the same objects as their new owners; public
signatures and runtime annotation resolution are preserved.

The first URL key, lower-cased with trailing slashes stripped, is consumed before
scheme/host validation and case-insensitive substring filtering. This preserves first
occurrence suppression even when that occurrence is rejected. No additional URL
normalization, scheme restriction, SSRF rule or parse-error recovery is introduced.
Liveness-disabled and empty-result paths perform no HTTP work. Enabled checks preserve
semaphore 10, timeout 5 seconds, no redirects, ordered results, 404/410 drops, retention
of 401/403/429/5xx and other responses, and the existing transport/URL-error boundary.
No search, query, ranking, configuration, source capability or serialized format changes.

Existing source, validator, ranking and investigation regressions run offline. Added
boundary coverage pins cold domain imports, export/class identity and type hints,
first-seen blocklist suppression, malformed URL propagation and bounded ordered HEAD
options/error handling. A synthetic comparison against the frozen pre-extraction tree
checks unchanged operation bodies and behavior without live requests. Local checks
are separate from runtime rollout, search availability and semantic acceptance. Rollback
is the compatible code/engine pin with existing state retained; no migration or replay.

## Shared query, coverage and route contracts

The adjacent local #147 continuation moves the unchanged two-field `SearchQuery`
dataclass to `domain/investigation/queries.py`. Query generation, evidence investigation
and source-registry consumers use that owner; the old query-generator and irritator
exports are the same class object. `domain/investigation/coverage.py` owns the unchanged
RSS and full-source coverage strings, retaining the evidence-stage aliases. The
Telegram sender compares the same full-source vocabulary without importing evidence
orchestration for it. No prompt, serialization or rendering content changes.

`application/review_routes.py` owns the shared `ALLOWED_REVIEW_MODELS` route set.
The trial command keeps `_ALLOWED_MODELS` as an alias to that exact mutable set, while
resume and post-delivery import the public owner. The same three provider/model pairs,
set type, route validation, budgets and request policies remain unchanged.

These pure owners import without orchestration or adapters. The existing eager
`irritator` package and source registry still initialize when a search adapter is
imported; removal of the direct query-value dependency is not cold adapter isolation.
Presentation retains type-only `IrritatorStatus`/`EvidenceIrritatorResult` coupling to
their orchestration owners, not domain I/O. Those broader ownership changes are
outside this extraction. Offline AST, import/alias checks and existing focused
regressions verify the local change; deployment and editorial acceptance remain separate.
