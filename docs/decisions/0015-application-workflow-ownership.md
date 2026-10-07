# 0015. Give prepared workflows an application owner

Status: proposed implementation record for the authorized structural migration.
Stage 1 is tracked in [#143](https://github.com/Lenivvenil/digest/issues/143) under
[#126](https://github.com/Lenivvenil/digest/issues/126). It is local and not deployed
until the implementing PR and runtime pin are verified.

## Context

`main._run` interleaves preparation, experimental source work, preview and immediate
sending. `edition_runtime` imports execution results and private presentation helpers
from `main`, while `main` invokes edition runtime. Function-local imports avoid an
import-time failure but leave orchestration ownership circular.

The owner requested a maintainable modular monolith and an explicit migration plan,
not more services or a generic workflow framework. The target and subsequent stages
are recorded in [Architecture](../ARCHITECTURE.md#structural-migration-current-stage-and-target).

## Decision

Introduce `digest.application` for the existing use cases and shared operations:

- `preparation`: recovery-aware candidate acquisition followed by ordinary edition
  preparation or a separate experimental technical terminal path.
- `analysis`: canonical RSS selection/category analysis used by both prepared and
  legacy applications.
- `presentation`: optional presentation orchestration; canonical source data remain
  unchanged and concrete renderers stay in their existing adapters for this stage.
- `investigation`: the existing synchronous external-investigation invocation;
  search is not owned by presentation.
- `run_state`: feedback collection, approved source application and fetch accounting.
- `results`: the existing `RunStats` value with unchanged fields.

The entrypoint selects a prepared or legacy scenario once. Lower modules do not import
`main`; edition runtime consumes the shared presentation and result owners directly.
The ordinary preparation coordinator retains the visible order: recover/select,
accept canonical work, record candidate handoff/accounting, present/archive/freeze.
The sender and receipt implementation are not redesigned.

`SelectedPreparation` is a transitional application handoff, not a domain entity or
persisted format. It carries the existing optional candidate state and distinguishes
canonical configuration from the collection portfolio. Stage 2 will establish pure
candidate contracts below selection/storage; this record does not ratify the temporary
handoff as the final domain model.

## Compatibility and deliberate restriction

All persisted envelopes, hashes, source/feedback identities, request routes, limits
and deadlines remain unchanged. Source preparation still honors previously accepted
ordinary work before fresh source work; its distinct terminal path is not complete
experimental isolation. Disabled source hooks remain in shared acquisition where needed
for parity. Legacy/discovery scenarios remain in `main` for a later migration stage.

The Python `run()` API now rejects `prepare_only` combined with `dry_run` or
`radar_only` before configuration or preparation effects. The CLI already rejects those combinations. Previously
the Python path could partially execute them; carrying preview flags into a mutating
prepared application would be unsafe. The signature remains compatible, but these
unsupported combinations explicitly fail instead of silently changing their effects.
The existing `run()` finally block still finalizes an explicitly supplied legacy
issue guard; this migration does not suppress that cleanup contract.

## Verification and rollback

Compare the moved operation bodies and effect ordering, scan actual imports including
function-local and type-only imports, and run existing preparation, closing, budget,
legacy and delivery recovery regressions. The three incompatible preview/preparation
combinations fail before configuration/preparation work. Model/source calls remain mocked.

A successful CI run is necessary but does not establish architectural completeness or
semantic quality. The implementing PR records remaining cycles and ownership debt.
Rollback is a reviewed code revert or the previous compatible engine pin; no runtime
state reset, receipt deletion or historical reclassification is required.
