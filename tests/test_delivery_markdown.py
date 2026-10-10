"""Tests for src.delivery.markdown."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from digest.delivery.markdown import (
    _build_counter_signals_section,
    _build_top_articles_section,
    write_digest,
)
from tests.factories import make_ranked_signal

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
        url=url,
        title=title,
        score=score,
        reasoning=reasoning,
        narrative_claim=narrative_claim,
    )


class TestBuildCounterSignalsSection:
    def test_empty_signals(self) -> None:
        assert _build_counter_signals_section([]) == ""


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
        nested = tmp_path / "a" / "b" / "c"
        config = _make_config(output_dir=str(nested))
        dt = datetime(2026, 4, 9, tzinfo=timezone.utc)
        result = write_digest(
            "# Digest\n\nContent here",
            config,
            date=dt,
            sources_count=5,
            articles_count=42,
        )
        assert result is not None
        assert result.exists()
        assert result.name == "2026-04-09.md"
        assert nested.exists()
        text = result.read_text(encoding="utf-8")
        assert "---" in text
        assert "title: Daily Digest 2026-04-09" in text
        assert "date: 2026-04-09" in text
        assert "sources_count: 5" in text
        assert "articles_count: 42" in text
        assert "tags: [digest, daily]" in text
        assert "Content here" in text

    def test_disabled_returns_none(self) -> None:
        config = _make_config(enabled=False)
        result = write_digest("text", config)
        assert result is None

    def test_with_counter_signals(self, tmp_path: Path) -> None:
        from digest.irritator import IrritatorStatus

        config = _make_config(output_dir=str(tmp_path))
        signals = [
            _make_ranked_signal(score=9),
            _make_ranked_signal(title="Signal B", score=7),
        ]
        result = write_digest(
            "Summary",
            config,
            ranked_signals=signals,
            irritator_status=IrritatorStatus("One source unavailable; valid counter-evidence retained", "incomplete"),
            date=datetime(2026, 4, 9, tzinfo=timezone.utc),
        )
        assert result is not None
        text = result.read_text(encoding="utf-8")
        assert "sources_count: 0" in text
        assert "articles_count: 0" in text
        assert "Counter-Signals" in text
        assert "Counter evidence" in text
        assert "Signal B" in text
        assert "https://example.com/a" in text
        assert "9/10" in text and "7/10" in text
        assert signals[0].narrative_claim in text
        assert signals[0].reasoning in text
        assert "Irritator status: incomplete" in text
        assert "One source unavailable; valid counter-evidence retained" in text

    def test_default_date_is_utc_now(self, tmp_path: Path) -> None:
        config = _make_config(output_dir=str(tmp_path))
        result = write_digest("text", config)
        assert result is not None
        assert result.exists()

    def test_invalid_dir_returns_none(self) -> None:
        config = _make_config(output_dir="/dev/null/impossible/path")
        result = write_digest("text", config, date=datetime(2026, 1, 1, tzinfo=timezone.utc))
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


class TestWriteDigestWithTopArticles:
    def test_with_top_articles_adds_section(self, tmp_path: Path) -> None:
        config = _make_config(output_dir=str(tmp_path))
        articles = [
            _make_article_summary(title="Key Story", source="HN", summary="Why architects care."),
            _make_article_summary(
                title="![badge](https://example.invalid/pixel)", source="Reddit", summary="**Useful** prose.",
            ),
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
        assert r"[\!\[badge\]\(https\:\/\/example\.invalid\/pixel\)](https://example.com/ai)" in text
        assert "**Useful** prose." in text
        assert articles[1].title == "![badge](https://example.invalid/pixel)"
        assert "https://example.com/ai" in text
        assert "HN" in text
        assert "Reddit" in text
        assert "AI & LLM" in text
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


def test_same_day_retries_preserve_previous_digests(tmp_path: Path) -> None:
    config = _make_config(output_dir=str(tmp_path))
    dt = datetime(2026, 4, 9, 10, tzinfo=timezone.utc)
    paths = [write_digest(content, config, date=dt) for content in ["First", "Retry", "Evening"]]
    assert [p.name for p in paths if p] == ["2026-04-09.md", "2026-04-09-2.md", "2026-04-09-3.md"]
    for path, expected in zip(paths, ["First", "Retry", "Evening"], strict=True):
        assert path is not None
        assert path.read_text(encoding="utf-8").endswith(f"{expected}\n")


def test_reading_appendix_preserves_original_qualifications_and_safe_fences(tmp_path: Path) -> None:
    from digest.config import ReadingBriefConfig
    from digest.radar.collector import article_hash
    from scripts.review_fixture import fixture_config

    config = fixture_config()
    config.obsidian.enabled = True
    config.obsidian.output_dir = str(tmp_path)
    config.reading_brief = ReadingBriefConfig(True, "gemini", "gemini-3.8-flash")
    card = _make_article_summary(
        title="![badge](https://example.invalid/pixel)",
        summary="Substantive brief. Only pilot clients; source conflict is unresolved.",
    )
    original = (
        "[S1] Original claim.\n```\n[S2] QUALIFICATION: only pilot clients.\n[S3] Contradictory source statement."
    )
    quotations = {article_hash(card.title, card.link): original}
    path = write_digest("Global status.", config, top_articles=[card], source_quotations=quotations)
    assert path is not None
    saved = path.read_bytes()
    brief, appendix = path.read_text().split("## Original source evidence (archive only)")
    assert r"### [\!\[badge\]\(https\:\/\/example\.invalid\/pixel\)](https://example.com/ai)" in appendix
    assert card.summary in brief and "Original claim" not in brief
    assert original in appendix and "````text\n" + original + "\n````" in appendix
    assert quotations == {article_hash(card.title, card.link): original}
    config.reading_brief = ReadingBriefConfig()
    legacy = write_digest("Global status.", config, top_articles=[card], source_quotations=quotations)
    assert legacy is not None
    assert "Original source evidence" not in legacy.read_text() and card.summary in legacy.read_text()
    assert path.read_bytes() == saved and legacy != path
