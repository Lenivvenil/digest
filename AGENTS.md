# Repository working guide — Daily News Digest

Start with [README](README.md) for the product and supported entrypoints,
[Contributing](.github/CONTRIBUTING.md) for the change workflow,
[Architecture](docs/ARCHITECTURE.md) for current owners and scenarios, and
[State and effects](docs/STATE_AND_EFFECTS.md) for exact persistence, request and
transport contracts. Architectural decisions and compatibility migrations belong
in the [existing ADRs](docs/decisions/README.md).

## Engine and runtime

This is the engine repository. Production configuration, credentials, schedules,
`.cache/` and `digests/` belong in the private `Lenivvenil/digest-prod` runtime.
Use synthetic evidence in public tests, examples, issues and PRs; never publish
private source bodies, model outputs or runtime artifacts. Runtime installation
uses a reviewed immutable engine commit; see [upgrades and retained state](docs/OPERATIONS.md#upgrades-and-retained-state).

## Working agreement

Tracked discipline: [#148](https://github.com/Lenivvenil/digest/issues/148).

- Before implementation, use an existing issue or create a scoped one with the
  problem, acceptance, affected contracts and current status. Check its placement
  and status on the existing project board; do not invent a replacement board.
- Keep one reviewable scope per change. Link the implementing issue and applicable
  ADR/domain evidence in the PR. Use an automatic closing reference only when the
  issue's full acceptance is satisfied; partial progress must leave remaining work open.
  Use `Refs #…` for incomplete work; closing keywords still trigger GitHub automation
  inside negated sentences, so do not pair them with an issue reference.
- After merge, verify the exact main checks and, when applicable, the deployed engine
  pin and retained runtime state. Update the issue and board from observed results.
  Do not equate a green check with editorial or real-runtime acceptance.
- Reconcile merged branches only after checking current tips, main ancestry and
  outstanding PR/dependent work. Preserve history and a recoverable commit reference.
- A blocked board update, deployment, check or cleanup is unfinished. Record the
  specific blocker and continue independent authorized work; never report a step as
  completed without readback evidence.
- Maintain code, comments, documentation, ADRs, issues, PRs and commit messages in
  English. Preserve historical discussions, quoted source evidence and deliberately
  localized product output; this does not authorize publishing private runtime data.

## Code and checks

- Python 3.12+, minimal dependencies and no heavy application frameworks
- Prefer functions for orchestration; use dataclasses for domain values and small
  classes where they own explicit execution state or provider behavior
- Type annotations on functions, including return types; no unused imports or variables
- Use asynchronous `httpx` for HTTP. Preserve ordered synchronous storage and its
  failure boundaries; asynchronous HTTP is not a mandate to rewrite local persistence
- YAML configuration; English diagnostics through `logging`, with clear actionable
  errors. Product output language follows configuration
- Keep tests offline and external calls mocked. Use fixtures and cover the affected
  malformed-input, failure, retry and recovery boundaries
- After test edits, run `ruff check tests/`. Before committing, run
  `ruff check digest/ tests/` and fix all errors
- Run affected checks while editing, then the contributor guide's final `make check`
  (Ruff, mypy and pytest). Check documentation links and rendered pages for doc changes
- Preserve existing checks and state/effect guarantees; passing mocks or CI does not
  establish semantic quality or real-runtime acceptance
