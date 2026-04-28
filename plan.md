# Plan: Fix irritator counter-signal pipeline (Issue #53)

## 1. Problem restatement

The irritator subsystem runs every day and consistently produces zero passing counter-signals. The
failure is not a crash or a config error — the pipeline runs cleanly to completion and reports
`"N narratives, M signals, M valid, 0 passed ranking"`. The root cause is a stack of three coupled
problems: (1) the query-generation prompt generates topical queries that mirror the narrative's own
vocabulary, so keyword-matching APIs (HN Algolia, Reddit search) return more consensus content
rather than contradictions; (2) each query is dispatched to only one source — chosen by the LLM —
meaning 3 queries for 5 sources leaves some sources never probed for a given narrative; (3) the
ranker prompt scores four criteria conjunctively (substance AND contradiction AND credibility AND
surprise), which collapses divergent signals to mid-range scores (5–7) that fall just below the
`min_signal_score=7` hard cutoff. Additionally, `devto.py` does a tag-based listing call — not text
search — so it structurally cannot return counter-signals regardless of the query.

## 2. Affected bounded contexts and files

**Bounded context: Irritator** (`digest/irritator/`)

| File | Change needed |
|---|---|
| `digest/irritator/query_generator.py` | Rewrite LLM prompt to enumerate adversarial query patterns; remove `target_source` from `SearchQuery` dataclass and prompt |
| `digest/irritator/sources/__init__.py` | Change `search_all_sources()` dispatch: fan each query to **all** configured sources instead of the single `target_source` |
| `digest/irritator/sources/devto.py` | Fix search to use `?q=` text search rather than `?tag=` listing |
| `digest/irritator/ranker.py` | Rewrite prompt: single primary criterion (contradiction score) with calibration anchors; lower `min_signal_score` default from 7 to 5 |
| `digest/config.py` | Lower `IrritatorConfig.min_signal_score` default from 7 to 5 |
| `tests/test_query_generator.py` | Update tests: no `target_source` field in assertions |
| `tests/test_sources_devto.py` | Update mock to match `?q=` parameter |
| `tests/test_sources_init.py` | Update fan-out behaviour: each query hits all sources |
| `tests/test_ranker.py` | Update prompt assertions; add threshold behaviour tests |

No cross-BC contract changes. The irritator's outbound interface to Delivery
(`list[Narrative], list[RankedSignal], IrritatorStatus`) is unchanged.

## 3. Considered approaches

### Option A — Prompt-only + threshold tweak (no structural change)

Rewrite the query-generator prompt with adversarial tokens; lower `min_signal_score` to 5–6; add
calibration anchors to the ranker prompt. Keep `target_source` routing as-is.

**Pros:** smallest diff; zero risk of breaking query-dispatch logic; ships in under 2 hours.

**Cons:** does not fix the sparse-coverage problem — with `queries_per_narrative=3` and 5 sources,
some sources are still never hit for a given narrative. Also does not fix dev.to.
**Red flag:** a/B testing would show improvement from threshold alone, not from actually finding
contradictions. This is a cosmetic fix for the wrong problem.

### Option B — Prompt rewrite + remove target_source fan-out + fix dev.to (chosen)

On top of A: remove `target_source` from `SearchQuery`; change `search_all_sources()` so each
query fans out to all configured sources; fix dev.to to use text search. Result:
`queries_per_narrative=3` queries × N sources = 3N fetches per narrative — all sources covered.

**Pros:** addresses all three coupled root causes; intra-BC only (no cross-context contract
change); outbound HTTP increases ~5×, but still within free-tier GitHub Actions limits
(5 narratives × 3 queries × 5 sources = 75 requests/run vs current ~15).

**Cons:** slightly more HTTP load; removes per-source query phrasing (arXiv vs Reddit might
benefit from different vocabulary). That concern is valid but secondary — uniform coverage beats
non-coverage. Revisitable in Option C.

### Option C — Two-pass pipeline: discovery + adversarial critique (deferred)

After raw signals are fetched, add a second LLM stage that generates adversarial follow-up queries
targeting failure/criticism vocabulary, runs those, then merges and ranks the combined pool.

**Pros:** most powerful; properly decouples topical discovery from adversarial probing.

**Cons:** adds a pipeline stage and an extra LLM call per narrative — would require an ADR (new
pipeline stage). Too heavy as a first fix. Revisit if B proves insufficient after one week of runs.

## 4. Chosen approach and why

**Option B.** The three coupled root causes (adversarial prompt, sparse fan-out, dev.to broken)
all live in one bounded context and can be fixed without touching the cross-BC interface or adding
dependencies. The change is reversible: if fan-out increases latency unacceptably, per-source
routing can be reintroduced as an optional config filter.

No ADR required: `SearchQuery` is internal to the Irritator BC; removing one field from it does
not change a cross-BC contract. Per `docs/principles.md` §"Что значит «архитектурно-значимо»",
none of the six triggers fire (no new cross-cutting dependency, no BC boundary change, no storage,
no public API, no security model change).

**Specific changes:**

