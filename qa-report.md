## QA

**Tests:** All changed paths covered. 444 tests pass (`make check` clean). `ruff check digest/ tests/` clean. `mypy digest/` clean.

| Changed file | Test file | Key new assertions |
|---|---|---|
| `digest/irritator/query_generator.py` | `tests/test_query_generator.py` | No `target_source` field; adversarial tokens in EN+RU prompt; prompt sent to LLM contains adversarial markers |
| `digest/irritator/sources/__init__.py` | `tests/test_sources_init.py` | Fan-out: 1 query × 2 sources = 2 adapter calls; N queries × M sources = N×M calls; unconfigured adapter not called; query string passed verbatim |
| `digest/irritator/sources/devto.py` | `tests/test_sources_devto.py` | `?q=` param present; `?tag=` absent; full query string passed (not just first word) |
| `digest/irritator/ranker.py` | `tests/test_ranker.py` | Calibration anchors "9-10"/"direct evidence" in EN prompt; single criterion (no "substance"/"credibility" conjunction); score 5 passes threshold |
| `digest/config.py` | `tests/test_config.py` | `test_defaults_applied` updated (7→5); `test_irritator_config_default_min_signal_score` added asserting `IrritatorConfig().min_signal_score == 5` |
| `digest/delivery/telegram.py` | `tests/test_delivery_telegram.py` | `test_card_includes_async_feedback_note` — asserts `_ASYNC_FEEDBACK_NOTE` text present and card ends with `_` (italics) in MarkdownV2 payload (Claude-authored in issue #66 QA pass) |

One gap noted and accepted: `test_irritator_orchestrator.py:17` sets `min_signal_score = 7` as an explicit mock override. `rank_signals` is mocked in all orchestrator tests so the threshold is never evaluated. Not a coverage gap — orchestrator tests validate pipeline branching, not scoring behaviour.

**Docs:** `docs/domain/irritator-bc.md` was updated during #53 implementation. `CLAUDE.md` file-structure references remain accurate (file names unchanged). No runbook in `docs/runbooks/` references the changed files. No public contracts changed by the #66 telegram fix.
