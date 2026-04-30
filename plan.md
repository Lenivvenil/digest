# Plan: fix Lobsters 400 Bad Request on adversarial queries (#77)

## 1. Problem restatement

A subset of Lobsters search calls return `400 Bad Request`. The exact server-side cause is **unconfirmed** — we do not have a failing production query to reproduce manually (AC pre-condition not yet met). What we know: the queries are LLM-generated adversarial strings containing characters like `"`, `:`, `(`, `)`, `-` — natural outputs of the query-generator prompt patterns (`"X didn't work"`, `"failure of X: case study"`). Lobsters is open source; inspecting `app/models/search.rb` shows it has a `strip_operators` method that converts non-word characters to spaces before building a MariaDB MATCH...AGAINST query, but this runs *after* `SearchParser.new.parse(params[:q])` in `Search#initialize`. The 400 mechanism is unclear — a raised exception from the parser would be a Rails 500, not 400. Other candidates: Rack/Nginx middleware, a custom error handler, or a Rails `head :bad_request` somewhere in the chain we haven't found.

**This plan ships a defensive sanitizer that mirrors Lobsters' own `strip_operators` logic.** This is valid defense-in-depth: even if the precise mechanism differs from our hypothesis, applying the same character-stripping the server applies before its own processing cannot make things worse, and very likely eliminates whatever character sequence triggers the 400. Root-cause confirmation via manual curl test is a merge gate (see §4).

## 2. Affected bounded contexts and files

**Irritator BC** (`docs/domain/irritator/`):

| File | Role |
|------|------|
| `digest/irritator/sources/lobsters.py` | Lobsters adapter — primary change target |
| `tests/test_sources_lobsters.py` | Adapter unit tests |

No BC boundary changes. All other adapters and `sources/__init__.py` are untouched.

## 3. Considered approaches

### A. Pre-send sanitization mirroring Lobsters' `strip_operators` ← chosen

Apply the equivalent of Lobsters' own `strip_operators` logic **before** sending the query. Lobsters does:

```ruby
def strip_operators s
  s.to_s
    .gsub(/[^\p{Word}']/, " ")  # replace non-word chars (keep apostrophe) with space
    .gsub("'", "\\\\'")         # escape apostrophes for MariaDB
    .strip
end
```

Python equivalent in `lobsters.py`:
```python
import re

# \w is Unicode-aware in Python 3 by default (matches \p{Word} like Ruby's regex),
# which matters because LLM prompts are bilingual (RU/EN) and may produce Cyrillic terms.
_STRIP_RE = re.compile(r"[^\w']")

def _sanitize_query(q: str) -> str:
    """Strip non-word characters to match Lobsters' own strip_operators logic."""
    q = _STRIP_RE.sub(" ", q)
    return " ".join(q.split())  # collapse whitespace, strip edges
```

No length cap: LLM queries are empirically short and a cap without a documented threshold is dead code with a magic number. If a length limit becomes relevant, it belongs in a follow-up with evidence.

**Trade-offs:**
- (+) Mirrors the server-side logic — queries we send are a subset of what Lobsters' sanitizer would produce anyway.
- (+) Self-contained in the adapter; no orchestrator changes.
- (+) Fully testable in unit tests without hitting real Lobsters.
- (+) Unicode-aware by default; handles Cyrillic queries correctly.
- (−) If Lobsters changes `strip_operators` in a future version, our sanitizer could diverge (low risk — small, stable OSS project).
- (−) Does not confirm the 400 cause; might not be the only fix needed.

### B. Retry on 400 with no sanitization

Catch `httpx.HTTPStatusError` for 400, log and return `[]` (which already happens in the fan-out wrapper). Effectively: accept the loss.

**Trade-offs:**
- (+) Zero code added to the adapter itself.
- (−) Lobsters contributes no signals for queries with adversarial characters — the fix doesn't fix anything, it just makes the failure explicit.
- (−) Does not satisfy the AC "fix applied in `lobsters.py`".

### C. Escape / percent-encode special characters

Use `urllib.parse.quote` to encode characters before sending, or wrap the query in quotes.

**Trade-offs:**
- (+) Alternative sanitization path.
- (−) httpx already percent-encodes query params — the issue is not encoding, it's that Lobsters' parser sees the decoded characters on the server side.
- (−) Quoting the whole query string as a phrase-search would narrow results significantly.

