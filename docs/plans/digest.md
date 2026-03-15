# Plan: Daily News Digest

Автономный генератор ежедневного дайджеста новостей для Technology Architect в банке.
Собирает RSS-фиды, суммаризирует через LLM, отправляет в Telegram и сохраняет markdown в репо (для Obsidian через git-sync).

Хостинг: GitHub Actions (бесплатно, 2000 мин/мес). Без VPS, без оплаты.

## Validation Commands

- `python -m pytest tests/ -v`
- `python -m mypy src/ --ignore-missing-imports`
- `python -c "import yaml; yaml.safe_load(open('config.yaml'))"`
- `python -m ruff check src/`

### Task 1: Project scaffold and config system

- [x] Create project structure: `src/`, `tests/`, `digests/`, `config.yaml`, `requirements.txt`, `.gitignore`, `README.md`
- [x] `requirements.txt` must include: `pyyaml`, `httpx`, `feedparser`, `pytest`, `mypy`, `ruff`. No heavyweight frameworks
- [x] `config.yaml` — the single source of truth for all settings. Must contain sections: `llm`, `delivery`, `digest`, `sources`
- [x] `llm` section supports multiple providers via `provider` field: `"anthropic"` (Claude Sonnet/Haiku), `"gemini"` (free, no card), `"groq"` (free, no card). Each provider has its own model field. Default provider: `"anthropic"`, default model: `"claude-sonnet-4-20250514"`
- [x] `delivery` section: `telegram: true/false`, `markdown_to_repo: true/false`, `markdown_dir: "digests"`
- [x] `digest` section: `language: "ru"`, `max_articles_per_source: 5`, `max_total_articles: 30`, `summary_style: "analytical"`
- [x] `sources` section: list of objects with fields `name`, `url` (RSS/Atom URL), `category`, `enabled` (bool). Pre-populate with these sources:
  - Category "Banking & Fintech": Finextra (`https://www.finextra.com/rss/headlines.aspx`), PYMNTS (`https://www.pymnts.com/feed/`), Finovate (`https://finovate.com/feed/`)
  - Category "AI & LLM": MIT Technology Review AI (`https://www.technologyreview.com/topic/artificial-intelligence/feed`), The Batch by Andrew Ng (`https://www.deeplearning.ai/the-batch/feed/`), Hugging Face Blog (`https://huggingface.co/blog/feed.xml`), Ars Technica AI (`https://feeds.arstechnica.com/arstechnica/technology-lab`)
  - Category "Enterprise Architecture": InfoQ (`https://feed.infoq.com/infoq/infoq`), ThoughtWorks Insights (`https://www.thoughtworks.com/rss/insights.xml`), Martin Fowler (`https://martinfowler.com/feed.atom`), Hacker News Best (`https://hnrss.org/best`)
  - Category "Geopolitics & CIS": Spot.uz (`https://www.spot.uz/ru/rss/`), Kun.uz EN (`https://kun.uz/en/news.rss`)
- [x] Write tests: config loading, config validation (missing required fields raise clear errors), default values

### Task 2: RSS/Atom feed collector

- [ ] Create `src/collector.py` — fetches and parses RSS/Atom feeds
- [ ] Use `feedparser` for robust RSS/Atom parsing (handles both formats, encoding issues, malformed XML)
- [ ] Use `httpx` with async support for parallel feed fetching. Timeout: 15 seconds per feed. Custom User-Agent header
- [ ] For each article extract: `title`, `link`, `description` (strip HTML tags, truncate to 500 chars), `source` (from config name), `category` (from config), `pub_date`
- [ ] Filter: only articles from the last 24 hours (compare `pub_date` with current UTC time). If `pub_date` is missing or unparseable, include the article
- [ ] Deduplication cache: maintain `.cache/seen_articles.json` — a dict of `{hash: iso_datetime}`. Hash = md5 of `title|link`. Prune entries older than 7 days on each run. Skip articles already in cache
- [ ] Respect `max_articles_per_source` and `max_total_articles` from config
- [ ] Return articles grouped by category as `dict[str, list[Article]]` where Article is a dataclass
- [ ] Handle errors gracefully: if a feed fails, log warning and continue with others. Never crash on a single feed failure
- [ ] Write tests: feed parsing with sample RSS/Atom XML fixtures, deduplication logic, 24h filtering, HTML stripping, error handling for malformed feeds

### Task 3: LLM summarization with multi-provider support

- [ ] Create `src/summarizer.py` — builds prompt from collected articles and calls LLM
- [ ] Provider abstraction: `BaseLLMProvider` protocol/ABC with method `async def summarize(prompt: str) -> str`
- [ ] Implement `AnthropicProvider`: calls `https://api.anthropic.com/v1/messages` with `x-api-key` header. Reads `ANTHROPIC_API_KEY` from env. Supports models: `claude-sonnet-4-20250514`, `claude-haiku-4-5-20251001`
- [ ] Implement `GeminiProvider`: calls `https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}`. Reads `GEMINI_API_KEY` from env. Default model: `gemini-2.5-flash`
- [ ] Implement `GroqProvider`: calls `https://api.groq.com/openai/v1/chat/completions` with Bearer token. Reads `GROQ_API_KEY` from env. Default model: `llama-3.3-70b-versatile`
- [ ] Factory function `get_provider(config) -> BaseLLMProvider` that selects provider based on `config["llm"]["provider"]`
- [ ] Prompt template (in Russian by default, controlled by `digest.language`):
  - Role: "Ты — аналитик, который готовит ежедневный дайджест новостей для Technology Architect в крупном банке"
  - Instructions: group by category, pick 3-5 most important per category, 2-3 sentence analytical comment per article, "Ключевые тренды дня" section at the end, markdown format with emoji for categories, preserve source links, skip irrelevant news
  - **Three perspectives rule**: for each significant topic or trend within a category, provide three viewpoints formatted as a compact block:
    - 🟢 **Оптимист** — the strongest argument in favor, the bullish take, why this is a breakthrough or opportunity
    - 🔴 **Скептик** — the strongest argument against, risks, why this might fail or be overhyped
    - ⚖️ **Реалист** — the balanced middle-ground assessment, what is most likely to actually happen
    - Not every minor news item needs three perspectives — only the 1-2 most significant topics per category. Minor items get a regular analytical comment
    - The perspectives should represent genuinely different reasoning, not just variations in tone. Each perspective should be 1-2 sentences
  - Style controlled by `digest.summary_style`: "analytical" (default, with trends and three perspectives), "brief" (one sentence per news, no perspectives), "detailed" (full context, three perspectives for all items)
