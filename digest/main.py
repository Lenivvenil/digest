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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from digest.feedback import FeedbackStore
    from digest.irritator import IrritatorStatus
    from digest.radar.collector import Article, SourceFetchMetrics
    from digest.radar.summarizer import ArticleSummary, CategorySummary
    from digest.review import BlindReviewReport
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
    required_delivery_failed: bool = False
    review_status: str = "not_requested"
    review_checkpoint: str = ""


async def _send_status_message(text: str) -> bool:
    """Send a one-line Telegram status notice. Returns True on success."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return False

    import httpx as _httpx

    from digest.delivery.telegram import _send_chunk, escape_markdownv2

    api_url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        async with _httpx.AsyncClient() as client:
            await _send_chunk(client, api_url, chat_id, escape_markdownv2(text))
    except Exception as exc:
        logging.getLogger(__name__).warning(
            "Failed to send status message: %s", exc,
        )
        return False
    return True


async def _notify_skipped_cards(top_articles: list[Any]) -> None:
    """Surface a Telegram notice when the LLM picker returned no cards.

    Prevents the perception of a 'skipped digest' when summaries were still
    written to markdown but no cards landed in Telegram.
    """
    if top_articles:
        return
    await _send_status_message(
        "⚠️ Radar: LLM picker returned no top articles "
        "— cards skipped; summary saved to markdown."
    )


async def _notify_summaries_failed(*, dry_run: bool, telegram_enabled: bool) -> None:
    """Surface a Telegram notice when all summarization providers failed.

    Prevents a 'silently skipped' digest when the whole pipeline aborted
    before any markdown or card landed.
    """
    if dry_run or not telegram_enabled:
        return
    await _send_status_message(
        "❌ Radar: all LLM providers for role=summarize failed "
        "— digest not assembled (see workflow logs)."
    )


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
    print(f"Blind review:       {stats.review_status}")
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
    """Thin wrapper: creates an AsyncClient and delegates to the public run_irritator()."""
    import httpx

    from digest.irritator import run_irritator

    async with httpx.AsyncClient() as client:
        return await run_irritator(summaries, config, client, verbose=verbose)


def _process_pending_approvals(
    config_path: str, cache_dir: str, feedback_store: Any
) -> None:
    """Process pending source approval decisions from Telegram callbacks."""
    from digest.discovery import (
        PendingSource,
        add_source_to_config,
        load_pending,
        save_pending,
        source_hash,
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
        decision = feedback_store.source_decisions.get(ps.source_hash)
        if decision:
            try:
                discovered = datetime.fromisoformat(ps.discovered_at)
                if discovered.tzinfo is None:
                    discovered = discovered.replace(tzinfo=timezone.utc)
                valid = (ps.source_hash == source_hash(ps.url)
                         and timedelta(0) <= datetime.now(timezone.utc) - discovered <= timedelta(days=30))
            except ValueError:
                valid = False
            if not valid:
                logger.warning("Ignoring stale or mismatched pending source approval")
                remaining.append(ps)
                continue
        if decision == "approved":
            try:
                add_source_to_config(config_path, ps)
                feedback_store.source_decisions.pop(ps.source_hash, None)
            except Exception as exc:
                logger.error("Failed to add source '%s' to config: %s", ps.name, exc)
                remaining.append(ps)
        elif decision == "rejected":
            feedback_store.source_decisions.pop(ps.source_hash, None)
            logger.info("Source '%s' rejected by user, removing from pending", ps.name)
        else:
            remaining.append(ps)
    save_pending(remaining, cache_dir)
    save_feedback(feedback_store, cache_dir)


def _record_source_stats(
    source_stats: dict[str, SourceStats],
    fetch_metrics: dict[str, SourceFetchMetrics],
    articles_by_category: dict[str, list[Article]],
    delivered_hashes: set[str],
) -> None:
    """Combine fetch observations with confirmed output, including failed feeds."""
    from digest.radar.collector import article_hash
    from digest.source_scorer import update_stats

    included: dict[str, int] = {}
    for articles in articles_by_category.values():
        for article in articles:
            if article_hash(article.title, article.link) in delivered_hashes:
                included[article.source] = included.get(article.source, 0) + 1
    for name, metrics in fetch_metrics.items():
        update_stats(
            source_stats, name, metrics.fetch_ok, metrics.articles_found,
            included.get(name, 0), metrics.avg_description_length,
        )


def _save_failed_run_stats(
    source_stats: dict[str, SourceStats],
    fetch_metrics: dict[str, SourceFetchMetrics],
    cache_dir: str,
    active_sources: set[str],
    *,
    dry_run: bool,
) -> None:
    """Retain fetch health on failed runs without consuming article/feedback state."""
    from digest.source_scorer import save_stats

    if not dry_run:
        _record_source_stats(source_stats, fetch_metrics, {}, set())
        save_stats(source_stats, cache_dir, active_sources=active_sources)


def _review_status_line(report: BlindReviewReport | None, language: str = "en") -> str:
    if report is None:
        return ""
    russian = language == "ru"
    statuses = ({"ok": "ответ принят", "partial": "часть карточек принята", "abstained": "нет выбора",
                 "invalid": "ответ не прошёл проверку", "unavailable": "ответ не получен"} if russian else
                {"ok": "accepted", "partial": "partially accepted", "abstained": "no selection",
                 "invalid": "response failed validation", "unavailable": "no response"})
    details = []
    for review in report.reviews:
        pending = review.error == "pending_independent_review"
        state = (("ожидает отдельного этапа" if russian else "waiting for separate stage")
                 if pending else statuses[review.status])
        details.append(f"{review.model}: {state}")
    complete = report.status == "complete"
    heading = (("Сравнение моделей завершено" if complete else "Сравнение моделей ещё не завершено") if russian else
               ("Model comparison complete" if complete else "Model comparison incomplete"))
    return "\n" + heading + ". " + "; ".join(details)


def _review_text(report: BlindReviewReport | None) -> str:
    from digest.review import render_review

    return render_review(report) if report is not None else ""


def _deferred_review_status(language: str) -> str:
    if language == "ru":
        return "Независимое сравнение и этап контрсигналов отложены до завершения основной доставки."
    return "Independent comparison and counter-signal stage postponed until after primary delivery."


def _combined_summary(
    summaries: list[CategorySummary], trends: str | None, review_led_only: bool, language: str,
) -> str:
    if review_led_only:
        return _deferred_review_status(language)
    return _clean_summary(
        "\n\n".join(summary.summary_text for summary in summaries) + (f"\n\n{trends}" if trends else "")
    )


def _print_dry_run(
    combined: str, top_articles: list[ArticleSummary], all_ranked: list[Any],
    irritator_status: IrritatorStatus, review_report: BlindReviewReport | None,
) -> None:
    print(combined)
    if top_articles:
        print("\n=== TOP ARTICLES ===\n")
        for article in top_articles:
            print(f"[{article.category}] {article.title}")
            print(f"  {article.summary}\n")
    for ranked in all_ranked:
        print(f"[{ranked.score}/10] {ranked.signal.title} — {ranked.signal.url}")
    print(f"\n💢 Irritator: {irritator_status.text}")
    print(_review_text(review_report))


async def _analyze_articles(
    articles: dict[str, list[Article]], config: Any,
) -> tuple[list[CategorySummary], str | None, list[ArticleSummary], BlindReviewReport | None]:
    """Prioritize blind selection before optional category prose consumes quota."""
    from digest.radar import pick_top_articles, summarize_all

    if getattr(getattr(config, "review", None), "enabled", False):
        from digest.review import primary_cards, run_blind_review, run_primary_review

        report = await (run_primary_review(articles, config) if config.review.review_led_only
                        else run_blind_review(articles, config))
        cards = primary_cards(report, articles, config.radar.language)
        if config.review.review_led_only:
            logging.getLogger(__name__).info(
                "Review-led only: skipping legacy category summaries, trends and counter-signal analysis."
            )
            return [], None, cards, report
        summaries, trends = await summarize_all(articles, config)
        return summaries, trends, cards, report
    summaries, trends = await summarize_all(articles, config)
    cards = await pick_top_articles(articles, config, max_articles=7) if summaries else []
    return summaries, trends, cards, None


def _print_radar_presentation(combined: str, cards: list[ArticleSummary], config: Any) -> None:
    print(combined)
    if getattr(getattr(config, "translation", None), "enabled", False):
        for card in cards:
            print(f"\n{card.title}\n{card.link}\n{card.summary}")


async def _primary_presentation(
    combined: str, cards: list[ArticleSummary], config: Any, cache: Path, dry_run: bool,
) -> tuple[str, list[ArticleSummary]]:
    """Optional rendering only; source evidence and supplementary work stay canonical."""
    translation = getattr(config, "translation", None)
    if translation is None or not translation.enabled:
        return combined, cards
    from digest.translation import translate_primary_presentation

    if dry_run:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory(prefix="digest-translation-preview-") as temporary:
            return await translate_primary_presentation(combined, cards, config, Path(temporary))
    return await translate_primary_presentation(combined, cards, config, cache)


async def _publication_presentation(
    combined: str, cards: list[ArticleSummary], ranked: list[Any], config: Any, cache: Path, dry_run: bool,
) -> tuple[str, list[ArticleSummary], list[Any]]:
    if not getattr(getattr(config, "translation", None), "enabled", False):
        return combined, cards, ranked
    from digest.translation import translate_publication_presentation

    if dry_run:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory(prefix="digest-translation-preview-") as temporary:
            return await translate_publication_presentation(combined, cards, ranked, config, Path(temporary))
    return await translate_publication_presentation(combined, cards, ranked, config, cache)


async def _collect_run_feedback(
    config: Any, cache_dir: str, dry_run: bool, precollected: bool,
) -> tuple[FeedbackStore, bool, int]:
    """Feedback durability is independent of today's analysis/delivery outcome."""
    from digest.feedback import FeedbackStore, collect_feedback, load_feedback

    logger = logging.getLogger(__name__)
    try:
        store = load_feedback(cache_dir, strict=True)
    except Exception as exc:
        logger.warning("Feedback state unavailable (%s); preserve it and continue without polling", type(exc).__name__)
        return FeedbackStore(), False, 0
    if dry_run or precollected or not config.telegram.enabled:
        return store, True, 0
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        return store, True, 0
    before = len(store.ratings)
    try:
        store = await collect_feedback(token, store, cache_dir=cache_dir)
    except Exception as exc:
        logger.warning("Feedback collection incomplete (%s); primary processing continues", type(exc).__name__)
        # Strict collector writes a candidate atomically. Reload any committed prefix
        # (including votes whose UI acknowledgement failed), never replace it blindly.
        try:
            store = load_feedback(cache_dir, strict=True)
        except Exception:
            return store, False, 0
    return store, True, max(0, len(store.ratings) - before)


