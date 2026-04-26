# Plan: Issue #28 — Irritator visibility and status

## 1. Problem restatement

The Irritator pipeline runs silently: when no counter-signals survive ranking, the user receives nothing and cannot tell whether the pipeline ran at all, failed partway through, or simply found nothing relevant. The issue identified three root causes, but a code audit reveals that two of them are **already fixed in the current codebase**: `_run_irritator()` already has per-stage granular error handling (stages 1-5 each have individual try/except and detailed status strings), and `send_counter_signals()` already renders the "💢🔥 РАЗДРАЖАТОР 🔥💢" header with the desired per-signal format. What remains is a small delta:

1. The dry-run path (`main.py:648-662`) does not print `irritator_status` when `all_ranked` is empty — so `--verbose --dry-run` gives no Irritator feedback.
2. The empty-state status message in `send_counter_signals()` is always sent with `disable_notification=True` — invisible unless Telegram is open.
3. Stage 4 (`validate_signals` at `main.py:451`) has no try/except, unlike stages 1, 2, 3, 5.

**Out of scope:** Lowering `min_signal_score` — the default of 7 is engine-level in `config.py`; the correct place to change it is `digest-prod/config.yaml`. This PR notes the recommendation in the PR body but does not change code.

## 2. Affected bounded contexts and files

**Bounded Context: Digest** (single BC; `docs/domain/digest/overview.md`)

Aggregates touched:
- **Narrative / CounterSignal** (Irritator BC-internal) — no invariant change, only observability
- **Delivery** aggregate — `send_counter_signals()` notification loudness

| File | Change |
|------|--------|
| `digest/main.py` | (a) Print irritator status in dry-run path unconditionally (always shown, even with signals — cleaner UX); (b) add try/except around stage 4 `validate_signals` |
| `digest/delivery/telegram.py` | Replace `disable_notification=True` blanket flag with level-driven logic |
| `digest/source_scorer.py` | No change |
| `tests/test_delivery_telegram.py` | Assert notification loudness per level |
| `tests/test_main.py` | Assert dry-run stdout contains irritator status |

## 3. Considered approaches

### Approach A — String heuristic for notification loudness

Emit `disable_notification = "failed" not in irritator_status`. Simple, zero new types.

**Trade-offs:**
- ✓ Minimal diff
- ✗ Fragile: "all signals filtered by blocklist" (main.py:458) does not contain "failed" but is arguably error-adjacent. Any future status copy-edit silently flips notification loudness. Not mitigation; wishful thinking.

### Approach B — Structured `IrritatorStatus` return (chosen)

Change `_run_irritator` return type from `tuple[list, list, str]` to `tuple[list, list, IrritatorStatus]` where:

```python
@dataclass
class IrritatorStatus:
    text: str
    level: Literal["ok", "empty", "error"]
```

- `level="error"` — pipeline stage failed with exception
- `level="empty"` — pipeline ran fully, nothing survived filters/ranking
- `level="ok"` — ranked signals exist (status sent alongside them)

`send_counter_signals` uses `status.level` to set `disable_notification`: loud on `"error"`, silent on `"empty"`, not needed on `"ok"`.

**Trade-offs:**
- ✓ Correct: loudness is explicit, not inferred from string content
- ✓ Two callsites change (`_run_irritator` and `send_counter_signals`); manageable
- ✓ `IrritatorStatus` is a natural domain concept — Delivery shouldn't parse Irritator's error strings
- ✗ One new type; mypy must see it in both modules (put in `digest/main.py`, import in telegram.py via `TYPE_CHECKING` or inline)

## 4. Chosen approach and why

**Approach B.** String heuristics that cross module boundaries are an anti-pattern: Delivery should not need to parse Irritator's error prose to decide notification loudness. The domain already has `CounterSignal` and `Narrative` as explicit types; `IrritatorStatus.level` is the same principle applied to observability.

No ADR triggered. Checking `docs/principles.md` criteria:
- No new cross-cutting dependency
- No BC boundary change
- No new storage or infrastructure component
- No public API change
- No hard-to-reverse constraint

Story-level fix.

**Stage 4 try/except:** Add a guarded call around `validate_signals` for consistency with stages 1-3; return `level="error"` if it raises. `validate_signals` does simple keyword filtering and is unlikely to raise, but unprotected I/O-adjacent code is a latent risk.

**`min_signal_score` default:** Not changed in this PR. Recommend in PR body that `digest-prod/config.yaml` adds `min_signal_score: 5`.

## 5. Test strategy

### Unit — `tests/test_delivery_telegram.py`

Extend `TestSendCounterSignals`:
- `test_empty_signals_sends_status_silent` — `IrritatorStatus("2 narratives, 0 signals", "empty")` → sent with `disable_notification=True`
- `test_empty_signals_error_sends_loud` — `IrritatorStatus("query generation failed: timeout", "error")` → sent WITHOUT `disable_notification` (assert key absent in POST body)

Implementation detail: inspect `json.loads(route.calls[0].request.content)` for `disable_notification` field.

### Unit — `tests/test_main.py`

Add a test that patches `_run_irritator` to return `([], [], IrritatorStatus("2 narratives, 0 signals", "empty"))` and calls `run(dry_run=True)`; assert `capsys.readouterr().out` contains the status text.

### No integration or e2e

All external calls are mocked. E2e not needed.

### Coverage target

≥ 70% floor (current: 79%). New tests cover the telegram.py empty-state branch (notification flag) and main.py dry-run irritator path.

## 6. Risks and unknowns

1. **`IrritatorStatus` import path** — `send_counter_signals` in `telegram.py` needs the type. Options: (a) put `IrritatorStatus` in `digest/_util.py` or a new `digest/irritator_types.py`; (b) use `TYPE_CHECKING` guard + string annotation; (c) inline `Any` and trust the `level` attribute. Option (a) is cleanest — one import, no forward reference. Verify no circular import before committing.

2. **Dry-run print ordering** — `_run_irritator` already prints verbose narrative/signal detail inside itself when `verbose=True`. The new dry-run status print should not duplicate verbose detail. Mitigation: the status print is always shown; verbose detail remains inside `_run_irritator`.

3. **Stage 4 try/except adds another error return path** — callers expect `(narratives, [], IrritatorStatus)` on error. All returns already follow this pattern; just add one more.

4. **`min_signal_score` recommendation** — digestprod operator may not notice the PR body note. Mitigation: add a `logger.warning` when `config.irritator.min_signal_score > 5` suggesting the operator consider lowering it. Non-blocking, one line.

---

*Closes #28*
