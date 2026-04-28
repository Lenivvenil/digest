# Plan — Issue #55: Verbose and repetitive article summaries

## 1. Problem restatement

The digest produces article summaries that restate headlines and echo the same observations
across multiple items in a single run. The Radar summarizer has two code paths: per-category
free-text (`build_category_prompt`) and per-article JSON selection (`build_per_article_prompt`).
Both paths lack an explicit instruction to focus on what is *surprising or non-obvious*, and
the per-article JSON path requests 2-3 sentences when 1-2 are sufficient. Because categories
run in parallel (`asyncio.gather`), nothing prevents two categories from making identical
observations about closely related articles. The result is a digest where 50-60% of content
carries no information gain over just reading the headlines.

## 2. Affected bounded contexts and files

**Radar BC** — sole affected context.

| File | Change |
|------|--------|
| `digest/radar/summarizer.py` | Tighten live `instructions_category_*` keys (10) + `instructions_trends` (2) + `_PER_ARTICLE_INSTRUCTIONS` (2) = 14 strings; add `_cap_sentences()` utility; apply cap in `pick_top_articles()` |
| `tests/test_radar_summarizer.py` | Add prompt-content assertions + sentence-cap unit tests |

**Dead code note:** `PROMPT_TEMPLATES` also contains `instructions_analytical`, `instructions_brief`,
`instructions_detailed`, and their `_no_persp` siblings (5 keys × 2 languages = 10 strings).
These are never referenced — `build_category_prompt` only uses `instructions_category_{style}` keys.
They are **not modified** in this PR (out of scope) but noted here so reviewers don't flag the
inconsistency. A follow-up cleanup is tracked in the PR description.

No cross-BC contracts touched. `irritator/` and `delivery/` are unaffected.

## 3. Considered approaches

### A — Prompt engineering only

Tighten the instruction strings:
- Reduce per-article JSON target from 2-3 sentences to 1-2 sentences
- Add: "do NOT restate the headline verbatim"
- Add: "focus on what is surprising, non-obvious, or has direct practical implications"

Trade-offs:
- Zero latency/cost overhead; reversible
- Purely behavioural — LLM can still ignore the instruction on a bad run
- Acceptance criterion 1 (≤ 2 sentences) cannot be verified mechanically

### B — Prompt engineering + mechanical sentence cap (chosen)

Same prompt changes as A, **plus** a `_cap_sentences(text: str, n: int) -> str` helper that
trims `ArticleSummary.summary` fields to at most `n=2` sentences before they are stored.

Trade-offs:
- Mechanically guarantees criterion 1 regardless of LLM drift; unit-testable
- Sentence splitting has edge cases (abbreviations, ellipsis); acceptable for a personal digest
- Adds ~10 lines of utility code + tests

### C — Cross-category context window

After all category summaries complete, run a second pass that receives all previously-generated
summaries as context so the LLM can avoid repeating points already made.

Trade-offs:
- Directly addresses cross-category repetition (hypothesis 3 in the issue)
- Doubles latency for the Radar phase (currently parallel → sequential second pass)
- Higher token cost; changes `summarize_all` return contract

Verdict: out of scope for this fix. Revisit if prompt tightening alone is insufficient.

### D — Pre-dedup clustering

Cluster near-duplicate articles before summarizing; summarize clusters not individual articles.

Trade-offs:
- Most thorough dedup; requires embedding model or TF-IDF — over-engineered for current scale

Verdict: out of scope.

## 4. Chosen approach and why

**Approach B** — prompt tightening + mechanical sentence cap.

Prompt engineering alone (A) is insufficient: the category prompts already say "1-2 предложения"
but LLMs routinely expand when given latitude. A mechanical cap enforces the hard constraint and
makes criterion 1 testable without a real LLM call. This is consistent with principle 3 (automate
deterministic, low-risk steps); sentence truncation is deterministic and reversible.

