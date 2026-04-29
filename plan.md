# Plan: fix Lobsters 429 on parallel fan-out (#63)

## 1. Problem restatement

The fan-out introduced in #53 fires one `(query, source)` task per combination. With 15 queries and Lobsters as one of five sources, up to 10 concurrent HTTP requests can land on `lobste.rs/search.json` at once — the global `Semaphore(10)` in `search_all_sources` caps total in-flight across all sources at 10, so in the worst case all 10 slots are held by Lobsters. Lobsters rate-limits aggressively and returns 429 on most of them. A separate class of 400 responses also appears; the cause is unknown and out of scope for this fix (see §6). Both failure modes are silently swallowed by the graceful-degradation wrapper, so the adapter contributes nothing to the digest without any actionable alert.

## 2. Affected bounded contexts and files

**Irritator BC** (`docs/domain/irritator/`):

| File | Role |
|------|------|
| `digest/irritator/sources/lobsters.py` | Lobsters adapter — primary change target |
| `digest/irritator/sources/__init__.py` | Fan-out orchestrator; global semaphore lives here (read-only for this fix) |
| `tests/test_sources_lobsters.py` | Unit tests for the adapter |
| `tests/test_sources_init.py` | Integration path for `search_all_sources` graceful degradation |

No BC boundary changes. Delivery and Radar are unaffected.

## 3. Considered approaches

### A. Per-adapter self-contained semaphore in `lobsters.py` ← chosen

Add a module-level `asyncio.Semaphore` (limit 1) inside `lobsters.py`, created lazily on first call inside the running event loop. The adapter acquires it before each HTTP request, fully serialising Lobsters calls without touching any other adapter or the orchestrator.

`Semaphore(1)` is chosen over a higher limit because: (a) Lobsters' rate limit is undocumented and empirically aggressive; (b) the digest runs once daily with ~15 Lobsters calls — fully serial worst-case is ~15 × `_TIMEOUT` = 15 s added to pipeline wall-clock, which is acceptable; (c) any higher value is a guess without measurement. If a follow-up profiling run shows this is a bottleneck, the limit can be raised with evidence.

The 400 responses are **out of scope** for this PR — the root cause (query length vs. special characters) is unconfirmed. A separate issue will be filed to investigate and reproduce. Bundling an unverified fix with a verified one makes post-merge attribution ambiguous.

**Trade-offs:**
- (+) Zero blast radius — other adapters and `search_all_sources` are untouched.
- (+) Constraint lives next to the code that causes it — self-documenting.
- (+) Per-adapter tuning without touching fan-out logic.
- (−) Module-level mutable state; tests must reset `_semaphore = None` between runs (standard `monkeypatch`).
- (−) Semaphore must be created inside a running event loop — lazy init handles this, adds minor ceremony.

### B. Per-host semaphore map in `search_all_sources`

Maintain a `dict[str, asyncio.Semaphore]` keyed by source name in the orchestrator, with per-source concurrency limits from config or hardcoded defaults.

**Trade-offs:**
- (+) All rate-limit policy visible in one place.
- (−) Adds non-trivial complexity to the orchestrator for a problem that currently affects only one adapter.
- (−) Invites per-source config-surface creep (`config.yaml` fields for each adapter's concurrency).
- (−) No benefit for adapters that already self-manage (e.g. Reddit OAuth).

### C. Retry with exponential backoff in `lobsters.py`

Catch `httpx.HTTPStatusError` for 429, sleep, retry up to N times.

**Trade-offs:**
- (+) Handles transient single-request blips.
- (−) Does not prevent the burst — 15 requests fire simultaneously, most get 429, then retries add wall-clock latency.
- (−) Compounds total pipeline runtime; does not address the 400 problem.
- (−) Cures the symptom, not the cause.

**Verdict:** A is the correct fix. B is over-engineering for one adapter. C solves the wrong problem.

## 4. Chosen approach and why

**Option A — per-adapter semaphore (limit 1) in `lobsters.py`.**

- The issue is Lobsters-specific; fixing it in the adapter keeps the change minimal and contained.
- `Semaphore(1)` is the only choice that is guaranteed to prevent the burst, requires no empirical measurement, and keeps the fix unambiguous. Reasoning documented above in §3A.
- 400 responses are deferred to a follow-up issue.
- ADR check against `docs/principles.md`: no new cross-cutting dependency, no BC boundary change, no infrastructure component, no public API change, no security model change. **Not architecturally significant. No ADR required.**

Implementation sketch:

```python
# lobsters.py
import asyncio

_semaphore: asyncio.Semaphore | None = None

def _get_semaphore() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(1)
    return _semaphore

@_register("lobsters")
async def search_lobsters(query: str, config: Any, client: httpx.AsyncClient) -> list[Signal]:
    async with _get_semaphore():
        resp = await client.get(
            _BASE_URL,
            params={"q": query, "what": "stories", "order": "relevance"},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        ...
```

## 5. Test strategy

**Unit tests** (new/updated in `tests/test_sources_lobsters.py`):

| Test | Assertion |
|------|-----------|
| `test_semaphore_limits_concurrency` | Launch N > 1 concurrent calls; mock handler uses `await asyncio.sleep(0)` to yield so concurrency actually manifests; assert peak in-flight count never exceeds 1 (shared counter via `monkeypatch`). Without the sleep, the event loop never switches and the test passes trivially against a broken implementation. |
| `test_429_raises` | Mock returns 429; assert `HTTPStatusError` propagates from adapter (fan-out catches it upstream) |
| `test_400_raises` | Mock returns 400; same assertion |
| `test_semaphore_reset_between_tests` | Use `monkeypatch` to reset `lobsters._semaphore = None` between tests; verify no cross-test state leak |

**Integration** (existing `tests/test_sources_init.py`):

- Verify `search_all_sources` returns non-empty when Lobsters raises (graceful degradation). Existing test covers this; confirm it passes unchanged with the new semaphore.

**e2e:** Not applicable — no real network calls in tests.

## 6. Risks and unknowns

| Risk | Likelihood | Mitigation |
|------|-----------|------------|
| Lobsters rate limit is time-window-based (not concurrency-based) — serial calls fired quickly may still 429 | Low | `Semaphore(1)` guarantees at most one in-flight at a time; each call takes up to `_TIMEOUT=10s`, giving natural pacing. If 429 persists, add `asyncio.sleep(1)` inside semaphore context (one-line follow-up) |
| Lazy semaphore init not thread-safe | Low | Pipeline runs in a single event loop; no threading. Non-issue in practice. |
| 400 responses unresolved by this PR | Medium | Deferred to a follow-up issue. The 429 fix is the verified problem; 400 root cause is unknown. |
| Lobsters search response format changes (API is undocumented) | Low | Already handled by `isinstance(data, list)` branch; no new exposure |
