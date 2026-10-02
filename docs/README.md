# Documentation map and status

Current English entry points:

- [Product and onboarding](../README.md)
- [Architecture](ARCHITECTURE.md)
- [Digest domain](domain/digest/overview.md)
- [Irritator domain](domain/irritator/overview.md)
- [Project principles](principles.md)
- [RSS review and supplementary-stage runbook](BLIND_REVIEW.md)
- [Architecture decision index](decisions/README.md)
- [Safe example configuration](../examples/config.example.yaml)

The canonical domain pages retain dated snapshots and distinguish intended behavior,
observed implementation, accepted owner requirements and proposals. Their translation
does not retroactively approve an architectural change. Source links remain evidence.

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

## Current work boundary — 2026-10-02

English documentation/onboarding #95 and configurable primary/supplementary translation
are implemented. [#94](https://github.com/Lenivvenil/digest/issues/94) remains open for
ordinary post-#102 translation, fallback/recovery and compact-presentation acceptance.
Previously validated synthetic translation is limited historical evidence; it does not
establish semantic fidelity of ordinary output using `presentation-translation-v2`.

[#55](https://github.com/Lenivvenil/digest/issues/55) remains an open full-source
factual-quality requirement. [PR #93](https://github.com/Lenivvenil/digest/pull/93) was
closed without merging; its experimental enrichment is not available on main. The
ordered requirements and acceptance status live in [#91](https://github.com/Lenivvenil/digest/issues/91).

An absent translation section preserves legacy direct `radar.language` generation.
README and ADR-0005 document explicit English canonical generation and optional
translation of generated publication prose. No software-license grant is supplied;
choosing a distribution license is outside this personal-runtime milestone.
