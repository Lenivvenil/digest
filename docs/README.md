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

1. [Digest domain story and identities](domain/digest/overview.md): explain what a
   candidate, accepted preparation, ready edition and receipt actually establish
2. [Ordinary prepare → deliver lifecycle](ARCHITECTURE.md#prepared-edition-data-flow),
   [persisted records](ARCHITECTURE.md#cache-architecture) and
   [code entrypoints](ARCHITECTURE.md#modules-and-responsibilities)
3. [Observed state → safe action](BLIND_REVIEW.md#delivery-states-and-recovery):
   distinguish a ready edition, uncertain send and unapplied confirmation
4. [Irritator current model](domain/irritator/overview.md#current-domain-model):
   independent external evidence has a different purpose from model comparison

## Historical material

The following retain their original language, date and decision context. They are not
current onboarding instructions or newly ratified operating contracts:

- [Prior Digest domain/acceptance record](history/digest-domain-2026-10-08.md),
  [architecture/migration record](history/architecture-2026-10-08.md) and
  [review/operations record](history/review-operations-2026-10-08.md): preserved before
  the current maintainer guides were rewritten. Their dated release claims are history.

- `decisions/0001-*.md`, `0002-*.md`, `0003-*.md`: original ADR records; their decisions
  remain relevant, while implementation must be checked against current source.
- `plans/completed/`: completed implementation plans.
- [Archived Irritator diagnostic](domain/irritator-bc.md): explicitly superseded by the
  canonical Irritator overview; do not rewrite its diagnostic history.
- [Terse summaries and bubble plan](plans/completed/terse-summaries-and-bubble.md) and
  [Reddit QA report](history/reddit-qa.md): earlier implementation records, not a second active backlog.
- [April 24 output](history/output/2026-04-24.md): preserved historical publication,
  not a current output example or runtime directory.
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
