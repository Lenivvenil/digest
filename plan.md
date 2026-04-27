# Plan: Issue #6 — Phase 5: Validator + Ranker

## 1. Problem Restatement

Most of the Irritator Phase 5 work has already landed in prior commits: `validator.py` deduplicates and blocklist-filters signals, `ranker.py` wraps LLM scoring into a typed `RankedSignal` dataclass, and unit tests for both exist. Two acceptance criteria remain open:

1. **Optional HEAD-based URL liveness check** — `validator.py` validates URL syntax but never hits the network to confirm a URL is live. The issue spec calls for this as an optional step.
2. **Public `run_irritator()` orchestrator** — the five-stage pipeline (extract → generate → search → validate → rank) currently lives in `main.py` as a private `_run_irritator()` function, making it untestable and architecturally misplaced relative to the module boundary the issue specifies.

A third minor gap: the `config` schema has no `check_liveness` flag, so liveness checking cannot be toggled from `config.yaml` today.

---

## 2. Affected Bounded Contexts and Files

**BC: Digest / Irritator** (counter-signal subdomain — all changes stay within this BC)

| File | Change |
|------|--------|
| `digest/irritator/validator.py` | Add `async def validate_signals_async(signals, blocklist, client, check_liveness=False) -> list[Signal]` |
| `digest/irritator/__init__.py` | Add `async def run_irritator(summaries, config, client, *, verbose=False) -> tuple[list[Narrative], list[RankedSignal], IrritatorStatus]`; `client: AsyncClient` is caller-managed |
| `digest/main.py` | Replace `_run_irritator()` body with a call to the new public `run_irritator()`; `httpx.AsyncClient` context wraps the full call |
| `digest/config.py` | Add `check_liveness: bool = False` to `IrritatorConfig` dataclass AND to `_load_irritator()` parser |
| `tests/test_validator.py` | Add async HEAD-path cases: 200 keeps, 404/503/timeout drops, 405 keeps, `check_liveness=False` never calls client |
| `tests/test_irritator_orchestrator.py` (new) | Smoke-test `run_irritator()` with all stages mocked; assert IrritatorStatus level per branch |

---

## 3. Considered Approaches

### Approach A — Async liveness wrapper; orchestrator extracted to `__init__.py` (chosen)

Add `validate_signals_async()` that calls the existing sync `validate_signals()` first, then optionally fires HEAD requests via an injected `httpx.AsyncClient`. `run_irritator()` goes into `__init__.py` and replaces the inline body in `main.py` (which becomes a thin wrapper).

**Pros:** sync path untouched and its tests remain non-async; async path testable via mock client; clean I/O separation; orchestrator is now publicly importable.
**Cons:** two validate functions to maintain; callers must choose which to call.

### Approach B — Augment sync `validate_signals()` with async flag

Make the existing function async and add `client: httpx.AsyncClient | None = None` and `check_liveness: bool = False` params.

**Cons:** forces every caller to `await` a function that may do no I/O. Breaks all existing sync tests without `pytest-asyncio`. Violates single-responsibility. **Wrong path.**

### Approach C — Skip HEAD check (config-gated no-op)

Ship a `check_liveness` flag always set to `False` in config, documenting it as future work.

**Cons:** Ships dead code, violates the issue's explicit checklist. Not acceptable.

**Verdict: Approach A.**

---

## 4. Chosen Approach and Why

**Approach A**, consistent with the project style (`async/await` for all I/O, dependency injection for HTTP client) and ADR-0002 (engine repo — code correctness over runtime convenience).

Implementation specifics:

- `validate_signals_async(signals, blocklist, client, check_liveness=False)`: always-wrapper design — calls `validate_signals()` first (sync dedup + blocklist), then when `check_liveness=True` fires HEAD requests in parallel via `asyncio.gather(*[_head_check(sem, client, s) for s in valid])`. `asyncio.Semaphore(10)` bounds concurrency. Per-signal: drops on status ≥ 400 or `httpx.TransportError`/`httpx.TimeoutException`; keeps on 405 (HEAD-rejected ≠ dead URL). Parallel execution means 50 signals × 5 s timeout stays bounded to ~5 s wall time.
- `run_irritator(summaries, config, client, *, verbose=False)` in `__init__.py`: `client: httpx.AsyncClient` is injected by the caller (makes orchestrator testable without network fakes). Calls `validate_signals_async(..., client, check_liveness=config.irritator.check_liveness)`. Logger at module level. `narrative.claim[:60]` preserved in ranking error log.
- `main._run_irritator()` becomes a thin wrapper: creates `async with httpx.AsyncClient() as client` wrapping the full `run_irritator()` call (not just the search stage as before, since liveness checks also need the client).
- `IrritatorConfig` gets `check_liveness: bool = False`; `_load_irritator()` adds isinstance-guarded parse matching the `_load_adaptive.enabled` pattern.
- **`narrative_title` vs `narrative_claim`:** issue spec says `narrative_title` but the existing `RankedSignal` field is `narrative_claim` (matches `Narrative.claim`). Spec wording is stale; `narrative_claim` is correct and stays. No rename.

No ADR required: no new dependencies (httpx already cross-cutting per ADR-0002 context), no BC boundary changes, no storage, no security model change.

---

## 5. Test Strategy

**Unit (no network):**

`tests/test_validator.py` — new async cases (use `AsyncMock` for `client.head`):
- `check_liveness=True`, mock HEAD → 200: signal kept
- `check_liveness=True`, mock HEAD → 404: signal dropped
- `check_liveness=True`, mock HEAD → 503: signal dropped
- `check_liveness=True`, mock HEAD raises `httpx.TimeoutException`: signal dropped
- `check_liveness=True`, mock HEAD → 405: signal kept (HEAD-rejected ≠ dead)
- `check_liveness=False`: `client.head()` never called

`tests/test_irritator_orchestrator.py` (new file):
- Mock all five sub-functions; assert `run_irritator()` returns `(list[Narrative], list[RankedSignal], IrritatorStatus)`
- Narrative extraction failure → `IrritatorStatus(level="error")`
- Empty narratives → `IrritatorStatus(level="empty")`
- All signals filtered → `IrritatorStatus(level="empty")`
- Ranking produces results → `IrritatorStatus(level="ok")`

**Assertions that matter:**
- `level == "ok"` only when `len(all_ranked) > 0`
- `level == "error"` only when a stage raised
- Per-narrative ranking failure does not set `level = "error"`
- HEAD 405 is not treated as a dead URL

**Integration (manual gate):**
```bash
python -m digest --dry-run
```

---

## 6. Risks and Unknowns

1. **HEAD rejection masking dead URLs** — 405 means server rejects HEAD method; keep-on-405 heuristic cannot distinguish live-HEAD-rejecting from dead. Accept; GET fallback is future work.
2. **`asyncio.Semaphore` tuning** — default 10 is conservative; can tune in follow-up.
3. **`_load_irritator()` must parse `check_liveness`** — easy to miss; must add to both dataclass and parser return call.
4. **`test_config.py` update** — `check_liveness` boolean parsing needs a test case.

Closes #6
