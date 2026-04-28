# Plan — Issue #66: fix(telegram): loading... при нажатии кнопки реакции

## 1. Problem restatement

Telegram requires `answerCallbackQuery` to be called within 10 seconds of a button press. Because the digest pipeline runs only twice daily via GitHub Actions, any button press between runs goes unanswered. The user sees a "loading..." spinner for ~30 seconds, then nothing — even though the feedback is actually recorded on the next pipeline run. The root problem is a mismatch between user expectations (synchronous confirmation) and the pipeline's batch execution model.

## 2. Affected bounded contexts and files

**Delivery BC** (`digest/delivery/`)
- `digest/delivery/telegram.py` — `send_article_cards()`: where article cards with voting buttons are assembled and sent

No other BCs are touched. The Irritator BC (feedback collection) is unchanged — it already records votes correctly on the next run.

## 3. Considered approaches

**Option A — Serverless webhook (Cloudflare Worker / Vercel)**
Deploy a lightweight handler that answers `callback_query` within 10 seconds and shows a toast "✓ Учтено". Eliminates the spinner entirely.
- Pros: proper UX, matches Telegram's intended flow
- Cons: requires infrastructure outside GitHub Actions (deploy, secrets, monitoring). Out of scope for a zero-infra pipeline. Deferred to a future issue.

**Option B — Increase pipeline run frequency**
Schedule the pipeline every 5–10 minutes so there is always a run within the 10-second window.
- Pros: no code change
- Cons: wasteful (LLM API calls, rate limits), still non-deterministic, and misses the window entirely if the user presses a button between runs. Red flag: this is a workaround for the wrong problem.

**Option C — Italics caption under each card (chosen)**
Append `_Реакции учитываются при след. запуске_` beneath each article card to set expectations before the user taps.
- Pros: zero infrastructure, one-line change, corrects user mental model permanently
- Cons: slightly more verbose cards; does not remove the spinner (just explains it)

Option A is the correct long-term fix but requires out-of-scope infrastructure. Option C is the right minimal fix given the zero-infra constraint. Option B is a red flag.

## 4. Chosen approach and why

Option C. The `send_article_cards()` function in `telegram.py` assembles each card's text before calling `_send_chunk`. The fix adds a module-level constant `_ASYNC_FEEDBACK_NOTE` and appends it as an italics line to every card's message body.

No ADR required: the change adds no new dependencies, does not alter any BC boundary or inter-context contract, introduces no infrastructure, and is trivially reversible. None of the six architectural-significance triggers in `docs/principles.md` fire.

**Implementation (committed in `7384f1e`):**
```python
_ASYNC_FEEDBACK_NOTE = "Реакции учитываются при след. запуске"

# in send_article_cards():
async_note = escape_markdownv2(_ASYNC_FEEDBACK_NOTE)
text = (
    f"[{title_esc}]({url_esc})\n\n"
    f"{summary_esc}\n\n"
    f"*{source_esc}* · _{cat_esc}_\n"
    f"_{async_note}_"
)
```

## 5. Test strategy

**Unit tests** (`tests/test_delivery_telegram.py`):
- Assert that `send_article_cards()` includes the `_ASYNC_FEEDBACK_NOTE` text (escaped) in the message body sent to `_send_chunk`.
- Assert that the note is italicised (wrapped in `_..._` after MarkdownV2 escaping).
- Assert existing card structure (title link, summary, source·category line) is preserved — no regression.

No integration or e2e tests needed: the change is purely string formatting in a function already covered by unit tests. All network calls are mocked per `CLAUDE.md` constraints.

## 6. Risks and unknowns

- **MarkdownV2 escaping of the note text:** The string "Реакции учитываются при след. запуске" contains a period (special char in MarkdownV2). Verified in code: `escape_markdownv2(_ASYNC_FEEDBACK_NOTE)` is called before interpolation — period is correctly escaped to `\.`.
- **Card length:** The extra line adds ~45 characters. Cards are well under `_SPLIT_LIMIT` (3800 chars), so no splitting risk.
- **Test coverage gap:** If the existing `send_article_cards` test does not assert on the full message body, the note could be silently dropped in a future refactor. The test strategy above closes this gap.
- **Future Option A cleanup:** When a webhook handler is eventually added, the note should be removed from card text. That cleanup must be tracked in the future serverless issue to avoid orphaned UX copy.

Closes #66
