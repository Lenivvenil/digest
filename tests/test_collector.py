"""Tests for src/collector.py"""

from __future__ import annotations

import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.collector import (
    AllFeedsFailedError,
    Article,
    _article_hash,
    _is_recent,
    _parse_pub_date,
    _prune_cache,
    _strip_html,
    allocate_slots,
    collect,
    save_dedup_cache,
)
from src.config import Config, DeliveryConfig, DigestConfig, LLMConfig, SourceConfig

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _rfc2822(hours_ago: int = 2) -> str:
    """Return an RFC 2822 date string for a recent time."""
    dt = datetime.now(tz=timezone.utc) - timedelta(hours=hours_ago)
    return dt.strftime("%a, %d %b %Y %H:%M:%S +0000")


def _iso8601(hours_ago: int = 3) -> str:
    """Return an ISO 8601 UTC date string for a recent time."""
    dt = datetime.now(tz=timezone.utc) - timedelta(hours=hours_ago)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def make_rss_sample() -> str:
    return textwrap.dedent(f"""\
        <?xml version="1.0" encoding="UTF-8"?>
        <rss version="2.0">
          <channel>
            <title>Test Feed</title>
            <link>https://example.com</link>
            <description>A test feed</description>
            <item>
              <title>Article One</title>
              <link>https://example.com/1</link>
              <description><![CDATA[<p>First <b>article</b> body.</p>]]></description>
              <pubDate>{_rfc2822(hours_ago=2)}</pubDate>
            </item>
            <item>
              <title>Article Two</title>
              <link>https://example.com/2</link>
              <description>Second article body.</description>
              <pubDate>{_rfc2822(hours_ago=3)}</pubDate>
            </item>
          </channel>
        </rss>
    """)


def make_atom_sample() -> str:
    return textwrap.dedent(f"""\
        <?xml version="1.0" encoding="utf-8"?>
        <feed xmlns="http://www.w3.org/2005/Atom">
          <title>Atom Feed</title>
          <link href="https://atom.example.com"/>
          <entry>
            <title>Atom Article</title>
            <link href="https://atom.example.com/1"/>
            <summary>Atom article summary.</summary>
            <updated>{_iso8601(hours_ago=3)}</updated>
          </entry>
        </feed>
    """)


# Keep module-level aliases for backward compatibility within this file
RSS_SAMPLE = make_rss_sample()
ATOM_SAMPLE = make_atom_sample()

MALFORMED_XML = b"<not valid xml><<<"


def make_source(
    name: str = "Test",
    url: str = "https://example.com/feed",
    category: str = "Tech",
    enabled: bool = True,
    priority: int = 3,
) -> SourceConfig:
    return SourceConfig(name=name, url=url, category=category, enabled=enabled, priority=priority)


def make_config(
    sources: list[SourceConfig] | None = None,
    max_articles_per_source: int = 10,
    max_total_articles: int = 50,
) -> Config:
    if sources is None:
        sources = [make_source()]
    return Config(
        llm=LLMConfig(provider="anthropic", model="claude-sonnet-4-20250514"),
        delivery=DeliveryConfig(telegram=False, markdown_to_repo=False, markdown_dir="digests"),
        digest=DigestConfig(
            language="ru",
            max_articles_per_source=max_articles_per_source,
            max_total_articles=max_total_articles,
            summary_style="analytical",
        ),
        sources=sources,
    )


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------

class TestStripHtml:
    def test_removes_tags(self) -> None:
        assert _strip_html("<p>Hello <b>world</b></p>") == "Hello world"

    def test_collapses_whitespace(self) -> None:
        assert _strip_html("a  \n  b") == "a b"

    def test_no_tags(self) -> None:
        assert _strip_html("plain text") == "plain text"

    def test_empty(self) -> None:
        assert _strip_html("") == ""