async def run(
    config_path: str, dry_run: bool, radar_only: bool, verbose: bool, *, feedback_precollected: bool = False,
) -> RunStats:
    """Full pipeline: feedback -> radar -> (irritator) -> delivery -> scoring."""
    from digest._util import cleanup_stale_tmp
    from digest.config import load_config
    from digest.feedback import (
        get_source_feedback_score,
        save_feedback,
    )
    from digest.radar import AllFeedsFailedError, collect, save_dedup_cache
    from digest.radar.collector import article_hash
    from digest.source_scorer import (
        apply_trial_decisions_to_cache,
        calculate_effective_priorities,
        calculate_feedback_priorities,
        evaluate_trial_sources,
        load_source_state,
        load_stats,
        save_source_category_map,
        save_source_state,
        save_stats,
    )

    _t_run_start = time.monotonic()
    config = load_config(config_path)
    review_led_only = bool(
        getattr(getattr(config, "review", None), "enabled", False) and config.review.review_led_only
    )
    logger = logging.getLogger(__name__)
    cache_dir = ".cache"
    source_state = load_source_state(cache_dir)
    feeds_count = len(config.enabled_sources)
    cleanup_stale_tmp(Path(cache_dir))
    source_stats = load_stats(cache_dir)
    feedback_store, feedback_usable, feedback_collected = await _collect_run_feedback(
        config, cache_dir, dry_run, feedback_precollected,
    )
    saved_article_source_map = dict(feedback_store.article_source_map)
    feedback_scores: dict[str, float] = {}
    for source in config.enabled_sources:
        score = get_source_feedback_score(feedback_store, source.name)
        if score is not None:
            feedback_scores[source.name] = score
    if config.adaptive.enabled:
        effective_priorities = calculate_effective_priorities(
            config.effective_sources(source_state), source_stats, feedback_scores, config.adaptive,
        )
    else:
        effective_priorities = calculate_feedback_priorities(
            config.effective_sources(source_state), feedback_scores, config.adaptive,
        )

    run_config = dataclasses.replace(
        config,
        sources=[s for s in config.sources if s.enabled and not source_state.is_demoted(s.name)],
    )
    fetch_metrics: dict[str, SourceFetchMetrics] = {}
    try:
        articles_by_category, cache = await collect(
            run_config, effective_priorities=effective_priorities, fetch_metrics=fetch_metrics,
        )
    except AllFeedsFailedError:
        _save_failed_run_stats(
            source_stats, fetch_metrics, cache_dir,
            {s.name for s in config.enabled_sources}, dry_run=dry_run,
        )
        raise

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
            _record_source_stats(source_stats, fetch_metrics, articles_by_category, set())
            save_stats(source_stats, cache_dir, active_sources={s.name for s in config.enabled_sources})
            if feedback_usable:
                save_feedback(feedback_store, cache_dir)
        return _empty_stats()

    contributing_sources = sorted(
        {a.source for articles in articles_by_category.values() for a in articles}
    )

    summaries, trends, top_articles, review_report = await _analyze_articles(articles_by_category, config)
    if not summaries and not top_articles and review_report is None:
        logger.error("All category summarizations failed.")
        _save_failed_run_stats(
            source_stats, fetch_metrics, cache_dir,
            {s.name for s in config.enabled_sources}, dry_run=dry_run,
        )
        await _notify_summaries_failed(
            dry_run=dry_run, telegram_enabled=config.telegram.enabled,
        )
        return _empty_stats(total_articles)

    combined = _combined_summary(summaries, trends, review_led_only, config.radar.language)

    if radar_only:
        combined, top_articles = await _primary_presentation(
            combined, top_articles, config, Path(cache_dir) / "translations", dry_run,
        )
        _print_radar_presentation(combined, top_articles, config)
        return RunStats(
            feeds_fetched=feeds_count, new_articles=total_articles,
            digest_length=len(combined), telegram_sent=False,
            telegram_partial=False, markdown_saved=False, markdown_path="",
            feedback_collected=feedback_collected,
        )

    # Irritator pipeline
    if review_led_only:
        from digest.irritator import IrritatorStatus

        all_ranked: list[Any] = []
        irritator_status = IrritatorStatus(_deferred_review_status(config.radar.language), "deferred")
    else:
        _, all_ranked, irritator_status = await _run_irritator(summaries, config, verbose)

    combined, top_articles, all_ranked = await _publication_presentation(
        combined, top_articles, all_ranked, config, Path(cache_dir) / "translations", dry_run,
    )

    # Dry-run output
    if dry_run:
        _print_dry_run(combined, top_articles, all_ranked, irritator_status, review_report)
        return RunStats(
            feeds_fetched=feeds_count, new_articles=total_articles,
            digest_length=len(combined), telegram_sent=False,
            telegram_partial=False, markdown_saved=False, markdown_path="",
            feedback_collected=feedback_collected,
        )

    # Delivery
    from digest.delivery import ArticleDeliveryResult, send_article_cards, send_counter_signals, write_digest

    md_path = write_digest(
        combined, config,
        top_articles=top_articles or None,
        ranked_signals=all_ranked or None,
        review_report=review_report,
        irritator_status=irritator_status,
        sources_count=len(articles_by_category), articles_count=total_articles,
    )
    markdown_saved = md_path is not None
    markdown_path = str(md_path) if md_path else ""

    telegram_sent = False
    telegram_partial = False
    card_delivery = ArticleDeliveryResult()
    if config.telegram.enabled:
        try:
            from digest.delivery.telegram import _send_chunk, escape_markdownv2

            card_delivery = await send_article_cards(
                articles_by_category, config, top_articles=top_articles,
            )
            feedback_store.article_source_map.update(card_delivery.article_source_map)
            telegram_sent = card_delivery.sent > 0 and card_delivery.failed == 0
            telegram_partial = card_delivery.sent > 0 and card_delivery.failed > 0
            if telegram_sent:
                feedback_store.last_digest_sources = contributing_sources
                feedback_store.last_digest_time = datetime.now(tz=timezone.utc).strftime(
                    "%Y-%m-%d %H:%M UTC"
                )
            if not review_led_only:
                await _notify_skipped_cards(top_articles)
                await send_counter_signals(all_ranked, config, irritator_status=irritator_status)

            # Send nano status footer
            nano_status = _build_nano_status(
                feeds_count, total_articles,
                sum(m.fetch_ok for m in fetch_metrics.values()),
                sum(not m.fetch_ok for m in fetch_metrics.values()),
                source_stats, config, effective_priorities,
            )
            nano_status += _review_status_line(review_report, config.radar.language)
            if review_led_only:
                nano_status += "\n" + irritator_status.text
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

    delivered_hashes = set(card_delivery.delivered_hashes)
    telegram_required = getattr(config.telegram, "required", False)
    if markdown_saved and (not telegram_required or telegram_sent):
        summarized_categories = {s.category for s in summaries}
        delivered_hashes.update(
            article_hash(a.title, a.link)
            for category, articles in articles_by_category.items()
            if category in summarized_categories
            for a in articles
        )
        delivered_hashes.update(article_hash(a.title, a.link) for a in top_articles)
    collected_hashes = {
        article_hash(a.title, a.link)
        for articles in articles_by_category.values() for a in articles
    }
    # Preserve old entries, but commit new entries only for confirmed output.
    delivered_cache = {
        key: timestamp for key, timestamp in cache.items()
        if key not in collected_hashes or key in delivered_hashes
    }
    _record_source_stats(source_stats, fetch_metrics, articles_by_category, delivered_hashes)
    delivery_ok = card_delivery.sent > 0 or markdown_saved
    sources_promoted = 0
    sources_demoted = 0

    if delivery_ok:
        save_dedup_cache(delivered_cache)

        # Process pending approvals BEFORE save_stats so newly approved
        # sources aren't pruned from stats as "unknown"
        if feedback_usable and feedback_store.source_decisions:
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
        # Only attribution for undelivered new cards is delivery-dependent.
        # Previously persisted votes/cursor must survive an unrelated delivery failure.
        feedback_store.article_source_map = saved_article_source_map

    if feedback_usable:
        save_feedback(feedback_store, cache_dir)
    save_source_state(source_state, cache_dir)
    save_stats(source_stats, cache_dir, active_sources={s.name for s in config.enabled_sources})
    save_source_category_map(config.enabled_sources, cache_dir)

    return RunStats(
        feeds_fetched=feeds_count, new_articles=total_articles,
        digest_length=len(combined), telegram_sent=telegram_sent,
        telegram_partial=telegram_partial, markdown_saved=markdown_saved,
        markdown_path=markdown_path, sources_promoted=sources_promoted,
        sources_demoted=sources_demoted, feedback_collected=feedback_collected,
        duration_seconds=time.monotonic() - _t_run_start,
        required_delivery_failed=telegram_required and not telegram_sent,
        review_status=review_report.status if review_report is not None else "not_requested",
        review_checkpoint=str(md_path.with_suffix(".review.json")) if md_path and review_report is not None else "",
    )


