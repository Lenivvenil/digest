"""Tests for src/radar/collector.py"""

from __future__ import annotations

import socket as _socket
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from digest._dns_pinning import ValidatedURL as _ValidatedURL
from digest.config import (
    Config,
    FiltersConfig,
    IrritatorConfig,
    LLMConfig,
    ObsidianConfig,
    ProviderConfig,
    RadarConfig,
    SourceConfig,
    TelegramConfig,
)
from digest.radar.collector import (
    AllFeedsFailedError,
    Article,
    _is_recent,
    _parse_pub_date,
    _prune_cache,
    _strip_html,
    allocate_slots,
    article_hash,
    collect,
    save_dedup_cache,
)

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


def _make_config(
    sources: list[SourceConfig] | None = None,
    max_articles_per_category: int = 10,
    blocklist_keywords: list[str] | None = None,
) -> Config:
    if sources is None:
        sources = [make_source()]
    return Config(
        llm=LLMConfig(providers=[ProviderConfig(name="groq", model="llama-3.3-70b-versatile")]),
        radar=RadarConfig(max_articles_per_category=max_articles_per_category),
        irritator=IrritatorConfig(),
        sources=sources,
        filters=FiltersConfig(blocklist_keywords=blocklist_keywords or []),
        telegram=TelegramConfig(enabled=False),
        obsidian=ObsidianConfig(enabled=False),
    )


def _make_fake_validated(url: str) -> _ValidatedURL:
    """Return a fake ValidatedURL for any URL (used in tests to avoid real DNS)."""
    from urllib.parse import urlparse

    hostname = urlparse(url).hostname or "example.com"
    return _ValidatedURL(
        url=url,
        hostname=hostname,
        pinned_addrinfos=[(_socket.AF_INET, _socket.SOCK_STREAM, 0, "", ("93.184.216.34", 0))],
    )


@pytest.fixture(autouse=True)
def _mock_validate_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch _validate_url in collector to avoid real DNS lookups in tests."""
    monkeypatch.setattr(
        "digest.radar.collector._validate_url",
        lambda url: _make_fake_validated(url),
    )
    monkeypatch.setattr(
        "digest.radar.collector._pin_dns",
        lambda hostname, addrinfos: _NullCtx(),
    )


class _NullCtx:
    """No-op context manager used by _mock_validate_url autouse fixture."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, *_: object) -> None:
        pass


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
        assert article_hash("Title", "https://x.com") == article_hash("Title", "https://x.com")

    def test_different_for_different_inputs(self) -> None:
        assert article_hash("A", "B") != article_hash("C", "D")


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

    config = _make_config(sources=[make_source()])

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

    config = _make_config(sources=[make_source(name="Atom", url="https://atom.example.com/feed")])

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

    config = _make_config(sources=[make_source()])

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
    config = _make_config(sources=[bad_source, good_source])

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
    config = _make_config(sources=[good_source, bad_source])

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

    config = _make_config(sources=[make_source()])

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

    import httpx as _httpx

    good_source = make_source(name="Good", url="https://good.example.com/feed")
    bad_source = make_source(name="Bad", url="https://bad.example.com/feed")
    config = _make_config(sources=[good_source, bad_source])

    async def fake_get(url: str, timeout: float) -> MagicMock:
        if "bad" in url:
            raise _httpx.TimeoutException("timed out")
        return make_http_response(RSS_SAMPLE.encode())

    with patch("asyncio.sleep"), patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=fake_get)):
        result, _ = await collect(config)

    assert len(result.get("Tech", [])) > 0


@pytest.mark.asyncio
async def test_collect_all_feeds_timeout_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When all feeds time out, AllFeedsFailedError is raised."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".cache").mkdir()

    import httpx as _httpx

    config = _make_config(sources=[make_source()])

    async def fake_get(url: str, timeout: float) -> MagicMock:
        raise _httpx.TimeoutException("timed out")

    with patch("asyncio.sleep"), patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=fake_get)):
        with pytest.raises(AllFeedsFailedError):
            await collect(config)