**Which path is the primary offender:** Both paths produce visible output, but they differ:
- `CategorySummary.summary_text` (from `summarize_all`) is the **main digest body** — each
  article in a category gets an inline 1-2 sentence comment embedded in free-text markdown.
  This is the higher-repetition path because categories run in parallel and can describe the
  same event from different angles without awareness of each other.
- `ArticleSummary.summary` (from `pick_top_articles`) is the **Telegram card** — 7 top
  articles selected across all categories, each getting a 2-3 sentence card. Lower repetition
  risk (the LLM sees all categories at once), but currently over-long.

The mechanical `_cap_sentences` cap applies **only** to `ArticleSummary.summary` (Telegram
cards). The category free-text path is controlled only by prompt instruction. This is accepted
scope: the cap enforces criterion 1 (≤2 sentences) on the path where it can be measured;
criterion 2 (information gain across summaries) is empirical and requires a real digest run.

**ADR check against `docs/principles.md`:**
- No new cross-cutting dependency
- No BC boundary or inter-BC contract change
- No infrastructure component selected
- No public API established or removed
- No hard-to-remove constraint introduced
- No security or data model change

Not architecturally significant. No ADR required. This is a story.

## 5. Test strategy

**Unit tests in `tests/test_radar_summarizer.py`:**

- `test_per_article_instructions_are_terse` — assert `_PER_ARTICLE_INSTRUCTIONS["ru"]` and
  `["en"]` contain the new constraint phrases (≤2 sentences, no headline restatement)
- `test_category_prompts_contain_non_obvious_instruction` — explicit `(language, style)` matrix:
  `("ru", "analytical")`, `("ru", "analytical_no_persp")`, `("ru", "brief")`,
  `("ru", "detailed")`, `("ru", "detailed_no_persp")`,
  `("en", "analytical")`, `("en", "analytical_no_persp")`, `("en", "brief")`,
  `("en", "detailed")`, `("en", "detailed_no_persp")`
  — assert the non-obvious/terse direction phrase is present in each combination
- `test_cap_sentences_truncates_to_n` — pure unit for `_cap_sentences`; cases: already short
  (unchanged), exactly n (unchanged), longer than n (truncated), empty string (empty)
- `test_pick_top_articles_caps_summaries` — mock `complete` to return a 5-sentence summary;
  assert the returned `ArticleSummary.summary` has ≤ 2 sentences

**No integration tests needed:** no HTTP boundary crossed, no BC contract changed.

**Acceptance criteria that remain empirical (no automated test):**
- "Reading all summaries adds new information each time" — verify manually on the next real
  digest run after deploy.

## 6. Risks and unknowns

1. **Sentence splitter false positives** — abbreviations like "Dr.", "U.S.A.", "e.g." may
   be read as sentence boundaries, causing premature truncation. Mitigation: use a simple
   period-followed-by-space-and-capital heuristic rather than a full NLP tokenizer; sufficient
   for news summaries.

2. **Perspectives block interaction** — `analytical` and `detailed` styles add a three-line
   perspectives block (🟢/🔴/⚖️) for the top topic. The sentence cap must apply *only* to
   `ArticleSummary.summary` (the per-article JSON field). It must NOT touch `CategorySummary.summary_text`
   (the free-text category output). The scope is correct: `_cap_sentences` is called inside
   `pick_top_articles` only.

3. **14 instruction strings to update** — `PROMPT_TEMPLATES` has many keys across two languages
   and multiple style variants. Missing one is the most likely implementation mistake. Mitigated
   by the parametric tests in §5 that cover all style/language combinations.

4. **Repetition is reduced, not eliminated** — the prompt tightening directly addresses
   hypotheses 1 and 2 (no repetition instruction, summaries too long). Hypothesis 3
   (cross-category dedup) is only partially addressed (by demanding non-obvious content).
   If repetition persists after this fix, approach C is the next candidate.
