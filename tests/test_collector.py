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
    collect,
    save_dedup_cache,
)
from src.config import Config, DeliveryConfig, DigestConfig, LLMConfig, SourceConfig

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

RSS_SAMPLE = textwrap.dedent("""\
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
          <pubDate>Mon, 16 Mar 2026 05:00:00 +0000</pubDate>
        </item>
        <item>
          <title>Article Two</title>
          <link>https://example.com/2</link>
          <description>Second article body.</description>
          <pubDate>Mon, 16 Mar 2026 04:00:00 +0000</pubDate>
        </item>
      </channel>
    </rss>
""")

ATOM_SAMPLE = textwrap.dedent("""\
    <?xml version="1.0" encoding="utf-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <title>Atom Feed</title>
      <link href="https://atom.example.com"/>
      <entry>
        <title>Atom Article</title>
        <link href="https://atom.example.com/1"/>
        <summary>Atom article summary.</summary>
        <updated>2026-03-16T06:00:00Z</updated>
      </entry>
    </feed>
""")

MALFORMED_XML = b"<not valid xml><<<"


def make_source(
    name: str = "Test",
    url: str = "https://example.com/feed",
    category: str = "Tech",
    enabled: bool = True,
) -> SourceConfig:
    return SourceConfig(name=name, url=url, category=category, enabled=enabled)


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
                  <pubDate>Mon, 16 Mar 2026 05:00:00 +0000</pubDate>
                </item>
                <item>
                  <title>Article {idx}-B</title>
                  <link>https://s{idx}.example.com/b</link>
                  <description>Body B</description>
                  <pubDate>Mon, 16 Mar 2026 04:00:00 +0000</pubDate>
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

    old_rss = textwrap.dedent("""\
        <?xml version="1.0" encoding="UTF-8"?>
        <rss version="2.0">
          <channel>
            <title>Old Feed</title>
            <item>
              <title>Old Article</title>
              <link>https://old.example.com/1</link>
              <description>Old news</description>
              <pubDate>Mon, 10 Mar 2026 05:00:00 +0000</pubDate>
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
                  <pubDate>Mon, 16 Mar 2026 05:00:00 +0000</pubDate>
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
