"""Main entrypoint for the daily news digest generator v2.

Run as:
    python -m digest
    python -m digest --config path/to/config.yaml
    python -m digest --dry-run
    python -m digest --verbose
    python -m digest --check
    python -m digest --radar-only
    python -m digest --discover
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from digest.irritator import IrritatorStatus
    from digest.source_scorer import SourceStats


def _clean_summary(text: str) -> str:
    """Remove LLM artifacts: greetings, redundant URLs, separators."""
    text = re.sub(
        r"(?m)^\s*(Link|URL|Source|Read more|Ссылка|Источник|Читать далее)\s*:\s*https?://\S+\s*$",
        "",
        text,
    )
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
    text = re.sub(r"(?m)^\s*---\s*$", "", text)
    text = re.sub(
        r"(?m)^([\U0001f300-\U0001faff\u2600-\u27bf]?\s*)Категория\s*[«\"](.*?)[»\"]\s*(?:содержит.*)?$",
        r"## \1\2",
        text,
    )
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _build_nano_status(
    feeds_count: int,
    total_articles: int,
    ok_count: int,
    err_count: int,
    source_stats: dict[str, SourceStats],
    config: Any,
    effective_priorities: dict[str, int] | None,
) -> str:
    """Build a two-line status footer for the digest message."""
    from digest.source_scorer import calculate_score

    provider_names: list[str] = []
    seen: set[str] = set()
    for pc in config.llm.providers:
        if pc.name not in seen:
            provider_names.append(pc.name)
            seen.add(pc.name)
    for route in getattr(config.llm, "routing", []):
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


async def check_config(config_path: str) -> int:
    """Validate config, check env vars, and probe all enabled feed URLs."""
    import feedparser
    import httpx

    from digest._dns_pinning import pin_dns as _pin_dns
    from digest._dns_pinning import validate_url as _validate_url
    from digest.config import load_config

    ok = True

    print("Checking config...")
    try:
        config = load_config(config_path)
        print(f"  [OK] Config loaded: {len(config.enabled_sources)} enabled sources")
    except Exception as exc:
        print(f"  [FAIL] Config load error: {exc}")
        return 1

    print("\nChecking environment variables...")
    _provider_env_vars: dict[str, str] = {
        "anthropic": "ANTHROPIC_API_KEY",
        "gemini": "GEMINI_API_KEY",
        "groq": "GROQ_API_KEY",
        "mistral": "MISTRAL_API_KEY",
        "deepseek": "DEEPSEEK_API_KEY",
    }
    all_provider_names: set[str] = {pc.name for pc in config.llm.providers}
    for route in getattr(config.llm, "routing", []):
        all_provider_names.add(route.provider)

    for provider_name in sorted(all_provider_names):
        var = _provider_env_vars.get(provider_name, "")
        if not var:
            continue
        val = os.environ.get(var, "")
        if val:
            print(f"  [OK] {var} is set ({provider_name})")
        else:
            print(f"  [WARN] {var} is not set (required for {provider_name})")
    for var in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        val = os.environ.get(var, "")
        if val:
            print(f"  [OK] {var} is set")
        else:
            print(f"  [WARN] {var} is not set (required for production)")

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

    print(f"\nProbing {len(config.enabled_sources)} feed URLs...")

    async def _probe(client: httpx.AsyncClient, source: Any) -> tuple[str, str, str]:
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
        if status in ("FAIL", "BLOCKED"):
            ok = False

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
    """Use LLM to suggest new RSS sources for underrepresented categories."""
    import httpx

    from digest._dns_pinning import pin_dns as _pin_dns
    from digest._dns_pinning import validate_url as _validate_url
    from digest.config import load_config
    from digest.discovery import (
        PendingSource,
        load_pending,
        save_pending,
        send_source_approval_message,
    )
    from digest.llm import LLMRole, complete

    logger = logging.getLogger(__name__)
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

    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": prompt},
    ]
    logger.info("Asking LLM for source suggestions...")
    response, _ = await complete(LLMRole.SUMMARIZE, messages, config)

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

    cache_dir = ".cache"
    existing_pending = load_pending(cache_dir)
    existing_hashes = {s.source_hash for s in existing_pending}

    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    new_pending: list[PendingSource] = []

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

            if status == "OK":
                pending = PendingSource(
                    name=name,
                    url=url,
                    category=category,
                    discovered_at=datetime.now(tz=timezone.utc).isoformat(),
                )
                if pending.source_hash in existing_hashes:
                    logger.info("Source '%s' already pending, skipping", name)
                    continue
                new_pending.append(pending)
                if bot_token and chat_id:
                    await send_source_approval_message(pending, bot_token, chat_id)
                else:
                    logger.warning(
                        "TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set — "
                        "cannot send approval message for '%s'",
                        name,
                    )

    if new_pending:
        save_pending(existing_pending + new_pending, cache_dir)
        logger.info("Saved %d new pending source(s) for approval", len(new_pending))

    return 0


async def _run_irritator(
    summaries: list[Any], config: Any, verbose: bool
) -> tuple[list[Any], list[Any], IrritatorStatus]:
    """Run irritator pipeline.

    Returns (narratives, ranked_signals, IrritatorStatus).
    status is always populated so the caller can relay pipeline
    progress to the user even when no counter-signals survive ranking.
    """
    from digest.irritator import (
        IrritatorStatus,
        extract_narratives,
        generate_queries,
        rank_signals,
        search_all_sources,
        validate_signals,
    )

    logger = logging.getLogger(__name__)
    narratives: list[Any] = []
    all_ranked: list[Any] = []
    raw_signal_count = 0
    valid_signal_count = 0

    # Stage 1: Narrative extraction
    try:
        narratives = await extract_narratives(summaries, config)
    except Exception as exc:
        logger.error("Irritator: narrative extraction failed: %s", exc)
        return [], [], IrritatorStatus(f"narrative extraction failed: {exc}", "error")

    if not narratives:
        logger.warning("Irritator: no narratives extracted from %d summaries", len(summaries))
        return [], [], IrritatorStatus(f"0 narratives from {len(summaries)} summaries", "empty")

    logger.info("Irritator: extracted %d narratives", len(narratives))

    # Stage 2: Query generation
    try:
        queries_by_narrative = await generate_queries(narratives, config)
        all_queries = [q for qs in queries_by_narrative.values() for q in qs]
    except Exception as exc:
        logger.error("Irritator: query generation failed: %s", exc)
        return narratives, [], IrritatorStatus(
            f"{len(narratives)} narratives, query generation failed: {exc}", "error"
        )

    if not all_queries:
        logger.warning("Irritator: no queries generated from %d narratives", len(narratives))
        return narratives, [], IrritatorStatus(
            f"{len(narratives)} narratives, 0 queries", "empty"
        )

    logger.info("Irritator: generated %d queries", len(all_queries))

    # Stage 3: Signal search
    try:
        import httpx

        async with httpx.AsyncClient() as client:
            raw_signals = await search_all_sources(all_queries, config, client)
        raw_signal_count = len(raw_signals)
    except Exception as exc:
        logger.error("Irritator: signal search failed: %s", exc)
        return narratives, [], IrritatorStatus(
            f"{len(narratives)} narratives, {len(all_queries)} queries, search failed: {exc}",
            "error",
        )

    if not raw_signals:
        logger.warning("Irritator: no signals found from %d queries", len(all_queries))
        return narratives, [], IrritatorStatus(
            f"{len(narratives)} narratives, {len(all_queries)} queries, 0 signals", "empty"
        )

    # Stage 4: Validation
    try:
        signals = validate_signals(raw_signals, config.filters.blocklist_keywords)
    except Exception as exc:
        logger.error("Irritator: signal validation failed: %s", exc)
        return narratives, [], IrritatorStatus(
            f"{len(narratives)} narratives, {raw_signal_count} signals, validation failed: {exc}",
            "error",
        )
    valid_signal_count = len(signals)
    if not signals:
        logger.warning(
            "Irritator: all %d signals filtered by blocklist", raw_signal_count,
        )
        return narratives, [], IrritatorStatus(
            f"{len(narratives)} narratives, {raw_signal_count} signals, all filtered",
            "empty",
        )

    logger.info("Irritator: %d/%d signals passed validation", valid_signal_count, raw_signal_count)

    # Stage 5: Ranking
    for narrative in narratives:
        try:
            ranked = await rank_signals(narrative, signals, config)
            all_ranked.extend(ranked)
        except Exception as exc:
            logger.error(
                "Irritator: ranking failed for '%s': %s",
                narrative.claim[:60], exc,
            )

    status_text = (
        f"{len(narratives)} narratives, {raw_signal_count} signals, "
        f"{valid_signal_count} valid, {len(all_ranked)} passed ranking"
    )
    logger.info("Irritator: %s", status_text)

    if verbose and narratives:
        print("\n=== DOMINANT NARRATIVES ===\n")
        for i, n in enumerate(narratives, 1):
            print(f"{i}. {n.claim}")
            print(f"   Category: {n.category}")
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

    # Stage-5 ranking failures are per-narrative and logged individually above.
    # If ranking raised for every narrative, all_ranked stays empty → "empty" not "error".
    # This is intentional: partial ranking is not a pipeline failure.
    level: Literal["ok", "empty"] = "ok" if all_ranked else "empty"
    return narratives, all_ranked, IrritatorStatus(status_text, level)


def _process_pending_approvals(
    config_path: str, cache_dir: str, feedback_store: Any
) -> None:
    """Process pending source approval decisions from Telegram callbacks."""
    from digest.discovery import (
        PendingSource,
        add_source_to_config,
        load_pending,
        save_pending,
    )
    from digest.feedback import save_feedback

    logger = logging.getLogger(__name__)
    if not feedback_store.source_decisions:
        return
    pending = load_pending(cache_dir)
    if not pending:
        return
    remaining: list[PendingSource] = []
    for ps in pending:
        decision = feedback_store.source_decisions.pop(ps.source_hash, None)
        if decision == "approved":
            try:
                add_source_to_config(config_path, ps)
            except Exception as exc:
                logger.error("Failed to add source '%s' to config: %s", ps.name, exc)
                remaining.append(ps)
        elif decision == "rejected":
            logger.info("Source '%s' rejected by user, removing from pending", ps.name)
        else:
            remaining.append(ps)
    save_pending(remaining, cache_dir)
    save_feedback(feedback_store, cache_dir)


async def run(
    config_path: str, dry_run: bool, radar_only: bool, verbose: bool
) -> RunStats:
    """Full pipeline: feedback -> radar -> (irritator) -> delivery -> scoring."""
    from digest._util import cleanup_stale_tmp
    from digest.config import load_config
    from digest.feedback import (
        collect_feedback,
        get_source_feedback_score,
        load_feedback,
        save_feedback,
    )
    from digest.radar import collect, pick_top_articles, save_dedup_cache, summarize_all
    from digest.source_scorer import (
        apply_trial_decisions_to_cache,
        calculate_effective_priorities,
        evaluate_trial_sources,
        load_source_state,
        load_stats,
        save_source_state,
        save_stats,
    )

    _t_run_start = time.monotonic()
    config = load_config(config_path)
    logger = logging.getLogger(__name__)
    cache_dir = ".cache"
    source_state = load_source_state(cache_dir)
    feeds_count = len(config.enabled_sources)
    cleanup_stale_tmp(Path(cache_dir))
    source_stats = load_stats(cache_dir)
    feedback_store = load_feedback(cache_dir)
    effective_priorities: dict[str, int] | None = None
    feedback_collected = 0

    saved_update_id = feedback_store.last_update_id
    saved_ratings_count = len(feedback_store.ratings)

    if config.adaptive.enabled:
        if not dry_run:
            bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
            if bot_token:
                old_count = len(feedback_store.ratings)
                feedback_store = await collect_feedback(bot_token, feedback_store)
                feedback_collected = len(feedback_store.ratings) - old_count
                if feedback_collected:
                    logger.info("Collected %d new feedback ratings", feedback_collected)

        feedback_scores: dict[str, float] = {}
        for source in config.enabled_sources:
            score = get_source_feedback_score(feedback_store, source.name)
            if score is not None:
                feedback_scores[source.name] = score
        effective_priorities = calculate_effective_priorities(
            config.effective_sources(source_state), source_stats, feedback_scores, config.adaptive
        )

    run_config = dataclasses.replace(
        config,
        sources=[s for s in config.sources if s.enabled and not source_state.is_demoted(s.name)],
    )
    articles_by_category, cache = await collect(run_config, effective_priorities=effective_priorities)
    total_articles = sum(len(arts) for arts in articles_by_category.values())

    def _empty_stats(n_articles: int = 0) -> RunStats:
        return RunStats(
            feeds_fetched=feeds_count, new_articles=n_articles, digest_length=0,
            telegram_sent=False, telegram_partial=False,
            markdown_saved=False, markdown_path="",
            feedback_collected=feedback_collected,
        )

    if not articles_by_category:
        logger.info("No new articles found. Nothing to summarize.")
        if not dry_run:
            save_dedup_cache(cache)
            save_stats(source_stats, cache_dir, active_sources={s.name for s in config.enabled_sources})
            save_feedback(feedback_store, cache_dir)
        return _empty_stats()

    contributing_sources = sorted(
        {a.source for articles in articles_by_category.values() for a in articles}
    )

    summaries, trends = await summarize_all(articles_by_category, config)
    if not summaries:
        logger.error("All category summarizations failed.")
        return _empty_stats(total_articles)

    combined = _clean_summary(
        "\n\n".join(s.summary_text for s in summaries)
        + (f"\n\n{trends}" if trends else "")
    )

    # Pick top articles for per-article Telegram cards
    top_articles = await pick_top_articles(articles_by_category, config, max_articles=7)

    if not dry_run:
        save_dedup_cache(cache)

    if radar_only:
        print(combined)
        return RunStats(
            feeds_fetched=feeds_count, new_articles=total_articles,
            digest_length=len(combined), telegram_sent=False,
            telegram_partial=False, markdown_saved=False, markdown_path="",
            feedback_collected=feedback_collected,
        )

    # Irritator pipeline
    _, all_ranked, irritator_status = await _run_irritator(summaries, config, verbose)

    # Dry-run output
    if dry_run:
        print(combined)
        if top_articles:
            print("\n=== TOP ARTICLES ===\n")
            for a in top_articles:
                print(f"[{a.category}] {a.title}")
                print(f"  {a.summary}\n")
        for r in all_ranked:
            print(f"[{r.score}/10] {r.signal.title} — {r.signal.url}")
        print(f"\n💢 Irritator: {irritator_status.text}")
        return RunStats(
            feeds_fetched=feeds_count, new_articles=total_articles,
            digest_length=len(combined), telegram_sent=False,
            telegram_partial=False, markdown_saved=False, markdown_path="",
            feedback_collected=feedback_collected,
        )

    # Delivery
    from digest.delivery import send_article_cards, send_counter_signals, write_digest

    md_path = write_digest(
        combined, config,
        top_articles=top_articles or None,
        ranked_signals=all_ranked or None,
        sources_count=len(articles_by_category), articles_count=total_articles,
    )
    markdown_saved = md_path is not None
    markdown_path = str(md_path) if md_path else ""

    telegram_sent = False
    telegram_partial = False
    if config.telegram.enabled:
        try:
            from digest.delivery.telegram import _send_chunk, escape_markdownv2

            article_source_map = await send_article_cards(
                articles_by_category, config, top_articles=top_articles,
            )
            if article_source_map:
                feedback_store.article_source_map.update(article_source_map)
            telegram_sent = bool(article_source_map)
            if telegram_sent:
                feedback_store.last_digest_sources = contributing_sources
                feedback_store.last_digest_time = datetime.now(tz=timezone.utc).strftime(
                    "%Y-%m-%d %H:%M UTC"
                )
            await send_counter_signals(all_ranked, config, irritator_status=irritator_status)

            # Send nano status footer
            nano_status = _build_nano_status(
                feeds_count, total_articles, 0, 0,
                source_stats, config, effective_priorities,
            )
            token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
            chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
            if token and chat_id:
                import httpx as _httpx

                api_url = f"https://api.telegram.org/bot{token}/sendMessage"
                async with _httpx.AsyncClient() as _client:
                    await _send_chunk(
                        _client, api_url, chat_id,
                        escape_markdownv2(nano_status),
                        disable_notification=True,
                    )
        except Exception as exc:
            logger.warning("Telegram delivery failed (non-critical): %s", exc)

    delivery_ok = telegram_sent or markdown_saved
    sources_promoted = 0
    sources_demoted = 0

    if delivery_ok:
        save_dedup_cache(cache)

        # Process pending approvals BEFORE save_stats so newly approved
        # sources aren't pruned from stats as "unknown"
        _process_pending_approvals(config_path, cache_dir, feedback_store)

        if config.adaptive.enabled:
            today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
            promote, demote, needs_start = evaluate_trial_sources(
                config.enabled_sources, source_stats, today, source_state
            )
            if promote or demote or needs_start:
                apply_trial_decisions_to_cache(source_state, promote, demote, today, needs_start)
                sources_promoted = len(promote)
                sources_demoted = len(demote)
    else:
        # Roll back feedback state — updates will be reprocessed on next run
        feedback_store.last_update_id = saved_update_id
        feedback_store.ratings = feedback_store.ratings[:saved_ratings_count]

    save_feedback(feedback_store, cache_dir)
    save_source_state(source_state, cache_dir)
    save_stats(source_stats, cache_dir, active_sources={s.name for s in config.enabled_sources})

    return RunStats(
        feeds_fetched=feeds_count, new_articles=total_articles,
        digest_length=len(combined), telegram_sent=telegram_sent,
        telegram_partial=telegram_partial, markdown_saved=markdown_saved,
        markdown_path=markdown_path, sources_promoted=sources_promoted,
        sources_demoted=sources_demoted, feedback_collected=feedback_collected,
        duration_seconds=time.monotonic() - _t_run_start,
    )


async def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint. Returns exit code (0 = success, 1 = critical failure)."""
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
        help="Validate config, check env vars, and probe feed URLs, then exit",
    )
    parser.add_argument(
        "--radar-only",
        action="store_true",
        help="Run only the radar pipeline (skip irritator)",
    )
    parser.add_argument(
        "--discover",
        action="store_true",
        help="Use LLM to suggest new RSS sources for underrepresented categories, then exit",
    )
    args = parser.parse_args(argv)
    _setup_logging(args.verbose)

    try:
        if args.check:
            return await check_config(args.config)
        if args.discover:
            return await discover_sources(args.config)

        stats = await run(args.config, args.dry_run, args.radar_only, args.verbose)
        _print_stats(stats)

        if stats.telegram_partial and not stats.markdown_saved:
            return 1
        if (
            not args.dry_run
            and stats.new_articles > 0
            and not (stats.telegram_sent or stats.markdown_saved or stats.telegram_partial)
        ):
            logging.getLogger(__name__).error(
                "No delivery channel produced output despite %d new articles — "
                "check Telegram credentials and obsidian config.",
                stats.new_articles,
            )
            return 1
        return 0
    except FileNotFoundError as exc:
        logging.getLogger(__name__).error("Config file not found: %s", exc)
        return 1
    except ValueError as exc:
        logging.getLogger(__name__).error("Configuration error: %s", exc)
        return 1
    except Exception as exc:
        logging.getLogger(__name__).error("Unexpected error: %s", exc, exc_info=True)
        return 1
