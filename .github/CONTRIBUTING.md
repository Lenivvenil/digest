# Contributing to Digest

Make one behavior or maintenance problem easier to understand, change and verify.
Start with an existing issue, or record the problem and acceptance before editing.
The [working agreement](../AGENTS.md#working-agreement) owns tracking and completion;
this guide is a short route through it.

## Find the contract

Read the relevant [domain model](../docs/domain/digest/overview.md),
[architecture boundary](../docs/ARCHITECTURE.md#entities-contracts-and-enforcement)
and [recovery behavior](../docs/BLIND_REVIEW.md#delivery-states-and-recovery).
Use the [ADR trigger](../docs/principles.md#what-architecturally-significant-means-adr-trigger)
for architecture, persisted-state or public-interface changes. Keep decisions in the
existing documentation rather than creating a second account of how Digest works.

The public repository is the engine. Real source configuration, credentials, receipts
and runtime evidence belong in the private instance; use synthetic examples in PRs.

## Make and verify a bounded change

Use the [development setup and commands](../README.md#development). In an activated
virtual environment, `make install` installs the engine and runtime requirements;
`make install-dev` also installs the development requirements. Both are normally
networked source installations with dependency resolution and build isolation.
They can replace incompatible installed packages; neither requests an upgrade or
an editable install.

During editing, run the affected offline checks. For the final code change, `make check` runs lint,
type checking and tests together. Prefer an existing test at the decision owner;
add a case when it protects a distinct failure or contract. Retire an assertion only
when its behavior is covered elsewhere or the underlying contract is explicitly retired.

For documentation changes, check local links and the actual rendered pages. Record
what was checked and any unverified layout; required remote CI still applies.

What the repository currently runs:

- [CI](workflows/ci.yml): Ruff lint, mypy and pytest, including the configured coverage gate
- [Local pre-commit hooks](../.pre-commit-config.yaml): Ruff lint/format and Bandit,
  when installed; CI does not run those hooks or a dependency-security audit
- [Review and security evidence](../docs/principles.md#definition-of-done): record the
  applicable checks and reviews actually performed; a green CI run is not the whole checklist

Pytest enforces **80%** coverage, aligned with the principles' accepted default.
[#148](https://github.com/Lenivvenil/digest/issues/148) records the inherited 70%
setting and the evidence for its correction. No release-please workflow is
installed in tracked CI; use the [release and rollback guidance](../README.md#releases-and-licensing).

## Make the PR easy to review

Link the issue and relevant ADR. Explain the concrete before/after behavior, affected
invariants, verification results and remaining limits. Use `Refs #…` for partial work;
auto-close an issue only when its full acceptance is met. The PR template asks for
this evidence rather than copying every policy checkbox.

Follow the [definition of done](../docs/principles.md#definition-of-done) for review.
Mocks and structural checks do not establish useful news, faithful generated prose
or actual delivery. Preserve that distinction in the PR and issue status.

After merge, maintainers verify exact main checks and any applicable runtime pin/state
before recording completion. They update the existing issue/board from observed results,
leaving blocked access, rollout or cleanup visibly unfinished. The working agreement contains
the full follow-through, so this guide introduces no separate approval process.
