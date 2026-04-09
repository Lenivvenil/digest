"""Tests for src.delivery.markdown."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from src.delivery.markdown import (
    _build_counter_signals_section,
    _build_frontmatter,
    write_digest,
)


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
) -> object:
    class SignalObj:
        pass
    class RankedObj:
        pass
    s = SignalObj()
    s.title = title
    s.url = url
    r = RankedObj()
    r.signal = s
    r.score = score
    r.reasoning = reasoning
    r.narrative_claim = narrative_claim
    return r


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

def _make_config(enabled: bool = True, output_dir: str = "digests") -> object:
    class ObsidianCfg:
        pass
    class Cfg:
        obsidian = ObsidianCfg()
    Cfg.obsidian.enabled = enabled
    Cfg.obsidian.output_dir = output_dir
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