**Verdict:** A is the only approach that addresses the root cause. B is not a fix. C misunderstands where the issue is.

## 4. Chosen approach and why

**Option A — pre-send `_sanitize_query()` in `lobsters.py`.**

- Defense-in-depth: mirrors the server's own `strip_operators` logic so our queries are a strict subset of what Lobsters processes safely.
- Self-contained; no BC boundary change, no new dependency (`re` is stdlib), no infrastructure choice.
- ADR check: not architecturally significant. **No ADR required.**
- Preserves `Semaphore(1)` from PR #76 — sanitization happens inside the semaphore context, before `client.get()`.

Implementation sketch:
```python
_STRIP_RE = re.compile(r"[^\w']")

def _sanitize_query(q: str) -> str:
    q = _STRIP_RE.sub(" ", q)
    return " ".join(q.split())

@_register("lobsters")
async def search_lobsters(query: str, config: Any, client: httpx.AsyncClient) -> list[Signal]:
    async with _get_semaphore():
        resp = await client.get(
            _BASE_URL,
            params={"q": _sanitize_query(query), "what": "stories", "order": "relevance"},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        ...
```

**Root-cause confirmation (merge gate):** The AC requires manual reproduction. Before merging, operator runs:
```bash
# With a query that contains operator chars — should 400 if our hypothesis is right:
curl -s -o /dev/null -w "%{http_code}\n" \
  'https://lobste.rs/search.json?q=%22AI+governance%3A+failure%22&what=stories&order=relevance'

# With sanitized form — should 200:
curl -s -o /dev/null -w "%{http_code}\n" \
  'https://lobste.rs/search.json?q=AI+governance+failure&what=stories&order=relevance'
```
If both return 200 (Lobsters sanitizes silently), the 400s must have a different cause — note it in the PR thread and decide whether to ship as defensive hardening anyway.

## 5. Test strategy

**Unit tests** (new/updated in `tests/test_sources_lobsters.py`):

| Test | Assertion |
|------|-----------|
| `test_sanitize_query_strips_operators` | `"AI: failure (2024)"` → `"AI failure 2024"` |
| `test_sanitize_query_keeps_apostrophes` | `"didn't work"` → `"didn't work"` (apostrophe preserved) |
| `test_sanitize_query_collapses_whitespace` | `"foo   :   bar"` → `"foo bar"` |
| `test_sanitize_query_unicode` | Cyrillic input `"провал ИИ: 2024"` → `"провал ИИ 2024"` (colon stripped, words kept) |
| `test_query_sanitized_before_send` | Mock captures `q` param; assert operator-containing input arrives sanitized. **Note:** this test proves we sanitize before sending; it does not prove the unsanitized form would 400 (that requires the manual curl gate above). The test is still valuable — it guards against regression where sanitization is accidentally removed. |
| `test_semaphore_not_regressed` | Existing `test_semaphore_limits_concurrency` continues to pass unchanged |

**Integration** (existing `tests/test_sources_init.py`): no changes needed — graceful degradation path unchanged.

**e2e / manual**: Operator runs the curl test above before merge to confirm the live Lobsters endpoint rejects the unsanitized query and accepts the sanitized one. This satisfies the AC's "root cause confirmed via manual reproduction."

## 6. Risks and unknowns

| Risk | Likelihood | Mitigation |
|------|-----------|------------|
| Root cause is different from our hypothesis — sanitizer doesn't fix the 400s | Medium | The manual curl gate before merge tests this. If both sanitized and unsanitized queries return 200, we ship as defensive hardening and track whether 400s persist in the next prod run |
| 400s were caused by the fan-out burst (#63) and already fixed by PR #76 | Medium | If first prod run after #76 has zero 400s, this PR is still safe to ship (sanitization is a no-op for well-formed queries) but we should note the finding |
| Sanitization removes meaningful query signal (e.g., `"phrase"` intent) | Low | Lobsters' `strip_operators` strips these server-side anyway — we lose nothing Lobsters would have acted on. Adversarial keywords ("failure", "criticism") survive as plain tokens |
| `_sanitize_query` returns empty string (all chars stripped) | Low | Lobsters returns empty results with `invalid("No search terms recognized")` — not a 400; graceful degradation handles it. We could add an explicit guard, but it's unnecessary given graceful degradation already exists |
| Lobsters changes `strip_operators` in the future | Low | Undocumented API; any upstream change requires re-investigation. Not worth designing for now |
