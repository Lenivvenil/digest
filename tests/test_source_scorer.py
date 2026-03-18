"""Tests for source scorer data models and scoring engine."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from src.config import AdaptiveConfig, SourceConfig
from src.source_scorer import (
    DailySnapshot,
    SourceStats,
    apply_trial_decisions,
    calculate_effective_priorities,
    calculate_score,
    detect_trending_sources,
    evaluate_trial_sources,
    load_stats,
    save_stats,
    update_stats,
)

import yaml


def test_daily_snapshot_creation() -> None:
    snap = DailySnapshot(
        date="2026-03-18",
        articles_found=5,
        articles_included=3,
        fetch_ok=True,
    )
    assert snap.date == "2026-03-18"
    assert snap.articles_found == 5
    assert snap.articles_included == 3
    assert snap.fetch_ok is True


def test_source_stats_defaults() -> None:
    stats = SourceStats(name="Test Feed")
    assert stats.name == "Test Feed"
    assert stats.total_fetches == 0
    assert stats.successful_fetches == 0
    assert stats.total_articles_found == 0
    assert stats.articles_included_in_digest == 0
    assert stats.avg_description_length == 0.0
    assert stats.last_seen is None
    assert stats.history == []


def test_source_stats_with_history() -> None:
    snap = DailySnapshot(
        date="2026-03-17", articles_found=10, articles_included=4, fetch_ok=True
    )
    stats = SourceStats(
        name="Rich Feed",
        total_fetches=5,
        successful_fetches=4,
        total_articles_found=50,
        articles_included_in_digest=20,
        avg_description_length=150.5,
        last_seen="2026-03-17",
        history=[snap],
    )
    assert stats.total_fetches == 5
    assert stats.successful_fetches == 4
    assert len(stats.history) == 1
    assert stats.history[0].articles_found == 10


# --- load_stats / save_stats ---


def test_load_stats_missing_file(tmp_path: Path) -> None:
    result = load_stats(str(tmp_path))
    assert result == {}


def test_save_and_load_round_trip(tmp_path: Path) -> None:
    stats = {
        "Feed A": SourceStats(
            name="Feed A",
            total_fetches=10,
            successful_fetches=9,
            total_articles_found=50,
            articles_included_in_digest=20,
            avg_description_length=120.5,
            last_seen="2026-03-18",
            history=[
                DailySnapshot(
                    date="2026-03-18",
                    articles_found=5,
                    articles_included=2,
                    fetch_ok=True,
                )
            ],
        )
    }
    save_stats(stats, str(tmp_path))
    loaded = load_stats(str(tmp_path))
    assert "Feed A" in loaded
    s = loaded["Feed A"]
    assert s.name == "Feed A"
    assert s.total_fetches == 10
    assert s.successful_fetches == 9
    assert s.avg_description_length == 120.5
    assert s.last_seen == "2026-03-18"
    assert len(s.history) == 1
    assert s.history[0].articles_found == 5
    assert s.history[0].fetch_ok is True


def test_load_stats_invalid_json(tmp_path: Path) -> None:
    path = tmp_path / "source_stats.json"
    path.write_text("not json", encoding="utf-8")
    result = load_stats(str(tmp_path))
    assert result == {}


def test_load_stats_non_dict(tmp_path: Path) -> None:
    path = tmp_path / "source_stats.json"
    path.write_text("[]", encoding="utf-8")
    result = load_stats(str(tmp_path))
    assert result == {}


# --- update_stats ---


def test_update_stats_new_source() -> None:
    stats: dict[str, SourceStats] = {}
    update_stats(stats, "New Feed", fetch_ok=True, articles_found=5,
                 articles_included=2, avg_desc_len=150.0)
    assert "New Feed" in stats
    s = stats["New Feed"]
    assert s.total_fetches == 1
    assert s.successful_fetches == 1
    assert s.total_articles_found == 5
    assert s.avg_description_length == 150.0
    assert len(s.history) == 1


def test_update_stats_failed_fetch() -> None:
    stats: dict[str, SourceStats] = {}
    update_stats(stats, "Bad Feed", fetch_ok=False, articles_found=0,
                 articles_included=0, avg_desc_len=0.0)
    s = stats["Bad Feed"]
    assert s.total_fetches == 1
    assert s.successful_fetches == 0
    assert s.last_seen is None
    assert len(s.history) == 1
    assert s.history[0].fetch_ok is False


def test_update_stats_caps_history_at_30() -> None:
    stats: dict[str, SourceStats] = {
        "Feed": SourceStats(
            name="Feed",
            history=[
                DailySnapshot(date=f"2026-02-{i:02d}", articles_found=1,
                              articles_included=0, fetch_ok=True)
                for i in range(1, 31)  # 30 entries
            ],
        )
    }
    assert len(stats["Feed"].history) == 30
    update_stats(stats, "Feed", fetch_ok=True, articles_found=3,
                 articles_included=1, avg_desc_len=100.0)
    assert len(stats["Feed"].history) == 30  # Still capped at 30


# --- calculate_score ---


def test_calculate_score_new_source() -> None:
    stats = SourceStats(name="New")
    score = calculate_score(stats)
    assert score == 0.5


def test_calculate_score_perfect_source() -> None:
    today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    stats = SourceStats(
        name="Perfect",
        total_fetches=10,
        successful_fetches=10,
        total_articles_found=50,
        articles_included_in_digest=50,
        avg_description_length=200.0,
        last_seen=today,
    )
    score = calculate_score(stats)
    # reliability=1.0*0.3 + productivity=1.0*0.3 + desc=1.0*0.2 + recency=1.0*0.2 = 1.0
    assert score >= 0.95


def test_calculate_score_dead_source() -> None:
    stats = SourceStats(
        name="Dead",
        total_fetches=10,
        successful_fetches=0,
        total_articles_found=0,
        articles_included_in_digest=0,
        avg_description_length=0.0,
        last_seen=None,
    )
    score = calculate_score(stats)
    # reliability=0 + productivity=0 + desc=0 + recency=0 = 0.0
    assert score <= 0.05


def test_calculate_score_partial() -> None:
    today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    stats = SourceStats(
        name="Partial",
        total_fetches=10,
        successful_fetches=8,
        total_articles_found=40,
        articles_included_in_digest=10,
        avg_description_length=50.0,
        last_seen=today,
    )
    score = calculate_score(stats)
    # Should be moderate
    assert 0.2 < score < 0.8


# --- detect_trending_sources ---


def test_detect_trending_flat_history() -> None:
    """Flat history should not detect any trends."""
    stats = {
        "Stable": SourceStats(
            name="Stable",
            history=[
                DailySnapshot(date=f"2026-03-{i:02d}", articles_found=5,
                              articles_included=2, fetch_ok=True)
                for i in range(1, 15)  # 14 days of flat data
            ],
        )
    }
    result = detect_trending_sources(stats, window=7)
    assert result == []


def test_detect_trending_rising_history() -> None:
    """Rising articles_found in recent window should be detected as trending."""
    previous = [
        DailySnapshot(date=f"2026-03-{i:02d}", articles_found=2,
                      articles_included=1, fetch_ok=True)
        for i in range(1, 8)  # days 1-7: 2 articles/day = 14 total
    ]
    recent = [
        DailySnapshot(date=f"2026-03-{i:02d}", articles_found=5,
                      articles_included=3, fetch_ok=True)
        for i in range(8, 15)  # days 8-14: 5 articles/day = 35 total (150% increase)
    ]
    stats = {
        "Rising": SourceStats(name="Rising", history=previous + recent)
    }
    result = detect_trending_sources(stats, window=7)
    assert "Rising" in result


def test_detect_trending_insufficient_history() -> None:
    """Sources with too few snapshots should be ignored."""
    stats = {
        "New": SourceStats(
            name="New",
            history=[
                DailySnapshot(date="2026-03-01", articles_found=10,
                              articles_included=5, fetch_ok=True)
            ],
        )
    }
    result = detect_trending_sources(stats, window=7)
    assert result == []


# --- calculate_effective_priorities ---


def _make_source(name: str, priority: int = 3) -> SourceConfig:
    return SourceConfig(name=name, url="https://x.com", category="Tech",
                        enabled=True, priority=priority)


def _default_adaptive() -> AdaptiveConfig:
    return AdaptiveConfig(enabled=True, feedback_weight=0.3, score_weight=0.5,
                          base_weight=0.2, trial_slots=2, min_priority=1,
                          max_priority=5)


def test_effective_priorities_high_score_bad_feedback() -> None:
    """High quality score + bad feedback -> moderate priority."""
    today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    sources = [_make_source("A", priority=3)]
    stats = {
        "A": SourceStats(
            name="A", total_fetches=10, successful_fetches=10,
            total_articles_found=50, articles_included_in_digest=50,
            avg_description_length=200.0, last_seen=today,
        )
    }
    feedback = {"A": 0.1}  # bad feedback
    result = calculate_effective_priorities(sources, stats, feedback, _default_adaptive())
    # score ~1.0, feedback 0.1, base 3/5=0.6
    # weighted = 0.6*0.2 + 1.0*0.5 + 0.1*0.3 = 0.12 + 0.5 + 0.03 = 0.65
    # priority = round(1 + 0.65*4) = round(3.6) = 4
    assert 2 <= result["A"] <= 4


def test_effective_priorities_low_score_good_feedback() -> None:
    """Low quality score + good feedback -> moderate priority."""
    sources = [_make_source("B", priority=3)]
    stats = {
        "B": SourceStats(
            name="B", total_fetches=10, successful_fetches=2,
            total_articles_found=10, articles_included_in_digest=1,
            avg_description_length=20.0, last_seen=None,
        )
    }
    feedback = {"B": 0.9}  # good feedback
    result = calculate_effective_priorities(sources, stats, feedback, _default_adaptive())
    assert 2 <= result["B"] <= 4


def test_effective_priorities_trending_bonus() -> None:
    """Trending source should get +1 bonus."""
    sources = [_make_source("T", priority=1)]
    previous = [
        DailySnapshot(date=f"2026-03-{i:02d}", articles_found=2,
                      articles_included=1, fetch_ok=True)
        for i in range(1, 8)
    ]
    recent = [
        DailySnapshot(date=f"2026-03-{i:02d}", articles_found=10,
                      articles_included=5, fetch_ok=True)
        for i in range(8, 15)
    ]
    stats = {
        "T": SourceStats(name="T", total_fetches=14, successful_fetches=14,
                         total_articles_found=84, articles_included_in_digest=42,
                         avg_description_length=150.0, last_seen="2026-03-14",
                         history=previous + recent)
    }
    adaptive = _default_adaptive()

    # Calculate without trending (use flat history)
    flat_stats = {
        "T": SourceStats(name="T", total_fetches=14, successful_fetches=14,
                         total_articles_found=84, articles_included_in_digest=42,
                         avg_description_length=150.0, last_seen="2026-03-14",
                         history=previous + previous)  # flat = no trend
    }
    no_trend = calculate_effective_priorities(sources, flat_stats, {}, adaptive)
    with_trend = calculate_effective_priorities(sources, stats, {}, adaptive)

    # With trend should be >= without trend (bonus applied)
    assert with_trend["T"] >= no_trend["T"]


def test_effective_priorities_no_stats_uses_neutral() -> None:
    """Source with no stats should get neutral score (0.5)."""
    sources = [_make_source("New", priority=3)]
    result = calculate_effective_priorities(sources, {}, {}, _default_adaptive())
    # base=0.6*0.2=0.12, score=0.5*0.5=0.25, feedback=0.5*0.3=0.15 => 0.52
    # priority = round(1 + 0.52*4) = round(3.08) = 3
    assert result["New"] == 3


# --- evaluate_trial_sources ---


def _make_trial_source(
    name: str, trial_started: str, trial_days: int = 7
) -> SourceConfig:
    return SourceConfig(
        name=name, url="https://x.com", category="Tech",
        enabled=True, priority=3, trial=True,
        trial_started=trial_started, trial_days=trial_days,
    )


def test_evaluate_trial_not_expired() -> None:
    """Trial source not yet past trial_days should not be promoted or demoted."""
    sources = [_make_trial_source("New", trial_started="2026-03-15", trial_days=7)]
    today = "2026-03-18"  # only 3 days elapsed
    stats = {
        "New": SourceStats(
            name="New", total_fetches=3, successful_fetches=3,
            total_articles_found=10, articles_included_in_digest=10,
            avg_description_length=200.0, last_seen="2026-03-18",
        )
    }
    promote, demote, needs_start = evaluate_trial_sources(sources, stats, today)
    assert promote == []
    assert demote == []
    assert needs_start == []


def test_evaluate_trial_promote_high_score() -> None:
    """Expired trial with high score (>0.6) should be promoted."""
    sources = [_make_trial_source("Good", trial_started="2026-03-01", trial_days=7)]
    today = "2026-03-18"
    stats = {
        "Good": SourceStats(
            name="Good", total_fetches=10, successful_fetches=10,
            total_articles_found=50, articles_included_in_digest=50,
            avg_description_length=200.0, last_seen=today,
        )
    }
    promote, demote, needs_start = evaluate_trial_sources(sources, stats, today)
    assert "Good" in promote
    assert demote == []
    assert needs_start == []


def test_evaluate_trial_demote_low_score() -> None:
    """Expired trial with low score (<0.3) should be demoted."""
    sources = [_make_trial_source("Bad", trial_started="2026-03-01", trial_days=7)]
    today = "2026-03-18"
    stats = {
        "Bad": SourceStats(
            name="Bad", total_fetches=10, successful_fetches=1,
            total_articles_found=5, articles_included_in_digest=0,
            avg_description_length=10.0, last_seen=None,
        )
    }
    promote, demote, needs_start = evaluate_trial_sources(sources, stats, today)
    assert promote == []
    assert "Bad" in demote
    assert needs_start == []


def test_evaluate_trial_middling_score_no_action() -> None:
    """Expired trial with score between 0.3 and 0.6 stays in trial."""
    sources = [_make_trial_source("Mid", trial_started="2026-03-01", trial_days=7)]
    today = "2026-03-18"
    stats = {
        "Mid": SourceStats(
            name="Mid", total_fetches=10, successful_fetches=5,
            total_articles_found=20, articles_included_in_digest=5,
            avg_description_length=80.0, last_seen=today,
        )
    }
    promote, demote, needs_start = evaluate_trial_sources(sources, stats, today)
    assert promote == []
    assert demote == []
    assert needs_start == []


def test_evaluate_trial_non_trial_ignored() -> None:
    """Non-trial sources should not be evaluated."""
    sources = [_make_source("Regular", priority=3)]
    promote, demote, needs_start = evaluate_trial_sources(sources, {}, "2026-03-18")
    assert promote == []
    assert demote == []
    assert needs_start == []


# --- apply_trial_decisions ---


def test_apply_trial_decisions_promote(tmp_path: Path) -> None:
    """Promoted source should have trial set to false."""
    config_data = {
        "llm": {"provider": "anthropic", "model": "test"},
        "delivery": {"telegram": False, "markdown_to_repo": False},
        "digest": {"language": "ru"},
        "sources": [
            {"name": "GoodFeed", "url": "https://x.com", "category": "Tech",
             "enabled": True, "trial": True, "trial_started": "2026-03-01"},
            {"name": "OtherFeed", "url": "https://y.com", "category": "Tech",
             "enabled": True, "trial": False},
        ],
    }
    config_path = tmp_path / "config.yaml"
    with config_path.open("w") as f:
        yaml.dump(config_data, f)

    apply_trial_decisions(str(config_path), promote=["GoodFeed"], demote=[])

    with config_path.open("r") as f:
        result = yaml.safe_load(f)

    good = next(s for s in result["sources"] if s["name"] == "GoodFeed")
    assert good["trial"] is False
    # Other source should be untouched
    other = next(s for s in result["sources"] if s["name"] == "OtherFeed")
    assert other["enabled"] is True


def test_apply_trial_decisions_demote(tmp_path: Path) -> None:
    """Demoted source should have enabled set to false."""
    config_data = {
        "llm": {"provider": "anthropic", "model": "test"},
        "delivery": {"telegram": False, "markdown_to_repo": False},
        "digest": {"language": "ru"},
        "sources": [
            {"name": "BadFeed", "url": "https://x.com", "category": "Tech",
             "enabled": True, "trial": True, "trial_started": "2026-03-01"},
        ],
    }
    config_path = tmp_path / "config.yaml"
    with config_path.open("w") as f:
        yaml.dump(config_data, f)

    apply_trial_decisions(str(config_path), promote=[], demote=["BadFeed"])

    with config_path.open("r") as f:
        result = yaml.safe_load(f)

    bad = next(s for s in result["sources"] if s["name"] == "BadFeed")
    assert bad["enabled"] is False


def test_apply_trial_decisions_noop(tmp_path: Path) -> None:
    """Empty promote/demote lists should not modify the file."""
    config_data = {
        "sources": [
            {"name": "Feed", "url": "https://x.com", "category": "Tech",
             "enabled": True, "trial": True},
        ],
    }
    config_path = tmp_path / "config.yaml"
    with config_path.open("w") as f:
        yaml.dump(config_data, f)

    original = config_path.read_text()
    apply_trial_decisions(str(config_path), promote=[], demote=[])
    assert config_path.read_text() == original


def test_evaluate_trial_needs_start_for_none_trial_started() -> None:
    """Trial source with trial_started=None should appear in needs_start."""
    sources = [
        SourceConfig(
            name="NewTrial", url="https://x.com", category="Tech",
            enabled=True, priority=3, trial=True, trial_started=None,
        )
    ]
    promote, demote, needs_start = evaluate_trial_sources(sources, {}, "2026-03-18")
    assert promote == []
    assert demote == []
    assert "NewTrial" in needs_start


def test_apply_trial_decisions_initializes_trial_started(tmp_path: Path) -> None:
    """needs_start sources should get trial_started set in config."""
    config_data = {
        "llm": {"provider": "anthropic", "model": "test"},
        "delivery": {"telegram": False, "markdown_to_repo": False},
        "digest": {"language": "ru"},
        "sources": [
            {"name": "NewTrial", "url": "https://x.com", "category": "Tech",
             "enabled": True, "trial": True},
        ],
    }
    config_path = tmp_path / "config.yaml"
    with config_path.open("w") as f:
        yaml.dump(config_data, f)

    apply_trial_decisions(str(config_path), promote=[], demote=[], needs_start=["NewTrial"])

    with config_path.open("r") as f:
        result = yaml.safe_load(f)

    source = result["sources"][0]
    assert source["trial_started"] is not None


def test_apply_trial_decisions_clears_trial_started_on_promote(tmp_path: Path) -> None:
    """Promoted source should have trial_started removed."""
    config_data = {
        "llm": {"provider": "anthropic", "model": "test"},
        "delivery": {"telegram": False, "markdown_to_repo": False},
        "digest": {"language": "ru"},
        "sources": [
            {"name": "GoodFeed", "url": "https://x.com", "category": "Tech",
             "enabled": True, "trial": True, "trial_started": "2026-03-01"},
        ],
    }
    config_path = tmp_path / "config.yaml"
    with config_path.open("w") as f:
        yaml.dump(config_data, f)

    apply_trial_decisions(str(config_path), promote=["GoodFeed"], demote=[])

    with config_path.open("r") as f:
        result = yaml.safe_load(f)

    source = result["sources"][0]
    assert source["trial"] is False
    assert "trial_started" not in source


def test_update_stats_deduplicates_same_day() -> None:
    """Calling update_stats twice on the same day should update the snapshot, not append."""
    stats: dict[str, SourceStats] = {}
    update_stats(stats, "Feed", fetch_ok=True, articles_found=5,
                 articles_included=0, avg_desc_len=100.0)
    assert len(stats["Feed"].history) == 1

    update_stats(stats, "Feed", fetch_ok=True, articles_found=8,
                 articles_included=3, avg_desc_len=120.0)
    # Should still be 1 snapshot (updated in place), not 2
    assert len(stats["Feed"].history) == 1
    assert stats["Feed"].history[0].articles_found == 8
    assert stats["Feed"].history[0].articles_included == 3
