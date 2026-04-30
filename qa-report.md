## QA

**Tests:** All changed paths covered.

- `digest/irritator/sources/lobsters.py` — `_STRIP_RE`, `_sanitize_query()`, empty-query guard,
  and sanitized `search_lobsters` exercised by `tests/test_sources_lobsters.py` (17 tests, 0 failures):

  *TestSanitizeQuery (direct unit tests — no HTTP):*
  - `test_strips_operators` — `"AI: failure (2024)"` → `"AI failure 2024"` ✓
  - `test_keeps_apostrophes` — apostrophe preserved through sanitization ✓
  - `test_collapses_whitespace` — multiple spaces collapsed ✓
  - `test_unicode_cyrillic` — Cyrillic words kept, colon stripped ✓
  - `test_all_operators_returns_empty` — `":::"` → `""` ✓
  - `test_empty_input` — empty string → empty string ✓

  *TestSearchLobsters (integration through adapter):*
  - `test_query_sanitized_before_send` — captures `request.url.params["q"]`; asserts
    `"AI: failure (2024)"` arrives as `"AI failure 2024"` ✓
  - `test_empty_query_skips_http` — `":::"` sanitizes to `""`; asserts `respx.calls.call_count == 0` ✓
  - `test_blank_query_skips_http` — `""` input; asserts no HTTP call made ✓
  - Existing 8 tests (success paths, 400/429/500 errors, semaphore concurrency) all pass ✓

- Full suite: 496 passed, 0 failed. Ruff and mypy clean on changed files.

**Docs:** All contracts current.

- `CLAUDE.md` references `lobsters.py` as a file-tree label only — no behavioral description requiring update.
- No `docs/runbooks/` entries for the Lobsters adapter.

**Merge gate (AC requirement):** Root-cause confirmation requires a manual curl test before merge:
```bash
# operator-char query — note the HTTP status code:
curl -s -o /dev/null -w "%{http_code}\n" \
  'https://lobste.rs/search.json?q=%22AI+governance%3A+failure%22&what=stories&order=relevance'
# sanitized form — should 200:
curl -s -o /dev/null -w "%{http_code}\n" \
  'https://lobste.rs/search.json?q=AI+governance+failure&what=stories&order=relevance'
```
If both return 200, ship as defensive hardening and note in PR thread.
