# Daily News Digest

Personal daily news digest generator for a Technology Architect.
Collects RSS/Atom feeds, summarizes them via an LLM, sends to Telegram,
and saves markdown files to the repository (for Obsidian via git-sync).

Runs on **GitHub Actions** free tier — no VPS, no paid hosting required.

## Architecture

```
config.yaml
    |
    v
collector.py  ──(feedparser + httpx)──>  RSS/Atom feeds
    |
    v  (list of Articles, grouped by category)
summarizer.py ──(httpx)──────────────>  LLM API (Anthropic / Gemini / Groq)
    |
    +──> telegram.py ──────────────────> Telegram Bot API
    |
    +──> markdown_writer.py ───────────> digests/YYYY-MM-DD.md
                                              |
                                         (git commit by GH Actions)
                                              |
                                         Obsidian (via git-sync)
```

## Quick Start

### 1. Fork / clone the repository

```bash
git clone https://github.com/<you>/digest.git
cd digest
```

### 2. Create a Telegram bot

1. Open Telegram, find **@BotFather**, send `/newbot`.
2. Save the **bot token** you receive.
3. Send any message to your new bot, then open:
   `https://api.telegram.org/bot<TOKEN>/getUpdates`
   and find your `chat.id` (or use @userinfobot).

### 3. Get an LLM API key

Choose one (all have free tiers except Anthropic, which has a generous trial):

- **Anthropic Claude** — https://console.anthropic.com/
- **Google Gemini** — https://aistudio.google.com/app/apikey
- **Groq** — https://console.groq.com/

### 4. Configure the digest

Edit `config.yaml` to select your LLM provider, language, and sources.

### 5. Add GitHub Secrets

In your fork: Settings → Secrets and variables → Actions → New repository secret.

Add the secrets matching your chosen provider:

| Secret name           | Description                        |
|-----------------------|------------------------------------|
| `ANTHROPIC_API_KEY`   | Anthropic API key (if using Claude)|
| `GEMINI_API_KEY`      | Gemini API key (if using Gemini)   |
| `GROQ_API_KEY`        | Groq API key (if using Groq)       |
| `TELEGRAM_BOT_TOKEN`  | Telegram bot token                 |
| `TELEGRAM_CHAT_ID`    | Telegram chat ID                   |

### 6. Enable the workflow

The digest runs daily at **06:00 UTC** (11:00 Tashkent time).
You can also trigger it manually: Actions → Daily News Digest → Run workflow.

## Running locally

```bash
# Install dependencies
pip install -r requirements.txt

# Copy and fill in credentials
cp .env.example .env
# edit .env with your keys

# Run
python -m src

# Dry-run (no sending, no saving)
python -m src --dry-run --verbose
```

## Adding / removing sources

Edit the `sources` list in `config.yaml`. Each source has:
- `name` — human-readable label used in the digest
- `url` — RSS or Atom feed URL
- `category` — groups sources in the digest
- `enabled` — set to `false` to temporarily disable

## Digest format

The digest uses a **three perspectives** format for the most important news:

- 🟢 **Optimist** — the bullish take, why this is an opportunity
- 🔴 **Skeptic** — risks, why this might fail or be overhyped
- ⚖️ **Realist** — balanced middle-ground assessment

Minor news items receive a standard 2-3 sentence analytical comment.

## Development

```bash
# Tests
python -m pytest tests/ -v

# Type checking
python -m mypy src/ --ignore-missing-imports

# Lint
python -m ruff check src/
```
