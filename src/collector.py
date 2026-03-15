"""RSS/Atom feed collector for the daily digest."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import feedparser  # type: ignore[import-untyped]
import httpx

from src.config import Config, SourceConfig

logger = logging.getLogger(__name__)

CACHE_FILE = Path(".cache/seen_articles.json")
FEED_TIMEOUT = 15.0
USER_AGENT = "DailyDigestBot/1.0 (https://github.com/user/digest)"
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
    """Remove HTML tags and collapse whitespace."""
    text = re.sub(r"<[^>]+>", " ", text)
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
        except Exception:
            pass
    removed = len(cache) - len(pruned)
    if removed:
        logger.debug("Pruned %d stale entries from deduplication cache", removed)
    return pruned


def _parse_feed_bytes(raw: bytes, url: str) -> feedparser.FeedParserDict:
    """Parse raw feed bytes with feedparser."""
    return feedparser.parse(raw, response_headers={"content-location": url})


async def _fetch_feed(client: httpx.AsyncClient, source: SourceConfig) -> list[Article]:
    """Fetch and parse a single RSS/Atom feed. Returns list of articles or empty list on error."""
    try:
        response = await client.get(source.url, timeout=FEED_TIMEOUT)
        response.raise_for_status()
        feed = _parse_feed_bytes(response.content, source.url)
    except httpx.TimeoutException:
        logger.warning("Timeout fetching feed '%s' (%s)", source.name, source.url)
        return []
    except httpx.HTTPStatusError as exc:
        logger.warning(
            "HTTP %d fetching feed '%s' (%s)",
            exc.response.status_code,
            source.name,
            source.url,
        )
        return []
    except Exception as exc:
        logger.warning("Error fetching feed '%s' (%s): %s", source.name, source.url, exc)
        return []

    if feed.bozo and not feed.entries:
        logger.warning(
            "Malformed feed '%s' (%s): %s",
            source.name,
            source.url,
            feed.bozo_exception,
        )
        return []

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


def _is_recent(article: Article, cutoff: datetime) -> bool:
    """Return True if article is within 24h window or has no date."""
    if article.pub_date is None:
        return True
    return article.pub_date >= cutoff


async def collect(config: Config) -> dict[str, list[Article]]:
    """Fetch all enabled feeds and return articles grouped by category.

    Applies 24h filtering, deduplication cache, and per-source/total limits.
    """
    cache = _load_cache()
    cache = _prune_cache(cache)
    now = datetime.now(tz=timezone.utc)
    cutoff_24h = now - timedelta(hours=24)

    headers = {"User-Agent": USER_AGENT}
    async with httpx.AsyncClient(headers=headers, follow_redirects=True) as client:
        tasks = [_fetch_feed(client, source) for source in config.enabled_sources]
        results = await asyncio.gather(*tasks)

    grouped: dict[str, list[Article]] = {}
    total_collected = 0

    for source, articles in zip(config.enabled_sources, results):
        per_source_count = 0
        for article in articles:
            if total_collected >= config.digest.max_total_articles:
                break
            if per_source_count >= config.digest.max_articles_per_source:
                break
            if not _is_recent(article, cutoff_24h):
                continue
            h = _article_hash(article.title, article.link)
            if h in cache:
                logger.debug("Skipping cached article: %s", article.title)
                continue
            cache[h] = now.isoformat()
            grouped.setdefault(source.category, []).append(article)
            per_source_count += 1
            total_collected += 1

    _save_cache(cache)

    logger.info(
        "Collected %d new articles across %d categories",
        total_collected,
        len(grouped),
    )
    return grouped
