# 0015. Give prepared workflows an application owner

Status: stage-1 implementation merged and deployed on 2026-10-07.
[PR #149](https://github.com/Lenivvenil/digest/pull/149) merged at engine
`de595797282b7b289561105820a55d467f8379a2`; runtime PR #72 deployed that pin.
Stage 1 is tracked in [#143](https://github.com/Lenivvenil/digest/issues/143) under
[#126](https://github.com/Lenivvenil/digest/issues/126). This status does not establish
semantic acceptance or completion of later structural stages.

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

The stage-2 implementation is recorded in
[ADR0016](0016-candidate-contracts-and-retirement.md); its release evidence in #144 is
separate from this deployed stage-1 boundary.

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

## Target ownership continuation — 2026-10-09

Accepted direction under [#164](https://github.com/Lenivvenil/digest/issues/164);
implementation and runtime acceptance remain tracked separately. The canonical
[target architecture and staged migration](../ARCHITECTURE.md#target-architecture-and-migration)
own the editable diagram, boundaries and exit criteria.

Continue the existing modular monolith rather than introduce services or a generic
workflow framework. Prepare owns the verified accepted editorial result; publication
owns the prepared receipt-to-application lifecycle. Category/no-news policy remains
distinct, and legacy/direct/optional scenarios keep their current guarantees until
explicitly migrated. Shared trust-boundary repairs precede deeper ownership changes.

This decision does not itself change code, persisted formats, command contracts,
transport behavior or accounting recovery. Strict prepared accounting
preflight is implemented by the scoped ADR0017 slice-1 amendment. Verified category
acceptance is implemented by the slice-2 continuation below; receipt-owned publication
remains later work.
Each implementing change must retire its former authority, preserve wire/rollback
compatibility and document any intentional behavior change. Reviewed release evidence
and ordinary product acceptance are separate requirements.


## Verified category acceptance — #220 slice 2

Candidate and category admission retain distinct policies. Candidate acceptance still
binds its resolved review and required disposition evidence; category acceptance keeps
its historical report order, selected-card and empty-work rules. Both successful
paths now use `preparation.persist_accepted_preparation`, which saves through the
unchanged codec, reloads, compares saved path/full snapshot and returns the restored
`AcceptedPreparation`. Failed readback retains evidence and blocks the handoff.

`edition_runtime.present_preparation` reloads the requested day from `.cache` before
any empty return or presentation effect. It compares the full path/body-hash/snapshot
reference and uses the freshly restored snapshot. The hash remains the canonical
stored body digest, not a hash of formatted file bytes. This is local verification,
not remote persistence, deep immutability, a concurrent-writer lock or power-loss
transaction. Category statistics/map effects occur between the two verification reads;
a later entry failure preserves that already-written prefix.

Internal API retirement is deliberate: `finish_preparation` and `_present_snapshot`
no longer provide a raw-snapshot archive/freeze route. `existing_preparation` is removed
in favor of `inspect_preparation` plus `preparation_stats`; `resume_preparation` is
removed in favor of `recover_preparation` plus accepted presentation. The supported
CLI/run API, existing preparation/ready/claim/receipt formats and lower-level freeze
primitive remain unchanged. `load_preparation` remains the useful codec projection.

Category prose, trends, optional perspectives, no-report mode, full-source optionality,
closing/source-credit decisions and direct/discovery scenarios retain their contracts.
The [current preparation flow](../ARCHITECTURE.md#preparing-an-edition) records exact
effects and failure limits. Receipt dispatch/application ownership and its three
remaining retirements belong to slice 3. Review, exact-head checks, rollout and natural
product acceptance are separately recorded in [#220](https://github.com/Lenivvenil/digest/issues/220).
Rollback uses a compatible reviewed code/pin while retaining canonical work and receipts.