class TestArticleHash:
    def test_deterministic(self) -> None:
        assert _article_hash("Title", "https://x.com") == _article_hash("Title", "https://x.com")

    def test_different_for_different_inputs(self) -> None:
        assert _article_hash("A", "B") != _article_hash("C", "D")


class TestParsePubDate:
    def test_parses_published_parsed(self) -> None:
        entry = MagicMock()
        entry.published_parsed = (2026, 3, 16, 5, 0, 0, 0, 0, 0)
        entry.updated_parsed = None
        dt = _parse_pub_date(entry)
        assert dt is not None
        assert dt.year == 2026
        assert dt.tzinfo == timezone.utc

    def test_falls_back_to_updated_parsed(self) -> None:
        entry = MagicMock()
        entry.published_parsed = None
        entry.updated_parsed = (2026, 3, 15, 12, 0, 0, 0, 0, 0)
        dt = _parse_pub_date(entry)
        assert dt is not None
        assert dt.day == 15

    def test_returns_none_when_missing(self) -> None:
        entry = MagicMock(spec=[])
        assert _parse_pub_date(entry) is None


class TestIsRecent:
    def test_recent_article(self) -> None:
        now = datetime.now(tz=timezone.utc)
        article = Article(
            title="T", link="L", description="", source="S", category="C",
            pub_date=now - timedelta(hours=12),
        )
        assert _is_recent(article, now - timedelta(hours=24))

    def test_old_article(self) -> None:
        now = datetime.now(tz=timezone.utc)
        article = Article(
            title="T", link="L", description="", source="S", category="C",
            pub_date=now - timedelta(hours=48),
        )
        assert not _is_recent(article, now - timedelta(hours=24))

    def test_no_date_is_included(self) -> None:
        now = datetime.now(tz=timezone.utc)
        article = Article(
            title="T", link="L", description="", source="S", category="C",
            pub_date=None,
        )
        assert _is_recent(article, now - timedelta(hours=24))


class TestPruneCache:
    def test_removes_old_entries(self) -> None:
        now = datetime.now(tz=timezone.utc)
        old = (now - timedelta(days=10)).isoformat()
        fresh = (now - timedelta(days=1)).isoformat()
        cache = {"old": old, "fresh": fresh}
        pruned = _prune_cache(cache)
        assert "old" not in pruned
        assert "fresh" in pruned

    def test_keeps_all_fresh(self) -> None:
        now = datetime.now(tz=timezone.utc)
        cache = {
            "a": (now - timedelta(days=1)).isoformat(),
            "b": (now - timedelta(days=3)).isoformat(),
        }
        assert len(_prune_cache(cache)) == 2

    def test_skips_malformed_timestamps(self) -> None:
        cache = {"bad": "not-a-date"}
        assert _prune_cache(cache) == {}


# ---------------------------------------------------------------------------
# Integration-style tests using mocked HTTP
# ---------------------------------------------------------------------------

def make_http_response(content: bytes, status_code: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.content = content
    resp.raise_for_status = MagicMock()
    if status_code >= 400:
        import httpx
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "error", request=MagicMock(), response=resp
        )
    return resp


@pytest.mark.asyncio
async def test_collect_rss_feed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".cache").mkdir()

    config = make_config(sources=[make_source()])

    async def fake_get(url: str, timeout: float) -> MagicMock:
        return make_http_response(RSS_SAMPLE.encode())

    with patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=fake_get)):
        result, _ = await collect(config)

    assert "Tech" in result
    articles = result["Tech"]
    assert len(articles) == 2
    assert articles[0].title == "Article One"
    assert "<" not in articles[0].description


@pytest.mark.asyncio
async def test_collect_atom_feed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".cache").mkdir()

    config = make_config(sources=[make_source(name="Atom", url="https://atom.example.com/feed")])

    async def fake_get(url: str, timeout: float) -> MagicMock:
        return make_http_response(ATOM_SAMPLE.encode())

    with patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=fake_get)):
        result, _ = await collect(config)

    assert "Tech" in result
    assert result["Tech"][0].title == "Atom Article"


