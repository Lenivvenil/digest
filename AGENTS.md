# Repository working guide — Daily News Digest

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

## Repository topology (ADR-0002)

This is the **engine repo** — source code, tests, CI only. Production runtime lives in `Lenivvenil/digest-prod` (private).

- Production `config.yaml`, `.cache/` and `digests/` belong in the private runtime repo, `digest-prod`.
- Entry point: `python -m digest`. Install the runtime from a reviewed immutable engine commit:
  `pip install "digest @ git+https://github.com/Lenivvenil/digest@<reviewed-commit-sha>"`.

## Working agreement

Tracked discipline: [#148](https://github.com/Lenivvenil/digest/issues/148).

- Before implementation, use an existing issue or create a scoped one with the
  problem, acceptance, affected contracts and current status. Check its placement
  and status on the existing project board; do not invent a replacement board.
- Keep one reviewable scope per change. Link the implementing issue and applicable
  ADR/domain evidence in the PR. Use an automatic closing reference only when the
  issue's full acceptance is satisfied; partial progress must leave remaining work open.
  Use `Refs #…` for incomplete work; closing keywords still trigger GitHub automation
  inside negated sentences, so do not pair them with an issue reference.
- After merge, verify the exact main checks and, when applicable, the deployed engine
  pin and retained runtime state. Update the issue and board from observed results.
  Do not equate a green check with editorial or real-runtime acceptance.
- Reconcile merged branches only after checking current tips, main ancestry and
  outstanding PR/dependent work. Preserve history and a recoverable commit reference.
- A blocked board update, deployment, check or cleanup is unfinished. Record the
  specific blocker and continue independent authorized work; never report a step as
  completed without readback evidence.
- Maintain code, comments, documentation, ADRs, issues, PRs and commit messages in
  English. Preserve historical discussions, quoted source evidence and deliberately
  localized product output; this does not authorize publishing private runtime data.

The implementation task for the first structural migration is
[#143](https://github.com/Lenivvenil/digest/issues/143). Architecture and staged ownership
remain in [the existing architecture reference](docs/ARCHITECTURE.md), not in a parallel
agent-only design.

## Project Structure

```
├── digest/
│   ├── application/         # prepared/direct/discovery scenarios and shared application operations
│   ├── __init__.py          # __version__
│   ├── __main__.py          # enables `python -m digest`
│   ├── main.py              # public compatibility wrappers and CLI dispatch
│   ├── cli/                 # arguments, diagnostics and result/preview reporting
│   ├── config.py            # config loading and validation
│   ├── llm.py               # LLM provider abstraction (Groq, Gemini, DeepSeek)
│   ├── filters.py           # blocklist keyword filtering
│   ├── _dns_pinning.py      # URL validation and DNS pinning for feed/article acquisition
│   ├── _sanitize.py         # HTML/text sanitization for feed content
│   ├── _util.py             # atomic_json_write and other shared utilities
│   ├── radar/               # Phase 1: Collection & Summarization
│   │   ├── collector.py     # RSS/Atom feed fetching, parsing, dedup cache
│   │   └── summarizer.py    # LLM-powered category summarization, three perspectives
│   ├── irritator/           # Phases 2-5: Counter-signal analysis
│   │   ├── narrative_extractor.py  # extract dominant narratives from summaries
│   │   ├── query_generator.py      # generate search queries per narrative
│   │   ├── validator.py            # dedup, blocklist filter, optional URL liveness check
│   │   ├── ranker.py               # score counter-signals for relevance
│   │   └── sources/                # multi-platform search adapters
│   │       ├── arxiv.py, devto.py, hackernews.py, lobsters.py, reddit.py
│   ├── presentation/        # Pure publication copy, escaping and chunk coverage
│   ├── adapters/telegram/   # Concrete Telegram protocols
│   ├── adapters/storage/    # Exact persisted records, path guards and local writes
│   └── delivery/            # Compatible distribution imports and legacy issue guard
│       ├── telegram.py      # Compatible Telegram presentation/transport exports
│       └── markdown.py      # Obsidian markdown file generation
├── tests/
│   ├── __init__.py
│   ├── factories.py         # shared test data factory helpers
│   ├── test_config.py, test_filters.py, test_llm.py, test_main.py
│   ├── test_radar_collector.py, test_radar_summarizer.py
│   ├── test_delivery_markdown.py, test_delivery_telegram.py
│   ├── test_narrative_extractor.py, test_query_generator.py
│   ├── test_ranker.py, test_validator.py
│   ├── test_irritator_orchestrator.py
│   ├── test_sources_arxiv.py, test_sources_devto.py
│   ├── test_sources_hackernews.py, test_sources_lobsters.py
│   └── test_sources_init.py, test_sources_reddit.py
├── .github/workflows/
│   └── ci.yml               # lint + typecheck + test on push/PR
├── pyproject.toml            # package name "digest", ruff, mypy, pytest config
├── Makefile                  # lint, typecheck, test, check targets
├── .pre-commit-config.yaml   # ruff + bandit pre-commit hooks
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
- GitHub Actions commits production digest Markdown files and cache to the private runtime repo (`digest-prod`)
- Must work offline for testing (all external calls mockable)

## Pre-commit Checklist

Always run `ruff check digest/ tests/` before committing. Fix all errors before creating a commit.

## Digest Format: Three Perspectives

The category-summary path can request three perspectives with `radar.perspectives: true`
(default: `false`). In `analytical` and `detailed` styles, the prompts request Optimist
(🟢), Skeptic (🔴), and Realist (⚖️) viewpoints for significant topics. These should
represent different reasoning chains, not just tonal variation. Brief mode omits
perspectives; minor items use ordinary comments. The review-led-only path skips
category summaries and does not use this format.
