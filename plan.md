# Plan: Issue #29 — Per-article LLM summaries in Telegram cards + per-article markdown

## 1. Problem restatement

The Telegram delivery currently sends two overlapping things: long monolithic category-summary texts (via `send_radar()`, which is now dead code) plus per-article cards with voting buttons. The cards use raw article descriptions (≤200 chars) as the preview text rather than LLM-generated summaries. The result is duplication, walls of text, and no feedback mechanism on the narrative analysis. The fix is to send only the per-article cards — each with a 2-3 sentence LLM summary — and update the Obsidian markdown file to match the same per-article structure.

**Key code-audit finding:** The majority of the issue's solution is already implemented in the current codebase:
- `ArticleSummary` and `CategorySummary` dataclasses exist in `digest/radar/summarizer.py`
- `pick_top_articles()` already calls LLM to select and summarize top articles as structured JSON
- `send_article_cards()` already accepts `top_articles: list[ArticleSummary]` and sends per-article posts with LLM summaries and voting buttons
- `main.py` already calls both and wires them together
- `send_radar()` is already removed from the active pipeline — it remains as dead code in `telegram.py`

The actual delta is small: (a) remove the dead `send_radar()` function, (b) update `write_digest()` in `markdown.py` to include a per-article section when `top_articles` are present, (c) wire `top_articles` into the `write_digest()` call in `main.py`.

## 2. Affected bounded contexts and files

**Bounded Context: Digest** (single BC; `docs/domain/digest/overview.md`)

Aggregates / concepts touched:
- **CategorySummary / ArticleSummary** — Radar→Delivery contract (already in place)
- **Delivery** — `markdown.py` output format, `send_radar()` dead-code removal

| File | Change |
|------|--------|
| `digest/delivery/telegram.py` | Remove `send_radar()` function (dead code — not called from pipeline) |
| `digest/delivery/markdown.py` | Add per-article section to `write_digest()` when `top_articles` provided |
| `digest/main.py` | Pass `top_articles` to `write_digest()` |
| `digest/delivery/__init__.py` | Remove `send_radar` from exported symbols if present |
| `tests/test_delivery_telegram.py` | Remove `send_radar` tests; verify they exist and what to do |
| `tests/test_delivery_markdown.py` | Add test for per-article markdown section |

**Not changed:**
- `digest/radar/summarizer.py` — `ArticleSummary`, `pick_top_articles()` already done
- `config.yaml` (digest-prod) — perspectives removal is `config.radar.perspectives: false`, out of scope per ADR-0002

## 3. Considered approaches

### Approach A — Remove `send_radar()` + add per-article to `write_digest()`

Add an optional `top_articles: list[ArticleSummary] | None` parameter to `write_digest()`. When present, append a `## Top Articles` section with per-article summaries in Obsidian callout format. Keep the existing `combined` (category summaries) as the primary body — Irritator still uses it for narrative extraction, and the long format is useful for Obsidian search/indexing.

**Trade-offs:**
- ✓ Minimal: only two code changes needed
- ✓ Keeps `combined` for Irritator (which extracts narratives from category summaries)
- ✓ Obsidian file becomes richer — both overview and per-article detail
- ✗ Obsidian file contains both category text and per-article section — some redundancy in the file itself

### Approach B — Replace `combined` with per-article-only markdown

Generate the Obsidian file solely from `top_articles`, dropping `combined` from the file. `summarize_all()` output is still needed for Irritator but not written to disk.

**Trade-offs:**
- ✓ No redundancy in the markdown file
- ✗ Loses the category-level analytical overview in Obsidian (useful for trend analysis)
- ✗ Breaks downstream consumers that read the markdown format (e.g., any personal notes referencing category headers)
- ✗ Larger diff — need to change `main.py` to pass per-article list to `write_digest()` instead of `combined`

## 4. Chosen approach and why

**Approach A.** The category summaries from `summarize_all()` serve dual purpose: Irritator needs them for narrative extraction, and they provide context that per-article summaries alone cannot. Adding a per-article section to the Obsidian file is additive and backwards-compatible. Removing `send_radar()` is pure cleanup with no behavioral change.

No ADR triggered. `docs/principles.md` ADR criteria:
- No new cross-cutting dependency
- No BC boundary change — same BC, same data flow, same contract (already in place)
- No new storage or infrastructure
- No public API change
- No hard-to-reverse constraint

**Perspectives removal:** Already supported via `config.radar.perspectives: false`. Operator should set this in `digest-prod/config.yaml`. No engine code change.

## 5. Test strategy

### Unit — `tests/test_delivery_telegram.py`

- Find and handle existing `send_radar` tests: if they exist, remove them (the function is being deleted).
- No new telegram tests needed — `send_article_cards` with `top_articles` is already covered.

### Unit — `tests/test_delivery_markdown.py`

- Add `test_write_digest_with_top_articles` — verifies that when `top_articles` is a non-empty list of `ArticleSummary`, the output markdown contains a `## Top Articles` section with each article's title, summary, and source.
- Add `test_write_digest_without_top_articles` — existing behavior unchanged when `top_articles` is None or empty.

### No integration or e2e

Both changes are pure output-format changes; all dependencies are mockable. E2e not needed.

### Coverage target

≥ 70% floor (currently 79%). New tests add coverage to the `top_articles` branch in `write_digest()`.

## 6. Risks and unknowns

1. **`send_radar` test impact** — there may be existing tests for `send_radar` in `test_delivery_telegram.py`. Deleting the function without removing the tests will break CI. Must check before deleting.

2. **`send_radar` in `__init__.py` exports** — if `send_radar` is re-exported from `digest/delivery/__init__.py`, the deletion needs to propagate there too. Grep required before commit.

3. **Markdown section duplication** — if `top_articles` contains the same articles that appear in `combined`, the Obsidian file will have redundant content. This is acceptable (the formats differ: combined is analytical narrative, per-article is standalone summaries), but worth documenting.

4. **`pick_top_articles()` failure** — if the LLM call fails, `top_articles` is `[]`. In this case `write_digest()` should gracefully omit the per-article section (no empty heading). The implementation must handle `top_articles = []` silently.

5. **Perspectives in prompts** — `summarize_all()` currently respects `config.radar.perspectives`. Setting it to `false` in `digest-prod/config.yaml` is the correct mechanism. No code change needed, but the PR description must document this as the action for the operator.

6. **`write_digest()` call in `main.py`** — wiring `top_articles` through to `write_digest()` is required; without it, the new parameter is dead code.

---

*Closes #29*
