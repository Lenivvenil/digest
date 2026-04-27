# Plan: Issue #48 — Verify feedback voting buttons persist ratings

## 1. Problem restatement

Telegram article cards are sent with 👍/👎 inline buttons, but no one has
confirmed that button taps actually reach `feedback.json` in `.cache/` and
that `get_source_feedback_score()` can read them back on the next pipeline
run. Code inspection shows the full path EXISTS — `collect_feedback()` polls
`getUpdates`, parses `fb:a:{g|b}:HASH` callbacks, looks up the source from
`article_source_map`, appends an `ArticleFeedback` rating, and `save_feedback()`
persists it. Tests cover this path. The problem is a silent failure mode that
makes the feedback loop useless without any log evidence: if the article hash
is not found in `article_source_map`, `source_name=""` is stored, and
`get_source_feedback_score()` will never match any real source name — the
rating is permanently orphaned. Nothing in the current code warns about this.

## 2. Affected bounded contexts and files

**Bounded context: Digest** — `FeedbackReceived` event path, hot spot #3
(feedback decay owner).

| File | Role |
|------|------|
| `digest/feedback.py` | `collect_feedback()` — hash lookup + rating append; `get_source_feedback_score()` — reads ratings |
| `digest/main.py` | Lines 585–610, 702–712, 756–760 — orchestrates load → collect → deliver → save |
| `digest/delivery/telegram.py` | Lines 237–244 — button creation with `callback_data: fb:a:{g|b}:HASH` |
| `tests/test_feedback.py` | Existing test suite — comprehensive but missing a warning-log assertion for the empty-source case |

## 3. Considered approaches

### A. Add warning log for empty-attribution + update test (preferred)

When `store.article_source_map.get(art_hash, "")` returns `""`, emit a
`logger.warning()` before appending the rating. This makes the silent failure
visible in GitHub Actions logs without changing any behaviour or data model.
Update `test_collect_feedback_per_article_unknown_hash_records_empty_source`
to assert the warning was emitted (using `caplog`).

**Trade-off:** Minimal, zero-risk change. Does not fix the root cause (missing
map entry), but makes it diagnosable. Production logs on the next run will
show whether the map is populated or stale.

### B. Drop ratings with empty source_name

Skip `store.ratings.append(...)` when `source_name == ""`. Ratings that
cannot be attributed to a source are useless for scoring, so storing them just
adds noise.

**Trade-off:** Cleaner store, but loses the `article_hash` record which could
be useful for future debugging. Also, the correct fix for an empty map entry
is to ensure the map is populated — not to silently discard feedback. If the
map is genuinely empty (first run ever, or cache wiped), every tap is dropped
with no log evidence. Rejected.

**Note:** Only one approach is viable here. The map lookup is correct; the gap
is purely observability. Approach A is the right move.

## 4. Chosen approach and why

**Approach A.** One-line `logger.warning()` in `collect_feedback()` at the
hash lookup miss in `feedback.py` (around line 236) when `source_name == ""`.
Update the corresponding test to assert the warning. No architectural change;
no ADR required (no new dependency, no BC boundary shift, no data model change
— see `docs/principles.md §"Что значит «архитектурно-значимо»"`).

## 5. Test strategy

**Unit (existing, no changes needed):**
- `test_collect_feedback_per_article_good` — happy path attribution ✓
- `test_collect_feedback_per_article_bad` — bad rating ✓
- `test_article_source_map_round_trip` — persistence ✓
- `test_get_source_feedback_score_*` — scoring ✓

**Unit (modified):**
- `test_collect_feedback_per_article_unknown_hash_records_empty_source`:
  add `caplog` fixture, assert `WARNING` level log containing the unknown
  hash is emitted when `article_source_map` is empty.

**Key assertion:** after `collect_feedback()` processes one `fb:a:g:HASH`
event where HASH is not in the map, `caplog.text` must contain `"HASH"` at
WARNING level. This proves the diagnostic is wired up correctly.

**No e2e test needed** — the full production path (GH Actions → digest-prod
`.cache/feedback.json`) is out of scope for the engine repo. The warning log
itself is the observable signal: if the next real pipeline run logs zero
attribution warnings, the map is populated and the loop works.

## 6. Risks and unknowns

1. **`adaptive.enabled` in digest-prod** — if `False`, `collect_feedback()`
   is never called regardless of this fix. Must be verified manually in
   `digest-prod/config.yaml`. Not addressable from this repo.

2. **`.cache/feedback.json` commit-back** — the digest-prod GitHub Actions
   workflow must commit `.cache/` back to the repo after each run, or the
   `article_source_map` from run N will not be available to run N+1. If
   `feedback.json` is absent on load, `article_source_map` starts empty and
   every tap in the first post-wipe run will hit the "unknown hash" warning.

3. **Webhook vs. polling conflict** — `collect_feedback()` calls
   `deleteWebhook` to ensure polling mode, but if the Telegram bot has an
   externally configured webhook, the delete may fail or take effect after a
   delay. Existing warning log covers this; no code change needed.

4. **First-ever-run attribution gap** — on the very first pipeline run,
   `article_source_map` is empty (nothing was ever saved before). All feedback
   tapped before the SECOND run will be stored with `source_name=""`. This is
   expected and unavoidable; the warning log makes it identifiable.
