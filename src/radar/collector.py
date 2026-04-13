"""RSS/Atom feed collector for the radar pipeline."""

from __future__ import annotations

import asyncio
import hashlib
import html as html_lib
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import feedparser
import httpx

from src._dns_pinning import pin_dns as _pin_dns
from src._dns_pinning import validate_url as _validate_url
from src._util import atomic_json_write
from src.config import Config, SourceConfig
from src.filters import is_blocked

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


def article_hash(title: str, link: str) -> str:
    return hashlib.md5(f"{title}|{link}".encode()).hexdigest()


def _parse_pub_date(entry: Any) -> datetime | None:
    """Parse publication date from a feedparser entry."""
    if hasattr(entry, "published_parsed") and entry.published_parsed:
        try:
            t = entry.published_parsed
            return datetime(t[0], t[1], t[2], t[3], t[4], t[5], tzinfo=timezone.utc)
        except Exception as exc:
            logger.debug("Failed to parse published_parsed date: %s", exc)
    if hasattr(entry, "updated_parsed") and entry.updated_parsed:
        try:
            t = entry.updated_parsed
            return datetime(t[0], t[1], t[2], t[3], t[4], t[5], tzinfo=timezone.utc)
        except Exception as exc:
            logger.debug("Failed to parse updated_parsed date: %s", exc)
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
    except json.JSONDecodeError as exc:
        logger.warning("Corrupted dedup cache JSON, starting fresh: %s", exc)
        return {}
    except Exception as exc:
        logger.warning("Failed to load deduplication cache: %s", exc)
        return {}


def _save_cache(cache: dict[str, str]) -> None:
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        atomic_json_write(CACHE_FILE, cache)
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


def _is_recent(article: Article, cutoff: datetime) -> bool:
    """Return True if article is within recency window or has no date."""
    if article.pub_date is None:
        return True
    return article.pub_date >= cutoff


_FEED_RETRY_STATUSES = {500, 502, 503, 504}

# Maximum Retry-After value (seconds) we will honour. If the server asks us
# to wait longer, we skip the source for this run rather than blocking the
# entire pipeline.
_MAX_RETRY_AFTER_SECS = 300


async def _fetch_feed(
    client: httpx.AsyncClient, source: SourceConfig
) -> list[Article] | None:
    """Fetch and parse a single RSS/Atom feed.

    Returns list of articles (possibly empty) on success, or None on error.
    Transient errors (5xx/timeout) are retried once with a 2s backoff.
    HTTP 429 respects Retry-After: waits the requested duration (up to
    _MAX_RETRY_AFTER_SECS) and retries once; if the requested wait exceeds
    the maximum, the source is skipped for this run.
    """
    validated = _validate_url(source.url)
    if validated is None:
        logger.warning(
            "Skipping feed '%s': URL '%s' failed SSRF validation "
            "(non-global or otherwise unsafe address)",
            source.name,
            source.url,
        )
        return None

    response: httpx.Response | None = None
    for attempt in range(2):
        try:
            with _pin_dns(validated.hostname, validated.pinned_addrinfos):
                response = await client.get(source.url, timeout=FEED_TIMEOUT)
            response.raise_for_status()
            break
        except httpx.TimeoutException:
            if attempt == 0:
                logger.info("Timeout fetching '%s', retrying...", source.name)
                await asyncio.sleep(2)
                continue
            logger.warning("Timeout fetching feed '%s' (%s) after retry", source.name, source.url)
            return None
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status == 429:
                retry_after_raw = exc.response.headers.get("Retry-After", "")
                try:
                    retry_after = float(retry_after_raw)
                except (ValueError, TypeError):
                    retry_after = 2.0
                if retry_after > _MAX_RETRY_AFTER_SECS:
                    logger.warning(
                        "HTTP 429 fetching '%s' (%s): Retry-After=%ds exceeds limit (%ds) — "
                        "skipping source for this run",
                        source.name,
                        source.url,
                        int(retry_after),
                        _MAX_RETRY_AFTER_SECS,
                    )
                    return None
                if attempt == 0:
                    logger.info(
                        "HTTP 429 fetching '%s', waiting %.1fs (Retry-After)...",
                        source.name,
                        retry_after,
                    )
                    await asyncio.sleep(retry_after)
                    continue
                logger.warning(
                    "HTTP 429 fetching feed '%s' (%s) after retry",
                    source.name,
                    source.url,
                )
                return None
            if status in _FEED_RETRY_STATUSES and attempt == 0:
                logger.info("HTTP %d fetching '%s', retrying...", status, source.name)
                await asyncio.sleep(2)
                continue
            logger.warning("HTTP %d fetching feed '%s' (%s)", status, source.name, source.url)
            return None
        except Exception as exc:
            logger.warning("Error fetching feed '%s' (%s): %s", source.name, source.url, exc)
            return None

    if response is None:
        return None

    try:
        feed = _parse_feed_bytes(response.content, source.url)
    except Exception as exc:
        logger.warning("Error parsing feed '%s' (%s): %s", source.name, source.url, exc)
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
    for entry in feed.entries[:200]:
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


