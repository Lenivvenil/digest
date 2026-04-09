"""Tests for src.main — CLI flags and pipeline orchestration."""

from __future__ import annotations

from dataclasses import dataclass, field
from unittest.mock import AsyncMock, patch

import pytest

from src.main import _clean_summary, check_config, main, run


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@dataclass
class _ProviderCfg:
    name: str = "gemini"
    model: str = "gemini-2.0-flash"
    role: list[str] = field(default_factory=lambda: ["summarize", "fallback"])


@dataclass
class _LLMCfg:
    providers: list[_ProviderCfg] = field(default_factory=lambda: [_ProviderCfg()])


@dataclass
class _RadarCfg:
    language: str = "ru"
    max_articles_per_category: int = 5
    summary_style: str = "analytical"
    perspectives: bool = False


@dataclass
class _IrritatorCfg:
    max_narratives: int = 5
    queries_per_narrative: int = 3
    top_signals: int = 3
    min_signal_score: int = 7
    sources: list[str] = field(default_factory=lambda: ["hackernews"])
    reddit_subreddits: list[str] = field(default_factory=list)


@dataclass
class _FiltersCfg:
    blocklist_keywords: list[str] = field(default_factory=list)


@dataclass
class _TelegramCfg:
    enabled: bool = False
    split_messages: bool = True


@dataclass
class _ObsidianCfg:
    enabled: bool = False
    output_dir: str = "digests"


@dataclass
class _SourceCfg:
    name: str = "test"
    url: str = "https://example.com/feed"
    category: str = "tech"
    enabled: bool = True
    priority: int = 3
    recency_hours: int = 24


@dataclass
class _Config:
    llm: _LLMCfg = field(default_factory=_LLMCfg)
    radar: _RadarCfg = field(default_factory=_RadarCfg)
    irritator: _IrritatorCfg = field(default_factory=_IrritatorCfg)
    sources: list[_SourceCfg] = field(default_factory=lambda: [_SourceCfg()])
    filters: _FiltersCfg = field(default_factory=_FiltersCfg)
    telegram: _TelegramCfg = field(default_factory=_TelegramCfg)
    obsidian: _ObsidianCfg = field(default_factory=_ObsidianCfg)

    @property
    def enabled_sources(self) -> list[_SourceCfg]:
        return [s for s in self.sources if s.enabled]


@dataclass
class _CategorySummary:
    category: str = "tech"
    summary_text: str = "## Tech\n\nSummary of tech news."


def _mock_config() -> _Config:
    return _Config()


# ---------------------------------------------------------------------------
# _clean_summary
# ---------------------------------------------------------------------------

class TestCleanSummary:
    def test_removes_link_lines(self) -> None:
        text = "Content here\nLink: https://example.com\nMore content"
        result = _clean_summary(text)
        assert "Link:" not in result
        assert "Content here" in result
        assert "More content" in result

    def test_removes_source_lines(self) -> None:
        text = "News\nSource: https://x.com/article\nEnd"
        assert "Source:" not in _clean_summary(text)

    def test_removes_russian_lines(self) -> None:
        text = "Новости\nСсылка: https://example.com\nКонец"
        assert "Ссылка:" not in _clean_summary(text)

    def test_preserves_normal_text(self) -> None:
        text = "Normal text without any links"
        assert _clean_summary(text) == text

    def test_strips_whitespace(self) -> None:
        assert _clean_summary("  hello  ") == "hello"