@pytest.mark.asyncio
async def test_collect_respects_max_per_category(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """max_articles_per_category=1 should limit a single-category run to 1 article."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".cache").mkdir()

    config = _make_config(sources=[make_source()], max_articles_per_category=1)

    async def fake_get(url: str, timeout: float) -> MagicMock:
        return make_http_response(RSS_SAMPLE.encode())

    with patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=fake_get)):
        result, _ = await collect(config)

    assert len(result.get("Tech", [])) == 1


@pytest.mark.asyncio
async def test_collect_respects_total_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """total_budget = max_articles_per_category * n_categories; enforced across all sources."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".cache").mkdir()

    sources = [
        make_source(name=f"S{i}", url=f"https://s{i}.example.com/feed", category="Tech")
        for i in range(3)
    ]
    # 3 sources, 1 category → total_budget = max_per_cat * 1 = 3
    config = _make_config(sources=sources, max_articles_per_category=3)

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

    config = _make_config(sources=[make_source()])

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
    config = _make_config(sources=sources)

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

    config = _make_config(sources=[make_source()])

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

    def test_priority_overrides(self) -> None:
        sources = [
            make_source(name="High", priority=5),
            make_source(name="Low", priority=1),
        ]
        # Without overrides: High dominates
        normal = allocate_slots(sources, total_budget=6)
        assert normal["High"] > normal["Low"]

        # With overrides: reverse the weights
        overridden = allocate_slots(sources, total_budget=6, priority_overrides={"High": 1, "Low": 5})
        assert overridden["Low"] > overridden["High"]

    def test_priority_overrides_partial(self) -> None:
        """Sources missing from overrides fall back to static priority."""
        sources = [
            make_source(name="A", priority=3),
            make_source(name="B", priority=3),
        ]
        slots = allocate_slots(sources, total_budget=6, priority_overrides={"A": 5})
        # A gets boosted (5), B stays at 3
        assert slots["A"] > slots["B"]


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
    # Budget=6 (max_per_cat=6, 1 category), weights=6 → High=5 slots, Low=1 slot
    config = _make_config(sources=[high, low], max_articles_per_category=6)

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

    # Quiet gets large proportional slot but has 0 articles.
    # Active has 5 articles but only gets 1 proportional slot.
    # With redistribution the active feed should fill the remaining budget.
    quiet = make_source(name="Quiet", url="https://quiet.example.com/feed", category="Tech", priority=5)
    active = make_source(name="Active", url="https://active.example.com/feed", category="Tech", priority=1)
    # Budget = max_per_cat * 1 category = 5
    config = _make_config(sources=[quiet, active], max_articles_per_category=5)

    async def fake_get(url: str, timeout: float) -> MagicMock:
        if "quiet" in url:
            return make_http_response(make_multi_rss("Quiet", count=0))
        return make_http_response(make_multi_rss("Active", count=5))

    with patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=fake_get)):
        result, _ = await collect(config)

    total = sum(len(v) for v in result.values())
    active_count = sum(1 for a in result.get("Tech", []) if a.source == "Active")
    assert active_count > 1
    assert total > 1


# ---------------------------------------------------------------------------
# Blocklist filtering tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_collect_blocklist_filters_by_title(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Articles whose title matches a blocklist keyword must be excluded."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".cache").mkdir()

    blocked_rss = textwrap.dedent(f"""\
        <?xml version="1.0" encoding="UTF-8"?>
        <rss version="2.0">
          <channel><title>Test</title>
            <item>
              <title>Trump signs executive order on tariffs</title>
              <link>https://example.com/blocked1</link>
              <description>Politics news</description>
              <pubDate>{_rfc2822(hours_ago=2)}</pubDate>
            </item>
            <item>
              <title>Tech Innovation Breakthrough</title>
              <link>https://example.com/allowed</link>
              <description>Clean tech description</description>
              <pubDate>{_rfc2822(hours_ago=2)}</pubDate>
            </item>
          </channel>
        </rss>
    """)

    config = _make_config(sources=[make_source()], blocklist_keywords=["trump", "tariff"])

    async def fake_get(url: str, timeout: float) -> MagicMock:
        return make_http_response(blocked_rss.encode())

    with patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=fake_get)):
        result, _ = await collect(config)

    articles = result.get("Tech", [])
    titles = [a.title for a in articles]
    assert "Tech Innovation Breakthrough" in titles
    assert all("trump" not in t.lower() and "tariff" not in t.lower() for t in titles)


