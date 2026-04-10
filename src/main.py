"""Main entrypoint for the daily news digest generator v2.

Run as:
    python -m src
    python -m src --config path/to/config.yaml
    python -m src --dry-run
    python -m src --verbose
    python -m src --check
    python -m src --radar-only
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.irritator.narrative_extractor import Narrative
    from src.irritator.ranker import RankedSignal


def _clean_summary(text: str) -> str:
    """Remove LLM artifacts: greetings, redundant URLs, separators."""
    # Remove redundant URL lines
    text = re.sub(
        r"(?m)^\s*(Link|URL|Source|Read more|Ссылка|Источник|Читать далее)\s*:\s*https?://\S+\s*$",
        "",
        text,
    )
    # Remove LLM greetings and introductory phrases
    text = re.sub(
        r"(?m)^(Добрый день|Привет|Здравствуйте|Hello|Hi)!?\s*.*?(дайджест|digest).*$",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"(?m)^(Ежедневный дайджест|Daily digest|Today'?s digest|Вот ваш ежедневный).*$",
        "",
        text,
        flags=re.IGNORECASE,
    )
    # Remove standalone horizontal rules between categories
    text = re.sub(r"(?m)^\s*---\s*$", "", text)
    # Normalize "Категория «X» содержит N статей" → "## X"
    text = re.sub(
        r"(?m)^([\U0001f300-\U0001faff\u2600-\u27bf]?\s*)Категория\s*[«\"](.*?)[»\"]\s*(?:содержит.*)?$",
        r"## \1\2",
        text,
    )
    # Collapse 3+ consecutive blank lines to 2
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


async def check_config(config_path: str) -> int:
    """Validate configuration and check required environment variables."""
    from src.config import load_config

    try:
        config = load_config(config_path)
    except (FileNotFoundError, ValueError) as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 1

    errors: list[str] = []
    provider_env_map = {
        "gemini": "GEMINI_API_KEY",
        "groq": "GROQ_API_KEY",
        "deepseek": "DEEPSEEK_API_KEY",
    }
    for p in config.llm.providers:
        env_var = provider_env_map.get(p.name)
        if env_var and not os.environ.get(env_var):
            errors.append(f"Missing env var {env_var} for provider '{p.name}'")
    if config.telegram.enabled:
        for var in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
            if not os.environ.get(var):
                errors.append(f"Missing env var {var} (required for telegram delivery)")

    if errors:
        for err in errors:
            print(f"  WARNING: {err}", file=sys.stderr)
        # Warn but don't fail — keys may be set at runtime
    print(
        f"Config OK: {len(config.sources)} sources "
        f"({len(config.enabled_sources)} enabled), "
        f"providers={[p.name for p in config.llm.providers]}"
    )
    return 0


async def run(
    config_path: str, dry_run: bool, radar_only: bool, verbose: bool
) -> int:
    """Full pipeline: radar → (irritator) → delivery."""
    from src.config import load_config
    from src.radar import collect, save_dedup_cache, summarize_all

    config = load_config(config_path)
    logger = logging.getLogger(__name__)
    logger.info(
        "Starting digest pipeline dry_run=%s radar_only=%s", dry_run, radar_only
    )

    # Radar pipeline (Phase 1)
    articles_by_category, cache = await collect(config)
    if not articles_by_category:
        logger.info("No new articles found. Nothing to summarize.")
        return 0

    summaries, trends = await summarize_all(articles_by_category, config)
    if not summaries:
        logger.error("All category summarizations failed.")
        return 1

    combined = "\n\n".join(s.summary_text for s in summaries)
    if trends:
        combined = f"{combined}\n\n{trends}"
    combined = _clean_summary(combined)

    if not dry_run:
        save_dedup_cache(cache)

    if radar_only:
        print(combined)
        return 0

    # Irritator pipeline (Phase 2-5)
    from src.irritator import (
        extract_narratives,
        generate_queries,
        rank_signals,
        search_all_sources,
        validate_signals,
    )

    narratives: list[Narrative] = []
    all_ranked: list[RankedSignal] = []
    try:
        narratives = await extract_narratives(summaries, config)
        if narratives:
            queries_by_narrative = await generate_queries(narratives, config)
            all_queries = [
                q for qs in queries_by_narrative.values() for q in qs
            ]
            if all_queries:
                import httpx

                async with httpx.AsyncClient() as client:
                    raw_signals = await search_all_sources(
                        all_queries, config, client
                    )
                signals = validate_signals(
                    raw_signals, config.filters.blocklist_keywords
                )
                for narrative in narratives:
                    ranked = await rank_signals(narrative, signals, config)
                    all_ranked.extend(ranked)
    except Exception as exc:
        logger.error("Irritator pipeline failed: %s", exc)

    if verbose and narratives:
        print("\n=== DOMINANT NARRATIVES ===\n")
        for i, n in enumerate(narratives, 1):
            print(f"{i}. {n.claim}")
            print(f"   Category: {n.category}")
            print("   Assumptions:")
            for a in n.implicit_assumptions:
                print(f"     - {a}")
            print(f"   Why challenge: {n.why_worth_challenging}\n")

    if verbose and all_ranked:
        print("\n=== COUNTER-SIGNALS ===\n")
        for r in all_ranked:
            print(f"[{r.score}/10] {r.signal.title}")
            print(f"   {r.signal.url}")
            print(f"   Narrative: {r.narrative_claim[:60]}")
            print(f"   Reasoning: {r.reasoning}\n")

    # Delivery (Phase 6)
    if dry_run:
        print(combined)
        if all_ranked:
            print("\n=== COUNTER-SIGNALS ===\n")
            for r in all_ranked:
                print(f"[{r.score}/10] {r.signal.title}")
                print(f"   {r.signal.url}")
                print(f"   {r.reasoning}\n")
        return 0

    from src.delivery import send_article_cards, send_counter_signals, send_radar, write_digest

    total_articles = sum(len(arts) for arts in articles_by_category.values())
    total_sources = len(articles_by_category)

    # Obsidian markdown
    md_path = write_digest(
        combined,
        config,
        ranked_signals=all_ranked or None,
        sources_count=total_sources,
        articles_count=total_articles,
    )
    if md_path:
        logger.info("Markdown digest written to %s", md_path)

    # Telegram
    if config.telegram.enabled:
        article_source_map = await send_article_cards(
            articles_by_category, config
        )
        if article_source_map:
            logger.info(
                "Sent %d article cards to Telegram", len(article_source_map)
            )
        tg_ok = await send_radar(combined, config)
        if tg_ok:
            logger.info("Radar digest sent to Telegram")
        if all_ranked:
            await send_counter_signals(all_ranked, config)

    return 0


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Daily News Digest — Radar + Irritator v2"
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to config.yaml (default: config.yaml)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run pipeline without sending to Telegram or writing files",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Validate config and env vars, then exit",
    )
    parser.add_argument(
        "--radar-only",
        action="store_true",
        help="Run only the radar pipeline (skip irritator)",
    )
    args = parser.parse_args(argv)
    _setup_logging(args.verbose)

    if args.check:
        return await check_config(args.config)
    return await run(args.config, args.dry_run, args.radar_only, args.verbose)
