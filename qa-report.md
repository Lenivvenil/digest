## QA

**Tests:** All changed paths covered. 431 tests pass. `ruff check digest/ tests/` clean. `mypy digest/` clean.

| Changed file | Test file | Key new assertions |
|---|---|---|
| `digest/irritator/query_generator.py` | `tests/test_query_generator.py` | No `target_source` field; adversarial tokens in EN+RU prompt; prompt sent to LLM contains adversarial markers |
| `digest/irritator/sources/__init__.py` | `tests/test_sources_init.py` | Fan-out: 1 query × 2 sources = 2 adapter calls; N queries × M sources = N×M calls; unconfigured adapter not called; query string passed verbatim |
| `digest/irritator/sources/devto.py` | `tests/test_sources_devto.py` | `?q=` param present; `?tag=` absent; full query string passed (not just first word) |
| `digest/irritator/ranker.py` | `tests/test_ranker.py` | Calibration anchors "9-10"/"direct evidence" in EN prompt; single criterion (no "substance"/"credibility" conjunction); score 5 passes threshold |
| `digest/config.py` | `tests/test_config.py` | `test_defaults_applied` updated (7→5); `test_irritator_config_default_min_signal_score` added asserting `IrritatorConfig().min_signal_score == 5` |

One gap noted and accepted: `test_irritator_orchestrator.py:17` sets `min_signal_score = 7` as an explicit mock override. `rank_signals` is mocked in all orchestrator tests so the threshold is never evaluated. Not a coverage gap — orchestrator tests validate pipeline branching, not scoring behaviour.

**Docs:** `docs/domain/irritator-bc.md` was stale (authored by domain-researcher pre-implement; described old `target_source` dispatch and `min_signal_score=7`). Patched inline with `[UPDATED — issue #53]` and `[REMOVED — issue #53]` markers:

- `SearchQuery` dataclass description: `{query, target_source, intent}` → `{query, intent}`
- Config inbound table: `min_signal_score=7` → `min_signal_score=5`
- Internal model diagram: `target_source assigned by LLM` → fan-out description
- Internal model diagram: `≥ min_signal_score (=7)` → `(=5)`
- Business rules: removed `target_source` invariant; updated default threshold
- Doc header: added provenance note and `[UPDATED]` scope explanation

*Note: domain-reviewer pass recommended before merge — the "Where the BC breaks" section now describes fixed failures and may need pruning or relabelling as historical record.*

`CLAUDE.md` file-structure references remain accurate (file names unchanged). No runbook in `docs/runbooks/` references the changed files.
