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
Discovery codecs and approval transport remain in `discovery.py`; Telegram still
combines rendering and transport. Those debts remain separate work; ADR0020 records
the follow-on replacement of `LLMConfig._runtime` copy/sharing with explicit holders.

## Verification and rollback

Existing affected regressions cover previews, approvals, failed collection/analysis,
prepared selection, compact/card differences, confirmed writes and guard cleanup.
Additional offline checks enforce the strict persistence → output → send barrier
at each failure point and reject lower-to-main/CLI imports. No live model, feed or
Telegram calls are used. Local check evidence is not release or editorial acceptance.
Rollback is a compatible reviewed code revert or prior engine pin, retaining all
operational state and receipts without migration, reset or automatic replay.
