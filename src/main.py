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
import contextlib
import logging
import os
import re
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
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
    SourceStats,
    apply_trial_decisions,
    calculate_effective_priorities,
    calculate_score,
    evaluate_trial_sources,
    load_stats,
    save_stats,
)
from src.summarizer import (
    build_category_prompt,
    build_trends_prompt,
    get_provider,
    resolve_category_providers,
)
from src.telegram import TelegramPartialDeliveryError, send_category_feedback_message, send_digest

logger = logging.getLogger(__name__)


def _clean_summary(text: str) -> str:
    """Remove redundant URL lines that duplicate links already in article titles.

    LLM providers sometimes emit standalone link lines like:
      Link: https://...   URL: https://...   Source: https://...
      Ссылка: https://... Источник: https://...
    These are redundant because titles are already formatted as [Title](url).
    """
    return re.sub(
        r"(?m)^\s*(Link|URL|Source|Read more|Ссылка|Источник|Читать далее)\s*:\s*https?://\S+\s*$",
        "",
        text,
    ).strip()


def _prune_digest_sources_map(
    mapping: dict[str, Any], max_entries: int = 30
) -> None:
    """Remove oldest entries from digest_sources_map to prevent unbounded growth."""
    if len(mapping) <= max_entries:
        return
    sorted_keys = sorted(mapping.keys())
    for key in sorted_keys[: len(sorted_keys) - max_entries]:
        del mapping[key]


