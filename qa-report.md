## QA

**Tests:** All changed paths covered.

- `digest/irritator/validator.py` — `validate_signals_async` and `_head_check`: 11 new async cases in `tests/test_validator.py` (200 keep, 404/503 drop, timeout drop, transport-error drop, 405 keep, `check_liveness=False` skips HEAD, blocklist applied before liveness, empty input, parallel multi-signal). All assertions on the same mock instance.
- `digest/irritator/__init__.py` — `run_irritator()`: 11 new cases in `tests/test_irritator_orchestrator.py` (extraction failure → error, empty narratives → empty, query failure → error, empty queries → empty, search failure → error, no signals → empty, all filtered → empty, ranking success → ok, per-narrative ranking failure → empty not error, typed return, client threading assertion confirms injected client reaches `search_all_sources` and `validate_signals_async`).
- `digest/config.py` — `check_liveness` field: 3 new cases in `tests/test_config.py` (default False, explicit true, invalid string raises ValueError with correct message).
- `digest/main.py` — `_run_irritator()` refactored to thin wrapper; covered indirectly via existing `test_main.py` + the orchestrator tests above.

411/411 tests pass. Lint (`ruff`) and type-check (`mypy`) clean.

**Docs:** Two CLAUDE.md entries updated (Claude-authored):

- `validator.py` description updated from "filter out blocklisted content" to "dedup, blocklist filter, optional URL liveness check".
- `tests/test_irritator_orchestrator.py` added to the test file listing in the project structure tree.

No runbooks reference `validator.py` or `run_irritator()`. `docs/backlog/grooming-2026-04-24.md` references `_run_irritator` by symbol name — still accurate as the private wrapper remains.
