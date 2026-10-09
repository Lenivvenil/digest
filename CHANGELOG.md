# Changelog

## [Unreleased] — Reliability rehabilitation

- Retired the unintegrated grouped-point prototype `digest.reading_points` (#207)
  and its two dedicated test files. Direct Python imports now fail; no replacement
  shim is supplied. [ADR0010](docs/decisions/0010-group-source-points-with-qualifications.md#retire-the-unused-grouped-point-prototype--2026-10-09)
  preserves exact historical code/test links and the failed research findings.
  Supported optional source reading, qualification evidence and source fixtures remain.

- Primary review now shares the caller's same-loop semaphore, spacing lock and
  timestamps, provider/model cooldowns, local cap and spent-attempt count with
  later translation even when full-source reading is disabled (#215). Existing
  caps/cooldowns can reduce admitted review attempts; zero-retry settings and
  primary/fallback holder sharing remain. The configured interval carries across
  stages without extra calls or allowance. Independent invocations, cross-loop
  rebinding and durable-ledger ownership remain unchanged. See the
  [ADR0020 amendment](docs/decisions/0020-explicit-model-execution.md#primary-review-pacing-correction--2026-10-09).
  Synthetic timing proof does not establish the cause of HTTP 429 or live success.

- Candidate admission now carries exact selected occurrences and canonical RSS
  evidence together (#211). Shared item preparation and bundle serialization remove
  repeated sanitization, proposed-bundle rebuilds and the temporary empty packet.
  The internal `candidate_policy.plan_articles` returns `AdmittedCandidates | None`
  instead of an article list; private `_admit_candidate` and `_closing_opportunity`
  helpers retire. Scheduling, size limits, request/persisted bytes and strict
  validators remain unchanged; no test-count reduction or editorial acceptance is implied.

- Restore visible, grounded Irritator material inside a later ordinary compact
  edition ([#208](https://github.com/Lenivvenil/digest/issues/208), continuing #77).
  New attempts target canonical cards from an exactly confirmed/applied publication,
  retain source occurrence and presentation provenance, and exclude speculative
  hypotheses from target-bound query planning. Accepted material can accompany an
  edition on origin day D+1 through D+3, before the closer, using its existing
  translated presentation or canonical fallback. No additional model/search calls,
  retries, jobs, token allowance or billing capacity are added. Ready schema 3 binds
  separate supplement coverage and the current review checkpoint; main-only ready2
  and historical ready1 remain readable. Consumption precedes the applied receipt
  marker, and uncertain dispatch is never replayed. Rollout requires the existing
  runtime receipt barrier to persist the consumed attempt under digests/ and the
  existing post-prepare step to receive the owner ID. Ordinary semantic/editorial
  acceptance, aggregate coverage and the <=1,500-test goal remain separate gates.

- Closing-enabled RSS reviews now request a complete optional v2 card separately
  from professional selections, within the existing shared detail allowance
  ([#127](https://github.com/Lenivvenil/digest/issues/127)). Selected dispositions
  derive from validated cards; contradictory residuals remain unresolved. Invalid
  optional output preserves main cards and fallback behavior. Disabled request
  bytes and historical v1 records remain compatible; translation and publication
  use the existing path without extra model calls or token allowances. See the
  [contract amendment](docs/decisions/0014-optional-humane-closing-item.md#complete-optional-response-card--2026-10-09-amendment).
  Synthetic coverage does not establish the remaining ordinary runtime editorial
  acceptance or activate any source.

- Ordinary Irritator query/search fan-out owns its payload and diagnostic counts
  together (#204). This is a deliberate exported Python API break: `generate_queries`
  now returns `QueryBatch` (`.queries_by_narrative`, `.diagnostics`), and
  `search_all_sources` returns `SearchBatch` (`.signals`, `.diagnostics`); both remove
  `diagnostics=`. Names/import paths remain; the
  [migration map](docs/decisions/0025-owned-irritator-fanout-results.md#compatibility-and-migration)
  lists destinations. Individually cancelled query children now propagate
  `CancelledError` instead of accidental `TypeError` or duplicate-claim masking.
  Non-cancelled `run_irritator` status and bounded persisted evidence remain unchanged.
  Rollback must retain ADR0024-compatible sender and poller readers and saved state.

- Editorial selection validation now shares one item-rule owner (#202). Live
  salvage validates items directly instead of reparsing synthetic responses or
  branching on exception text. Strict literal quotes, narrow live typography
  alignment, duplicate/error ordering and both ASCII-escaped size gates remain;
  checkpoint revalidation keeps its separate persisted-trust boundary.

- Retired the root RSS import bridges `digest.review`, `digest.candidate_dispositions`,
  `digest.candidate_storage` and `digest.candidate_review` (#194). Python consumers
  must import their canonical domain/application/storage/presentation owners; no
  replacement shim is supplied. The old `save_candidate_progress(retire=...)`
  dispatcher becomes explicit `checkpoint_candidates` or `persist_candidates`.
  [ADR0016's migration map](docs/decisions/0016-candidate-contracts-and-retirement.md#retire-the-rss-import-bridges--2026-10-08)
  lists destinations and retired private aliases. Canonical types, persisted formats,
  CLI/run/config and the supported feedback/discovery/source-scoring interfaces remain.

- Preserve full article identity in new vote links and confirmed attribution
  (#199). Ready editions now use schema 2; readers retain schema 1, and claims and
  receipts stay schema 1. Existing short votes/history remain readable without
  guessed migration. Known-colliding unstarted legacy editions are held before
  dispatch. Once full-token links are published, both sender and poller require a
  compatible-reader engine; an arbitrary older pin is not a safe rollback.

- Give the existing protected unseen-candidate opportunity to the oldest fitting
  identity before technical retries and fresh backfill. This changes future
  admission order within existing limits; it adds no model calls or packet slots
  and preserves stored decisions and optional closing protection. See
  [#196](https://github.com/Lenivvenil/digest/issues/196) for the explicit freshness,
  source-diversity and character-budget tradeoffs.

- Search adapters now own response validation and bounded buffering (#190).
  The bounded Irritator no longer installs or removes response hooks on a caller's
  HTTP client. Direct/legacy Hacker News requests now reject bodies over 512,000
  decoded bytes, refuse redirects and check status before reading the body.
  Bounded search already enforced those rules; its request/policy identity, limits,
  saved results and source pacing remain unchanged.

- Category preparation now owns its save and empty-completion decisions (#189).
  The single-caller internal `edition_runtime.save_accepted_preparation` helper and
  undocumented `selection_complete` keyword are retired. Internal callers must use
  the category coordinator or the appropriate canonical acceptance boundary.
  `finish_preparation` retains its default snapshot presentation behavior. CLI,
  historical category outcomes, persistence formats and effect ordering are unchanged.

- Bounded Irritator ranking now owns admission and response evidence together (#187).
  The private `_ranking_candidates` and `_ranking_audit` helpers are removed;
  `_admit_ranking` supplies one packet/pending-audit result, and `_parse_rankings`
  returns ranked output with its completed audit instead of mutating an optional
  caller-supplied audit. Direct internal imports require adaptation. Saved JSON,
  request bytes, ranking policy, limits and supported CLI behavior are unchanged.

- Retired the unused internal reading-angle publication protocol (#183):
  `reading_brief.BriefRun`, `enrich_selected_cards`, `_render`, `_citation_note`,
  `mark_briefs_delivered` and `reconcile_briefs_delivered`. Direct Python imports of
  these names now require migration; no supported CLI command is removed or silently
  redirected. Candidate-bound source preparation, its acquisition/request engine,
  reconciliation and historical state readers remain unchanged. Source fixtures and
  qualification evidence are preserved; [ADR0010](docs/decisions/0010-group-source-points-with-qualifications.md#retire-the-unintegrated-reading-angle-publication-protocol--2026-10-08)
  records the precise retirement and historical implementation reference.

- Explicit model execution ownership (#147-B): `ModelExecution` owns lazy per-loop
  concurrency, pacing, cooldowns and local request counts outside `LLMConfig`.
  Internal model-consuming Python helpers now require an explicit execution argument;
  budget/wait helpers take `(config, execution)` and limits take `(config, execution, limit)`.
  Approval reloads return both settings and their owner. Public CLI/run entrypoints
  remain compatible. Fresh versus initialized-sharing copies, independent loop resets,
  durable reservations and absolute deadlines retain the policy recorded in
  [ADR0020](docs/decisions/0020-explicit-model-execution.md). No persisted state migration
  or provider/source policy change is introduced.

- Disabled-by-default humane closing slice (#127): optional same-response
  designation, exact delivery-slot/source provenance, strict preparation v1/v2
  compatibility and isolated presentation before archive/freeze. Main capacity,
  sender and receipts stay unchanged. Source activation, translated capacity and
  editorial acceptance remain open. A fitting closing explanation shares the
  existing translation request, with independent optional-field validation. Exact-feed
  NHS/Environment Agency credits share the final archive/frozen card path; optional
  split attribution is omitted, while required-main attribution holds before send.

- First discovery slice (#132): configurable cross-field proposal targets, deterministic
  least-recent-offer preference within fair passes, and finite exact-proposal validation
  cooldowns in existing delivery metadata. Legacy approval bindings and send barriers
  stay intact; active feeds and daily candidate scheduling are unchanged. Professional
  coverage after approval and real recommendation quality remain open.

- Prepared-edition summaries retain actual collection, review and archive results;
  cache-only resumes do not report historical work as new fetching (#55, #120).
- Relevance selection no longer receives the publication-card cap. The complete
  validated report is retained, while locally capped overflow stays eligible for
  ordinary later processing instead of becoming editorial rejection (#121).

- Offline reconciliation response binding validates a sparse evidence input against
  exact request/admission/completion metadata and original citation IDs. It preserves
  nominated conditions without claiming semantic completeness or adding dispatch.

- External evidence responses enforce the existing 512,000-byte decoded-body limit
  during streaming; the fixed-endpoint arXiv adapter applies the same bound in both
  investigation paths. The fixed arXiv endpoint explicitly rejects redirects, including
  when a custom caller enables them; owned pipeline callers already rejected them.
  Oversized bodies fail explicitly without parsing a prefix.
  Accepted bytes and charset decoding are retained. This bounds accumulated response
  data, not decompressor allocations or XML-parser memory. Existing MD5 article/source
  IDs remain byte-identical and are explicitly marked as non-security hashing.

- Bounded investigation queries (#77) use optional literal source provenance.
  Valid nonempty queries proceed without a copied source phrase; the mandatory
  anchor proposal was superseded before deployment. Query planning receives labelled
  hypotheses separately from source claims; ranking still excludes those hypotheses.
  Useful retrieval and semantic relations remain acceptance gaps.
  Exact cited target/final URLs are excluded from external candidates with explicit
  diagnostics; different documents from the same publisher remain eligible.
- Draft source admission (#122) reuses complete-source acquisition and exact/estimated
  request accounting from #107, bound to current saved candidate selections. Durable
  generation-intent holds and actual per-page fallback provenance remain separate from
  #55 semantic acceptance; technical completion does not publish draft prose.
- Candidate accounting (#121): ordinary review-led preparation captures pre-slot
  identities and resumes later bounded packets through persisted selection evidence.
  Existing accepted editions still take precedence. Same-response typed per-ID
  dispositions distinguish metadata reasons/duplicates from capacity-deferred work;
  old missing reasons remain unresolved. Exact source objects and packet-local archives
  keep resolved history outside current-work admission, with reversible exclusions
  and unsent selections recoverable. Real-output and sustained throughput acceptance
  remain open.

- Prepared-edition boundary (#120): accepted canonical analysis is resumable before
  presentation; exact Telegram payloads are frozen and remotely persisted before a
  separate sender claims them. Message IDs and complete article coverage are retained;
  ambiguous sends or incomplete state persistence hold replay. Existing daily schedule
  and job ceilings remain unchanged; natural-release acceptance is still required.

- English current documentation and safe onboarding; historical records retain their
  original language, and no software-license grant is supplied.
- Configurable generated-text translation with a pinned route, bounded requests,
  canonical fallback and durable content/target/model/prompt-bound cache. Absent
  configuration preserves legacy generation. Finite runtime acceptance includes a
  faithful prompt-v2 archived narrative and visible English fallback after a rate limit.
- Compact daily editions (#105) preserve selected text, links and article identities
  across bounded Telegram chunks. Durable reservation precedes publication; only
  confirmed coverage earns delivery attribution, and unknown outcomes hold replay.
  Optional results remain archived. One production edition confirmed five articles
  in one chunk with persisted archive and state.
- Batch-compatible source approvals (#106) bind ordinary-message decisions to a
  current proposal and apply them before collection. Backup/config/state failures
  retain decisions; replay does not duplicate an already configured source.

- Opt-in blind evidence review: immutable RSS snapshot and identical prompts for
  explicit primary/secondary model slots, no implicit provider fallback, optional
  third family only on validated selection disagreement. Strict source-ID/quote
  validation and shared input/output budgets; Markdown plus JSON provenance.
- Blind selection runs before optional category prose. Primary cards survive
  prose failure; abstention/unavailable diagnostics are still archived.
- Synthetic offline contract demo: `python -m scripts.review_fixture --output DIR`.
  This does not call providers and is not evidence of real-model quality.

- Optional LLM pacing controls bound shared concurrency and request spacing across
  all stages, with bounded retries for 429/5xx/transport failures. Permanent
  authentication, billing and missing-model errors fail over immediately.
  Provider diagnostics expose only status and a sanitized machine error code.
- `telegram.required: true` makes incomplete article-card delivery a failed run,
  even when Markdown exists. It is opt-in (default false). Under this policy,
  Markdown alone does not consume new articles after a Telegram failure.

- Telegram card delivery returns `ArticleDeliveryResult` with confirmed counts,
  delivered hashes and feedback attribution. Callers must no longer interpret
  the return value as an article-source dictionary. Exhausted 429 retries raise.
- Dedup commits only articles covered by successful Markdown output or accepted
  Telegram cards; failed categories and undelivered Telegram-only cards remain
  retryable. `--radar-only` no longer consumes articles.
- Picker failures tolerate malformed JSON and invalid field types. Selected
  links must match input articles; titles/source/category retain canonical
  identities, and duplicate or invented links are discarded.
- Source fetch health and delivered inclusion counts now update adaptive stats,
  including fetch observations from unsuccessful runs. Dry-run does not persist
  observations. Nano status uses actual fetch success/failure counts.
- Same-day Markdown runs keep previous output (`YYYY-MM-DD.md`, then
  `YYYY-MM-DD-2.md`, etc.) instead of replacing the earlier digest.
- DEV.to counter-signal search is deliberately disabled with a warning: the
  documented `/api/articles` API does not support full-text `q` search. Existing
  configs remain loadable, but this adapter returns no signals until a supported
  search integration is implemented.

Compatibility notes: by default, Markdown remains an independent successful delivery channel.
With `telegram.required: false`, saved Markdown can acknowledge an article even if Telegram fails; this
patch does not introduce a per-channel outbox or exactly-once delivery. Runtime
workflows must persist cache files after partial runs for selective retries to
survive a fresh GitHub runner. No production workflow or credentials are changed.

## [1.0.0] — 2026-04-07

Historical release record translated from the [original Russian entry](https://github.com/Lenivvenil/digest/blob/0b939fb6b065723eee278069fec546980788e439/CHANGELOG.md#100--2026-04-07). Claims below are preserved as recorded for this release.

First stable release. The project has been running in production since March 2026.

### Features

**News collection**
- RSS/Atom feed collection via `httpx` + `feedparser`
- Parallel fetching of all sources
- Article deduplication by MD5(title|link) with a 7-day window
- Priority-based slot allocation: higher-priority sources receive more slots
- Per-category limit (`max_articles_per_source`) and global limit (`max_total_articles`)
- Recency filtering (`recency_hours`)

**LLM summarization**
- Support for 5 providers: Anthropic Claude, Google Gemini, Groq, Mistral, DeepSeek
- `ProviderChain`: automatic fallback to the next provider on failure
- Category routing: different providers for different categories
- Parallel category processing via `asyncio.gather()`
- Three perspectives format (Optimist 🟢 / Skeptic 🔴 / Realist ⚖️) for top stories
- Cross-category trends at the end of the digest
- Three styles: `analytical`, `brief`, `detailed`

**Delivery**
- Telegram Bot API: a separate card for each article with 👍/👎 buttons
- Markdown files in `digests/` with YAML front matter for Obsidian
- Status footer with run metrics (sources, articles, providers, average score)
- Telegram notification on workflow failure

**Adaptive system**
- User ratings 👍/👎 affect source priorities
- Automatic quality metrics: reliability, productivity, description quality, recency
- Trending-source detection (+1 to priority for growth >50% over 7 days)
- Trial source system: LLM-discovery → Telegram approval → trial period → promote/disable

**Source discovery**
- `--discover`: the LLM generates RSS candidates for underrepresented categories
- URL validation before proposing a source
- Telegram approval workflow with ✅/❌ buttons
- Automatic addition to `config.yaml` after approval

**CI/CD**
- GitHub Actions: daily digest (02:00 + 13:00 UTC) with a test gate (lint, typecheck, tests)
- Weekly discovery (Sundays 06:00 UTC) with a test gate
- Concurrency group prevents concurrent writes to `.cache/`
- Commit-back pattern: cache and digests are automatically committed to `main`

**Security**
- DNS pinning + SSRF protection via `_dns_pinning.py`
- Input sanitization of feed content via `_sanitize.py`
- Atomic JSON writes via `_util.py`

### Quality

- 260+ unit/integration tests (pytest + pytest-asyncio + respx)
- Coverage: all src/ modules
- Lint: ruff (E, F, B rules)
- Type checking: mypy (strict)
- Pre-commit hooks for automatic formatting
