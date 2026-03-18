"""Main entrypoint for the daily news digest generator.

Run as:
    python -m src
    python -m src --config path/to/config.yaml
    python -m src --dry-run
    python -m src --verbose
    python -m src --discover
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone

from src.collector import AllFeedsFailedError, collect, save_dedup_cache
from src.config import load_config
from src.feedback import (
    collect_feedback,
    get_source_feedback_score,
    load_feedback,
    save_feedback,
)
from src.markdown_writer import write_digest
from src.source_scorer import (
    apply_trial_decisions,
    calculate_effective_priorities,
    evaluate_trial_sources,
    load_stats,
    save_stats,
)
from src.summarizer import build_prompt, get_provider
from src.telegram import TelegramPartialDeliveryError, send_digest

logger = logging.getLogger(__name__)


def _prune_digest_sources_map(
    mapping: dict[str, list[str]], max_entries: int = 30
) -> None:
    """Remove oldest entries from digest_sources_map to prevent unbounded growth."""
    if len(mapping) <= max_entries:
        return
    sorted_keys = sorted(mapping.keys())
    for key in sorted_keys[: len(sorted_keys) - max_entries]:
        del mapping[key]


@dataclass
class RunStats:
    feeds_fetched: int
    new_articles: int
    digest_length: int
    telegram_sent: bool
    telegram_partial: bool
    markdown_saved: bool
    markdown_path: str
    sources_promoted: int = 0
    sources_demoted: int = 0
    feedback_collected: int = 0


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
    parser.add_argument(
        "--discover",
        action="store_true",
        help="Use LLM to suggest new RSS sources for underrepresented categories, then exit.",
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
    print(f"New articles:       {stats.new_articles}")
    print(f"Digest length:      {stats.digest_length} chars")
    if stats.telegram_partial:
        print("Telegram sent:      partial (some chunks failed)")
    else:
        print(f"Telegram sent:      {'yes' if stats.telegram_sent else 'no'}")
    if stats.markdown_saved:
        print(f"Markdown saved:     {stats.markdown_path}")
    else:
        print("Markdown saved:     no")
    if stats.feedback_collected:
        print(f"Feedback collected: {stats.feedback_collected}")
    if stats.sources_promoted:
        print(f"Sources promoted:   {stats.sources_promoted}")
    if stats.sources_demoted:
        print(f"Sources demoted:    {stats.sources_demoted}")
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

    # Load source stats and feedback
    cache_dir = ".cache"
    source_stats = load_stats(cache_dir)
    feedback_store = load_feedback(cache_dir)
    effective_priorities: dict[str, int] | None = None
    feedback_collected = 0

    if config.adaptive.enabled:
        # Poll new Telegram feedback
        bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        if bot_token:
            old_count = len(feedback_store.ratings)
            feedback_store = await collect_feedback(bot_token, feedback_store)
            feedback_collected = len(feedback_store.ratings) - old_count
            if feedback_collected:
                logger.info("Collected %d new feedback ratings", feedback_collected)

        # Calculate effective priorities
        feedback_scores: dict[str, float] = {}
        for source in config.enabled_sources:
            score = get_source_feedback_score(feedback_store, source.name)
            if score is not None:
                feedback_scores[source.name] = score
        effective_priorities = calculate_effective_priorities(
            config.enabled_sources, source_stats, feedback_scores, config.adaptive
        )
        logger.info("Computed effective priorities for %d sources", len(effective_priorities))

    articles_by_category, pending_cache = await collect(
        config, source_stats=source_stats, effective_priorities=effective_priorities
    )
    total_articles = sum(len(v) for v in articles_by_category.values())
    logger.info("Collected %d new articles across %d categories", total_articles, len(articles_by_category))

    if not articles_by_category:
        logger.info("No new articles found. Nothing to summarize.")
        if not dry_run:
            save_dedup_cache(pending_cache)
            save_stats(source_stats, cache_dir)
            save_feedback(feedback_store, cache_dir)
        return RunStats(
            feeds_fetched=feeds_count,
            new_articles=0,
            digest_length=0,
            telegram_sent=False,
            telegram_partial=False,
            markdown_saved=False,
            markdown_path="",
            feedback_collected=feedback_collected,
        )

    # Record which sources contributed to this digest so that chunk-level
    # Telegram feedback can be distributed to the right sources next run.
    # Updated after delivery — see below.
    contributing_sources = sorted(
        {a.source for articles in articles_by_category.values() for a in articles}
    )
    digest_id = (
        datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
        if config.adaptive.enabled
        else ""
    )

    prompt = build_prompt(articles_by_category, config)
    provider = get_provider(config)
    logger.info("Summarizing with provider=%s model=%s", config.llm.provider, config.llm.model)
    summary = await provider.summarize(prompt)
    logger.info("Summary generated: %d chars", len(summary))

    telegram_sent = False
    telegram_partial = False
    markdown_saved = False
    markdown_path = ""

    if dry_run:
        logger.info("Dry-run mode: skipping delivery.")
    else:
        # Run telegram delivery and markdown write in parallel
        telegram_task = asyncio.create_task(send_digest(summary, config, digest_id=digest_id))
        markdown_result = write_digest(
            summary,
            config,
            sources_count=feeds_count,
            articles_count=total_articles,
        )

        try:
            telegram_sent = await telegram_task
        except TelegramPartialDeliveryError as exc:
            # Some chunks were delivered but later chunks failed.
            # Do NOT persist the cache here — we need to know whether the
            # markdown fallback succeeded first (see logic below).
            logger.warning("Telegram delivery partially failed (non-critical): %s", exc)
            telegram_partial = True
        except Exception as exc:
            logger.warning("Telegram delivery failed (non-critical): %s", exc)

        if markdown_result is not None:
            markdown_saved = True
            markdown_path = str(markdown_result)

        # Persist dedup cache only when at least one channel delivered the
        # digest.  If both Telegram and markdown output are disabled or fail,
        # do not mark articles as seen so they are retried on the next run.
        #
        # For partial Telegram delivery: save the cache only when the markdown
        # fallback also succeeded.  The markdown file contains the full digest,
        # so the user can read everything there.  If markdown was NOT written,
        # articles from the failed chunks would be permanently lost — so we
        # intentionally skip saving the cache so they are picked up on the
        # next run.
        if telegram_sent or markdown_saved:
            save_dedup_cache(pending_cache)
        elif telegram_partial and not markdown_saved:
            logger.warning(
                "Partial Telegram delivery with no markdown fallback — "
                "dedup cache NOT updated; articles will be retried on the next run."
            )
        elif not (telegram_sent or markdown_saved or telegram_partial):
            logger.warning(
                "No delivery channel produced output — dedup cache not updated; "
                "articles will be retried on the next run."
            )

        # Only update last_digest_sources when Telegram delivery succeeded,
        # because feedback buttons only exist in Telegram messages.  If we
        # always overwrite, a failed run would cause the *next* run to
        # attribute feedback from the previous successful digest to the
        # wrong set of sources.
        if telegram_sent:
            feedback_store.last_digest_sources = contributing_sources
            feedback_store.digest_sources_map[digest_id] = contributing_sources
            # Prune old entries (keep last 30 days)
            _prune_digest_sources_map(feedback_store.digest_sources_map, max_entries=30)

        # Save source stats and feedback
        save_stats(source_stats, cache_dir)
        save_feedback(feedback_store, cache_dir)

    # Evaluate trial sources after delivery
    sources_promoted = 0
    sources_demoted = 0
    if config.adaptive.enabled and not dry_run:
        today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
        promote, demote, needs_start = evaluate_trial_sources(
            config.enabled_sources, source_stats, today
        )
        if promote or demote or needs_start:
            apply_trial_decisions(config_path, promote, demote, needs_start=needs_start)
            sources_promoted = len(promote)
            sources_demoted = len(demote)
            logger.info(
                "Trial evaluation: %d promoted, %d demoted",
                sources_promoted,
                sources_demoted,
            )

    return RunStats(
        feeds_fetched=feeds_count,
        new_articles=total_articles,
        digest_length=len(summary),
        telegram_sent=telegram_sent,
        telegram_partial=telegram_partial,
        markdown_saved=markdown_saved,
        markdown_path=markdown_path,
        sources_promoted=sources_promoted,
        sources_demoted=sources_demoted,
        feedback_collected=feedback_collected,
    )


async def discover_sources(config_path: str) -> int:
    """Use LLM to suggest new RSS sources for underrepresented categories.

    Builds a prompt with current categories and source names, asks the LLM
    to suggest 2-3 RSS feed URLs, validates them by attempting to fetch,
    and prints valid suggestions to stdout.
    """
    import httpx

    config = load_config(config_path)
    categories: dict[str, list[str]] = {}
    for source in config.enabled_sources:
        categories.setdefault(source.category, []).append(source.name)

    category_summary = "\n".join(
        f"- {cat}: {', '.join(names)}" for cat, names in categories.items()
    )
    prompt = (
        "You are an expert at finding high-quality RSS/Atom feeds for technology professionals.\n\n"
        f"Current categories and sources:\n{category_summary}\n\n"
        "Suggest 2-3 new RSS feed URLs for categories that are underrepresented or missing. "
        "Focus on feeds relevant to a Technology Architect at a bank: "
        "architecture, distributed systems, fintech, security, cloud infrastructure.\n\n"
        "For each suggestion, output EXACTLY this format (one per line):\n"
        "FEED|<url>|<category>|<name>\n\n"
        "Only suggest feeds you are confident have working RSS/Atom URLs."
    )

    provider = get_provider(config)
    logger.info("Asking LLM for source suggestions...")
    response = await provider.summarize(prompt)

    suggestions: list[tuple[str, str, str]] = []
    for line in response.strip().splitlines():
        line = line.strip()
        if not line.startswith("FEED|"):
            continue
        parts = line.split("|")
        if len(parts) != 4:
            continue
        _, url, category, name = parts
        suggestions.append((url.strip(), category.strip(), name.strip()))

    if not suggestions:
        print("No suggestions returned by LLM.")
        return 0

    print(f"\nValidating {len(suggestions)} suggested feeds...\n")
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        for url, category, name in suggestions:
            try:
                resp = await client.get(url)
                resp.raise_for_status()
                status = "OK"
            except Exception as exc:
                status = f"FAILED ({exc})"
            print(f"  [{status}] {name}")
            print(f"    URL:      {url}")
            print(f"    Category: {category}")
            print()

    print("Add valid sources to config.yaml manually.")
    return 0


async def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint. Returns exit code (0 = success, 1 = critical failure)."""
    args = _parse_args(argv)
    _setup_logging(args.verbose)

    try:
        if args.discover:
            return await discover_sources(config_path=args.config)

        stats = await run(config_path=args.config, dry_run=args.dry_run)
        _print_stats(stats)
        # Partial Telegram delivery with no markdown fallback means the user
        # received an incomplete digest and has no way to see the full content.
        # Exit 1 so GitHub Actions triggers the failure notification step.
        if stats.telegram_partial and not stats.markdown_saved:
            return 1
        # If new articles were collected but nothing was delivered to any channel
        # (both Telegram and markdown failed/disabled), treat as a critical failure
        # so the failure notification step fires.
        if (
            not args.dry_run
            and stats.new_articles > 0
            and not (stats.telegram_sent or stats.markdown_saved or stats.telegram_partial)
        ):
            logger.error(
                "No delivery channel produced output despite %d new articles — "
                "check Telegram credentials and markdown_to_repo config.",
                stats.new_articles,
            )
            return 1
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
    except AllFeedsFailedError as exc:
        logger.error("All feeds failed: %s", exc)
        return 1
    except RuntimeError as exc:
        logger.error("LLM error — digest generation failed: %s", exc)
        return 1
    except Exception as exc:
        logger.error("Unexpected error: %s", exc, exc_info=True)
        return 1
