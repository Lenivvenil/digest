"""Tests for src.main — CLI flags and pipeline orchestration."""

from __future__ import annotations

from dataclasses import dataclass, field
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from digest.irritator import IrritatorStatus
from digest.main import RunStats, _clean_summary, check_config, main, run


@dataclass
class _Article:
    title: str = "Test Article"
    link: str = "https://example.com/1"
    description: str = "Description"
    source: str = "test"

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
    routing: list[object] = field(default_factory=list)


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
    max_messages: int = 10


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
    trial: bool = False
    trial_started: str | None = None
    trial_days: int = 7


@dataclass
class _AdaptiveCfg:
    enabled: bool = False
    feedback_weight: float = 0.3
    score_weight: float = 0.5
    base_weight: float = 0.2
    trial_slots: int = 2
    min_priority: int = 1
    max_priority: int = 5


@dataclass
class _Config:
    llm: _LLMCfg = field(default_factory=_LLMCfg)
    radar: _RadarCfg = field(default_factory=_RadarCfg)
    irritator: _IrritatorCfg = field(default_factory=_IrritatorCfg)
    sources: list[_SourceCfg] = field(default_factory=lambda: [_SourceCfg()])
    filters: _FiltersCfg = field(default_factory=_FiltersCfg)
    telegram: _TelegramCfg = field(default_factory=_TelegramCfg)
    obsidian: _ObsidianCfg = field(default_factory=_ObsidianCfg)
    adaptive: _AdaptiveCfg = field(default_factory=_AdaptiveCfg)

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

    def test_removes_greeting_ru(self) -> None:
        text = "Добрый день! Вот ваш дайджест\n\n## Tech\nNews here"
        result = _clean_summary(text)
        assert "Добрый день" not in result
        assert "Tech" in result

    def test_removes_daily_digest_ru(self) -> None:
        text = "Ежедневный дайджест новостей для Technology Architect:\n\n## AI\nContent"
        result = _clean_summary(text)
        assert "Ежедневный дайджест" not in result
        assert "Content" in result

    def test_removes_daily_digest_en(self) -> None:
        text = "Daily digest of news:\n\nContent"
        result = _clean_summary(text)
        assert "Daily digest" not in result

    def test_removes_horizontal_rules(self) -> None:
        text = "## Tech\nNews\n\n---\n\n## AI\nMore news"
        result = _clean_summary(text)
        assert "---" not in result
        assert "Tech" in result
        assert "AI" in result

    def test_normalizes_category_header(self) -> None:
        text = 'Категория «Финансы» содержит 5 статей'
        result = _clean_summary(text)
        assert result.startswith("## ")
        assert "Финансы" in result
        assert "содержит" not in result

    def test_collapses_blank_lines(self) -> None:
        text = "A\n\n\n\n\nB"
        result = _clean_summary(text)
        assert "\n\n\n" not in result
        assert "A\n\nB" == result


# ---------------------------------------------------------------------------
# check_config
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestCheckConfig:
    async def test_valid_config(self) -> None:
        cfg = _mock_config()
        # Patch feed probing to return OK for all sources
        mock_client = AsyncMock()
        mock_response = AsyncMock()
        mock_response.text = "<rss><channel><item><title>A</title></item></channel></rss>"
        mock_response.raise_for_status = lambda: None
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_response)

        with (
            patch("digest.config.load_config", return_value=cfg),
            patch("httpx.AsyncClient", return_value=mock_client),
            patch("digest._dns_pinning.validate_url", return_value=type("V", (), {
                "hostname": "example.com", "pinned_addrinfos": [],
                "url": "https://example.com/feed",
            })()),
            patch("digest._dns_pinning.pin_dns", MagicMock()),
        ):
            result = await check_config("config.yaml")
        assert result == 0

    async def test_invalid_config(self) -> None:
        with patch("digest.config.load_config", side_effect=FileNotFoundError("nope")):
            result = await check_config("missing.yaml")
        assert result == 1

    async def test_warns_missing_env_vars(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = _mock_config()
        cfg.telegram.enabled = True
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)

        mock_client = AsyncMock()
        mock_response = AsyncMock()
        mock_response.text = "<rss><channel></channel></rss>"
        mock_response.raise_for_status = lambda: None
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_response)

        with (
            patch("digest.config.load_config", return_value=cfg),
            patch("httpx.AsyncClient", return_value=mock_client),
            patch("digest._dns_pinning.validate_url", return_value=type("V", (), {
                "hostname": "example.com", "pinned_addrinfos": [],
                "url": "https://example.com/feed",
            })()),
            patch("digest._dns_pinning.pin_dns", MagicMock()),
        ):
            result = await check_config("config.yaml")
        assert result == 0


