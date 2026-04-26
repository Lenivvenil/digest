"""Tests for src.delivery.markdown."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from digest.delivery.markdown import (
    _build_counter_signals_section,
    _build_frontmatter,
    _build_top_articles_section,
    write_digest,
)
from tests.factories import make_ranked_signal

# ---------------------------------------------------------------------------
# _build_frontmatter
# ---------------------------------------------------------------------------

class TestBuildFrontmatter:
    def test_basic_frontmatter(self) -> None:
        result = _build_frontmatter("2026-04-09", 5, 42)
        assert "---" in result
        assert "title: Daily Digest 2026-04-09" in result
        assert "date: 2026-04-09" in result
        assert "sources_count: 5" in result
        assert "articles_count: 42" in result
        assert "tags: [digest, daily]" in result

    def test_zero_counts(self) -> None:
        result = _build_frontmatter("2026-01-01", 0, 0)
        assert "sources_count: 0" in result
        assert "articles_count: 0" in result


# ---------------------------------------------------------------------------
# _build_counter_signals_section
# ---------------------------------------------------------------------------

def _make_ranked_signal(
    title: str = "Counter evidence",
    url: str = "https://example.com/a",
    score: int = 8,
    reasoning: str = "Compelling argument",
    narrative_claim: str = "AI is perfect" * 20,
) -> Any:
    return make_ranked_signal(
        url=url, title=title, score=score,
        reasoning=reasoning, narrative_claim=narrative_claim,
    )


class TestBuildCounterSignalsSection:
    def test_empty_signals(self) -> None:
        assert _build_counter_signals_section([]) == ""

    def test_single_signal(self) -> None:
        result = _build_counter_signals_section([_make_ranked_signal()])
        assert "Counter-Signals" in result
        assert "[Counter evidence]" in result
        assert "https://example.com/a" in result
        assert "8/10" in result

    def test_multiple_signals(self) -> None:
        signals = [
            _make_ranked_signal(title="Signal A", score=9),
            _make_ranked_signal(title="Signal B", score=7),
        ]
        result = _build_counter_signals_section(signals)
        assert "Signal A" in result
        assert "Signal B" in result

    def test_narrative_claim_truncated(self) -> None:
        result = _build_counter_signals_section([_make_ranked_signal()])
        # narrative_claim is "AI is perfect" * 20 = 260 chars, truncated to 100
        lines = result.split("\n")
        narrative_lines = [line for line in lines if "Narrative:" in line]
        assert len(narrative_lines) == 1


# ---------------------------------------------------------------------------
# write_digest
# ---------------------------------------------------------------------------

def _make_config(enabled: bool = True, output_dir: str = "digests") -> Any:
    class ObsidianCfg:
        pass
    class Cfg:
        obsidian = ObsidianCfg()
    Cfg.obsidian.enabled = enabled  # type: ignore[attr-defined]
    Cfg.obsidian.output_dir = output_dir  # type: ignore[attr-defined]
    return Cfg()


class TestWriteDigest:
    def test_writes_file(self, tmp_path: Path) -> None:
        config = _make_config(output_dir=str(tmp_path / "out"))
        dt = datetime(2026, 4, 9, tzinfo=timezone.utc)
        result = write_digest(
            "# Digest\n\nContent here",
            config,
            date=dt,
            sources_count=3,
            articles_count=10,
        )
        assert result is not None
        assert result.exists()
        assert result.name == "2026-04-09.md"
        text = result.read_text(encoding="utf-8")
        assert "Daily Digest 2026-04-09" in text
        assert "Content here" in text

    def test_disabled_returns_none(self) -> None:
        config = _make_config(enabled=False)
        result = write_digest("text", config)
        assert result is None

    def test_creates_output_dir(self, tmp_path: Path) -> None:
        nested = tmp_path / "a" / "b" / "c"
        config = _make_config(output_dir=str(nested))
        result = write_digest("text", config, date=datetime(2026, 1, 1, tzinfo=timezone.utc))
        assert result is not None
        assert nested.exists()

    def test_with_counter_signals(self, tmp_path: Path) -> None:
        config = _make_config(output_dir=str(tmp_path))
        signals = [_make_ranked_signal()]
        result = write_digest(
            "Summary",
            config,
            ranked_signals=signals,
            date=datetime(2026, 4, 9, tzinfo=timezone.utc),
        )
        assert result is not None
        text = result.read_text(encoding="utf-8")
        assert "Counter-Signals" in text
        assert "Counter evidence" in text

    def test_default_date_is_utc_now(self, tmp_path: Path) -> None:
        config = _make_config(output_dir=str(tmp_path))
        result = write_digest("text", config)
        assert result is not None
        assert result.exists()

    def test_invalid_dir_returns_none(self) -> None:
        config = _make_config(output_dir="/dev/null/impossible/path")
        result = write_digest(
            "text", config, date=datetime(2026, 1, 1, tzinfo=timezone.utc)
        )
        assert result is None


# ---------------------------------------------------------------------------
# _build_top_articles_section
# ---------------------------------------------------------------------------

def _make_article_summary(
    title: str = "Big AI News",
    link: str = "https://example.com/ai",
    source: str = "TechCrunch",
    category: str = "AI & LLM",
    summary: str = "This is a critical development for architects.",
) -> Any:
    from digest.radar.summarizer import ArticleSummary
    return ArticleSummary(title=title, link=link, source=source, category=category, summary=summary)


class TestBuildTopArticlesSection:
    def test_empty_returns_empty_string(self) -> None:
        assert _build_top_articles_section([]) == ""

    def test_single_article(self) -> None:
        result = _build_top_articles_section([_make_article_summary()])
        assert "## Top Articles" in result
        assert "Big AI News" in result
        assert "https://example.com/ai" in result
        assert "TechCrunch" in result
        assert "AI & LLM" in result
        assert "critical development" in result

    def test_multiple_articles(self) -> None:
        articles = [
            _make_article_summary(title="First", source="HN"),
            _make_article_summary(title="Second", source="Reddit"),
        ]
        result = _build_top_articles_section(articles)
        assert "First" in result
        assert "Second" in result
        assert "HN" in result
        assert "Reddit" in result


class TestWriteDigestWithTopArticles:
    def test_with_top_articles_adds_section(self, tmp_path: Path) -> None:
        config = _make_config(output_dir=str(tmp_path))
        articles = [
            _make_article_summary(title="Key Story", summary="Why architects care."),
        ]
        result = write_digest(
            "# Overview",
            config,
            top_articles=articles,
            date=datetime(2026, 4, 26, tzinfo=timezone.utc),
        )
        assert result is not None
        text = result.read_text(encoding="utf-8")
        assert "## Top Articles" in text
        assert "Key Story" in text
        assert "Why architects care." in text

    def test_without_top_articles_no_section(self, tmp_path: Path) -> None:
        config = _make_config(output_dir=str(tmp_path))
        result = write_digest(
            "# Overview",
            config,
            date=datetime(2026, 4, 26, tzinfo=timezone.utc),
        )
        assert result is not None
        text = result.read_text(encoding="utf-8")
        assert "Top Articles" not in text

    def test_empty_top_articles_no_section(self, tmp_path: Path) -> None:
        config = _make_config(output_dir=str(tmp_path))
        result = write_digest(
            "# Overview",
            config,
            top_articles=[],
            date=datetime(2026, 4, 26, tzinfo=timezone.utc),
        )
        assert result is not None
        text = result.read_text(encoding="utf-8")
        assert "Top Articles" not in text
