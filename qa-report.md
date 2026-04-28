## QA

**Tests:** All changed paths covered.

- `digest/radar/summarizer.py` — new `_cap_sentences()` utility covered by `TestCapSentences`
  (8 cases: empty, single, exact-n, truncation, exclamation/question, Cyrillic boundary,
  Cyrillic single, n > count). All 10 `(language, style)` combinations verified by
  `TestTersePromptInstructions.test_category_prompt_contains_non_obvious_instruction`.
  `_PER_ARTICLE_INSTRUCTIONS` tightening covered by `test_per_article_instructions_ru_are_terse`
  and `test_per_article_instructions_en_are_terse`. Cap application in `pick_top_articles`
  covered by `TestPickTopArticlesSentenceCap` (3 cases: long English, short English, Cyrillic).
  `instructions_trends` covered by `test_trends_prompt_*_contains_non_obvious_instruction`.
- Full suite: 457 passed, 0 failed. Ruff clean. Mypy clean.

**Acceptance criterion not automatable:** "Reading all summaries in a category adds new
information each time" — requires a real LLM run. Verify empirically on next digest run.

**Docs:** All contracts current. No runbooks reference sentence counts. CLAUDE.md and README
references to `summarizer.py` are structural labels unchanged by this diff.