# ---------------------------------------------------------------------------
# run — radar only
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestRunRadarOnly:
    async def test_radar_only_prints_summary(self, capsys: pytest.CaptureFixture[str]) -> None:
        summary = _CategorySummary()
        mock_collect = AsyncMock(return_value=({"tech": [_Article()]}, {}))
        mock_summarize = AsyncMock(return_value=([summary], ""))
        mock_save = MagicMock()

        with (
            patch("digest.config.load_config", return_value=_mock_config()),
            patch("digest.radar.collect", mock_collect),
            patch("digest.radar.summarize_all", mock_summarize),
            patch("digest.radar.pick_top_articles", AsyncMock(return_value=[])),
            patch("digest.radar.save_dedup_cache", mock_save),
        ):
            result = await run("config.yaml", dry_run=False, radar_only=True, verbose=False)

        assert isinstance(result, RunStats)
        captured = capsys.readouterr()
        assert "Tech" in captured.out

    async def test_no_articles_returns_zero(self) -> None:
        mock_collect = AsyncMock(return_value=({}, {}))

        with (
            patch("digest.config.load_config", return_value=_mock_config()),
            patch("digest.radar.collect", mock_collect),
        ):
            result = await run("config.yaml", dry_run=False, radar_only=True, verbose=False)

        assert isinstance(result, RunStats)
        assert result.new_articles == 0

    async def test_failed_summarization_returns_empty(self) -> None:
        mock_collect = AsyncMock(return_value=({"tech": [_Article()]}, {}))
        mock_summarize = AsyncMock(return_value=([], ""))

        with (
            patch("digest.config.load_config", return_value=_mock_config()),
            patch("digest.radar.collect", mock_collect),
            patch("digest.radar.summarize_all", mock_summarize),
        ):
            result = await run("config.yaml", dry_run=False, radar_only=True, verbose=False)

        assert isinstance(result, RunStats)
        assert result.digest_length == 0