@pytest.mark.asyncio
async def test_collect_deduplication(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Second collect call should skip already-seen articles."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".cache").mkdir()

    config = make_config(sources=[make_source()])

    async def fake_get(url: str, timeout: float) -> MagicMock:
        return make_http_response(RSS_SAMPLE.encode())

    with patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=fake_get)):
        first, first_cache = await collect(config)
        save_dedup_cache(first_cache)
        second, _ = await collect(config)

    assert len(first.get("Tech", [])) == 2
    assert len(second.get("Tech", [])) == 0


@pytest.mark.asyncio
async def test_collect_malformed_feed_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Malformed feed should not crash; other sources still processed."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".cache").mkdir()

    good_source = make_source(name="Good", url="https://good.example.com/feed", category="Tech")
    bad_source = make_source(name="Bad", url="https://bad.example.com/feed", category="Tech")
    config = make_config(sources=[bad_source, good_source])

    call_count = 0

    async def fake_get(url: str, timeout: float) -> MagicMock:
        nonlocal call_count
        call_count += 1
        if "bad" in url:
            return make_http_response(MALFORMED_XML, status_code=200)
        return make_http_response(RSS_SAMPLE.encode())

    with patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=fake_get)):
        result, _ = await collect(config)

    assert call_count == 2
    # Good feed should still produce articles
    assert len(result.get("Tech", [])) > 0


@pytest.mark.asyncio
async def test_collect_http_error_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HTTP error on one feed should not crash if another feed succeeds."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".cache").mkdir()

    good_source = make_source(name="Good", url="https://good.example.com/feed")
    bad_source = make_source(name="Bad", url="https://bad.example.com/feed")
    config = make_config(sources=[good_source, bad_source])

    async def fake_get(url: str, timeout: float) -> MagicMock:
        if "bad" in url:
            return make_http_response(b"", status_code=500)
        return make_http_response(RSS_SAMPLE.encode())

    with patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=fake_get)):
        result, _ = await collect(config)

    assert len(result.get("Tech", [])) > 0


@pytest.mark.asyncio
async def test_collect_all_feeds_http_error_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When all feeds fail with HTTP errors, AllFeedsFailedError is raised."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".cache").mkdir()

    config = make_config(sources=[make_source()])

    async def fake_get(url: str, timeout: float) -> MagicMock:
        return make_http_response(b"", status_code=500)

    with patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=fake_get)):
        with pytest.raises(AllFeedsFailedError):
            await collect(config)


@pytest.mark.asyncio
async def test_collect_timeout_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Timeout on one feed should not crash if another feed succeeds."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".cache").mkdir()

    import httpx

    good_source = make_source(name="Good", url="https://good.example.com/feed")
    slow_source = make_source(name="Slow", url="https://slow.example.com/feed")
    config = make_config(sources=[good_source, slow_source])

    async def fake_get(url: str, timeout: float) -> MagicMock:
        if "slow" in url:
            raise httpx.TimeoutException("timeout")
        return make_http_response(RSS_SAMPLE.encode())

    with patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=fake_get)):
        result, _ = await collect(config)

    assert len(result.get("Tech", [])) > 0


@pytest.mark.asyncio
async def test_collect_all_feeds_timeout_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When all feeds time out, AllFeedsFailedError is raised."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".cache").mkdir()

    import httpx

    config = make_config(sources=[make_source()])

    async def fake_get(url: str, timeout: float) -> MagicMock:
        raise httpx.TimeoutException("timeout")

    with patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=fake_get)):
        with pytest.raises(AllFeedsFailedError):
            await collect(config)


@pytest.mark.asyncio
async def test_collect_respects_max_per_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".cache").mkdir()

    config = make_config(sources=[make_source()], max_articles_per_source=1)

    async def fake_get(url: str, timeout: float) -> MagicMock:
        return make_http_response(RSS_SAMPLE.encode())

    with patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=fake_get)):
        result, _ = await collect(config)

    assert len(result.get("Tech", [])) == 1