@pytest.mark.asyncio
async def test_collect_blocklist_filters_by_description(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Articles whose description matches a blocklist keyword must be excluded."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".cache").mkdir()

    blocked_rss = textwrap.dedent(f"""\
        <?xml version="1.0" encoding="UTF-8"?>
        <rss version="2.0">
          <channel><title>Test</title>
            <item>
              <title>Economy Update</title>
              <link>https://example.com/blocked2</link>
              <description>Market reacts to new cryptocurrency scam wave</description>
              <pubDate>{_rfc2822(hours_ago=2)}</pubDate>
            </item>
            <item>
              <title>AI Research News</title>
              <link>https://example.com/allowed2</link>
              <description>Researchers publish new benchmark results</description>
              <pubDate>{_rfc2822(hours_ago=2)}</pubDate>
            </item>
          </channel>
        </rss>
    """)

    config = _make_config(sources=[make_source()], blocklist_keywords=["scam"])

    async def fake_get(url: str, timeout: float) -> MagicMock:
        return make_http_response(blocked_rss.encode())

    with patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=fake_get)):
        result, _ = await collect(config)

    articles = result.get("Tech", [])
    assert all("scam" not in a.description.lower() for a in articles)
    assert any(a.title == "AI Research News" for a in articles)


@pytest.mark.asyncio
async def test_collect_empty_blocklist_passes_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty blocklist should not filter any articles."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".cache").mkdir()

    config = _make_config(sources=[make_source()], blocklist_keywords=[])

    async def fake_get(url: str, timeout: float) -> MagicMock:
        return make_http_response(RSS_SAMPLE.encode())

    with patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=fake_get)):
        result, _ = await collect(config)

    assert len(result.get("Tech", [])) == 2


@pytest.mark.asyncio
async def test_collect_blocklist_case_insensitive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Blocklist matching must be case-insensitive."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".cache").mkdir()

    mixed_case_rss = textwrap.dedent(f"""\
        <?xml version="1.0" encoding="UTF-8"?>
        <rss version="2.0">
          <channel><title>Test</title>
            <item>
              <title>CRYPTO Market Update</title>
              <link>https://example.com/crypto</link>
              <description>Cryptocurrency prices surge</description>
              <pubDate>{_rfc2822(hours_ago=2)}</pubDate>
            </item>
          </channel>
        </rss>
    """)

    config = _make_config(sources=[make_source()], blocklist_keywords=["crypto"])

    async def fake_get(url: str, timeout: float) -> MagicMock:
        return make_http_response(mixed_case_rss.encode())

    with patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=fake_get)):
        result, _ = await collect(config)

    assert result.get("Tech", []) == []


# ---------------------------------------------------------------------------
# atomic_json_write round-trip (via src._util)
# ---------------------------------------------------------------------------


def test_atomic_json_write_round_trip(tmp_path: Path) -> None:
    """atomic_json_write writes correct JSON and leaves no .tmp file."""
    import json

    from digest._util import atomic_json_write

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

    from digest._util import atomic_json_write

    target = tmp_path / "data.json"
    atomic_json_write(target, {"v": 1})
    atomic_json_write(target, {"v": 2})

    loaded = json.loads(target.read_text(encoding="utf-8"))
    assert loaded == {"v": 2}
    assert not (tmp_path / "data.json.tmp").exists()


# ---------------------------------------------------------------------------
# _fetch_feed retry behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_feed_retries_503(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_fetch_feed retries once on HTTP 503 and returns articles on success."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".cache").mkdir()

    config = _make_config(sources=[make_source()])
    call_count = 0

    async def fake_get(url: str, timeout: float) -> MagicMock:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return make_http_response(b"", 503)
        return make_http_response(make_rss_sample().encode())

    with patch("asyncio.sleep"), patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=fake_get)):
        result, _ = await collect(config)

    assert call_count == 2
    assert "Tech" in result
    assert len(result["Tech"]) == 2


@pytest.mark.asyncio
async def test_fetch_feed_retries_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_fetch_feed retries once after a TimeoutException and returns articles on success."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".cache").mkdir()

    import httpx as _httpx

    config = _make_config(sources=[make_source()])
    call_count = 0

    async def fake_get(url: str, timeout: float) -> MagicMock:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise _httpx.TimeoutException("timed out")
        return make_http_response(make_rss_sample().encode())

    with patch("asyncio.sleep"), patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=fake_get)):
        result, _ = await collect(config)

    assert call_count == 2
    assert "Tech" in result


@pytest.mark.asyncio
async def test_fetch_feed_no_retry_404(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_fetch_feed does not retry on HTTP 404; source is skipped, others still process."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".cache").mkdir()

    source_404 = make_source(name="Bad", url="https://example.com/bad")
    source_ok = make_source(name="Good", url="https://example.com/good", category="Tech")
    config = _make_config(sources=[source_404, source_ok])
    calls: list[str] = []

    async def fake_get(url: str, timeout: float) -> MagicMock:
        calls.append(url)
        if "bad" in url:
            return make_http_response(b"", 404)
        return make_http_response(make_rss_sample().encode())

    with patch("asyncio.sleep"), patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=fake_get)):
        result, _ = await collect(config)

    bad_calls = [c for c in calls if "bad" in c]
    assert len(bad_calls) == 1
    assert "Tech" in result


