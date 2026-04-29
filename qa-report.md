## QA

**Tests:** All changed paths covered.

- `digest/irritator/sources/lobsters.py` — `_get_semaphore()` and semaphore-wrapped `search_lobsters`
  exercised by `tests/test_sources_lobsters.py` (8 tests, 0 failures):
  - `test_success_list_format` — happy path, list response format ✓
  - `test_success_dict_format` — dict/`results` response format ✓
  - `test_fallback_to_short_id_url` — URL fallback logic ✓
  - `test_empty_results` — empty list response ✓
  - `test_http_error_raises` — 500 raises `HTTPStatusError` ✓
  - `test_429_raises` — 429 raises `HTTPStatusError` (fan-out catches it upstream) ✓
  - `test_400_raises` — 400 raises `HTTPStatusError` ✓
  - `test_semaphore_limits_concurrency` — 5 concurrent callers; asserts peak in-flight == 1 ✓
    (mock handler uses `await asyncio.sleep(0.01)` — load-bearing yield so event loop
    actually switches tasks; removing it would make the test pass trivially against a broken impl)
  - `autouse` fixture resets `_semaphore = None` before each test — prevents event-loop
    cross-contamination between pytest-asyncio test runs ✓

- Full suite: 488 passed, 0 failed. Ruff and mypy clean on changed files.

**Docs:** All contracts current.

- `CLAUDE.md` references `lobsters.py` as a file-tree label only (lines 65, 79) — no behavioral
  description requiring update.
- No `docs/runbooks/` entries for the Lobsters adapter — nothing to check.

**Open item (not blocking):** 400 responses from Lobsters are deferred to a follow-up issue
(root cause — query length vs. special characters — is unconfirmed). Note this in the PR body.
