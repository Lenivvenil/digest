# CLAUDE.md — Daily News Digest

## Project Context

This is a personal daily news digest generator for a Technology Architect at a major bank in Uzbekistan. It collects RSS feeds, summarizes them via LLM, delivers to Telegram and saves markdown files for Obsidian.

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

## Project Structure

```
├── src/
│   ├── __init__.py
│   ├── __main__.py          # enables `python -m src`
│   ├── main.py              # entrypoint, orchestration
│   ├── config.py            # config loading and validation
│   ├── collector.py         # RSS/Atom feed fetching and parsing
│   ├── summarizer.py        # LLM provider abstraction and prompt building
│   ├── telegram.py          # Telegram Bot API delivery
│   └── markdown_writer.py   # Markdown file output for Obsidian
├── tests/
│   ├── __init__.py
│   ├── test_config.py
│   ├── test_collector.py
│   ├── test_summarizer.py
│   ├── test_telegram.py
│   └── test_markdown_writer.py
├── digests/                  # generated markdown files (committed to repo)
│   └── .gitkeep
├── .cache/                   # deduplication cache (committed to repo)
│   └── .gitkeep
├── .github/workflows/
│   └── digest.yml
├── config.yaml               # user-editable configuration
├── .env.example
├── requirements.txt
├── .gitignore
├── CLAUDE.md
└── README.md
```

## Testing

- Use `pytest` with `pytest-asyncio` for async tests
- Use fixtures for sample RSS/Atom XML data
- Mock all HTTP calls in tests (never make real network requests in tests)
- Test edge cases: malformed feeds, empty feeds, missing config fields, API errors

## Important Constraints

- No heavy frameworks (no Flask, no Django, no FastAPI)
- All HTTP calls via `httpx` (async)
- Config is YAML only (no TOML, no JSON for config)
- The digest markdown files and cache are committed back to the repo by GitHub Actions
- Must work offline for testing (all external calls mockable)

## Digest Format: Three Perspectives

A key feature of the digest is the "three perspectives" format for significant news items. For the 1-2 most important topics in each category, the LLM must produce three viewpoints: Optimist (🟢), Skeptic (🔴), and Realist (⚖️). These should represent genuinely different reasoning chains, not just tonal variation. This applies in "analytical" and "detailed" summary styles, but not in "brief" mode. Minor news items get a regular 2-3 sentence comment without perspectives.
