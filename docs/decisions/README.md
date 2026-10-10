# Architecture decision records

Find the decision behind a boundary. Each record owns its rationale; the current
[architecture](../ARCHITECTURE.md) and [state/effect contracts](../STATE_AND_EFFECTS.md)
explain implemented behavior.

- [Runtime, ownership and external boundaries](#runtime-ownership-and-external-boundaries)
- [Selection and evidence](#selection-and-evidence)
- [Publication and presentation](#publication-and-presentation)
- [Feedback and source portfolio](#feedback-and-source-portfolio)
- [Investigation and optional source reading](#investigation-and-optional-source-reading)

## Existing decisions

“Recorded status” follows each ADR's header and cited amendments, including historical
proposed or under-review wording. It is not a live deployment ledger. Implementation,
rollout and useful editorial output remain separate evidence; preserve the original
status, rationale and approval context when editing a record.

### Runtime, ownership and external boundaries

| Decision | Recorded status |
| --- | --- |
| [0001 · Adopt the claude-mini engineering workflow](0001-adopt-claude-mini-governance.md) | Accepted |
| [0002 · Separate public engine and runtime instances](0002-engine-instance-split.md) | Proposed (historical header) |
| [0015 · Give prepared workflows an application owner](0015-application-workflow-ownership.md) | Stage 1 merged/deployed; [target continuation](0015-application-workflow-ownership.md#target-ownership-continuation--2026-10-09) |
| [0019 · Give direct execution and discovery explicit owners](0019-remaining-application-scenarios.md) | Scoped #147-A implementation record |
| [0020 · Separate model execution from configuration](0020-explicit-model-execution.md) | Scoped #147-B implementation record; [pacing amendment](0020-explicit-model-execution.md#primary-review-pacing-correction--2026-10-09) |
| [0026 · Own bounded public acquisition](0026-public-acquisition-boundary.md) | Implementation under review (header); plan approved |

### Selection and evidence

| Decision | Recorded status |
| --- | --- |
| [0008 · Account for candidates across bounded packets](0008-candidate-selection-progress.md) | Accepted; natural-run and disposition acceptance open |
| [0016 · Separate candidate contracts, storage and retirement](0016-candidate-contracts-and-retirement.md) | Scoped #144 implementation merged/deployed |
| [0018 · Share review reuse and separate source attribution](0018-review-reuse-and-source-attribution.md) | Scoped #146 implementation record; deployment recorded |
| [0023 · Resolve authority from response-owned attempts](0023-response-owned-editorial-outcome.md) | Decision record for #181 |

### Publication and presentation

| Decision | Recorded status |
| --- | --- |
| [0005 · Opt-in translation of publication text](0005-optional-presentation-translation.md) | Accepted, implemented/deployed; finite acceptance passed |
| [0007 · Compact presentation and publication reservation](0007-compact-issue-reservation.md) | Compact transport deployed. [Ready update](0007-compact-issue-reservation.md#2026-10-04-amendment-freeze-readiness-before-claiming-delivery-120): accepted for implementation, rollout/acceptance pending in header. [Supplement amendment](0007-compact-issue-reservation.md#2026-10-09-later-edition-supplementary-evidence-208) |
| [0014 · Keep an optional humane closing item in preparation](0014-optional-humane-closing-item.md) | Implemented; optional/off by default; activation and editorial acceptance open |
| [0017 · Apply confirmed delivery through scenario policies](0017-confirmed-delivery-application.md) | Scoped #145 implementation record; [strict preflight](0017-confirmed-delivery-application.md#strict-prepared-accounting-preflight--2026-10-09), [receipt ownership](0017-confirmed-delivery-application.md#receipt-owned-prepared-publication--220-slice-3) |

### Feedback and source portfolio

| Decision | Recorded status |
| --- | --- |
| [0003 · Separate source configuration and lifecycle state](0003-source-state-split.md) | Proposed (historical header) |
| [0006 · Message voting for a batch-only runtime](0006-batch-message-voting.md) | Accepted/deployed; ordinary collector acceptance open |
| [0013 · Keep exploration attempts separate from offers](0013-discovery-exploration-state.md) | Accepted first slice; post-approval protection and recommendation quality open |
| [0021 · Separate catalog/feedback policy, persistence and Telegram](0021-catalog-feedback-boundaries.md) | #147-C and continuations deployed; [local-collection observation](0021-catalog-feedback-boundaries.md#retained-successful-local-collection--148-g9-2026-10-10) |
| [0024 · Preserve full article identity in future votes](0024-full-article-vote-identity.md) | Reviewed decision, pre-implementation; [rollback floor](0024-full-article-vote-identity.md#deployment-and-rollback-floor) |

### Investigation and optional source reading

| Decision | Recorded status |
| --- | --- |
| [0009 · Bind resumable source admission to candidate work](0009-selected-source-admission.md) | Proposed; local integration under review; [attempt-owned completion](0009-selected-source-admission.md#attempt-owned-page-completion--2026-10-10) |
| [0010 · Group source points with material qualifications](0010-group-source-points-with-qualifications.md) | Proposed; unused offline prototype retired under #207 |
| [0011 · Ground queries with optional literal provenance](0011-source-anchored-investigation-queries.md) | Revised optional-provenance accepted; mandatory-anchor proposal superseded; full-source activation unaccepted |
| [0012 · Preserve bounded private ranking evidence](0012-private-ranking-evidence.md) | Accepted bounded private audit; semantic acceptance open |
| [0022 · Bind investigation to a versioned search policy](0022-versioned-bounded-search-policy.md) | Accepted bounded repair |
| [0025 · Own ordinary Irritator fan-out results](0025-owned-irritator-fanout-results.md) | Approved bounded decision, pre-implementation |

## Historical context and numbering

The historical records retain their original language and decision context. Current
English explanations are in [Architecture](../ARCHITECTURE.md) and the canonical
[domain overview](../domain/digest/overview.md).

ADR0004 exists only in [closed, unmerged PR #93](https://github.com/Lenivvenil/digest/pull/93).
It is not on main; the proposal's existence is not a release approval.

Choose the next unused sequence number after checking open PRs. If the local claude-mini
skill package is installed, its `next_adr_number.sh` helper can assist; that private tool
installation is not required to read, configure or run Digest.
