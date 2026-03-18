# Daily News Digest

Personal daily news digest generator for a Technology Architect at a major bank.
Collects RSS/Atom feeds, summarizes them via an LLM, sends to Telegram,
and saves markdown files to the repository (for Obsidian via git-sync).

Runs on **GitHub Actions** free tier — no VPS, no paid hosting required.

## Architecture

```
config.yaml
    |
    v
collector.py  ──(feedparser + httpx)──>  RSS/Atom feeds (50 sources)
    |
    v  (Articles grouped by category, deduplicated, last 24h only)
summarizer.py ──(httpx)──────────────>  LLM API
    |                                    Anthropic Claude  (ANTHROPIC_API_KEY)
    |                                    Google Gemini     (GEMINI_API_KEY)
    |                                    Groq / LLaMA      (GROQ_API_KEY)
    |
    +──> telegram.py ──────────────────> Telegram Bot API  (MarkdownV2, chunked)
    |
    +──> markdown_writer.py ───────────> digests/YYYY-MM-DD.md
                                              |
                                  git commit by GitHub Actions
                                              |
                                   Obsidian (via git-sync plugin)
```

## Quick Start

### 1. Fork / clone the repository

```bash
git clone https://github.com/<you>/digest.git
cd digest
```

### 2. Create a Telegram bot

1. Open Telegram, find **@BotFather**, send `/newbot`.
2. Follow the prompts and save the **bot token** you receive.
3. Send any message to your new bot, then open:
   `https://api.telegram.org/bot<TOKEN>/getUpdates`
   Find your `chat.id` in the response. Alternatively use **@userinfobot**.

### 3. Get an LLM API key

Choose one provider (Gemini and Groq have free tiers):

| Provider | Console | Free tier |
|----------|---------|-----------|
| Anthropic Claude | https://console.anthropic.com/ | Trial credits |
| Google Gemini | https://aistudio.google.com/app/apikey | Yes |
| Groq | https://console.groq.com/ | Yes |

### 4. Configure the digest

Edit `config.yaml`:

```yaml
llm:
  provider: "anthropic"   # or "gemini" or "groq"
  model: "claude-sonnet-4-20250514"

digest:
  language: "ru"          # "ru" or "en"
  summary_style: "analytical"  # "analytical" | "brief" | "detailed"
```

### 5. Add GitHub Secrets

In your fork: **Settings → Secrets and variables → Actions → New repository secret**.

Add the secrets matching your chosen provider:

| Secret name           | Description                              |
|-----------------------|------------------------------------------|
| `ANTHROPIC_API_KEY`   | Anthropic API key (if using Claude)      |
| `GEMINI_API_KEY`      | Gemini API key (if using Gemini)         |
| `GROQ_API_KEY`        | Groq API key (if using Groq)             |
| `TELEGRAM_BOT_TOKEN`  | Telegram bot token from @BotFather       |
| `TELEGRAM_CHAT_ID`    | Your Telegram chat ID                    |

### 6. Enable the workflow

The digest runs daily at **06:00 UTC** (11:00 Tashkent time, UTC+5).

You can also trigger it manually:
**Actions → Daily News Digest → Run workflow**

Generated digest files appear in `digests/` and are committed back to the repo automatically.

## Running locally

```bash
# Install dependencies
pip install -r requirements.txt

# Copy and fill in credentials
cp .env.example .env
# edit .env with your keys

# Run the digest
python -m src

# Dry-run: collect and summarize, but do not send or save
python -m src --dry-run --verbose

# Use a different config file
python -m src --config my-config.yaml
```

## Adding / removing sources

Edit the `sources` list in `config.yaml`. Each source has:

```yaml
sources:
  - name: "My Feed"          # human-readable label used in the digest
    url: "https://..."       # RSS or Atom feed URL
    category: "AI & LLM"    # groups sources together in the digest
    enabled: true            # set to false to temporarily disable
```

Pre-configured categories:
- **Banking & Fintech** — Finextra, PYMNTS, Finovate
- **AI & LLM** — MIT Technology Review, The Batch, Hugging Face Blog, Ars Technica
- **Enterprise Architecture** — InfoQ, ThoughtWorks, Martin Fowler, Hacker News Best
- **Geopolitics & CIS** — Spot.uz, Kun.uz

## Digest format

The digest uses a **three perspectives** format for the most important news in each category:

```
### AI & LLM

**OpenAI releases GPT-5** — [OpenAI Blog](https://...)

Short analytical summary of the news item.

🟢 **Optimist** — This is a breakthrough that will 10x developer productivity
and finally bring AI agents to production-grade reliability.

🔴 **Skeptic** — Benchmark inflation and hallucinations persist. Enterprise
adoption will lag 12–18 months behind the hype cycle as usual.

⚖️ **Realist** — A genuine capability jump, but integration complexity means
most teams will benefit gradually over 6–12 months, not overnight.
```

Minor news items receive a standard 2–3 sentence analytical comment without perspectives.

Summary styles (set via `digest.summary_style`):
- `analytical` — trend analysis + three perspectives for top topics (default)
- `brief` — one sentence per item, no perspectives
- `detailed` — full context, three perspectives for all items

## Example digest output

```markdown
---
title: "Daily Digest 2026-03-14"
date: "2026-03-14"
sources_count: 13
articles_count: 24
llm_provider: anthropic
tags:
  - digest
  - daily
---

## 🏦 Banking & Fintech

**Visa launches real-time cross-border payment rail** — [Finextra](https://...)

Visa's new infrastructure targets B2B corridor payments in 40 countries...

🟢 **Optimist** — ...
🔴 **Skeptic** — ...
⚖️ **Realist** — ...

---

## 🤖 AI & LLM

...

## 📈 Ключевые тренды дня

1. Real-time payments continue displacing correspondent banking...
2. ...
```

## Development

```bash
# Run tests
python -m pytest tests/ -v

# Type checking
python -m mypy src/ --ignore-missing-imports

# Lint
python -m ruff check src/
```

## Environment variables

See `.env.example` for the full list. All variables are optional — the script
degrades gracefully: if Telegram credentials are missing, it skips delivery but
still saves the markdown file.
