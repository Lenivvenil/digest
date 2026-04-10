# CLAUDE.md — Daily News Digest

## Project Context

This is a personal daily news digest generator for a Technology Architect at a major bank in Uzbekistan. The pipeline has three phases:

1. **Radar** — collects RSS feeds and summarizes them via LLM (with three-perspective analysis for key topics)
2. **Irritator** — extracts dominant narratives from summaries, generates search queries, and finds counter-signals across multiple platforms (Hacker News, Reddit, arXiv, dev.to, Lobsters)
3. **Delivery** — sends results to Telegram (with article voting buttons) and saves markdown files for Obsidian

Runs on GitHub Actions (free tier). No VPS. No paid services required (though Claude API can be used if the user has a key).

## Tech Stack

- Python 3.12+
- Async: `asyncio` + `httpx`
- RSS parsing: `feedparser`
- Config: `pyyaml`
- Testing: `pytest` + `pytest-asyncio`
- Linting: `ruff`
- Type checking: `mypy`
- CI/CD: GitHub Actions

## Code Style

- Use `async/await` for all I/O operations (HTTP requests)
- Type hints everywhere. All functions must have return type annotations
- Dataclasses for data structures (not dicts)
- No classes where a function will do. Use classes only for the LLM provider abstraction
- Keep it simple: stdlib where possible, minimal dependencies
- Error messages must be clear and actionable, with URLs to documentation where relevant
- Logging via `logging` module, not `print()`
- All strings that face the user (log messages, errors) in English. The digest content language is controlled by config
- Never import symbols that are not used in the file. Never assign to variables that are not read.

## Project Structure

```
├── src/
│   ├── __init__.py          # __version__
│   ├── __main__.py          # enables `python -m src`
│   ├── main.py              # 6-phase pipeline orchestrator, CLI flags
│   ├── config.py            # config loading and validation
│   ├── llm.py               # LLM provider abstraction (Groq, Gemini, DeepSeek)
│   ├── filters.py           # blocklist keyword filtering
│   ├── _dns_pinning.py      # DNS pinning and SSRF protection for outbound HTTP
│   ├── _sanitize.py         # HTML/text sanitization for feed content
│   ├── _util.py             # atomic_json_write and other shared utilities
│   ├── radar/               # Phase 1: Collection & Summarization
│   │   ├── collector.py     # RSS/Atom feed fetching, parsing, dedup cache
│   │   └── summarizer.py    # LLM-powered category summarization, three perspectives
│   ├── irritator/           # Phases 2-5: Counter-signal analysis
│   │   ├── narrative_extractor.py  # extract dominant narratives from summaries
│   │   ├── query_generator.py      # generate search queries per narrative
│   │   ├── validator.py            # filter out blocklisted content
│   │   ├── ranker.py               # score counter-signals for relevance
│   │   └── sources/                # multi-platform search adapters
│   │       ├── arxiv.py, devto.py, hackernews.py, lobsters.py, reddit.py
│   └── delivery/            # Phase 6: Output distribution
│       ├── telegram.py      # Telegram Bot API, MarkdownV2, voting buttons
│       └── markdown.py      # Obsidian markdown file generation
├── tests/
│   ├── __init__.py
│   ├── factories.py         # shared test data factory helpers
│   ├── test_config.py, test_filters.py, test_llm.py, test_main.py
│   ├── test_radar_collector.py, test_radar_summarizer.py
│   ├── test_delivery_markdown.py, test_delivery_telegram.py
│   ├── test_narrative_extractor.py, test_query_generator.py
│   ├── test_ranker.py, test_validator.py
│   ├── test_sources_arxiv.py, test_sources_devto.py
│   ├── test_sources_hackernews.py, test_sources_lobsters.py
│   ├── test_sources_init.py, test_sources_reddit.py
│   └── test_ruff_config.py
├── digests/                  # generated markdown files (committed to repo)
│   └── .gitkeep
├── .cache/                   # deduplication cache (committed to repo)
│   └── .gitkeep
├── .github/workflows/
│   ├── digest.yml            # daily digest (02:00 + 13:00 UTC), includes test gate
│   └── discover.yml          # weekly source discovery (Sundays 06:00 UTC), includes test gate
├── config.yaml               # user-editable configuration
├── pyproject.toml            # ruff, mypy, pytest config
├── Makefile                  # lint, typecheck, test, check targets
├── .pre-commit-config.yaml   # ruff pre-commit hooks
├── .env.example
├── requirements.txt
├── requirements-dev.txt
├── .gitignore
├── CHANGELOG.md
├── CLAUDE.md
└── README.md
```

## Testing

- Use `pytest` with `pytest-asyncio` for async tests
- Use fixtures for sample RSS/Atom XML data
- Mock all HTTP calls in tests (never make real network requests in tests)
- Test edge cases: malformed feeds, empty feeds, missing config fields, API errors
- After writing or modifying test files, run `ruff check tests/` to catch unused imports and undefined names before committing.

## Important Constraints

- No heavy frameworks (no Flask, no Django, no FastAPI)
- All HTTP calls via `httpx` (async)
- Config is YAML only (no TOML, no JSON for config)
- The digest markdown files and cache are committed back to the repo by GitHub Actions
- Must work offline for testing (all external calls mockable)

## Pre-commit Checklist

Always run `ruff check src/ tests/` before committing. Fix all errors before creating a commit.

## Digest Format: Three Perspectives

A key feature of the digest is the "three perspectives" format for significant news items. For the 1-2 most important topics in each category, the LLM must produce three viewpoints: Optimist (🟢), Skeptic (🔴), and Realist (⚖️). These should represent genuinely different reasoning chains, not just tonal variation. This applies in "analytical" and "detailed" summary styles, but not in "brief" mode. Minor news items get a regular 2-3 sentence comment without perspectives.
