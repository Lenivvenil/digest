# Plan: /bubble command — on-demand filter bubble analytics (issue #49)

## 1. Problem restatement

The system already collects rich signal data: per-article vote ratings in `feedback.json`, per-source fetch statistics and daily snapshots in `source_stats.json`, and trial/graduated/demoted lifecycle state in `source_state.json`. None of this is surfaced to the user directly — the only bot command available is `/status`, which returns two lines (last digest time, source count). The user has no way to ask "how homogeneous is my information diet right now, and is my voting behaviour actually influencing anything?" without inspecting raw JSON files. The `/bubble` command fills this gap by computing a filter bubble snapshot from local cache and sending it as a single Telegram message.

## 2. Affected bounded contexts and files

**BC: Digest (single BC, as per `docs/domain/digest/overview.md`)**

Modules touched:

| File | Change |
|------|--------|
| `digest/source_scorer.py` | Add `compute_bubble_report()` pure function |
| `digest/feedback.py` | Add `/bubble` handler in `collect_feedback()`; add `cache_dir` keyword-only param |
| `digest/main.py` | Pass `cache_dir` to `collect_feedback()` |
| `tests/test_sources_init.py` — no | n/a |
| `tests/test_bubble_analytics.py` (new) | Unit tests for `compute_bubble_report()` and `/bubble` handler path |

No BC boundary changes. No new domain terms introduced.

## 3. Considered approaches

### Approach A — Handle /bubble in `collect_feedback()` (chosen)

Extend `collect_feedback(bot_token, store)` with an optional `cache_dir: str = ".cache"` keyword argument. When the polling loop sees a `/bubble` text message, load `source_stats` and `source_state` from `cache_dir` (local disk reads, <5ms), call `compute_bubble_report()`, and send the reply.

**Pros:**
- All bot text command handling lives in one place (consistent with existing `/status` handler at `feedback.py:186`)
- No new CLI flag, no new GitHub Actions job
- Pure function `compute_bubble_report()` is independently testable
- `cache_dir` default makes the signature change backward-compatible; `main.py` call site needs one argument added

**Cons:**
- `collect_feedback()` gains a second responsibility (compute analytics, not just poll)
- Response is delivered on the *next pipeline run* (~24h later in production), same limitation as `/status` — this is a known trade-off of the polling-not-webhook model, not a regression

### Approach B — Dedicated `--bubble` CLI mode + GitHub Actions workflow_dispatch

Add `--bubble` to `main.py` argparse; it loads cache, computes, sends, exits. A `workflow_dispatch` trigger in `ci.yml` lets the user invoke it on demand without waiting for the daily run.

**Pros:**
- True on-demand delivery (sub-minute round-trip)
- `collect_feedback()` stays focused

**Cons:**
- Requires changes to `.github/workflows/ci.yml` — architectural scope (changes CI/CD pipeline, which requires careful review)
- Two separate code paths for "send message to Telegram from bot" (delivery and this)
- Adds operational surface: user needs to know to trigger the workflow, not just send `/bubble` in chat

**Verdict:** Approach B's latency improvement is real but the CI/CD scope and dual code paths are disproportionate for a personal observability command. Approach A is correct for the current architecture.

## 4. Chosen approach and why

**Approach A.** Consistent with the existing `/status` pattern, minimal scope, independently testable analytics logic.

No ADR triggered: no new library, no storage change, no BC boundary shift, no public API, no data model change, no security model change. This is a story.

**Relevant ADRs:** none directly; respects ADR-0003 (SourceStateStore is read-only here).

**Note:** This task touches 3 modules → 2 advisor calls required per `docs/principles.md §"нетривиальная задача"`.

## 5. Metrics computed by `compute_bubble_report()`

All derived from existing cache files only. No LLM calls, no network calls.

| Metric | Source data | Formula |
|--------|-------------|---------|
| **Source concentration** | `source_stats.history[-7:]` per source | Shannon entropy over `articles_included` share; expressed as "diversity score" 0–100 |
| **Top 5 sources by inclusion** | `source_stats.history[-7:]` per source | Sorted descending, capped at 5 |
| **Feedback engagement** | `feedback_store.ratings` (14d window) | Raw vote counts: total, +pos / -neg |
| **Positive/negative ratio** | `feedback_store.ratings` (14d window) | Count +1 vs -1 (included in engagement row) |
| **Source health** | `source_state.sources` | Count: active / trial / graduated / demoted |
| **Days since last digest** | `feedback_store.last_digest_time` | `now - last_digest_time` |

Metrics deliberately excluded:
- **Narrative drift velocity** — Narratives are transient (not persisted, per domain doc line 30). No data to compute this.
- **Counter-signal hit rate** — `IrritatorStatus` is not persisted between runs. No data.

## 6. Test strategy

**Unit tests (new `tests/test_bubble_analytics.py`):**
- `compute_bubble_report()` with zero ratings, zero stats → output contains all section headers, no division-by-zero
- `compute_bubble_report()` with known fixtures → diversity score matches expected value
- `compute_bubble_report()` with 14d-old ratings only → engagement rate is 0, not stale data included
- Formatted output fits within 4096 chars (Telegram limit)

**Unit tests (extend `tests/test_delivery_telegram.py` or `tests/test_feedback.py`):**
- Mock `getUpdates` returning a `/bubble` message → verify `sendMessage` called with a non-empty text payload
- Mock `getUpdates` returning `/bubble` with no cache files → graceful degradation (no crash, sends a "no data yet" message)

**No integration / e2e tests:** no cross-BC path, no real network calls needed. All HTTP mocked.

After writing tests, run `ruff check tests/` before committing (per CLAUDE.md).

## 7. Risks and unknowns

1. **Source stats sparsity in fresh installs:** if `source_stats.json` is empty (new prod instance), Shannon entropy over empty dict is undefined. `compute_bubble_report()` must handle this gracefully and return a "no data yet" message.

2. **`collect_feedback()` signature change:** `main.py` calls `collect_feedback(bot_token, feedback_store)` at line 472. Adding `cache_dir` as keyword-only with default `.cache` is backward-compatible, but must verify no other callers in tests use positional args that would silently mismatch.

3. **24h response delay:** users expecting real-time `/bubble` response will be surprised. The bot gives no acknowledgement when it *receives* `/bubble` — only when it *processes* it (next run). Mitigant: the reply message includes a timestamp so the user knows when the snapshot was computed.

4. **Telegram message length:** with many sources, the report could exceed 4096 chars. Must cap the "top sources" list (e.g. top 5) and truncate gracefully.

5. **Shannon entropy interpretation:** entropy value alone is not user-friendly. Must convert to a 0–100 scale or plain-language label ("Very concentrated", "Moderate", "Diverse") to be mobile-readable.