def _build_nano_status(
    feeds_count: int,
    total_articles: int,
    ok_count: int,
    err_count: int,
    source_stats: dict[str, SourceStats],
    config: Any,
    effective_priorities: dict[str, int] | None,
) -> str:
    """Build a two-line status footer for the digest message.

    Line 1: feed counts for this run and LLM model.
    Line 2: adaptive priority changes and average quality score.
    """
    # Collect unique provider names from the providers chain + routing
    provider_names: list[str] = []
    seen: set[str] = set()
    for pc in config.llm.providers:
        if pc.name not in seen:
            provider_names.append(pc.name)
            seen.add(pc.name)
    for route in config.llm.routing:
        if route.provider not in seen:
            provider_names.append(route.provider)
            seen.add(route.provider)
    models_str = ", ".join(provider_names) if provider_names else config.llm.model

    line1 = (
        f"\U0001f4ca {feeds_count} src | {total_articles} art | "
        f"{ok_count} ok / {err_count} err | {models_str}"
    )

    promoted_count = 0
    demoted_count = 0
    if effective_priorities:
        for source in config.enabled_sources:
            ep = effective_priorities.get(source.name)
            if ep is not None:
                if ep > source.priority:
                    promoted_count += 1
                elif ep < source.priority:
                    demoted_count += 1

    scores = [calculate_score(s) for s in source_stats.values() if s.total_fetches > 0]
    avg_score = sum(scores) / len(scores) if scores else 0.0

    line2 = (
        f"\U0001f4c8 {promoted_count} \u2191 | {demoted_count} \u2193 | "
        f"avg score: {avg_score:.2f}"
    )
    return f"{line1}\n{line2}"


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
    duration_seconds: float = 0.0


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
    parser.add_argument(
        "--check",
        action="store_true",
        help="Validate config and probe all feed URLs, then exit (0 = all OK, 1 = any failures).",
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
    _t_run_start = time.monotonic()
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
        # Poll new Telegram feedback — skip in dry-run to avoid advancing
        # last_update_id without persisting, which would cause duplicate
        # ratings on the next real run.
        if not dry_run:
            bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
            if bot_token:
                old_count = len(feedback_store.ratings)
                feedback_store = await collect_feedback(bot_token, feedback_store)
                feedback_collected = len(feedback_store.ratings) - old_count
                if feedback_collected:
                    logger.info("Collected %d new feedback ratings", feedback_collected)
                # Persist updated last_update_id immediately so that
                # already-answered callbacks are not reprocessed if a later
                # stage (collect, summarize, deliver) raises an exception.
                save_feedback(feedback_store, cache_dir)

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

    # Snapshot fetch counters before collect so we can compute per-run ok/err counts
    _stats_snapshot: dict[str, tuple[int, int]] = {
        name: (s.total_fetches, s.successful_fetches) for name, s in source_stats.items()
    }

    _t_collect_start = time.monotonic()
    articles_by_category, pending_cache = await collect(
        config, source_stats=source_stats, effective_priorities=effective_priorities
    )
    _t_collect = time.monotonic() - _t_collect_start
    logger.info("Stage: collect %.1fs", _t_collect)
    total_articles = sum(len(v) for v in articles_by_category.values())
    logger.info("Collected %d new articles across %d categories", total_articles, len(articles_by_category))

    # Compute ok/err counts from the delta between snapshot and current stats
    ok_count = 0
    err_count = 0
    for source in config.enabled_sources:
        name = source.name
        if name in source_stats:
            old_total, old_ok = _stats_snapshot.get(name, (0, 0))
            new_total = source_stats[name].total_fetches
            new_ok = source_stats[name].successful_fetches
            if new_total > old_total:
                if new_ok > old_ok:
                    ok_count += 1
                else:
                    err_count += 1

    if not articles_by_category:
        logger.info("No new articles found. Nothing to summarize.")
        if not dry_run:
            save_dedup_cache(pending_cache)
            save_stats(source_stats, cache_dir, active_sources={s.name for s in config.enabled_sources})
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

    _t_summarize_start = time.monotonic()
    default_provider = get_provider(config)
    category_providers = resolve_category_providers(
        list(articles_by_category.keys()), config, default_chain=default_provider
    )
    logger.info(
        "Summarizing %d categories in parallel (providers: %s)",
        len(articles_by_category),
        ", ".join(sorted({p.name for p in config.llm.providers})),
    )

    async def _summarize_one(
        cat: str, arts: list[Any], prov: Any
    ) -> tuple[str, str]:
        prompt = build_category_prompt(cat, arts, config)
        return cat, _clean_summary(await prov.summarize(prompt))

    gather_tasks = [
        _summarize_one(cat, arts, category_providers.get(cat, default_provider))
        for cat, arts in articles_by_category.items()
    ]
    gather_results = await asyncio.gather(*gather_tasks, return_exceptions=True)

    category_summaries: dict[str, str] = {}
    for res in gather_results:
        if isinstance(res, BaseException):
            logger.error("Category summarization failed: %s", res)
        else:
            cat_name, cat_text = res
            category_summaries[cat_name] = cat_text

    if not category_summaries:
        raise RuntimeError("All category summarizations failed — no content to deliver.")

    combined = "\n\n".join(category_summaries.values())

    # Generate trends section separately across all categories (if >1 category)
    if len(category_summaries) > 1:
        trends_prompt = build_trends_prompt(category_summaries, config)
        try:
            trends = _clean_summary(await default_provider.summarize(trends_prompt))
            combined = f"{combined}\n\n{trends}"
        except Exception as exc:
            logger.warning("Trends generation failed, skipping: %s", exc)

    summary = combined
    _t_summarize = time.monotonic() - _t_summarize_start
    logger.info("Stage: summarize %.1fs", _t_summarize)
    logger.info("Summary generated: %d chars", len(summary))

    # Build delivery text: summary + nano-status footer (not added to markdown/RunStats)
    nano_status = _build_nano_status(
        feeds_count=feeds_count,
        total_articles=total_articles,
        ok_count=ok_count,
        err_count=err_count,
        source_stats=source_stats,
        config=config,
        effective_priorities=effective_priorities,
    )
    delivery_text = f"{summary}\n\n{nano_status}"

    telegram_sent = False
    telegram_partial = False
    markdown_saved = False
    markdown_path = ""
    delivery_succeeded = False

    _t_deliver_start = time.monotonic()
    if dry_run:
        logger.info("Dry-run mode: skipping delivery.")
    else:
        # Run telegram delivery and markdown write in parallel
        telegram_task = asyncio.create_task(
            send_digest(
                delivery_text,
                config,
                digest_id=digest_id,
                show_feedback=config.adaptive.enabled,
            )
        )
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
            if digest_id:
                feedback_store.digest_sources_map[digest_id] = contributing_sources
            # Prune old entries (keep last 30 days)
            _prune_digest_sources_map(feedback_store.digest_sources_map, max_entries=30)

            # Build per-category source map and send category feedback buttons
            if digest_id and config.adaptive.enabled:
                cat_sources: dict[str, list[str]] = {
                    cat: sorted({a.source for a in arts})
                    for cat, arts in articles_by_category.items()
                }
                feedback_store.digest_category_sources_map[digest_id] = cat_sources
                _prune_digest_sources_map(
                    feedback_store.digest_category_sources_map, max_entries=30
                )
                await send_category_feedback_message(cat_sources, config, digest_id)

        delivery_succeeded = telegram_sent or markdown_saved
        if delivery_succeeded:
            feedback_store.last_digest_time = datetime.now(tz=timezone.utc).strftime(
                "%Y-%m-%d %H:%M UTC"
            )

        # Save feedback unconditionally — it tracks polled Telegram callbacks
        # (last_update_id) from previous digests and must be persisted even if
        # the current delivery fails, to avoid re-processing answered callbacks.
        save_feedback(feedback_store, cache_dir)

        # Always persist source stats so the adaptive system accumulates data
        # even when delivery fails (e.g. Telegram is down, markdown write fails).
        active_source_names = {s.name for s in config.enabled_sources}
        save_stats(source_stats, cache_dir, active_sources=active_source_names)
        _t_deliver = time.monotonic() - _t_deliver_start
        logger.info("Stage: deliver %.1fs", _t_deliver)

    # Evaluate trial sources only after successful delivery — promoting or
    # demoting based on a digest the user never received is misleading.
    sources_promoted = 0
    sources_demoted = 0
    if config.adaptive.enabled and not dry_run and delivery_succeeded:
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
        duration_seconds=time.monotonic() - _t_run_start,
    )