def make_http_response_with_headers(
    content: bytes, status_code: int = 200, headers: dict[str, str] | None = None
) -> MagicMock:
    """Like make_http_response but supports custom response headers."""
    import httpx as _httpx

    resp = MagicMock()
    resp.status_code = status_code
    resp.content = content
    resp.headers = headers or {}
    resp.raise_for_status = MagicMock()
    if status_code >= 400:
        resp.raise_for_status.side_effect = _httpx.HTTPStatusError(
            "error", request=MagicMock(), response=resp
        )
    return resp


@pytest.mark.asyncio
async def test_fetch_feed_429_retry_after_honoured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HTTP 429 with Retry-After within limit: waits and retries once."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".cache").mkdir()

    source = make_source(name="RateLimited", url="https://example.com/rl")
    config = _make_config(sources=[source])
    calls: list[str] = []

    async def fake_get(url: str, timeout: float) -> MagicMock:
        calls.append(url)
        if len(calls) == 1:
            return make_http_response_with_headers(b"", 429, {"Retry-After": "5"})
        return make_http_response(make_rss_sample().encode())

    sleep_calls: list[float] = []

    async def fake_sleep(secs: float) -> None:
        sleep_calls.append(secs)

    with patch("asyncio.sleep", side_effect=fake_sleep), patch(
        "httpx.AsyncClient.get", new=AsyncMock(side_effect=fake_get)
    ):
        result, _ = await collect(config)

    assert len(calls) == 2
    assert sleep_calls == [5.0]
    assert "Tech" in result


@pytest.mark.asyncio
async def test_fetch_feed_429_retry_after_exceeds_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HTTP 429 with Retry-After exceeding _MAX_RETRY_AFTER_SECS: source skipped immediately."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".cache").mkdir()

    source = make_source(name="SlowServer", url="https://example.com/slow")
    config = _make_config(sources=[source])
    calls: list[str] = []

    async def fake_get(url: str, timeout: float) -> MagicMock:
        calls.append(url)
        return make_http_response_with_headers(b"", 429, {"Retry-After": "600"})

    sleep_calls: list[float] = []

    async def fake_sleep(secs: float) -> None:
        sleep_calls.append(secs)

    with patch("asyncio.sleep", side_effect=fake_sleep), patch(
        "httpx.AsyncClient.get", new=AsyncMock(side_effect=fake_get)
    ):
        with pytest.raises(AllFeedsFailedError):
            await collect(config)

    assert len(calls) == 1
    assert sleep_calls == []


@pytest.mark.asyncio
async def test_fetch_feed_ssrf_unsafe_url_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_fetch_feed returns None immediately when _validate_url returns None."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".cache").mkdir()

    source = make_source(name="Internal", url="http://192.168.1.1/feed.rss")
    config = _make_config(sources=[source])
    calls: list[str] = []

    monkeypatch.setattr("digest.radar.collector._validate_url", lambda url: None)

    async def fake_get(url: str, timeout: float) -> MagicMock:  # pragma: no cover
        calls.append(url)
        return make_http_response(make_rss_sample().encode())

    with patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=fake_get)):
        with pytest.raises(AllFeedsFailedError):
            await collect(config)

    assert calls == []


@pytest.mark.asyncio
async def test_fetch_feed_ssrf_valid_url_proceeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_fetch_feed fetches URLs that pass SSRF validation (autouse mock covers DNS)."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".cache").mkdir()

    source = make_source(name="Good", url="https://example.com/feed.rss")
    config = _make_config(sources=[source])
    calls: list[str] = []

    async def fake_get(url: str, timeout: float) -> MagicMock:
        calls.append(url)
        return make_http_response(make_rss_sample().encode())

    with patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=fake_get)):
        result, _ = await collect(config)

    assert calls == ["https://example.com/feed.rss"]


# ---------------------------------------------------------------------------
# Recency window tests
# ---------------------------------------------------------------------------


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
    config = _make_config(sources=[source])

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

    assert len(result.get("Tech", [])) == 1


@pytest.mark.asyncio
async def test_collect_default_recency_rejects_48h_old(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default recency_hours=24 should reject articles older than 24h."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".cache").mkdir()

    config = _make_config(sources=[make_source()])

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
