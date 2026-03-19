"""RSS/Atom feed collector for the daily digest."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import feedparser  # type: ignore[import-untyped]
import html as html_lib
import httpx

from src.config import Config, SourceConfig
from src.source_scorer import SourceStats, update_stats

logger = logging.getLogger(__name__)

CACHE_FILE = Path(".cache/seen_articles.json")


class AllFeedsFailedError(RuntimeError):
    """Raised when every configured feed fails to fetch."""
FEED_TIMEOUT = 15.0
USER_AGENT = "DailyDigestBot/1.0 (https://github.com/lenivvenil/digest)"
CACHE_MAX_AGE_DAYS = 7
DESCRIPTION_MAX_CHARS = 500


@dataclass
class Article:
    title: str
    link: str
    description: str
    source: str
    category: str
    pub_date: datetime | None


def _strip_html(text: str) -> str:
    """Remove HTML tags, decode HTML entities, and collapse whitespace."""
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_lib.unescape(text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _article_hash(title: str, link: str) -> str:
    return hashlib.md5(f"{title}|{link}".encode()).hexdigest()


def _parse_pub_date(entry: Any) -> datetime | None:
    """Parse publication date from a feedparser entry."""
    if hasattr(entry, "published_parsed") and entry.published_parsed:
        try:
            t = entry.published_parsed
            return datetime(t[0], t[1], t[2], t[3], t[4], t[5], tzinfo=timezone.utc)
        except Exception:
            pass
    if hasattr(entry, "updated_parsed") and entry.updated_parsed:
        try:
            t = entry.updated_parsed
            return datetime(t[0], t[1], t[2], t[3], t[4], t[5], tzinfo=timezone.utc)
        except Exception:
            pass
    return None


def _load_cache() -> dict[str, str]:
    if not CACHE_FILE.exists():
        return {}
    try:
        with CACHE_FILE.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            return {}
        return data
    except Exception as exc:
        logger.warning("Failed to load deduplication cache: %s", exc)
        return {}


def _save_cache(cache: dict[str, str]) -> None:
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        with CACHE_FILE.open("w", encoding="utf-8") as fh:
            json.dump(cache, fh, indent=2)
    except Exception as exc:
        logger.warning("Failed to save deduplication cache: %s", exc)


def _prune_cache(cache: dict[str, str]) -> dict[str, str]:
    """Remove cache entries older than CACHE_MAX_AGE_DAYS days."""
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=CACHE_MAX_AGE_DAYS)
    pruned = {}
    for h, ts in cache.items():
        try:
            dt = datetime.fromisoformat(ts)
            if dt >= cutoff:
                pruned[h] = ts
        except Exception as exc:
            logger.debug("Skipping cache entry %s with unparseable timestamp: %s", h, exc)
    removed = len(cache) - len(pruned)
    if removed:
        logger.debug("Pruned %d stale entries from deduplication cache", removed)
    return pruned


def _parse_feed_bytes(raw: bytes, url: str) -> feedparser.FeedParserDict:
    """Parse raw feed bytes with feedparser."""
    return feedparser.parse(raw, response_headers={"content-location": url})


async def _fetch_feed(
    client: httpx.AsyncClient, source: SourceConfig
) -> list[Article] | None:
    """Fetch and parse a single RSS/Atom feed.

    Returns list of articles (possibly empty) on success, or None on fetch/parse error.
    """
    try:
        response = await client.get(source.url, timeout=FEED_TIMEOUT)
        response.raise_for_status()
        feed = _parse_feed_bytes(response.content, source.url)
    except httpx.TimeoutException:
        logger.warning("Timeout fetching feed '%s' (%s)", source.name, source.url)
        return None
    except httpx.HTTPStatusError as exc:
        logger.warning(
            "HTTP %d fetching feed '%s' (%s)",
            exc.response.status_code,
            source.name,
            source.url,
        )
        return None
    except Exception as exc:
        logger.warning("Error fetching feed '%s' (%s): %s", source.name, source.url, exc)
        return None

    if feed.bozo and not feed.entries:
        logger.warning(
            "Malformed feed '%s' (%s): %s",
            source.name,
            source.url,
            feed.bozo_exception,
        )
        return None

    articles: list[Article] = []
    for entry in feed.entries:
        title = getattr(entry, "title", "") or ""
        link = getattr(entry, "link", "") or ""
        if not title and not link:
            continue

        raw_desc = ""
        if hasattr(entry, "summary") and entry.summary:
            raw_desc = entry.summary
        elif hasattr(entry, "description") and entry.description:
            raw_desc = entry.description
        elif hasattr(entry, "content") and entry.content:
            raw_desc = entry.content[0].get("value", "")

        description = _strip_html(raw_desc)[:DESCRIPTION_MAX_CHARS]
        pub_date = _parse_pub_date(entry)

        articles.append(
            Article(
                title=_strip_html(title),
                link=link,
                description=description,
                source=source.name,
                category=source.category,
                pub_date=pub_date,
            )
        )

    logger.debug("Fetched %d entries from '%s'", len(articles), source.name)
    return articles


def _clamp_partition(
    result: dict[str, int],
    names: list[str],
    budget: int,
) -> None:
    """Trim allocated slots so their sum does not exceed *budget*.

    When ``max(1, round(...))`` guarantees at least 1 slot per source the
    partition total can overshoot the budget.  This helper iteratively
    reduces the largest allocations until the total fits.
    """
    total = sum(result[n] for n in names)
    while total > budget:
        # Find the source with the largest allocation and reduce it
        max_name = max(names, key=lambda n: result[n])
        if result[max_name] <= 0:
            break
        result[max_name] -= 1
        total -= 1


def allocate_slots(
    sources: list[SourceConfig],
    total_budget: int,
    trial_budget: int | None = None,
) -> dict[str, int]:
    """Return per-source article slot counts proportional to source priorities.

    When trial_budget is provided, trial sources get a separate budget that
    does not compete with regular sources.

    slot(source) = max(1, round(budget * source.priority / total_weight))

    If total_weight is zero (all sources have priority=0), every source gets 1 slot.
    """
    regular = [s for s in sources if not s.trial]
    trials = [s for s in sources if s.trial]

    if trial_budget is not None and trials:
        if regular:
            regular_budget = total_budget - trial_budget
        else:
            # No regular sources — give trials the full budget, but only if
            # trial_budget was nonzero.  An explicit trial_slots=0 means
            # "no trial articles" and must be honoured even when all active
            # sources happen to be trials.
            regular_budget = 0
            if trial_budget > 0:
                trial_budget = total_budget
    else:
        regular_budget = total_budget
        trial_budget = 0
        # Treat trials as regular if no separate budget
        regular = sources
        trials = []

    result: dict[str, int] = {}

    # Allocate for regular sources
    if regular and regular_budget > 0:
        total_weight = sum(s.priority for s in regular)
        if total_weight == 0:
            for s in regular:
                result[s.name] = 1
        else:
            for s in regular:
                result[s.name] = max(
                    1, round(regular_budget * s.priority / total_weight)
                )
        # Clamp partition total to budget
        _clamp_partition(result, [s.name for s in regular], regular_budget)
    elif regular:
        for s in regular:
            result[s.name] = 0

    # Allocate for trial sources from separate budget
    if trials and trial_budget:
        total_weight = sum(s.priority for s in trials)
        if total_weight == 0:
            for s in trials:
                result[s.name] = 1
        else:
            for s in trials:
                result[s.name] = max(
                    1, round(trial_budget * s.priority / total_weight)
                )
        # Clamp partition total to budget
        _clamp_partition(result, [s.name for s in trials], trial_budget)

    return result


def _is_recent(article: Article, cutoff: datetime) -> bool:
    """Return True if article is within 24h window or has no date."""
    if article.pub_date is None:
        return True
    return article.pub_date >= cutoff


def save_dedup_cache(cache: dict[str, str]) -> None:
    """Persist the deduplication cache to disk.

    Called by the orchestrator after the digest is successfully generated,
    so that a failed run does not permanently suppress articles.
    """
    _save_cache(cache)


async def collect(
    config: Config,
    source_stats: dict[str, SourceStats] | None = None,
    effective_priorities: dict[str, int] | None = None,
) -> tuple[dict[str, list[Article]], dict[str, str]]:
    """Fetch all enabled feeds and return articles grouped by category.

    Applies 24h filtering, deduplication cache, and per-source/total limits.

    Returns:
        A tuple of (articles_by_category, updated_cache). The caller is
        responsible for persisting the cache via save_dedup_cache() once the
        digest has been successfully generated — this prevents a failed run
        from permanently suppressing articles that were never delivered.
    """
    cache = _load_cache()
    cache = _prune_cache(cache)
    now = datetime.now(tz=timezone.utc)
    cutoff_24h = now - timedelta(hours=24)

    headers = {"User-Agent": USER_AGENT}
    async with httpx.AsyncClient(headers=headers, follow_redirects=True) as client:
        tasks = [_fetch_feed(client, source) for source in config.enabled_sources]
        results = await asyncio.gather(*tasks)

    # Update source stats after fetching
    if source_stats is not None:
        for source, raw_articles in zip(config.enabled_sources, results):
            fetch_ok = raw_articles is not None
            articles_found = len(raw_articles) if raw_articles else 0
            avg_desc_len = 0.0
            if raw_articles:
                desc_lens = [len(a.description) for a in raw_articles if a.description]
                if desc_lens:
                    avg_desc_len = sum(desc_lens) / len(desc_lens)
            update_stats(
                source_stats,
                source.name,
                fetch_ok=fetch_ok,
                articles_found=articles_found,
                articles_included=0,  # updated after slot allocation
                avg_desc_len=avg_desc_len,
            )

    if config.enabled_sources and all(r is None for r in results):
        raise AllFeedsFailedError(
            f"All {len(results)} configured feeds failed to fetch. "
            "Check network connectivity and feed URLs."
        )

    successful_sources = [
        s for s, r in zip(config.enabled_sources, results) if r is not None
    ]
    # Override priorities with effective values when provided
    if effective_priorities:
        patched_sources: list[SourceConfig] = []
        for s in successful_sources:
            if s.name in effective_priorities:
                patched_sources.append(replace(s, priority=effective_priorities[s.name]))
            else:
                patched_sources.append(s)
        alloc_sources = patched_sources
    else:
        alloc_sources = successful_sources
    trial_budget = (
        config.adaptive.trial_slots
        if config.adaptive.enabled and any(s.trial for s in alloc_sources)
        else None
    )
    raw_slots = allocate_slots(
        alloc_sources, config.digest.max_total_articles, trial_budget=trial_budget
    )
    slots = {
        name: min(count, config.digest.max_articles_per_source)
        for name, count in raw_slots.items()
    }

    grouped: dict[str, list[Article]] = {}
    total_collected = 0

    # Pre-compute eligible articles per source (recency + dedup filter) in
    # descending priority order so that pass 2 redistribution also favours
    # higher-priority sources when filling the remaining budget.
    def _effective_priority(s: SourceConfig) -> int:
        if effective_priorities and s.name in effective_priorities:
            return effective_priorities[s.name]
        return s.priority

    source_eligible: list[tuple[SourceConfig, list[tuple[str, Article]]]] = []
    for source, raw_articles in sorted(
        zip(config.enabled_sources, results),
        key=lambda x: _effective_priority(x[0]),
        reverse=True,
    ):
        if raw_articles is None:
            continue
        eligible: list[tuple[str, Article]] = []
        for article in raw_articles:
            if not _is_recent(article, cutoff_24h):
                continue
            h = _article_hash(article.title, article.link)
            if h in cache:
                logger.debug("Skipping cached article: %s", article.title)
                continue
            eligible.append((h, article))
        source_eligible.append((source, eligible))

    per_source_taken: dict[str, int] = {s.name: 0 for s, _ in source_eligible}

    # Pass 1: fill up to proportional slot limits.
    for source, eligible in source_eligible:
        slot = slots.get(source.name, 0)
        taken = 0
        for h, article in eligible:
            if total_collected >= config.digest.max_total_articles:
                break
            if taken >= slot:
                break
            if h in cache:
                continue
            cache[h] = now.isoformat()
            grouped.setdefault(source.category, []).append(article)
            taken += 1
            total_collected += 1
        per_source_taken[source.name] = taken

    # Pass 2: redistribute unused budget to sources that still have eligible
    # articles, respecting the absolute per-source cap.
    # Trial sources are capped at their allocated slot count (no redistribution).
    if total_collected < config.digest.max_total_articles:
        for source, eligible in source_eligible:
            taken = per_source_taken[source.name]
            cap = slots.get(source.name, 0) if source.trial else config.digest.max_articles_per_source
            for h, article in eligible[taken:]:
                if total_collected >= config.digest.max_total_articles:
                    break
                if taken >= cap:
                    break
                if h in cache:
                    continue
                cache[h] = now.isoformat()
                grouped.setdefault(source.category, []).append(article)
                taken += 1
                total_collected += 1
            per_source_taken[source.name] = taken

    # Update articles_included counts in source stats
    if source_stats is not None:
        for source_name, taken in per_source_taken.items():
            if source_name in source_stats:
                st = source_stats[source_name]
                # The last snapshot was added with articles_included=0; fix it now
                if st.history:
                    st.history[-1].articles_included = taken
                # Also update the rolling counter with actual included count
                st.articles_included_in_digest += taken

    logger.info(
        "Collected %d new articles across %d categories",
        total_collected,
        len(grouped),
    )
    return grouped, cache
