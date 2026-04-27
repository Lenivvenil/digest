## QA

**Tests:** All changed paths covered.

- `digest/feedback.py` — changed function `collect_feedback()`. Test file `tests/test_feedback.py` covers the new branch:
  - `test_collect_feedback_per_article_unknown_hash_records_empty_source` (modified) — asserts `WARNING` fires and contains `"article_source_map miss"` and hash `"deadbeef"` when map lookup misses. ✓
  - `test_collect_feedback_per_article_good` (modified) — negative assertion: `"article_source_map miss"` must NOT appear in caplog when source is found. ✓
  - 37/37 tests pass.

**Docs:** All contracts current.

- No CLI flags changed.
- No `docs/runbooks/` directory exists in the repo.
- `CLAUDE.md` does not reference `feedback.py` or `collect_feedback` — no update needed.
- `docs/domain/digest/overview.md` describes `FeedbackReceived` and hot spot #3 correctly; the new `logger.warning()` is an internal implementation detail, not a domain contract change.
