"""Editorial admission precedes legacy slot budgets and never consumes dedup."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from digest.config import SourceConfig
from digest.radar.collector import (
    AllFeedsFailedError,
    Article,
    CollectionCoverage,
    article_hash,
    collect,
)
from scripts.review_fixture import fixture_config


@pytest.fixture(autouse=True)
def _ignore_proxy_settings_for_mocked_collection(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("ALL_PROXY", "all_proxy", "HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
        monkeypatch.delenv(key, raising=False)


def _source(name: str, priority: int = 3, category: str = "Tech") -> SourceConfig:
    return SourceConfig(name, f"https://{name}.example/feed", category, True, priority=priority, recency_hours=48)


def _articles(name: str, count: int) -> list[Article]:
    return [Article(f"{name} {index}", f"https://{name}.example/{index}", "Synthetic source description",
                    name, "Tech", datetime.now(timezone.utc) - timedelta(hours=1)) for index in range(count)]


@pytest.mark.asyncio
async def test_all_unique_eligible_articles_are_admitted_before_allocation() -> None:
    config = fixture_config()
    config.sources = [_source("low", 1), _source("high", 5)]
    config.radar.max_articles_per_category = 1
    batches = [_articles("low", 12), _articles("high", 12)]
    coverage = CollectionCoverage()
    with (
        patch("digest.radar.collector._fetch_feed", AsyncMock(side_effect=batches)),
        patch("digest.radar.collector._load_cache", return_value={}),
        patch("digest.radar.collector._save_cache") as save,
        patch("digest.radar.collector.allocate_slots") as allocate,
    ):
        grouped, cache = await collect(config, admit_all=True, coverage=coverage)
    assert len(grouped["Tech"]) == 24
    assert grouped["Tech"][0].source == "high"
    assert cache == {}
    assert asdict(coverage) == {"raw_fetched": 24, "eligible_unique": 24, "admitted": 24, "legacy_allocated": None}
    allocate.assert_not_called()
    save.assert_not_called()


@pytest.mark.asyncio
async def test_admission_preserves_ingestion_filters_confirmed_dedup_and_unique_identity() -> None:
    config = fixture_config()
    config.sources = [_source("one"), _source("two")]
    config.filters.blocklist_keywords = ["blocked"]
    one = _articles("one", 4)
    one[1].pub_date = datetime.now(timezone.utc) - timedelta(days=3)
    one[2].description = "blocked item"
    confirmed = {article_hash(one[3].title, one[3].link): datetime.now(timezone.utc).isoformat()}
    duplicate = one[0]
    coverage = CollectionCoverage()
    with (
        patch("digest.radar.collector._fetch_feed", AsyncMock(side_effect=[one, [duplicate, *_articles("two", 2)]])),
        patch("digest.radar.collector._load_cache", return_value=confirmed.copy()),
        patch("digest.radar.collector._save_cache") as save,
    ):
        grouped, cache = await collect(config, admit_all=True, coverage=coverage)
    assert len(grouped["Tech"]) == 3
    assert cache == confirmed
    assert coverage.raw_fetched == 7 and coverage.eligible_unique == coverage.admitted == 3
    save.assert_not_called()


@pytest.mark.asyncio
async def test_adaptive_priority_orders_admission_but_zero_priority_does_not_discard() -> None:
    config = fixture_config()
    config.sources = [_source("static_high", 5), _source("adaptive_high", 1)]
    with (
        patch("digest.radar.collector._fetch_feed", AsyncMock(side_effect=[
            _articles("static_high", 2), _articles("adaptive_high", 2),
        ])),
        patch("digest.radar.collector._load_cache", return_value={}),
    ):
        grouped, _ = await collect(config, {"static_high": 0, "adaptive_high": 5}, admit_all=True)
    assert [item.source for item in grouped["Tech"]] == ["adaptive_high"] * 2 + ["static_high"] * 2


@pytest.mark.asyncio
async def test_legacy_default_still_limits_allocation_and_returns_proposed_dedup() -> None:
    config = fixture_config()
    config.sources = [_source("one")]
    config.radar.max_articles_per_category = 2
    coverage = CollectionCoverage()
    with (
        patch("digest.radar.collector._fetch_feed", AsyncMock(return_value=_articles("one", 10))),
        patch("digest.radar.collector._load_cache", return_value={}),
        patch("digest.radar.collector._save_cache") as save,
    ):
        grouped, cache = await collect(config, coverage=coverage)
    assert len(grouped["Tech"]) == 2 and len(cache) == 2
    assert asdict(coverage) == {"raw_fetched": 10, "eligible_unique": 10, "admitted": 2, "legacy_allocated": 2}
    save.assert_not_called()


@pytest.mark.asyncio
async def test_failed_sources_and_reused_coverage_do_not_claim_admission() -> None:
    config = fixture_config()
    config.sources = [_source("failed")]
    coverage = CollectionCoverage(99, 88, 77, 66)
    with (
        patch("digest.radar.collector._fetch_feed", AsyncMock(return_value=None)),
        patch("digest.radar.collector._load_cache", return_value={}),
    ):
        with pytest.raises(AllFeedsFailedError):
            await collect(config, admit_all=True, coverage=coverage)
    assert asdict(coverage) == {"raw_fetched": 0, "eligible_unique": 0, "admitted": 0, "legacy_allocated": None}