1. **`query_generator.py` prompt** — add explicit adversarial instruction:
   > "Do NOT generate queries that describe or expand the narrative. Generate queries designed to
   > find EVIDENCE AGAINST it. Use adversarial patterns: 'failure of X', 'X didn't work',
   > 'criticism of X', 'post-mortem X', 'X considered harmful', 'why X is wrong', 'X limitations',
   > 'X hype'. Each query MUST contain at least one negation, failure, or doubt keyword."
   Remove `target_source` field from `SearchQuery` dataclass and prompt.

2. **`sources/__init__.py:search_all_sources()`** — remove single-source dispatch; call all
   registered adapters for each query. URL dedup is handled downstream by `validate_signals()`.

3. **`devto.py`** — replace `?tag=first_word` with `?q=full_query_string`
   (dev.to API supports text search via `?q=`; no `sort` param — default relevance ranking is preferable to recency for counter-signal discovery).

4. **`ranker.py` prompt** — replace four-criteria scoring with a single calibrated question:
   > "Does this content CONTRADICT or COMPLICATE the narrative? Score 1-10 where:
   > 9-10 = direct evidence the narrative is wrong or overstated
   > 7-8 = significant complication or important caveat the narrative ignores
   > 5-6 = mildly relevant alternative perspective
   > 1-4 = agrees with or restates the narrative"

5. **`config.py`** — `IrritatorConfig.min_signal_score` default: 7 → 5. (Note: `digest-prod/config.yaml`
   already overrides to 5, confirming the threshold is NOT the binding constraint in production —
   the funnel empties before the ranker. This code change is for consistency and new deployments.)

## 5. Test strategy

**Unit tests (existing files, update assertions):**

- `tests/test_query_generator.py`:
  - Assert `SearchQuery` dataclass has no `target_source` field.
  - Assert the prompt string sent to LLM includes at least one adversarial marker
    (e.g. "failure", "criticism", "wrong", "didn't work"). Check via mock `complete()`.
  - Existing JSON-parse / field-validation tests stay unchanged (minus `target_source`).

- `tests/test_ranker.py`:
  - Assert prompt includes calibration band text ("9-10" or "direct evidence").
  - Assert default `min_signal_score=5` in `IrritatorConfig` (not 7).
  - Existing score-threshold and top-signals-limit tests: update threshold expectations from 7 → 5.

- `tests/test_sources_devto.py`:
  - Assert HTTP request contains `?q=` parameter (not `?tag=`).
  - Assert full query string is passed, not just `query.split()[0]`.

- `tests/test_sources_init.py`:
  - With 1 query and 2 registered adapter mocks, assert both adapters are called (fan-out).
  - With N queries × M sources, assert M×N total adapter calls.
  - Note: URL dedup is handled downstream by `validate_signals()`, not by `search_all_sources()`.

**Integration (existing `tests/test_irritator_orchestrator.py`):**
- Run `run_irritator()` with mocked LLM + mocked sources returning ≥1 signal per narrative.
- Assert `IrritatorStatus.level == "ok"` when ≥1 signal scores ≥ 5.
- Assert each adapter mock called once per query (fan-out count).

**What assertions matter most:**
- Adversarial token present in query prompt — this is the root fix; assert its presence.
- Fan-out count: `len(adapters) × len(queries)` calls — confirms coverage fix.
- dev.to `?q=` parameter — confirms structural fix.
- Default threshold = 5 in config — confirms threshold change.

**No e2e tests.** Real network calls are never made per `CLAUDE.md` constraints.

## 6. Risks and unknowns

1. **Funnel diagnosis from production data.** `digest-prod/config.yaml` already sets
   `min_signal_score: 5` (not 7), yet the 2026-04-24 digest has no Counter-Signals section —
   confirming the funnel empties before the ranker. The failure is at the search stage.
   This makes the adversarial-prompt + fan-out changes the load-bearing fixes; the threshold
   change in `config.py` default is now cosmetic for production but correct for consistency.

2. **Adversarial prompt may not hold under small LLMs.** Groq/DeepSeek free-tier models may
   still produce topical queries despite the instruction. The fix is behaviorally testable by
   running `python -m digest --verbose` and inspecting generated queries, but cannot be verified
   via unit tests without a live LLM call.

3. **Fan-out increases HTTP load ~5×.** From ~15 to ~75 adapter calls per run. GitHub Actions
   timeout is 6 hours — not a concern. `_SEMAPHORE_LIMIT=10` in `sources/__init__.py` throttles
   concurrency. Low risk.

4. **dev.to `?q=` API behaviour is unverified.** The text-search endpoint exists but result
   quality and rate limits are undocumented. May need a fallback if it returns empty or errors.
   Low-risk to ship: if dev.to returns nothing, that's the same as the current state.

5. **Lowering `min_signal_score` to 5 may admit mediocre signals.** Score 5–6 is "mildly
   relevant alternative perspective" — not the provocative counter the product promises. Acceptable
   as first step: better to show something and tune up than show nothing. Threshold is config, not
   code — easy to raise without a deploy.

6. **Removing `target_source` removes per-source query phrasing.** arXiv benefits from academic
   vocabulary; Reddit from colloquial phrasing. A single query string going to all sources may
   underperform source-specific queries. Known trade-off for this iteration; Option C addresses it
   if needed.

Closes #53
