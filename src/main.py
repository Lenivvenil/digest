"""Main entrypoint for the daily news digest generator.

Run as:
    python -m src
    python -m src --config path/to/config.yaml
    python -m src --dry-run
    python -m src --verbose
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from dataclasses import dataclass

from src.collector import collect
from src.config import load_config
from src.markdown_writer import write_digest
from src.summarizer import build_prompt, get_provider
from src.telegram import send_digest

logger = logging.getLogger(__name__)


@dataclass
class RunStats:
    feeds_fetched: int
    articles_collected: int
    new_articles: int
    digest_length: int
    telegram_sent: bool
    markdown_saved: bool
    markdown_path: str


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Daily news digest generator — collects RSS feeds, summarizes via LLM, "
        "delivers to Telegram and saves markdown for Obsidian.",
        prog="python -m src",
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        metavar="PATH",
        help="Path to config YAML file (default: config.yaml)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Collect and summarize but do not send to Telegram or write markdown files.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug-level logging.",
    )
    return parser.parse_args(argv)


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )


def _print_stats(stats: RunStats) -> None:
    print("\n--- Digest Run Summary ---")
    print(f"Feeds fetched:      {stats.feeds_fetched}")
    print(f"Articles collected: {stats.articles_collected}")
    print(f"New articles:       {stats.new_articles}")
    print(f"Digest length:      {stats.digest_length} chars")
    print(f"Telegram sent:      {'yes' if stats.telegram_sent else 'no'}")
    if stats.markdown_saved:
        print(f"Markdown saved:     {stats.markdown_path}")
    else:
        print("Markdown saved:     no")
    print("--------------------------\n")


async def run(config_path: str = "config.yaml", dry_run: bool = False) -> RunStats:
    """Execute the full digest pipeline and return run statistics.

    Args:
        config_path: Path to the YAML config file.
        dry_run: If True, skip delivery (Telegram + markdown write).

    Returns:
        RunStats with counters and delivery status.
    """
    config = load_config(config_path)
    feeds_count = len(config.enabled_sources)
    logger.info("Starting digest run: %d enabled sources, dry_run=%s", feeds_count, dry_run)

    articles_by_category = await collect(config)
    total_articles = sum(len(v) for v in articles_by_category.values())
    logger.info("Collected %d new articles across %d categories", total_articles, len(articles_by_category))

    if not articles_by_category:
        logger.info("No new articles found. Nothing to summarize.")
        return RunStats(
            feeds_fetched=feeds_count,
            articles_collected=0,
            new_articles=0,
            digest_length=0,
            telegram_sent=False,
            markdown_saved=False,
            markdown_path="",
        )

    prompt = build_prompt(articles_by_category, config)
    provider = get_provider(config)
    logger.info("Summarizing with provider=%s model=%s", config.llm.provider, config.llm.model)
    summary = await provider.summarize(prompt)
    logger.info("Summary generated: %d chars", len(summary))

    telegram_sent = False
    markdown_saved = False
    markdown_path = ""

    if dry_run:
        logger.info("Dry-run mode: skipping delivery.")
    else:
        # Run telegram delivery and markdown write in parallel
        telegram_task = asyncio.create_task(send_digest(summary, config))
        markdown_result = write_digest(
            summary,
            config,
            sources_count=feeds_count,
            articles_count=total_articles,
        )

        try:
            telegram_sent = await telegram_task
        except Exception as exc:
            logger.warning("Telegram delivery failed (non-critical): %s", exc)

        if markdown_result is not None:
            markdown_saved = True
            markdown_path = str(markdown_result)

    return RunStats(
        feeds_fetched=feeds_count,
        articles_collected=total_articles,
        new_articles=total_articles,
        digest_length=len(summary),
        telegram_sent=telegram_sent,
        markdown_saved=markdown_saved,
        markdown_path=markdown_path,
    )


async def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint. Returns exit code (0 = success, 1 = critical failure)."""
    args = _parse_args(argv)
    _setup_logging(args.verbose)

    try:
        stats = await run(config_path=args.config, dry_run=args.dry_run)
        _print_stats(stats)
        return 0
    except FileNotFoundError as exc:
        logger.error("Config file not found: %s", exc)
        return 1
    except ValueError as exc:
        logger.error("Configuration error: %s", exc)
        return 1
    except EnvironmentError as exc:
        logger.error("Environment setup error: %s", exc)
        return 1
    except RuntimeError as exc:
        logger.error("LLM error — digest generation failed: %s", exc)
        return 1
    except Exception as exc:
        logger.error("Unexpected error: %s", exc, exc_info=True)
        return 1
