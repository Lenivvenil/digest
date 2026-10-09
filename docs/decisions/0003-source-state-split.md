# 0003. Split Source state: static config vs runtime cache

* Status: proposed
* Date: 2026-04-26
* Deciders: venil
* Tags: architecture, data-model, config, source-management

Historical ADR translated from the [original Russian record](https://github.com/Lenivvenil/digest/blob/0b939fb6b065723eee278069fec546980788e439/docs/decisions/0003-source-state-split.md). Status, claims and references are preserved as recorded.

## Context and Problem Statement

Issue #37 describes the problem as "dual-storage Source state: `user_priority` in `config.yaml` versus `system_priority` in `.cache/dynamic_sources.json`". **This description does not match the code.** There is no `dynamic_sources.json` file in the repository and no `system_priority` field on `SourceConfig` (`digest/config.py:80–89`). The actual invariant violation is in `digest/source_scorer.py:389–465`: `apply_trial_decisions()` mutates `config.yaml` at runtime by manipulating YAML lines with regex, setting `trial: false` for graduated sources, `enabled: false` for demoted sources, and `trial_started: <date>` for new trials. The domain overview (`docs/domain/digest/overview.md` Hot Spot #1) also refers to the nonexistent `dynamic_sources.json`; both artifacts describe the symptom inaccurately. This ADR is based on the code's state, rather than the wording of the issue or overview.

Symptom: `config.yaml` serves both as a human-edited declaration of intent **and** as a store for mutable runtime state written by GitHub Actions. This violates the explicitly expected invariant "config.yaml is read-only while the engine runs" (inherited from ADR-0002, where config.yaml physically lives in the private `digest-prod` and is edited by a human). The current implementation, regex-based YAML mutation with `.bak`/`.tmp` files, is also fragile: manual YAML formatting changes (such as adding comments) can cause source-block parsing to miss its target.

A decision is needed **now**, before the next pipeline run with an active trial source; otherwise, any merge of config changes in `digest-prod` risks a conflict with automatic writes.

## Decision Drivers

* **The config.yaml invariant** — after ADR-0002, `config.yaml` is physically separated into `digest-prod`. If the engine keeps writing to it with regex, the "human-edited declaration" invariant is structurally violated, and any manual diff in `digest-prod` can conflict with automatic writes on the next cron run.
* **Principle 4 (Knowledge in tools, not in memory)** (`docs/principles.md`) — everything significant belongs in git, not in implicit side effects. The fact that the engine writes to config is currently documented nowhere outside the code itself.
* **Fragile regex parsing of YAML** — `_find_source_block` and `_set_field_in_block` in `source_scorer.py` rely on indentation and line structure. Any unusual formatting (comments inside a block, YAML anchors/aliases, multiline values) breaks them silently. A cache file with an explicit JSON schema does not have this risk.
* **Domain language: Trial as a value object on Source** (`docs/domain/digest/overview.md`, Yellow): trial_state is part of the Source aggregate. Mutable trial state logically belongs alongside other mutable state (stats, feedback), not in declarative config.
* **Engine portability per ADR-0002** — the engine must not assume `config_path` is writable. The cache directory is already writable by contract. The engine should have exactly one explicit dependency on writable storage: cache.
* **Testability** — the round-trip property test in acceptance criteria #37 is impossible while state is mutated by regex in the physical file: the test requires two files (config + cache), but there is currently one file with regex-based reads and writes.

## Considered Options

* **Option A (literal, as framed in issue #37)** — create `.cache/source_state.json` with **only** a `trial_started` field per source. The `trial: bool` and `enabled: bool` fields stay in `config.yaml` as human declarations. `apply_trial_decisions()` writes `trial_started` to cache instead of config; `trial=true → trial=false` (graduate) and `enabled=true → enabled=false` (demote) **continue to be written to config.yaml**, or are not written at all.
* **Option A' (completed)** — like A, but **all three** mutations (`trial_started`, graduate outcome, demote outcome) go to cache. `config.yaml` becomes strictly read-only at runtime. The `trial: bool` and `enabled: bool` fields in config are declarations, over which cache applies overrides (`graduated`, `demoted`).
* **Option B (full state split)** — create `.cache/source_state.json` with the full mutable state per source: `{trial_started, graduated, demoted, effective_enabled_override, schema_version}`. `config.yaml` is truly read-only. The loader in `config.py` builds `SourceConfig` from config and applies cache overrides in a separate merge phase. `apply_trial_decisions()` is renamed to `apply_trial_decisions_to_cache()` and writes the cache file atomically via `atomic_json_write` (as in `feedback.py`).
* **Option C (config write only for trial_started)** — remove the `apply_trial_decisions()` write path for graduate/demote: graduated/demoted move to cache as outcomes. `trial_started` stays in `config.yaml`, but its write path is removed; a human must manually stamp the date when declaring a new trial source. The engine never writes to `config.yaml`.

## Decision Outcome

Chosen option: **Option B — Full state split**.

Only B and A' fully meet the "config.yaml read-only invariant" driver. A (literal) leaves two of the three config write paths, without resolving the problem. C resolves the write path but changes the user experience: a human must remember the date when declaring a trial, creating a new silent failure mode (forgotten date → trial never evaluated).

Between B and A', B was chosen for two critical drivers:
1. **Testability** — an explicit schema with `schema_version` enables migration tests and the round-trip property test in acceptance criterion #37; A' has no version field and no protection from schema drift.
2. **Domain language** (`docs/principles.md`, Principle 4) — Trial as a value object is serialized in full within one structure (`SourceStateStore`), rather than as two fields in config and one in cache. Split state needs explaining when debugging every incident.

B follows the `FeedbackStore` pattern (`digest/feedback.py`): the same `dataclass + atomic_json_write + graceful degradation` style. Consistency with the already working cache mechanism reduces the cognitive load of implementation.

Acknowledged costs: migration of existing `trial_started` values, config + cache merge semantics (see Bootstrap and Migration), and a fourth cache file.

### Reversibility

The decision is **fully reversible** until `source_state.json` has accumulated >3 months of graduated/demoted outcome history in `digest-prod`. Before that threshold, rollback takes one script to move `trial_started` from cache back into config.yaml, deletion of `source_state.json` from `.cache/`, and restoring `apply_trial_decisions()` to regex-based config mutation. Estimate: 1–2 hours. After 6 months of accumulated outcomes, rollback is more expensive: graduated/demoted decisions must be manually moved back into config fields, or the history loss accepted. Estimate: 4–8 hours, depending on the number of sources.

It is **not reversible** without data loss if `source_state.json` contains outcomes absent from config (for example, a source was demoted and removed from config, leaving only cache to explain why). This is the boundary: forward migration is cheaper than rollback.

References to principles (`docs/principles.md`):
* Principle 4 (Knowledge in tools) — the cache schema must be documented, not merely inferable from code.
* The "what architecturally significant means" section → "the data model changes" item: this is precisely the subject of this ADR.

<a id="positive-consequences-для-option-b-как-ведущего-варианта"></a>

### Positive Consequences (for Option B as the leading option)

* The "config.yaml is read-only at runtime" invariant becomes structural rather than decorative: the engine physically never opens config for writing.
* A round-trip property test becomes possible: load config + load cache → mutate state → save cache → reload → equality (acceptance criterion #37).
* Fragile regex parsing of YAML (`_find_source_block`, `_set_field_in_block`, `_remove_field_in_block`, ~80 lines) is removed and replaced with JSON serialization through a `dataclass`.
* Migration for new runtime-state fields becomes inexpensive (add a dataclass field + update graceful degradation in the loader), without changing the config parser.
* The engine is fully portable: the only writable path is cache_dir, and the only read path for config.yaml is `load_config()`. This meets the ADR-0002 contract.

<a id="negative-consequences-для-option-b"></a>

### Negative Consequences (for Option B)

* **Migration cost**: existing `trial_started` values in `digest-prod/config.yaml` must move to cache. Either a one-shot import on the first run (risk of two sources of truth until cleanup), or manual cleanup (risk of forgetting). Without an explicit migration step, state is temporarily duplicated.
* **Cognitive load**: "effective Source state = config + cache merge" means two places to debug instead of one. Investigating why source X is disabled requires checking both config and cache. Everything is currently in one file (fragile, but one).
* **Schema evolution discipline**: the cache file is committed to `digest-prod` through GitHub Actions (like `feedback.json`, `source_stats.json`), so breaking schema changes require a migration script or an explicit `schema_version` field. Config changes currently require only a YAML edit.
* **`enabled: false` override in cache vs config**: creates ambiguity when a human wants to enable a source but cache still overrides it as demoted. An explicit rule is needed: human config edit → invalidates cache override (how? timestamp comparison? manual flag?).
* **Incompatibility with the existing cache format**: the new `source_state.json` is the fourth cache file (after `feedback.json`, `source_stats.json`, and `articles_seen.json` for deduplication). Each has a separate dataclass with graceful degradation; the total cache I/O surface grows.
* **Acceptance criterion #37, "`config.yaml` schema documented"**: requires writing and maintaining a README or CLAUDE.md section with an explicit list of "static fields / forbidden runtime fields". This document can drift from the code unless backed by a check.
* **Bootstrap edge cases**: on the first run without `source_state.json` (after migration to `digest-prod`), the loader must correctly initialize an empty store. If cache is corrupted, start gracefully with a warning (as `feedback.py` already does). This code needs testing.

### Bootstrap and Migration (mandatory for any chosen option)

These questions must be resolved in `/plan` before work starts (contract, not implementation):

1. **First run without the cache file**: the loader returns an empty `SourceStateStore`; config.yaml is the only source for `trial_started`. The first mutating call to the new write function creates the cache file.
2. **Migration of existing `trial_started` values from `digest-prod/config.yaml`**: a one-shot migration helper is proposed (form TBD in `/plan`: CLI flag, separate entry point or script). It reads config, moves `trial_started` values to cache and leaves only the `trial: true` declaration in config. After one successful run, the operator manually removes `trial_started` from `digest-prod/config.yaml`. **Do not automate removal**: that would write to config, precisely the invariant violation being eliminated.
3. **Schema versioning**: `source_state.json` starts with `{"schema_version": 1, "sources": {...}}`. The loader checks `schema_version`; on an unknown version, warn and start fresh (as `feedback.py` handles corrupted JSON).
4. **Conflict resolution, config vs cache**: on conflict (`enabled: false` in config + `demoted: true` in cache), config wins for declarations and cache wins for outcomes. The specific merge algorithm is set in `/plan`.

## Pros and Cons of the Options

### Option A (literal)

* Good, because the diff is minimal: only one new field migrates to cache.
* Good, because no loader merge logic is needed; config remains a flat source for declarations + outcomes.
* **Bad, because it does not address the main driver**: two of the three config write paths (graduate → `trial: false`, demote → `enabled: false`) remain. Issue #37 is not resolved; the invariant is violated in 2/3 cases.
* Bad, because the code does not explain why one runtime write migrates and the other two do not; the asymmetry needs justification in comments.
* Bad, because the fragile regex parser `_find_source_block` cannot be removed; it is still needed for the two remaining write paths.

### Option A' (completed)

* Good, because it removes all three config write paths, fully restoring the invariant.
* Good, because the cache schema is minimal: `{trial_started, graduated, demoted}` per source, without extra fields.
* Good, because migration is simpler than B: one field (`trial_started`) moves, and graduated/demoted are created in cache on the next run.
* Bad, because logic is spread between config (`trial: bool`, `enabled: bool` declarations) and cache (`graduated`, `demoted` outcomes): two places to debug why a source is disabled.
* Bad, because conflict semantics are nontrivial: what does `trial: false` in config + `graduated: false` in cache mean? (Never a trial, or previously a trial that graduated but whose cache was lost?) An explicit rule and test are required.
* Bad, because without a `schema_version` field, migration for the next schema change requires ad-hoc detection.

### Option B (full state split)

See Positive/Negative Consequences above: this is the leading option.

* Good, because it follows the `FeedbackStore` pattern: the code structure is familiar and tests are trivial.
* Good, because Trial as a value object is serialized in full within one structure, matching the domain language.
* Good, because adding new mutable fields (such as `last_evaluated_at`, `evaluation_count`) requires no changes to the config parser.
* Bad, because the cache surface grows: a fourth JSON file with its own graceful-degradation logic.
* Bad, because config + cache merge semantics need explicit design (see positive/negative consequences above).
* Bad, because migration requires a one-shot helper or manual cleanup of existing `trial_started` values in `digest-prod/config.yaml`.

### Option C (config write only for trial_started, with manual stamp)

* Good, because it completely removes the config write path: the strictest read-only invariant.
* Good, because the cache schema is minimal: only `graduated` / `demoted` outcomes; `trial_started` remains a declaration in config.
* Bad, because it **changes the user experience**: a human must manually stamp `trial_started` when declaring a new source. The current behavior, "trial source added without a date → pipeline stamps it", disappears. This is a new failure mode: forgotten date → trial never evaluated, leaving the source in permanent trial. Without a reminder (issue, lint rule), the error is silent.
* Bad, because `trial_started` is conceptually mutable (set once, but still a mutation from unset → set), and storing it in declarative config violates domain language: trial_state is a value object, and a value object with partial state in two places is a code smell.
* Bad, because migration seems simple ("leave it as-is"), but actually shifts responsibility for date stamping to the operator without a compensating control.
* Bad, because although the regex parser is removed (the write path disappears), declarative `trial_started` in config encourages repeating the "human stamps date → engine reads it" pattern for future value-object fields. A regression in domain language becomes a local convention.

## Confirmation

1. **Round-trip property test** (acceptance criterion #37): `tests/test_source_state_roundtrip.py` loads config + cache, mutates a runtime field (such as `trial_started`), saves cache, reloads and compares. Passes in CI after merge.
2. **Engine config write surface = 0**: `grep -rn "config_path.*open.*['\"]w['\"]" digest/` returns 0 results after merge. This check can be automated with a custom ruff rule or pre-commit grep.
3. **Schema document existence**: `README.md` or the `digest/config.py` docstring contains an explicit list of "runtime-only fields, forbidden in config.yaml". The reviewer checks this on the PR.
4. **Bootstrap correctness**: an integration test with an empty `cache_dir` (no `source_state.json`) completes the pipeline without errors; on the second run, the cache file has been created.
5. **Migration helper test**: the one-shot migration helper (form TBD in `/plan`), given a fixture with `trial_started` in config, outputs a migration plan in dry-run mode and writes cache in normal mode, **leaving config untouched** in both cases.

## Re-visit Trigger

1. **Schema breakage**: at the first schema change (a new field in `source_state.json`), graceful degradation in the loader fails and the pipeline crashes on cron. If repeated, a formal migration framework is needed instead of graceful degradation.
2. **Cache corruption frequency**: > 2 cases of corrupted `source_state.json` in `digest-prod` per quarter (for example, a write interrupted by a GHA timeout). The current `atomic_json_write` should cover this; if it does not, the pattern is not working.
3. **Source aggregate growth**: the number of sources in `config.yaml` exceeds 50, OR a second list-typed field (such as `evaluation_history`) is added to `source_state.json`. At either threshold, the JSON file becomes unwieldy: O(N) lookup, unreadable diffs and expensive atomic writes. Reconsider then: either a separate `digest/source/` module, or move Source state to SQLite.
4. **Multi-instance setup**: if a second engine instance ever runs in parallel (racing on cache), a JSON file with atomic_json_write becomes insufficient; a lock or separate backend (SQLite WAL, Redis) is needed. With the current topology (one cron, one machine), this is not a trigger.
5. **Fundamental change to ADR-0002**: if the engine/instance split is rolled back (config.yaml returns to the engine repository), this ADR loses one of its drivers and needs revisiting.

## Links

* Related issue: #37 (Engine: separate static config from source runtime state)
* Previous: ADR-0002 — Split engine and instance repositories (defined the invariant that this ADR makes structural)
* Domain context: `docs/domain/digest/overview.md` Hot Spot #1 (wording needs updating: it describes the nonexistent `dynamic_sources.json`; a separate issue for domain-overview cleanup is recommended)
* Principles: `docs/principles.md` — Principle 4 (Knowledge in tools), the "what architecturally significant means" section ("the data model changes" item)
* Code references:
  * `digest/source_scorer.py:389–465` — `apply_trial_decisions()` (config.yaml write path)
  * `digest/source_scorer.py:324–386` — regex helpers `_find_source_block`, `_set_field_in_block`, `_remove_field_in_block` (removed in B and A')
  * `digest/config.py:80–89` — `SourceConfig` dataclass (mutable fields to split)
  * `digest/feedback.py` — reference pattern for the cache store (FeedbackStore + atomic_json_write + graceful degradation)