- [ ] Error handling: retry once on timeout/5xx, raise clear error on auth failure (missing/invalid API key)
- [ ] Write tests: prompt building, provider selection, mock API responses for each provider, error handling

### Task 4: Telegram delivery

- [ ] Create `src/telegram.py` — sends digest to Telegram
- [ ] Use `httpx` to call Telegram Bot API: `https://api.telegram.org/bot{token}/sendMessage`
- [ ] Reads `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` from env
- [ ] Send as MarkdownV2 format with `parse_mode: "MarkdownV2"`. Properly escape special characters for MarkdownV2 (`_`, `*`, `[`, `]`, `(`, `)`, `~`, `` ` ``, `>`, `#`, `+`, `-`, `=`, `|`, `{`, `}`, `.`, `!`)
- [ ] Split messages longer than 4096 chars at paragraph boundaries (double newline). Send each chunk as a separate message with 1 second delay between messages
- [ ] If Telegram delivery is disabled in config (`delivery.telegram: false`), skip silently
- [ ] If env vars are missing and Telegram is enabled, log warning but do not crash (still produce markdown)
- [ ] Write tests: message splitting, MarkdownV2 escaping, graceful handling of missing credentials

### Task 5: Markdown file output for Obsidian

- [ ] Create `src/markdown_writer.py` — saves digest as a markdown file in the repo
- [ ] File path: `{config.delivery.markdown_dir}/{YYYY-MM-DD}.md` (e.g., `digests/2026-03-14.md`)
- [ ] File starts with YAML frontmatter: `title`, `date`, `sources_count`, `articles_count`, `llm_provider`, `tags: [digest, daily]`
- [ ] Body is the LLM-generated summary as-is (it is already markdown)
- [ ] If file for today already exists, overwrite it (idempotent re-runs)
- [ ] If `delivery.markdown_to_repo` is false, skip silently
- [ ] Create `digests/.gitkeep` so the directory is tracked
- [ ] Write tests: file creation, frontmatter format, overwrite behavior

### Task 6: Main entrypoint and CLI

- [ ] Create `src/main.py` as the main entrypoint. Also make it callable as `python -m src`
- [ ] Flow: load config → collect feeds (async) → summarize → deliver (telegram + markdown, parallel)
- [ ] CLI arguments via `argparse`: `--config` (path to config, default `config.yaml`), `--dry-run` (collect and summarize but don't send/save), `--verbose` (debug logging)
- [ ] Print summary stats on completion: feeds fetched, articles collected, new articles (not cached), digest length, delivery status
- [ ] Exit code 0 on success, 1 on critical failure (all feeds failed, LLM error). Partial failures (some feeds down, telegram failed but markdown saved) should still exit 0
- [ ] Write tests: dry-run mode, argument parsing

### Task 7: GitHub Actions workflow

- [ ] Create `.github/workflows/digest.yml`
- [ ] Schedule: `cron: '0 6 * * *'` (runs daily at 06:00 UTC, which is ~11:00 Tashkent time)
- [ ] Also trigger on `workflow_dispatch` for manual runs
- [ ] Python 3.12, install dependencies from `requirements.txt`
- [ ] Pass secrets from GitHub repository secrets: `ANTHROPIC_API_KEY` (or `GEMINI_API_KEY` or `GROQ_API_KEY` depending on config), `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
- [ ] Run `python -m src --config config.yaml`
- [ ] After the script, commit and push new markdown files in `digests/` directory and updated `.cache/` back to the repo. Use `git config` for bot user, `git add digests/ .cache/`, `git diff --cached --quiet || git commit -m "digest: {date}" && git push`
- [ ] Cache pip dependencies for faster runs
- [ ] Add a step that posts workflow status (success/failure) — use the existing Telegram bot to send a short status message if the digest generation fails
- [ ] Write the workflow so it works on both `main` and `master` branches
- [ ] Add `README.md` with: project description, setup instructions (create Telegram bot, get API keys, configure GitHub secrets), how to add/remove sources, architecture diagram (text-based), example digest output

### Task 8: Quality and polish

- [ ] Ensure all tests pass: `python -m pytest tests/ -v`
- [ ] Ensure type checking passes: `python -m mypy src/ --ignore-missing-imports`
- [ ] Ensure linting passes: `python -m ruff check src/`
- [ ] Review all error messages — they should be clear and actionable (e.g., "ANTHROPIC_API_KEY environment variable is not set. Get your key at https://console.anthropic.com/")
- [ ] Verify `config.yaml` is well-documented with comments explaining each option
- [ ] Add `.env.example` file listing all possible env vars with placeholder values
- [ ] Verify the GitHub Actions workflow YAML is valid
- [ ] Ensure `README.md` is complete and includes a "Quick Start" section with step-by-step instructions
