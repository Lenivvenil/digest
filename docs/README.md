# Documentation map and status

Choose a route for the work you need to do. These are the existing canonical guides;
dated decisions and archived models remain linked for context.

## Operate a runtime

1. [Purpose and safe quick start](../README.md#quick-start): install the engine and
   validate the [disabled example configuration](../examples/config.example.yaml)
   without credentials or external requests
2. [CLI and configuration](../README.md#cli-reference): distinguish offline validation,
   report-only model work and enabled delivery
3. [Prepared publication and recovery](BLIND_REVIEW.md#prepared-editions-and-delivery-recovery-120):
   use the runtime's saved-state, reservation and recovery boundaries
4. [Primary-first supplementary processing](BLIND_REVIEW.md#primary-first-runtime-with-preserved-irritator):
   run bounded Irritator work from saved evidence after primary delivery

The engine does not install a schedule. The separate runtime owns secrets, source
configuration, delivery destinations, the engine pin and durable state.

## Contribute a change

1. [Working agreement](../AGENTS.md#working-agreement) and [development commands](../README.md#development)
2. [Project principles](principles.md#definition-of-done): required evidence, optional
   local tooling and the unresolved 70% configured / 80% default coverage disposition
3. [Decision index](decisions/README.md): accepted decisions, proposals and release scope

## Understand the architecture

1. [Runtime boundary and scenarios](ARCHITECTURE.md#overview)
2. [Entities and invariants](ARCHITECTURE.md#entities-contracts-and-enforcement), then
   [current ownership](ARCHITECTURE.md#modules-and-responsibilities)
3. [Digest current model](domain/digest/overview.md#current-domain-model) and
   [Irritator current model](domain/irritator/overview.md#current-domain-model), followed
   by their requirement, acceptance and historical records
4. [Migration and release appendix](ARCHITECTURE.md#appendix-migration-and-release-history)
   for why the boundaries changed and what has actually been deployed

## Historical material

The following retain their original language, date and decision context. They are not
current onboarding instructions or newly ratified operating contracts:

- `decisions/0001-*.md`, `0002-*.md`, `0003-*.md`: original ADR records; their decisions
  remain relevant, while implementation must be checked against current source.
- `plans/completed/`: completed implementation plans.
- [Archived Irritator diagnostic](domain/irritator-bc.md): explicitly superseded by the
  canonical Irritator overview; do not rewrite its diagnostic history.
- Repository-root `plan.md` and `qa-report.md`: earlier investigation/implementation
  artifacts, not a second active backlog.
- [CHANGELOG.md](../CHANGELOG.md): historical release entries, not current quality evidence.

Original versions of translated documents remain available through git history. Legacy
section anchors referenced by existing documents are retained where required. Historical
issues and PR discussions are not translated or erased.

## Current work boundary — 2026-10-07

English documentation/onboarding and configurable primary/supplementary translation
are implemented and deployed. The finite acceptance under
[#94](https://github.com/Lenivvenil/digest/issues/94) covers an independently reviewed
ordinary archived narrative using prompt v2, exact canonical fallback after a provider
rate limit, and a confirmed compact edition with persisted archive/state. It does not
claim general translation accuracy or Russian delivery for that English-fallback edition.

[#55](https://github.com/Lenivvenil/digest/issues/55) remains open for useful, faithful
daily content. Full-source processing is an optional experimental mechanism. [PR #93](https://github.com/Lenivvenil/digest/pull/93) was
closed without merging; its experimental enrichment is not available on main. The
ordered requirements and acceptance status live in [#91](https://github.com/Lenivvenil/digest/issues/91).

An absent translation section preserves legacy direct `radar.language` generation.
README and ADR-0005 document explicit English canonical generation and optional
translation of generated publication prose. No software-license grant is supplied;
choosing a distribution license is outside this personal-runtime milestone.