@pytest.mark.asyncio
async def test_collect_respects_max_total(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".cache").mkdir()

    sources = [
        make_source(name=f"S{i}", url=f"https://s{i}.example.com/feed", category="Tech")
        for i in range(3)
    ]
    config = make_config(sources=sources, max_total_articles=3)

    def make_unique_rss(idx: int) -> bytes:
        return textwrap.dedent(f"""\
            <?xml version="1.0" encoding="UTF-8"?>
            <rss version="2.0">
              <channel><title>Feed {idx}</title>
                <item>
                  <title>Article {idx}-A</title>
                  <link>https://s{idx}.example.com/a</link>
                  <description>Body A</description>
                  <pubDate>{_rfc2822(hours_ago=2)}</pubDate>
                </item>
                <item>
                  <title>Article {idx}-B</title>
                  <link>https://s{idx}.example.com/b</link>
                  <description>Body B</description>
                  <pubDate>{_rfc2822(hours_ago=3)}</pubDate>
                </item>
              </channel>
            </rss>
        """).encode()

    call_count = 0

    async def fake_get(url: str, timeout: float) -> MagicMock:
        nonlocal call_count
        idx = call_count
        call_count += 1
        return make_http_response(make_unique_rss(idx))

    with patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=fake_get)):
        result, _ = await collect(config)

    total = sum(len(v) for v in result.values())
    assert total == 3


@pytest.mark.asyncio
async def test_collect_filters_old_articles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Articles older than 24h should be excluded unless pub_date is missing."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".cache").mkdir()

    old_rss = textwrap.dedent(f"""\
        <?xml version="1.0" encoding="UTF-8"?>
        <rss version="2.0">
          <channel>
            <title>Old Feed</title>
            <item>
              <title>Old Article</title>
              <link>https://old.example.com/1</link>
              <description>Old news</description>
              <pubDate>{_rfc2822(hours_ago=48)}</pubDate>
            </item>
          </channel>
        </rss>
    """)

    config = make_config(sources=[make_source()])

    async def fake_get(url: str, timeout: float) -> MagicMock:
        return make_http_response(old_rss.encode())

    with patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=fake_get)):
        result, _ = await collect(config)

    assert result.get("Tech", []) == []


@pytest.mark.asyncio
async def test_collect_groups_by_category(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".cache").mkdir()

    sources = [
        make_source(name="Tech Feed", url="https://t.example.com/feed", category="Tech"),
        make_source(name="Finance Feed", url="https://f.example.com/feed", category="Finance"),
    ]
    config = make_config(sources=sources)

    def make_rss_for(domain: str) -> bytes:
        return textwrap.dedent(f"""\
            <?xml version="1.0" encoding="UTF-8"?>
            <rss version="2.0">
              <channel><title>{domain}</title>
                <item>
                  <title>{domain} Article</title>
                  <link>https://{domain}.example.com/1</link>
                  <description>Some body</description>
                  <pubDate>{_rfc2822(hours_ago=2)}</pubDate>
                </item>
              </channel>
            </rss>
        """).encode()

    async def fake_get(url: str, timeout: float) -> MagicMock:
        domain = "tech" if "t.example" in url else "finance"
        return make_http_response(make_rss_for(domain))

    with patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=fake_get)):
        result, _ = await collect(config)

    assert "Tech" in result
    assert "Finance" in result


@pytest.mark.asyncio
async def test_collect_html_stripped_in_description(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".cache").mkdir()

    config = make_config(sources=[make_source()])

    async def fake_get(url: str, timeout: float) -> MagicMock:
        return make_http_response(RSS_SAMPLE.encode())

    with patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=fake_get)):
        result, _ = await collect(config)

    for articles in result.values():
        for article in articles:
            assert "<" not in article.description
            assert ">" not in article.description


# ---------------------------------------------------------------------------
# allocate_slots tests
# ---------------------------------------------------------------------------

