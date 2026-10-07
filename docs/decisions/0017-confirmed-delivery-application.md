# 0017. Apply confirmed delivery through explicit scenario policies

Status: implementation record for the scoped #145 structural migration.
Merge, exact-head checks and runtime rollout evidence are tracked in
[#145](https://github.com/Lenivvenil/digest/issues/145); this record does not establish
runtime or editorial acceptance.
On 2026-10-07, [PR #151](https://github.com/Lenivvenil/digest/pull/151) merged at
`fbee27319751d2fdb94b30b0c83f6bddeb65b7e8`; runtime
[PR #74](https://github.com/Lenivvenil/digest-prod/pull/74) deployed that pin at
`0e658d815d241442250188559df69bb121daac45`. This verifies rollout, not editorial acceptance.

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

`delivery/edition.py` retains compatible exports after the later #147 continuations
below separate Telegram rendering, prepared policy/storage and concrete transports. Feedback and source-scoring modules
still mix policies and adapters: `FeedbackStore`, `SourceStats` and `SourceStateStore`
remain there, with type-only source-state imports in the new storage adapter.
`main` retains legacy/discovery orchestration. This slice does not complete the
feedback/catalog domains.
The scoped #146 ownership extraction is recorded separately in
[ADR0018](0018-review-reuse-and-source-attribution.md); it leaves these delivery
policies, receipt boundaries and remaining adapter debts unchanged.
Rollback uses a compatible reviewed code revert or prior engine pin, retaining all
runtime state and receipts without a format migration or state reset.

## Telegram presentation and transport continuation

The first delivery-ownership slice under #147 deployed through engine PR #158 and
runtime PR #81 on 2026-10-07. This does not establish complete #147 or editorial acceptance.

Pure `presentation/telegram.py` now owns card, compact and supplementary rendering,
Markdown encoding, deep-link buttons and chunk/range coverage. The unchanged shared
signal text and lossless splitter live in `presentation/supplement.py`. Prepared
preflight and edition freeze import the renderer directly. Compatible Telegram and
supplement imports remain, and the delivery package's existing exports are lazy to
avoid loading HTTP/archive owners when importing a pure supplement helper.

`adapters/telegram/delivery.py` owns legacy, direct-compact and post-delivery Telegram
protocols. Their distinct acceptance, retry, fallback, deadline and notification
policies are retained in the [architecture effect matrix](../ARCHITECTURE.md#stage-5-first-telegram-delivery-ownership-slice).
Legacy cards/status accept HTTP success and retain three attempts and HTTP-400
plaintext fallback. Direct compact accepts HTTP 200 plus `ok: true` with a 30-second
bound and no retry/fallback. Post-delivery requires HTTP success and `ok: true`, sends
silent chunks once within 30 seconds, and reports failures to the existing unknown
marker. These weaker receipts do not replace prepared delivery's positive message-ID
and matching-chat validation. All early credential/empty-selection skips remain.

`post_delivery.py` owns persisted marker/archive ordering, model and translation
work, compact archive-only exit and final outcome. Concrete persistence now delegates
to storage as recorded in the second continuation below; Telegram send has its own adapter. The full-source coverage string remains owned by the evidence stage;
the adapter compares it and passes a boolean to the pure localized renderer.

The second continuation below separates the prepared claim/receipt values, codecs,
state checks, writes and transport previously combined in `delivery/edition.py`. No schema,
hash, receipt, retained state, provider call or retry policy changes. Rollback remains
a compatible code revert or engine pin, without resetting state or resending messages.


## Prepared delivery persistence and application continuation

Local second delivery-ownership slice under #147, 2026-10-07. This record does not
establish merge, rollout, complete #147 acceptance or editorial acceptance.

`domain/delivery/edition.py` owns unchanged prepared article/edition/claim/receipt
values and pure shape, binding, count, state, publication-window and checkpoint-reference
checks. `ArticleSummary` moves unchanged to `domain/editorial/summaries.py`; its old
summarizer import retains the same identity. The domain imports no model, application,
transport or storage owners. Receipt checks receive an explicit owner hash. Reference
validation is separate from checkpoint-byte reads, which remain in storage.

`adapters/storage/edition.py` owns canonical encoding, exact file hashes, JSON
restoration, checkpoint-byte validation and durable local writes. The unchanged
symlink guard moves to `adapters/storage/issue_paths.py` with its old alias retained.
The old legacy compact marker codec remains its staged owner; this is not a redesign
of the compact issue guard. `application/prepared_delivery.py` owns the five prepared
operations and current time/environment observations. Production runtime callers use
that application; the old edition module remains a compatibility facade.

`adapters/telegram/prepared.py` sends one frozen payload and interprets its strict
message-ID/chat receipt without persistent-state access. The application keeps the
single 30-second dispatch window, persists attempted count before every POST and each
accepted receipt afterward, and retains the exact caught exception set. In particular,
a post-acceptance `OSError` escapes and the preceding `sending` record remains held;
there is no automatic resend or repair of potentially accepted messages.

The [ordering matrix](../ARCHITECTURE.md#stage-5-prepared-delivery-values-persistence-and-application)
records prepare/claim/send/apply effects, including ready-before-old-claim cleanup,
confirmed-and-applied return before freshness, and external ready/claim barriers.
Schema 1, filenames, sorted compact UTF-8 JSON with `allow_nan=False`, hashes, frozen
payloads, private owner restriction, UTC windows, exclusive writes and fsync remain
unchanged. No multi-file transaction, journal, reset or crash-idempotence promise is added.

Post-delivery concrete marker/result/archive effects move to
`adapters/storage/post_delivery.py`. Initial exclusive markers retain indentation,
trailing newline and file fsync; subsequent writes retain their previous atomic JSON
encoding. The application keeps execute-started → canonical result → translation →
archive → dispatching → send → terminal order and compact `archive_only` behavior.

Existing prepared, legacy guard, runtime, post-delivery and confirmed-outcome tests
remain the behavioral baseline. Focused additions cover owner-free domain imports,
explicit receipt owner evidence, value/entrypoint aliases, interrupted receipt writes
and initial-marker byte/fsync ordering. Synthetic old/new byte and effect comparison
supports parity; root and independent review remain required before publication.
Rollback remains a compatible code revert or prior engine pin, preserving all runtime
state, claims and receipts without reset or replay.
