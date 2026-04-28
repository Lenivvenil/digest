## QA

**Tests:** All changed paths covered.

- `digest/source_scorer.py` — `_diversity_score()` and `compute_bubble_report()`: covered by `tests/test_bubble_analytics.py` (14 unit tests: empty data, single source, equal sources, concentrated, 5-equal, 7-day window, empty report, known diversity, 14d feedback window, last_digest_time parsing, invalid timestamp, source health, 4096-char limit, top-5 cap).
- `digest/feedback.py` — `/bubble` handler in `collect_feedback()`: covered by 3 integration-style tests (sends report, empty cache no crash, send failure swallowed). Signature change (`cache_dir` kwarg) is backward-compatible; all 37 existing `test_feedback.py` tests still pass.
- `digest/main.py` — trivial one-line kwarg pass-through (`cache_dir=cache_dir`); exercised indirectly by the `collect_feedback` tests. No dedicated `test_main.py` test for this line — gap accepted (change is a mechanical kwarg threading, no logic).

Full suite: **449 passed**, ruff clean, mypy clean.

**Docs:** `docs/ARCHITECTURE.md` updated — added `/bubble` section under "Bot commands" alongside existing `/status` entry. Authored by Claude as part of QA pass.