@dataclass
class _ValidatedURL:
    """Result of URL validation with pinned DNS resolution."""

    url: str
    hostname: str
    pinned_addrinfos: list[tuple[int, int, int, str, Any]]


def _validate_url(url: str) -> _ValidatedURL | None:
    """Validate URL safety by checking all resolved IPs are globally routable.

    Returns a _ValidatedURL with pinned DNS results if safe, or None if the URL
    resolves to non-global addresses (private, loopback, multicast, CGNAT, etc.).
    The pinned addrinfos must be used for the actual fetch to prevent DNS rebinding.
    """
    import ipaddress
    import socket
    from urllib.parse import urlparse

    try:
        parsed = urlparse(url)
    except Exception:
        return None

    if parsed.scheme not in ("http", "https"):
        return None

    hostname = parsed.hostname
    if not hostname:
        return None

    # Normalize IDN hostnames to ASCII/punycode so that _pin_dns
    # matches the form that httpx/socket will actually use.
    try:
        hostname = hostname.encode("idna").decode("ascii")
    except (UnicodeError, UnicodeDecodeError):
        return None

    # Reject out-of-range ports early (parsed.port raises ValueError)
    try:
        _ = parsed.port
    except ValueError:
        return None

    # Block well-known cloud metadata endpoints
    if hostname in ("localhost", "metadata.google.internal"):
        return None

    def _is_safe(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
        return addr.is_global and not addr.is_multicast

    # Use is_global as allowlist — rejects private, loopback, link-local,
    # reserved, CGNAT (100.64/10), and any other non-routable space.
    # Multicast is excluded separately (Python considers it "global").
    pinned_addrinfos: list[tuple[int, int, int, str, Any]] = []
    try:
        addr = ipaddress.ip_address(hostname)
        if not _is_safe(addr):
            return None
        # IP literal — synthesize a single addrinfo entry
        family = socket.AF_INET6 if addr.version == 6 else socket.AF_INET
        sockaddr: Any = (
            (str(addr), 0, 0, 0) if addr.version == 6 else (str(addr), 0)
        )
        pinned_addrinfos = [(family, socket.SOCK_STREAM, 0, "", sockaddr)]
    except ValueError:
        # hostname is a DNS name — resolve and check all resulting IPs
        try:
            addrinfos = socket.getaddrinfo(
                hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM
            )
        except (socket.gaierror, UnicodeError):
            return None
        for family, type_, proto, canonname, sockaddr in addrinfos:  # type: ignore[misc]
            ip_str = str(sockaddr[0])
            try:
                addr = ipaddress.ip_address(ip_str)
                if not _is_safe(addr):
                    return None
                pinned_addrinfos.append((family, type_, proto, canonname, sockaddr))
            except ValueError:
                return None

    if not pinned_addrinfos:
        return None

    return _ValidatedURL(url=url, hostname=hostname, pinned_addrinfos=pinned_addrinfos)


def _pin_dns(
    hostname: str, addrinfos: list[tuple[int, int, int, str, Any]]
) -> contextlib.AbstractContextManager[None]:
    """Pin DNS resolution for *hostname* to pre-validated addresses.

    Prevents DNS rebinding by ensuring httpx connects to the same IPs
    that were checked during validation. Safe for sequential async I/O
    (one outstanding fetch at a time).
    """
    import socket as _socket

    @contextlib.contextmanager
    def _ctx() -> Iterator[None]:
        original = _socket.getaddrinfo

        def _pinned(
            host: str | bytes,
            port: int | str | None,
            family: int = 0,
            type: int = 0,  # noqa: A002
            proto: int = 0,
            flags: int = 0,
        ) -> list[tuple[int, int, int, str, Any]]:
            _host = host.decode("ascii") if isinstance(host, bytes) else host
            if _host == hostname:
                p = int(port) if port is not None and str(port).isdigit() else 0
                result: list[tuple[int, int, int, str, Any]] = []
                for af, st, pr, cn, sa in addrinfos:
                    if af == _socket.AF_INET:
                        result.append((af, st, pr, cn, (sa[0], p)))
                    else:
                        result.append((af, st, pr, cn, (sa[0], p, sa[2], sa[3])))
                return result
            return original(host, port, family, type, proto, flags)  # type: ignore[return-value]

        _socket.getaddrinfo = _pinned  # type: ignore[assignment]
        try:
            yield
        finally:
            _socket.getaddrinfo = original  # type: ignore[assignment]

    return _ctx()


async def check_config(config_path: str) -> int:
    """Validate config and probe all enabled feed URLs.

    Checks:
    - Config loads without errors
    - Required env vars present for the configured LLM provider and Telegram
    - No duplicate source names or URLs
    - All enabled feed URLs respond and return parseable RSS/Atom content

    Returns 0 if all checks pass, 1 if any failures.
    """
    import feedparser  # type: ignore[import-untyped]
    import httpx

    ok = True

    # --- Load config ---
    print("Checking config...")
    try:
        config = load_config(config_path)
        print(f"  [OK] Config loaded: {len(config.enabled_sources)} enabled sources")
    except Exception as exc:
        print(f"  [FAIL] Config load error: {exc}")
        return 1

    # --- Env vars ---
    print("\nChecking environment variables...")
    _provider_env_vars: dict[str, str] = {
        "anthropic": "ANTHROPIC_API_KEY",
        "gemini": "GEMINI_API_KEY",
        "groq": "GROQ_API_KEY",
        "mistral": "MISTRAL_API_KEY",
        "deepseek": "DEEPSEEK_API_KEY",
    }
    # Collect all unique provider names from providers list + routing
    all_provider_names: set[str] = {pc.name for pc in config.llm.providers}
    for route in config.llm.routing:
        all_provider_names.add(route.provider)

    telegram_vars = ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]
    for provider_name in sorted(all_provider_names):
        var = _provider_env_vars.get(provider_name, "")
        if not var:
            continue
        val = os.environ.get(var, "")
        if val:
            print(f"  [OK] {var} is set ({provider_name})")
        else:
            print(f"  [WARN] {var} is not set (required for {provider_name})")
    for var in telegram_vars:
        val = os.environ.get(var, "")
        if val:
            print(f"  [OK] {var} is set")
        else:
            print(f"  [WARN] {var} is not set (required for production)")

    # --- Duplicate detection ---
    print("\nChecking for duplicates...")
    seen_names: dict[str, int] = {}
    seen_urls: dict[str, str] = {}
    for source in config.enabled_sources:
        seen_names[source.name] = seen_names.get(source.name, 0) + 1
        if source.url in seen_urls:
            print(
                f"  [WARN] Duplicate URL: {source.url!r} used by "
                f"{seen_urls[source.url]!r} and {source.name!r}"
            )
        else:
            seen_urls[source.url] = source.name
    for name, count in seen_names.items():
        if count > 1:
            print(f"  [WARN] Duplicate source name: {name!r} appears {count} times")
    if all(c == 1 for c in seen_names.values()) and len(seen_urls) == len(config.enabled_sources):
        print("  [OK] No duplicate names or URLs")

    # --- Feed probing ---
    print(f"\nProbing {len(config.enabled_sources)} feed URLs...")

    async def _probe(client: httpx.AsyncClient, source: Any) -> tuple[str, str, str]:
        """Return (name, status_label, detail)."""
        validated = _validate_url(source.url)
        if validated is None:
            return source.name, "BLOCKED", "unsafe URL (private/local/non-http)"
        try:
            with _pin_dns(validated.hostname, validated.pinned_addrinfos):
                resp = await client.get(validated.url, timeout=15.0)
            resp.raise_for_status()
            feed = feedparser.parse(resp.text)
            if feed.bozo and not feed.entries:
                return source.name, "WARN", f"feedparser error: {feed.bozo_exception}"
            entry_count = len(feed.entries)
            return source.name, "OK", f"{entry_count} entries"
        except httpx.TimeoutException:
            return source.name, "FAIL", "timeout"
        except Exception as exc:
            return source.name, "FAIL", str(exc)

    async with httpx.AsyncClient(
        timeout=15.0, follow_redirects=True, trust_env=False
    ) as client:
        tasks = [asyncio.create_task(_probe(client, s)) for s in config.enabled_sources]
        results = await asyncio.gather(*tasks)

    for name, status, detail in sorted(results):
        print(f"  [{status:6}] {name}: {detail}")
        if status == "FAIL" or status == "BLOCKED":
            ok = False

    # --- Summary ---
    fail_count = sum(1 for _, s, _ in results if s in ("FAIL", "BLOCKED"))
    warn_count = sum(1 for _, s, _ in results if s == "WARN")
    ok_count_feeds = sum(1 for _, s, _ in results if s == "OK")
    print(
        f"\nResult: {ok_count_feeds} OK, {warn_count} WARN, {fail_count} FAIL"
        f" out of {len(config.enabled_sources)} feeds"
    )
    if ok:
        print("All checks passed.")
    else:
        print("Some checks FAILED — fix the issues above before running the digest.")

    return 0 if ok else 1


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

    async with httpx.AsyncClient(
        timeout=15.0, follow_redirects=False, trust_env=False
    ) as client:
        for url, category, name in suggestions:
            validated = _validate_url(url)
            if validated is None:
                status = "BLOCKED (unsafe URL: private/local network or non-http scheme)"
            else:
                try:
                    with _pin_dns(validated.hostname, validated.pinned_addrinfos):
                        resp = await client.get(validated.url)
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
        if args.check:
            return await check_config(config_path=args.config)

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