# ---------------------------------------------------------------------------
# check_config
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestCheckConfig:
    async def test_valid_config(self, tmp_path: pytest.TempPathFactory) -> None:
        with patch("src.config.load_config", return_value=_mock_config()):
            result = await check_config("config.yaml")
        assert result == 0

    async def test_invalid_config(self) -> None:
        with patch("src.config.load_config", side_effect=FileNotFoundError("nope")):
            result = await check_config("missing.yaml")
        assert result == 1

    async def test_warns_missing_env_vars(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = _mock_config()
        cfg.telegram.enabled = True
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
        with patch("src.config.load_config", return_value=cfg):
            result = await check_config("config.yaml")
        # Warns but does not fail
        assert result == 0


# ---------------------------------------------------------------------------
# run — radar only
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestRunRadarOnly:
    async def test_radar_only_prints_summary(self, capsys: pytest.CaptureFixture[str]) -> None:
        summary = _CategorySummary()
        mock_collect = AsyncMock(return_value=({"tech": ["a1"]}, {}))
        mock_summarize = AsyncMock(return_value=([summary], ""))
        mock_save = AsyncMock()

        with (
            patch("src.config.load_config", return_value=_mock_config()),
            patch("src.radar.collect", mock_collect),
            patch("src.radar.summarize_all", mock_summarize),
            patch("src.radar.save_dedup_cache", mock_save),
        ):
            result = await run("config.yaml", dry_run=False, radar_only=True, verbose=False)

        assert result == 0
        captured = capsys.readouterr()
        assert "Tech" in captured.out

    async def test_no_articles_returns_zero(self) -> None:
        mock_collect = AsyncMock(return_value=({}, {}))

        with (
            patch("src.config.load_config", return_value=_mock_config()),
            patch("src.radar.collect", mock_collect),
        ):
            result = await run("config.yaml", dry_run=False, radar_only=True, verbose=False)

        assert result == 0

    async def test_failed_summarization_returns_one(self) -> None:
        mock_collect = AsyncMock(return_value=({"tech": ["a1"]}, {}))
        mock_summarize = AsyncMock(return_value=([], ""))

        with (
            patch("src.config.load_config", return_value=_mock_config()),
            patch("src.radar.collect", mock_collect),
            patch("src.radar.summarize_all", mock_summarize),
        ):
            result = await run("config.yaml", dry_run=False, radar_only=True, verbose=False)

        assert result == 1


# ---------------------------------------------------------------------------
# run — dry run (full pipeline)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestRunDryRun:
    async def test_dry_run_does_not_save_cache(self) -> None:
        summary = _CategorySummary()
        mock_collect = AsyncMock(return_value=({"tech": ["a1"]}, {}))
        mock_summarize = AsyncMock(return_value=([summary], ""))
        mock_save = AsyncMock()
        mock_extract = AsyncMock(return_value=[])

        with (
            patch("src.config.load_config", return_value=_mock_config()),
            patch("src.radar.collect", mock_collect),
            patch("src.radar.summarize_all", mock_summarize),
            patch("src.radar.save_dedup_cache", mock_save),
            patch("src.irritator.extract_narratives", mock_extract),
        ):
            result = await run("config.yaml", dry_run=True, radar_only=False, verbose=False)

        assert result == 0
        mock_save.assert_not_called()

    async def test_dry_run_prints_combined(self, capsys: pytest.CaptureFixture[str]) -> None:
        summary = _CategorySummary(summary_text="Test output")
        mock_collect = AsyncMock(return_value=({"tech": ["a1"]}, {}))
        mock_summarize = AsyncMock(return_value=([summary], ""))
        mock_extract = AsyncMock(return_value=[])

        with (
            patch("src.config.load_config", return_value=_mock_config()),
            patch("src.radar.collect", mock_collect),
            patch("src.radar.summarize_all", mock_summarize),
            patch("src.radar.save_dedup_cache", AsyncMock()),
            patch("src.irritator.extract_narratives", mock_extract),
        ):
            await run("config.yaml", dry_run=True, radar_only=False, verbose=False)

        captured = capsys.readouterr()
        assert "Test output" in captured.out


# ---------------------------------------------------------------------------
# run — full pipeline with delivery
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestRunFullPipeline:
    async def test_delivery_called_when_not_dry_run(self) -> None:
        summary = _CategorySummary()
        mock_collect = AsyncMock(return_value=({"tech": ["a1"]}, {}))
        mock_summarize = AsyncMock(return_value=([summary], ""))
        mock_extract = AsyncMock(return_value=[])
        mock_write = AsyncMock(return_value=None)
        mock_send_radar = AsyncMock(return_value=True)

        cfg = _mock_config()
        cfg.telegram.enabled = True

        with (
            patch("src.config.load_config", return_value=cfg),
            patch("src.radar.collect", mock_collect),
            patch("src.radar.summarize_all", mock_summarize),
            patch("src.radar.save_dedup_cache", AsyncMock()),
            patch("src.irritator.extract_narratives", mock_extract),
            patch("src.delivery.write_digest", mock_write),
            patch("src.delivery.send_radar", mock_send_radar),
        ):
            result = await run("config.yaml", dry_run=False, radar_only=False, verbose=False)

        assert result == 0
        mock_write.assert_called_once()
        mock_send_radar.assert_called_once()

    async def test_irritator_failure_does_not_crash(self) -> None:
        summary = _CategorySummary()
        mock_collect = AsyncMock(return_value=({"tech": ["a1"]}, {}))
        mock_summarize = AsyncMock(return_value=([summary], ""))
        mock_extract = AsyncMock(side_effect=RuntimeError("LLM exploded"))
        mock_write = AsyncMock(return_value=None)

        with (
            patch("src.config.load_config", return_value=_mock_config()),
            patch("src.radar.collect", mock_collect),
            patch("src.radar.summarize_all", mock_summarize),
            patch("src.radar.save_dedup_cache", AsyncMock()),
            patch("src.irritator.extract_narratives", mock_extract),
            patch("src.delivery.write_digest", mock_write),
        ):
            result = await run("config.yaml", dry_run=False, radar_only=False, verbose=False)

        # Pipeline continues despite irritator failure
        assert result == 0


# ---------------------------------------------------------------------------
# main (CLI entrypoint)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestMain:
    async def test_check_flag(self) -> None:
        with patch("src.main.check_config", new_callable=AsyncMock, return_value=0) as mock_check:
            result = await main(["--check"])
        assert result == 0
        mock_check.assert_called_once_with("config.yaml")

    async def test_default_flags(self) -> None:
        with patch("src.main.run", new_callable=AsyncMock, return_value=0) as mock_run:
            result = await main([])
        assert result == 0
        mock_run.assert_called_once_with("config.yaml", False, False, False)

    async def test_all_flags(self) -> None:
        with patch("src.main.run", new_callable=AsyncMock, return_value=0) as mock_run:
            result = await main(["--dry-run", "--radar-only", "--verbose", "--config", "alt.yaml"])
        assert result == 0
        mock_run.assert_called_once_with("alt.yaml", True, True, True)