def _clamp_partition(result: dict[str, int], names: list[str], budget: int) -> None:
    """Trim allocated slots so their sum does not exceed *budget*.

    When max(1, round(...)) guarantees at least 1 slot per source the
    partition total can overshoot the budget. This helper iteratively
    reduces the largest allocations until the total fits.
    """
    total = sum(result[n] for n in names)
    while total > budget:
        max_name = max(names, key=lambda n: result[n])
        if result[max_name] <= 0:
            break
        result[max_name] -= 1
        total -= 1


def allocate_slots(sources: list[SourceConfig], total_budget: int) -> dict[str, int]:
    """Return per-source article slot counts proportional to source priorities.

    slot(source) = max(1, round(budget * source.priority / total_weight))

    If total_weight is zero (all sources have priority=0), every source gets 1 slot.
    """
    result: dict[str, int] = {}

    if not sources or total_budget <= 0:
        return {s.name: 0 for s in sources}

    total_weight = sum(s.priority for s in sources)
    if total_weight == 0:
        for s in sources:
            result[s.name] = 1
    else:
        for s in sources:
            result[s.name] = max(1, round(total_budget * s.priority / total_weight))

    _clamp_partition(result, [s.name for s in sources], total_budget)
    return result


def save_dedup_cache(cache: dict[str, str]) -> None:
    """Persist the deduplication cache to disk.

    Called by the orchestrator after the digest is successfully generated,
    so that a failed run does not permanently suppress articles.
    """
    _save_cache(cache)


async def collect(config: Config) -> tuple[dict[str, list[Article]], dict[str, str]]:
    """Fetch all enabled feeds and return articles grouped by category.

    Applies blocklist filtering, per-source recency filtering, deduplication
    cache, and per-category slot limits.

    Returns:
        A tuple of (articles_by_category, updated_cache). The caller is
        responsible for persisting the cache via save_dedup_cache() once the
        digest has been successfully generated — this prevents a failed run
        from permanently suppressing articles that were never delivered.
    """
    cache = _load_cache()
    cache = _prune_cache(cache)
    now = datetime.now(tz=timezone.utc)

    blocklist = config.filters.blocklist_keywords

    headers = {"User-Agent": USER_AGENT}
    async with httpx.AsyncClient(headers=headers, follow_redirects=True) as client:
        sem = asyncio.Semaphore(20)

        async def _limited(src: SourceConfig) -> list[Article] | None:
            async with sem:
                return await _fetch_feed(client, src)

        tasks = [_limited(source) for source in config.enabled_sources]
        results = await asyncio.gather(*tasks)

    if config.enabled_sources and all(r is None for r in results):
        raise AllFeedsFailedError(
            f"All {len(results)} configured feeds failed to fetch. "
            "Check network connectivity and feed URLs."
        )

    # Compute total budget based on unique categories among enabled sources
    categories = {s.category for s in config.enabled_sources}
    max_per_cat = config.radar.max_articles_per_category
    total_budget = max_per_cat * len(categories)

    successful_sources = [
        s for s, r in zip(config.enabled_sources, results, strict=True) if r is not None
    ]
    raw_slots = allocate_slots(successful_sources, total_budget)

    grouped: dict[str, list[Article]] = {}
    total_collected = 0

    # Pre-compute eligible articles per source in descending priority order
    source_eligible: list[tuple[SourceConfig, list[tuple[str, Article]]]] = []
    for source, raw_articles in sorted(
        zip(config.enabled_sources, results, strict=True),
        key=lambda x: x[0].priority,
        reverse=True,
    ):
        if raw_articles is None:
            continue
        source_cutoff = now - timedelta(hours=source.recency_hours)
        eligible: list[tuple[str, Article]] = []
        for article in raw_articles:
            if not _is_recent(article, source_cutoff):
                continue
            if is_blocked(article.title, blocklist) or is_blocked(article.description, blocklist):
                logger.debug("Blocked article (blocklist): %s", article.title)
                continue
            h = article_hash(article.title, article.link)
            if h in cache:
                logger.debug("Skipping cached article: %s", article.title)
                continue
            eligible.append((h, article))
        source_eligible.append((source, eligible))

    per_source_taken: dict[str, int] = {s.name: 0 for s, _ in source_eligible}

    # Pass 1: fill up to proportional slot limits.
    for source, eligible in source_eligible:
        slot = raw_slots.get(source.name, 0)
        taken = 0
        for h, article in eligible:
            if total_collected >= total_budget:
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

    # Pass 2: redistribute unused budget to sources that still have eligible articles,
    # respecting the per-category cap per source.
    if total_collected < total_budget:
        for source, eligible in source_eligible:
            taken = per_source_taken[source.name]
            cap = max_per_cat
            for h, article in eligible[taken:]:
                if total_collected >= total_budget:
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

    logger.info(
        "Collected %d new articles across %d categories",
        total_collected,
        len(grouped),
    )
    return grouped, cache