def _publish_review_checkpoint(stats: RunStats) -> None:
    """Expose a generated local archive only after successful delivery and saves."""
    output = os.environ.get("GITHUB_OUTPUT")
    if not output or not stats.markdown_saved or not stats.review_checkpoint:
        return
    try:
        checkpoint = Path(stats.review_checkpoint).resolve(strict=True)
        expected = Path(stats.markdown_path).resolve(strict=True).with_suffix(".review.json")
        relative = checkpoint.relative_to(Path.cwd().resolve()).as_posix()
        if (checkpoint != expected or not checkpoint.is_file()
                or not re.fullmatch(r"[A-Za-z0-9_./-]+", relative)
                or not re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:-\d+)?\.review\.json", checkpoint.name)):
            raise ValueError("Unsafe or non-generated review checkpoint path.")
        with Path(output).open("a", encoding="utf-8") as handle:
            handle.write(f"review_checkpoint={relative}\n")
    except (OSError, ValueError) as exc:
        logging.getLogger(__name__).warning("Review checkpoint output unavailable: %s", exc)


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
    parser.add_argument(
        "--feedback-precollected", action="store_true",
        help="Managed runtime owns feedback collection/persistence; do not poll again in this process",
    )
    args = parser.parse_args(argv)
    _setup_logging(args.verbose)

    try:
        if args.check:
            return await check_config(args.config)
        if args.discover:
            return await discover_sources(args.config)

        stats = await run(args.config, args.dry_run, args.radar_only, args.verbose,
                          feedback_precollected=args.feedback_precollected)
        _print_stats(stats)

        if stats.required_delivery_failed:
            logging.getLogger(__name__).error("Required Telegram article delivery did not complete.")
            return 1
        if stats.telegram_partial and not stats.markdown_saved:
            return 1
        if (
            not args.dry_run
            and not args.radar_only
            and stats.new_articles > 0
            and not (stats.telegram_sent or stats.markdown_saved or stats.telegram_partial)
        ):
            logging.getLogger(__name__).error(
                "No delivery channel produced output despite %d new articles — "
                "check Telegram credentials and obsidian config.",
                stats.new_articles,
            )
            return 1
        if not args.dry_run and not args.radar_only:
            _publish_review_checkpoint(stats)
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