# ---------------------------------------------------------------------------
# run — dry run (full pipeline)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestRunDryRun:
    async def test_dry_run_does_not_save_cache(self) -> None:
        summary = _CategorySummary()
        mock_collect = AsyncMock(return_value=({"tech": [_Article()]}, {}))
        mock_summarize = AsyncMock(return_value=([summary], ""))
        mock_save = MagicMock()
        mock_extract = AsyncMock(return_value=[])

        with (
            patch("digest.config.load_config", return_value=_mock_config()),
            patch("digest.radar.collect", mock_collect),
            patch("digest.radar.summarize_all", mock_summarize),
            patch("digest.radar.pick_top_articles", AsyncMock(return_value=[])),
            patch("digest.radar.save_dedup_cache", mock_save),
            patch("digest.irritator.extract_narratives", mock_extract),
        ):
            result = await run("config.yaml", dry_run=True, radar_only=False, verbose=False)

        assert isinstance(result, RunStats)
        mock_save.assert_not_called()

    async def test_dry_run_prints_combined(self, capsys: pytest.CaptureFixture[str]) -> None:
        summary = _CategorySummary(summary_text="Test output")
        mock_collect = AsyncMock(return_value=({"tech": [_Article()]}, {}))
        mock_summarize = AsyncMock(return_value=([summary], ""))
        mock_extract = AsyncMock(return_value=[])

        with (
            patch("digest.config.load_config", return_value=_mock_config()),
            patch("digest.radar.collect", mock_collect),
            patch("digest.radar.summarize_all", mock_summarize),
            patch("digest.radar.pick_top_articles", AsyncMock(return_value=[])),
            patch("digest.radar.save_dedup_cache", MagicMock()),
            patch("digest.irritator.extract_narratives", mock_extract),
        ):
            await run("config.yaml", dry_run=True, radar_only=False, verbose=False)

        captured = capsys.readouterr()
        assert "Test output" in captured.out

    async def test_dry_run_prints_irritator_status(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Dry-run always prints irritator status — visible even when no signals survive."""
        summary = _CategorySummary(summary_text="News")
        mock_status = IrritatorStatus("2 narratives, 0 signals", "empty")

        with (
            patch("digest.config.load_config", return_value=_mock_config()),
            patch("digest.radar.collect", AsyncMock(return_value=({"tech": [_Article()]}, {}))),
            patch("digest.radar.summarize_all", AsyncMock(return_value=([summary], ""))),
            patch("digest.radar.pick_top_articles", AsyncMock(return_value=[])),
            patch("digest.radar.save_dedup_cache", MagicMock()),
            patch("digest.main._run_irritator", AsyncMock(return_value=([], [], mock_status))),
        ):
            await run("config.yaml", dry_run=True, radar_only=False, verbose=False)

        captured = capsys.readouterr()
        assert "2 narratives, 0 signals" in captured.out


# ---------------------------------------------------------------------------
# run — full pipeline with delivery
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestRunFullPipeline:
    async def test_delivery_called_when_not_dry_run(self) -> None:
        summary = _CategorySummary()
        mock_collect = AsyncMock(return_value=({"tech": [_Article()]}, {}))
        mock_summarize = AsyncMock(return_value=([summary], ""))
        mock_extract = AsyncMock(return_value=[])
        mock_write = AsyncMock(return_value=None)
        mock_send_cards = AsyncMock(return_value={"abc12345": "TechCrunch"})

        cfg = _mock_config()
        cfg.telegram.enabled = True

        with (
            patch("digest.config.load_config", return_value=cfg),
            patch("digest.radar.collect", mock_collect),
            patch("digest.radar.summarize_all", mock_summarize),
            patch("digest.radar.pick_top_articles", AsyncMock(return_value=[])),
            patch("digest.radar.save_dedup_cache", MagicMock()),
            patch("digest.irritator.extract_narratives", mock_extract),
            patch("digest.delivery.write_digest", mock_write),
            patch("digest.delivery.send_article_cards", mock_send_cards),
            patch("digest.delivery.send_counter_signals", AsyncMock(return_value=False)),
        ):
            result = await run("config.yaml", dry_run=False, radar_only=False, verbose=False)

        assert isinstance(result, RunStats)
        mock_write.assert_called_once()
        mock_send_cards.assert_called_once()

    async def test_irritator_failure_does_not_crash(self) -> None:
        summary = _CategorySummary()
        mock_collect = AsyncMock(return_value=({"tech": [_Article()]}, {}))
        mock_summarize = AsyncMock(return_value=([summary], ""))
        mock_extract = AsyncMock(side_effect=RuntimeError("LLM exploded"))
        mock_write = AsyncMock(return_value=None)

        with (
            patch("digest.config.load_config", return_value=_mock_config()),
            patch("digest.radar.collect", mock_collect),
            patch("digest.radar.summarize_all", mock_summarize),
            patch("digest.radar.pick_top_articles", AsyncMock(return_value=[])),
            patch("digest.radar.save_dedup_cache", MagicMock()),
            patch("digest.irritator.extract_narratives", mock_extract),
            patch("digest.delivery.write_digest", mock_write),
        ):
            result = await run("config.yaml", dry_run=False, radar_only=False, verbose=False)

        assert isinstance(result, RunStats)


# ---------------------------------------------------------------------------
# main (CLI entrypoint)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestMain:
    async def test_check_flag(self) -> None:
        with patch("digest.main.check_config", new_callable=AsyncMock, return_value=0) as mock_check:
            result = await main(["--check"])
        assert result == 0
        mock_check.assert_called_once_with("config.yaml")

    async def test_default_flags(self) -> None:
        mock_stats = RunStats(
            feeds_fetched=1, new_articles=0, digest_length=0,
            telegram_sent=False, telegram_partial=False,
            markdown_saved=False, markdown_path="",
        )
        with patch("digest.main.run", new_callable=AsyncMock, return_value=mock_stats) as mock_run:
            result = await main([])
        assert result == 0
        mock_run.assert_called_once_with("config.yaml", False, False, False)

    async def test_all_flags(self) -> None:
        mock_stats = RunStats(
            feeds_fetched=1, new_articles=0, digest_length=0,
            telegram_sent=False, telegram_partial=False,
            markdown_saved=False, markdown_path="",
        )
        with patch("digest.main.run", new_callable=AsyncMock, return_value=mock_stats) as mock_run:
            result = await main(["--dry-run", "--radar-only", "--verbose", "--config", "alt.yaml"])
        assert result == 0
        mock_run.assert_called_once_with("alt.yaml", True, True, True)