class TestAllocateSlots:
    def test_proportional(self) -> None:
        sources = [
            make_source(name="High", priority=5),
            make_source(name="Med", priority=3),
            make_source(name="Low", priority=1),
        ]
        slots = allocate_slots(sources, total_budget=18)
        # total_weight=9; expected: round(18*5/9)=10, round(18*3/9)=6, round(18*1/9)=2
        assert slots["High"] == 10
        assert slots["Med"] == 6
        assert slots["Low"] == 2

    def test_minimum_one(self) -> None:
        sources = [make_source(name="Tiny", priority=1)]
        slots = allocate_slots(sources, total_budget=1)
        assert slots["Tiny"] >= 1

    def test_zero_weight_fallback(self) -> None:
        sources = [make_source(name="A", priority=0), make_source(name="B", priority=0)]
        slots = allocate_slots(sources, total_budget=10)
        assert slots["A"] == 1
        assert slots["B"] == 1



@pytest.mark.asyncio
async def test_collect_respects_priority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """High-priority source should receive more article slots than low-priority source."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".cache").mkdir()

    def make_multi_rss(prefix: str, count: int = 5) -> bytes:
        items = "\n".join(
            f"""<item>
              <title>{prefix} Article {i}</title>
              <link>https://{prefix.lower()}.example.com/{i}</link>
              <description>Body {i}</description>
              <pubDate>{_rfc2822(hours_ago=i + 1)}</pubDate>
            </item>"""
            for i in range(count)
        )
        return textwrap.dedent(f"""\
            <?xml version="1.0" encoding="UTF-8"?>
            <rss version="2.0">
              <channel><title>{prefix}</title>
                {items}
              </channel>
            </rss>
        """).encode()

    high = make_source(name="High", url="https://high.example.com/feed", category="Tech", priority=5)
    low = make_source(name="Low", url="https://low.example.com/feed", category="Tech", priority=1)
    # Budget=6, weights=6 → High gets round(6*5/6)=5 slots, Low gets round(6*1/6)=1 slot
    config = make_config(sources=[high, low], max_total_articles=6, max_articles_per_source=5)

    async def fake_get(url: str, timeout: float) -> MagicMock:
        prefix = "High" if "high" in url else "Low"
        return make_http_response(make_multi_rss(prefix, count=5))

    with patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=fake_get)):
        result, _ = await collect(config)

    high_count = sum(1 for a in result.get("Tech", []) if a.source == "High")
    low_count = sum(1 for a in result.get("Tech", []) if a.source == "Low")
    assert high_count > low_count


@pytest.mark.asyncio
async def test_collect_redistributes_unused_slots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unused slots from a quiet high-priority source must flow to active lower-priority sources."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".cache").mkdir()

    def make_multi_rss(prefix: str, count: int) -> bytes:
        items = "\n".join(
            f"""<item>
              <title>{prefix} Article {i}</title>
              <link>https://{prefix.lower()}.example.com/{i}</link>
              <description>Body {i}</description>
              <pubDate>{_rfc2822(hours_ago=i + 1)}</pubDate>
            </item>"""
            for i in range(count)
        )
        return textwrap.dedent(f"""\
            <?xml version="1.0" encoding="UTF-8"?>
            <rss version="2.0">
              <channel><title>{prefix}</title>
                {items}
              </channel>
            </rss>
        """).encode()

    # priority=5 quiet feed gets large slot allocation but only has 0 articles.
    # priority=1 active feed has 5 articles but only gets 1 proportional slot.
    # With redistribution the active feed should fill the remaining budget.
    quiet = make_source(name="Quiet", url="https://quiet.example.com/feed", category="Tech", priority=5)
    active = make_source(name="Active", url="https://active.example.com/feed", category="Tech", priority=1)
    config = make_config(sources=[quiet, active], max_total_articles=6, max_articles_per_source=5)

    async def fake_get(url: str, timeout: float) -> MagicMock:
        if "quiet" in url:
            return make_http_response(make_multi_rss("Quiet", count=0))
        return make_http_response(make_multi_rss("Active", count=5))

    with patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=fake_get)):
        result, _ = await collect(config)

    total = sum(len(v) for v in result.values())
    active_count = sum(1 for a in result.get("Tech", []) if a.source == "Active")
    # Without redistribution only 1 article would be collected; with it up to 5 should be.
    assert active_count > 1
    assert total > 1


# ---------------------------------------------------------------------------
# effective_priorities tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_collect_uses_effective_priorities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When effective_priorities are provided, they override static priorities."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".cache").mkdir()

    def make_multi_rss(prefix: str, count: int = 5) -> bytes:
        items = "\n".join(
            f"""<item>
              <title>{prefix} Article {i}</title>
              <link>https://{prefix.lower()}.example.com/{i}</link>
              <description>Body {i}</description>
              <pubDate>{_rfc2822(hours_ago=i + 1)}</pubDate>
            </item>"""
            for i in range(count)
        )
        return textwrap.dedent(f"""\
            <?xml version="1.0" encoding="UTF-8"?>
            <rss version="2.0">
              <channel><title>{prefix}</title>
                {items}
              </channel>
            </rss>
        """).encode()

    # Static: High=5, Low=1. Effective: flip them — High=1, Low=5.
    high = make_source(name="High", url="https://high.example.com/feed", category="Tech", priority=5)
    low = make_source(name="Low", url="https://low.example.com/feed", category="Tech", priority=1)
    config = make_config(sources=[high, low], max_total_articles=6, max_articles_per_source=5)

    effective = {"High": 1, "Low": 5}

    async def fake_get(url: str, timeout: float) -> MagicMock:
        prefix = "High" if "high" in url else "Low"
        return make_http_response(make_multi_rss(prefix, count=5))

    with patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=fake_get)):
        result, _ = await collect(config, effective_priorities=effective)

    low_count = sum(1 for a in result.get("Tech", []) if a.source == "Low")
    high_count = sum(1 for a in result.get("Tech", []) if a.source == "High")
    # With flipped effective priorities, Low should get more slots
    assert low_count > high_count


@pytest.mark.asyncio
async def test_collect_falls_back_to_static_priorities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When effective_priorities is None, static priorities are used."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".cache").mkdir()

    def make_multi_rss(prefix: str, count: int = 5) -> bytes:
        items = "\n".join(
            f"""<item>
              <title>{prefix} Article {i}</title>
              <link>https://{prefix.lower()}.example.com/{i}</link>
              <description>Body {i}</description>
              <pubDate>{_rfc2822(hours_ago=i + 1)}</pubDate>
            </item>"""
            for i in range(count)
        )
        return textwrap.dedent(f"""\
            <?xml version="1.0" encoding="UTF-8"?>
            <rss version="2.0">
              <channel><title>{prefix}</title>
                {items}
              </channel>
            </rss>
        """).encode()

    high = make_source(name="High", url="https://high.example.com/feed", category="Tech", priority=5)
    low = make_source(name="Low", url="https://low.example.com/feed", category="Tech", priority=1)
    config = make_config(sources=[high, low], max_total_articles=6, max_articles_per_source=5)

    async def fake_get(url: str, timeout: float) -> MagicMock:
        prefix = "High" if "high" in url else "Low"
        return make_http_response(make_multi_rss(prefix, count=5))

    with patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=fake_get)):
        result, _ = await collect(config, effective_priorities=None)

    high_count = sum(1 for a in result.get("Tech", []) if a.source == "High")
    low_count = sum(1 for a in result.get("Tech", []) if a.source == "Low")
    # With static priorities, High (priority=5) should get more
    assert high_count > low_count


# ---------------------------------------------------------------------------
# Trial source slot budget tests
# ---------------------------------------------------------------------------


class TestAllocateSlotsTrial:
    def test_trial_sources_get_separate_budget(self) -> None:
        """Trial sources should use trial_budget, reserved from total budget."""
        regular = make_source(name="Regular", priority=5)
        trial = SourceConfig(
            name="Trial", url="https://t.com/feed", category="Tech",
            enabled=True, priority=3, trial=True, trial_started="2026-03-01",
        )
        slots = allocate_slots([regular, trial], total_budget=10, trial_budget=2)
        # Regular should get total_budget - trial_budget = 8
        assert slots["Regular"] == 8
        # Trial should get all 2 of the trial budget
        assert slots["Trial"] == 2

    def test_trial_no_budget_treated_as_regular(self) -> None:
        """Without trial_budget, trial sources compete with regular sources."""
        regular = make_source(name="Regular", priority=5)
        trial = SourceConfig(
            name="Trial", url="https://t.com/feed", category="Tech",
            enabled=True, priority=3, trial=True, trial_started="2026-03-01",
        )
        slots = allocate_slots([regular, trial], total_budget=8)
        # total_weight=8, Regular=round(8*5/8)=5, Trial=round(8*3/8)=3
        assert slots["Regular"] == 5
        assert slots["Trial"] == 3

    def test_trial_budget_reserved_from_regular(self) -> None:
        """Trial budget is subtracted from total, so regular sources get the remainder."""
        sources = [
            make_source(name="R1", priority=3),
            make_source(name="R2", priority=3),
            SourceConfig(
                name="T1", url="https://t.com", category="Tech",
                enabled=True, priority=3, trial=True, trial_started="2026-03-01",
            ),
        ]
        slots = allocate_slots(sources, total_budget=10, trial_budget=2)
        # Regular sources split (10 - 2) = 8 evenly
        assert slots["R1"] == 4
        assert slots["R2"] == 4
        # Trial gets from trial budget
        assert slots["T1"] == 2

    def test_trial_budget_equals_total_no_regular_overflow(self) -> None:
        """When trial_budget == total_budget, regular sources must get 0 slots."""
        regular_a = make_source(name="RegularA", priority=3)
        regular_b = make_source(name="RegularB", priority=3)
        trial = SourceConfig(
            name="Trial", url="https://t.com/feed", category="Tech",
            enabled=True, priority=3, trial=True, trial_started="2026-03-01",
        )
        slots = allocate_slots(
            [regular_a, regular_b, trial], total_budget=2, trial_budget=2
        )
        assert slots["RegularA"] == 0
        assert slots["RegularB"] == 0
        assert slots["Trial"] == 2
        assert sum(slots.values()) <= 2


@pytest.mark.asyncio
async def test_collect_trial_sources_separate_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Trial sources should get a separate slot budget when adaptive is enabled."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".cache").mkdir()

    from src.config import AdaptiveConfig

    def make_multi_rss(prefix: str, count: int = 5) -> bytes:
        items = "\n".join(
            f"""<item>
              <title>{prefix} Article {i}</title>
              <link>https://{prefix.lower()}.example.com/{i}</link>
              <description>Body {i}</description>
              <pubDate>{_rfc2822(hours_ago=i + 1)}</pubDate>
            </item>"""
            for i in range(count)
        )
        return textwrap.dedent(f"""\
            <?xml version="1.0" encoding="UTF-8"?>
            <rss version="2.0">
              <channel><title>{prefix}</title>
                {items}
              </channel>
            </rss>
        """).encode()

    regular = make_source(name="Regular", url="https://regular.example.com/feed", priority=5)
    trial = SourceConfig(
        name="Trial", url="https://trial.example.com/feed", category="Tech",
        enabled=True, priority=3, trial=True, trial_started="2026-03-01",
    )
    config = Config(
        llm=LLMConfig(provider="anthropic", model="test"),
        delivery=DeliveryConfig(telegram=False, markdown_to_repo=False, markdown_dir="digests"),
        digest=DigestConfig(
            language="ru", max_articles_per_source=10,
            max_total_articles=10, summary_style="analytical",
        ),
        sources=[regular, trial],
        adaptive=AdaptiveConfig(enabled=True, trial_slots=2),
    )

    async def fake_get(url: str, timeout: float) -> MagicMock:
        prefix = "Regular" if "regular" in url else "Trial"
        return make_http_response(make_multi_rss(prefix, count=5))

    with patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=fake_get)):
        result, _ = await collect(config)

    regular_count = sum(1 for a in result.get("Tech", []) if a.source == "Regular")
    trial_count = sum(1 for a in result.get("Tech", []) if a.source == "Trial")
    # Trial should get at most trial_slots=2
    assert trial_count <= 2
    # Regular should get more since it has the full budget
    assert regular_count > trial_count


@pytest.mark.asyncio
async def test_collect_per_source_recency_hours(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Source with recency_hours=168 should include 48h-old articles that default 24h window would reject."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".cache").mkdir()

    source = SourceConfig(
        name="Weekly Blog",
        url="https://weekly.example.com/feed",
        category="Tech",
        enabled=True,
        priority=3,
        recency_hours=168,
    )
    config = make_config(sources=[source])

    old_rss = textwrap.dedent(f"""\
        <?xml version="1.0" encoding="UTF-8"?>
        <rss version="2.0">
          <channel>
            <title>Weekly Blog</title>
            <item>
              <title>Week-Old Article</title>
              <link>https://weekly.example.com/1</link>
              <description>Published 48 hours ago</description>
              <pubDate>{_rfc2822(hours_ago=48)}</pubDate>
            </item>
          </channel>
        </rss>
    """)

    async def fake_get(url: str, timeout: float) -> MagicMock:
        return make_http_response(old_rss.encode())

    with patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=fake_get)):
        result, _ = await collect(config)

    # With recency_hours=168 the 48h-old article must be included
    assert len(result.get("Tech", [])) == 1


@pytest.mark.asyncio
async def test_collect_default_recency_rejects_48h_old(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default recency_hours=24 should reject articles older than 24h."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".cache").mkdir()

    # make_source() returns SourceConfig with default recency_hours=24
    config = make_config(sources=[make_source()])

    old_rss = textwrap.dedent(f"""\
        <?xml version="1.0" encoding="UTF-8"?>
        <rss version="2.0">
          <channel>
            <title>Test Feed</title>
            <item>
              <title>Old Article</title>
              <link>https://example.com/old</link>
              <description>Published 48 hours ago</description>
              <pubDate>{_rfc2822(hours_ago=48)}</pubDate>
            </item>
          </channel>
        </rss>
    """)

    async def fake_get(url: str, timeout: float) -> MagicMock:
        return make_http_response(old_rss.encode())

    with patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=fake_get)):
        result, _ = await collect(config)

    assert result.get("Tech", []) == []


# ---------------------------------------------------------------------------
# atomic_json_write round-trip (via src._util)
# ---------------------------------------------------------------------------


def test_atomic_json_write_round_trip(tmp_path: Path) -> None:
    """atomic_json_write writes correct JSON and leaves no .tmp file."""
    import json

    from src._util import atomic_json_write

    target = tmp_path / "data.json"
    data = {"key": "value", "num": 42}
    atomic_json_write(target, data)

    assert target.exists()
    assert not (tmp_path / "data.json.tmp").exists()
    loaded = json.loads(target.read_text(encoding="utf-8"))
    assert loaded == data


def test_atomic_json_write_overwrites_existing(tmp_path: Path) -> None:
    """atomic_json_write replaces an existing file atomically."""
    import json

    from src._util import atomic_json_write

    target = tmp_path / "data.json"
    atomic_json_write(target, {"v": 1})
    atomic_json_write(target, {"v": 2})

    loaded = json.loads(target.read_text(encoding="utf-8"))
    assert loaded == {"v": 2}
    assert not (tmp_path / "data.json.tmp").exists()
