# 0017. Apply confirmed delivery through explicit scenario policies

Status: implementation record for the scoped #145 structural migration.
Merge, exact-head checks and runtime rollout evidence are tracked in
[#145](https://github.com/Lenivvenil/digest/issues/145); this record does not establish
runtime or editorial acceptance.

Refs [the staged architecture](../ARCHITECTURE.md#structural-migration-current-stage-and-target),
[ADR0007](0007-compact-issue-reservation.md),
[ADR0015](0015-application-workflow-ownership.md) and
[ADR0016](0016-candidate-contracts-and-retirement.md).

## Context

Prepared sending updated feedback, deduplication and source accounting in
`edition_runtime`; direct delivery repeated related coordination in `main`.
Compact senders also duplicated article-to-chunk coverage projection. Their
placement obscured ownership, but their distinct policies are existing contracts:
prepared sending reloads mutable state and holds interrupted application, while
direct runs combine fetch observations with output and support legacy Markdown
consumption. A common entrypoint must not erase those differences.

## Decision

- `domain/delivery/outcomes.py` owns `ArticleDeliveryResult`, `IssueDeliveryResult`,
  `ArticleCoverage` and pure `project_issue_coverage`. Both compact senders use it.
  Complete covering chunks establish article attribution; whole-issue completion
  additionally includes notice-only chunks. Transport owns receipt validation and
  the outcome supplied to the projection. This domain module has no project imports.
- `application/delivery.py` owns `apply_confirmed_outcome`, selecting an explicit
  `PreparedOutcomePolicy` or `LegacyOutcomePolicy`. `edition_runtime._merge_delivery`
  and `main` delegate effects to it and finalize receipts/guards afterward.
  The broad legacy policy is a transitional in-memory run context pending #147,
  not an ideal domain policy or a persisted entity/schema.
- `feedback.apply_delivery_attribution` owns confirmed mappings and complete-output
  last-digest metadata. `source_scorer.record_delivered_articles` owns prepared
  inclusion accounting without a fetch; direct fetch accounting remains in
  `application.run_state.record_source_stats` with the existing scorer operation.
- `adapters/storage/delivery_state.py` owns strict cache loading and strict raw
  prepared deduplication/statistics/lifecycle writes. Feedback and legacy saves
  retain their existing owners and failure behavior. The application owns ordering,
  not JSON codecs or Telegram transport.

The canonical [effect matrix](../ARCHITECTURE.md#stage-3-confirmed-delivery-application)
specifies both scenarios. Prepared application writes feedback → statistics →
optional adaptive state → seen cache, then the caller marks receipts applied.
It counts only confirmed hashes absent from the current cache, on the intended
publication day, and does not invent fetch observations or prune inactive sources.
With no confirmed article coverage, it performs no operational-state reads or writes.

Direct application retains seen cache → usable feedback → lifecycle state →
statistics → category map. Cards may consume saved Markdown output under the
existing optional/complete Telegram rule; direct compact may not. Fetch observations,
current-day history, inactive-source pruning, conditional trial evaluation and
existing caught versus propagated write failures remain unchanged. Prepared strict
writes do not convert the existing permissive statistics/lifecycle loaders into
strict readers. Failed delivery preserves collected votes and polling cursors.
Markdown consumption does not imply Telegram confirmation. Legacy cards retain the
deduplication writer's default cache path, ignoring the supplied `cache_dir`;
compact uses that supplied directory.

## Repeat safety and interruption

After all prepared state writes succeed, reapplying the same coverage does not
increment inclusion counters or replace existing deduplication timestamps while
those hashes remain in the cache. This is a bounded counter/deduplication property,
not byte idempotence of metadata, time or adaptive evaluation. Normal confirmed-and-
applied inspection returns without a new send or application. Legacy fetch accounting
is not promised to be repeat-idempotent.

If a write or the final applied-receipt write fails, preceding files can already
have changed. Unapplied receipts hold the edition for inspection. There is no
automatic rollback, counter reconciliation or resend; partial/unknown transport
outcomes remain held even when their known coverage is marked applied. Per-file
atomic writes are not a transaction, and only the runtime establishes remote durability.

## Compatibility, verification and remaining work

Old Telegram result imports retain the same value identities. Serialized formats,
hashes, source identities, receipt history, provider calls, deadlines and retention
policies remain unchanged. Prepared transport still validates positive message IDs
and matching chat; direct compact retains its HTTP/`ok` acceptance and coarse guard.
Both stop without retry/fallback; legacy cards keep their existing transport policy.

Behavioral verification covers partial/notice-only coverage, current feedback merge,
successful-repeat inclusion accounting, Markdown rules, failed-fetch accounting and
failures at every prepared state write before receipt application. Transport regressions
retain unknown/unapplied holds and no replay. Check results belong to the implementing
change, separately from product acceptance.

`delivery/edition.py` still combines receipt/claim storage and transport;
`delivery/telegram.py` still renders and sends. Feedback and source-scoring modules
still mix policies and adapters: `FeedbackStore`, `SourceStats` and `SourceStateStore`
remain there, with type-only source-state imports in the new storage adapter.
`main` retains legacy/discovery orchestration. This slice does not complete the
feedback/catalog domains or later review/source-attribution work.
Rollback uses a compatible reviewed code revert or prior engine pin, retaining all
runtime state and receipts without a format migration or state reset.
