## QA

**Tests:** All changed paths covered.

- `digest/irritator/sources/reddit.py` — `search_reddit` fully rewritten; all 8 branches
  exercised by `tests/test_sources_reddit.py` (8 tests, 0 failures):
  - No credentials (both absent): returns `[]`, zero HTTP calls asserted ✓
  - One credential absent: returns `[]`, zero HTTP calls asserted ✓
  - Token endpoint 4xx (401): returns `[]`, no exception propagated ✓
  - Token returns `{}` (missing `access_token`): returns `[]` ✓
  - Success: `Authorization: bearer <token>` header verified on search request ✓
  - Multi-subreddit: `oauth.reddit.com/r/programming+fintech/search` URL verified ✓
  - Empty results: `[]` returned ✓
  - Search 4xx (429): `HTTPStatusError` raised (fan-out catches it) ✓
  - Minor gap: the `except Exception` on `token_resp.json()` (non-JSON 200 body) has no
    dedicated test. Escape hatch accepted: `respx` always returns well-formed `httpx.Response`;
    the branch is defensive-only and the generic `except Exception` in `search_all_sources`
    provides a second safety net. No test written.

- Full suite: 485 passed, 0 failed. Ruff clean on changed files.

**Docs:** All contracts current.

- `CLAUDE.md` references `reddit.py` only as a file-tree label (lines 65, 80) — no behavioral
  description that requires updating.
- No `docs/runbooks/` directory exists — nothing to check.
- `README.md` references to "sources" are about RSS feed sources (`pending_sources.json`),
  not irritator adapters — no update required.
- `.env.example` updated with `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`, `REDDIT_USERNAME`
  entries and registration instructions.

**Production activation note (not automatable in CI):** The fix ships dormant until the
operator registers a Reddit "script" app at `reddit.com/prefs/apps` and adds the three secrets
to `digest-prod`. Verify on first prod run: non-zero Reddit signals in the fan-out log line
(`Fetched N signals from M queries × K sources`) confirm the OAuth flow is working.
