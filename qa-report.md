## QA

**Tests:** all changed paths covered.

| File | Test file | Changed symbols covered? |
|------|-----------|--------------------------|
| `digest/irritator/__init__.py` | `tests/test_delivery_telegram.py`, `tests/test_main.py` | `IrritatorStatus` — instantiated in 5 new tests ✓ |
| `digest/main.py` | `tests/test_main.py` | `_run_irritator` dry-run path — `test_dry_run_prints_irritator_status` ✓ |
| `digest/delivery/telegram.py` | `tests/test_delivery_telegram.py` | `send_counter_signals` empty/error branches — `test_empty_signals_sends_status_silent`, `test_empty_signals_error_sends_loud` ✓ |

Coverage: 76% total (floor: 70% ✓). `irritator/__init__.py`: 100%, `telegram.py`: 93%, `main.py`: 54% (orchestrator — expected, unchanged from baseline).

**Docs:** all contracts current.

- `CLAUDE.md` lists `digest/irritator/` and `digest/delivery/` — descriptions unchanged (no new public CLI flags, no new files visible to users).
- `docs/backlog/grooming-2026-04-24.md` references `_run_irritator` and `send_counter_signals` — the grooming doc noted these as partially done; this PR completes them. The doc is historical/read-only, no update needed.
- No runbooks in `docs/runbooks/` exist for these modules.
