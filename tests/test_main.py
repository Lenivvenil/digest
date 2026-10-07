"""Tests for src.main — CLI flags and pipeline orchestration."""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from digest.adapters.models.execution import ModelExecution
from digest.application.presentation import clean_summary as _clean_summary
from digest.delivery import ArticleDeliveryResult
from digest.feedback import FeedbackStore
from digest.irritator import IrritatorStatus
from digest.main import RunStats, check_config, main, run
from digest.radar.collector import SourceFetchMetrics, article_hash
from digest.radar.summarizer import ArticleSummary


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
    required: bool = False
    split_messages: bool = True
    max_messages: int = 10
    delivery_mode: str = "cards"
    bot_username: str = "digest_test_bot"


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

    def effective_sources(self, state) -> list[_SourceCfg]:
        return [s for s in self.enabled_sources if not state.is_demoted(s.name)]


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
            patch("digest.application.investigation.run_irritator", AsyncMock(return_value=([], [], mock_status))),
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
        mock_write = MagicMock(return_value=None)
        mock_send_cards = AsyncMock(return_value=ArticleDeliveryResult())

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
        mock_write = MagicMock(return_value=None)

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
        mock_run.assert_called_once_with("config.yaml", False, False, False, feedback_precollected=False)

    async def test_all_flags(self) -> None:
        mock_stats = RunStats(
            feeds_fetched=1, new_articles=0, digest_length=0,
            telegram_sent=False, telegram_partial=False,
            markdown_saved=False, markdown_path="",
        )
        with patch("digest.main.run", new_callable=AsyncMock, return_value=mock_stats) as mock_run:
            result = await main(["--dry-run", "--radar-only", "--verbose", "--config", "alt.yaml",
                                 "--feedback-precollected"])
        assert result == 0
        mock_run.assert_called_once_with("alt.yaml", True, True, True, feedback_precollected=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("sent", "failed", "markdown", "expected_titles"),
    [
        (0, 2, False, set()),
        (1, 1, False, {"One"}),
        (2, 0, False, {"One", "Two"}),
        (0, 2, True, {"One", "Two"}),
    ],
)
@pytest.mark.parametrize("required", [False, True])
async def test_delivery_commits_only_confirmed_articles(
    sent: int, failed: int, markdown: bool, expected_titles: set[str], required: bool,
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    articles = [_Article(title="One", link="https://example.com/1"),
                _Article(title="Two", link="https://example.com/2")]
    hashes = [article_hash(a.title, a.link) for a in articles]
    cache = {"existing": "old", **dict.fromkeys(hashes, "new")}
    top_articles = [ArticleSummary(a.title, a.link, a.source, "tech", "Summary") for a in articles]
    delivery = ArticleDeliveryResult(
        attempted=2, sent=sent, failed=failed,
        article_source_map={h[:8]: "test" for h in hashes[:sent]},
        delivered_hashes=set(hashes[:sent]),
    )
    save_cache = MagicMock()
    source_stats: dict = {}
    feedback = FeedbackStore(
        article_source_map={"previous": "Previous source"},
        last_digest_sources=["Previous source"], last_digest_time="Previous time",
    )
    cfg = _mock_config()
    cfg.telegram.enabled = True
    cfg.telegram.required = required
    if required and (sent == 0 or failed > 0):
        expected_titles = {a.title for a in articles[:sent]}

    async def collect_stub(*args: object, **kwargs: object) -> tuple:
        metrics = kwargs["fetch_metrics"]
        assert isinstance(metrics, dict)
        metrics["test"] = SourceFetchMetrics(True, 2, 30.0)
        metrics["broken"] = SourceFetchMetrics(False, 0, 0.0)
        return {"tech": articles}, cache

    async def send_stub(*args: object, **kwargs: object) -> ArticleDeliveryResult:
        save_cache.assert_not_called()  # No eager cache commit before Telegram responds.
        return delivery

    with ExitStack() as stack:
        replacements = {
            "digest.config.load_config": MagicMock(return_value=cfg),
            "digest.radar.collect": AsyncMock(side_effect=collect_stub),
            "digest.radar.summarize_all": AsyncMock(return_value=([_CategorySummary()], "")),
            "digest.radar.pick_top_articles": AsyncMock(return_value=top_articles),
            "digest.radar.save_dedup_cache": save_cache,
            "digest.application.investigation.run_irritator": AsyncMock(return_value=([],
                  [], IrritatorStatus("empty", "empty"))),
            "digest.delivery.write_digest": MagicMock(return_value=Path("digest.md") if markdown else None),
            "digest.delivery.send_article_cards": AsyncMock(side_effect=send_stub),
            "digest.delivery.send_counter_signals": AsyncMock(),
            "digest.application.run_state.process_pending_approvals": MagicMock(),
            "digest.feedback.load_feedback": MagicMock(return_value=feedback),
            "digest.source_scorer.load_stats": MagicMock(return_value=source_stats),
            "digest.source_scorer.save_stats": MagicMock(),
        }
        for target, replacement in replacements.items():
            stack.enter_context(patch(target, replacement))
        result = await run("config.yaml", False, False, False)
    expected_hashes = {article_hash(a.title, a.link) for a in articles if a.title in expected_titles}
    if expected_hashes:
        save_cache.assert_called_once_with({"existing": "old", **dict.fromkeys(expected_hashes, "new")})
    elif markdown:
        save_cache.assert_called_once_with({"existing": "old"})
    else:
        save_cache.assert_not_called()
    assert result.telegram_sent is (sent > 0 and failed == 0)
    assert result.telegram_partial is (sent > 0 and failed > 0)
    assert result.required_delivery_failed is (required and not result.telegram_sent)
    assert source_stats["test"].total_fetches == 1
    assert source_stats["test"].articles_included_in_digest == len(expected_hashes)
    assert source_stats["broken"].successful_fetches == 0
    assert source_stats["broken"].total_fetches == 1
    assert feedback.article_source_map == {"previous": "Previous source", **delivery.article_source_map}
    assert feedback.last_digest_sources == (["test"] if result.telegram_sent else ["Previous source"])
    assert (feedback.last_digest_time != "Previous time") is result.telegram_sent


@pytest.mark.asyncio
async def test_failed_category_remains_retryable_in_markdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    good = _Article(title="Good")
    failed = _Article(title="Failed", link="https://example.com/failed")
    good_hash = article_hash(good.title, good.link)
    failed_hash = article_hash(failed.title, failed.link)
    save_cache = MagicMock()
    with (
        patch("digest.config.load_config", return_value=_mock_config()),
        patch("digest.radar.collect", AsyncMock(return_value=(
            {"tech": [good], "failed": [failed]}, {good_hash: "new", failed_hash: "new"},
        ))),
        patch("digest.radar.summarize_all", AsyncMock(return_value=([_CategorySummary()], ""))),
        patch("digest.radar.pick_top_articles", AsyncMock(return_value=[])),
        patch("digest.radar.save_dedup_cache", save_cache),
        patch("digest.application.investigation.run_irritator",
              AsyncMock(return_value=([], [], IrritatorStatus("empty", "empty")))),
        patch("digest.delivery.write_digest", return_value=Path("digest.md")),
        patch("digest.application.run_state.process_pending_approvals"),
    ):
        await run("config.yaml", False, False, False)
    save_cache.assert_called_once_with({good_hash: "new"})


@pytest.mark.asyncio
async def test_radar_only_does_not_consume_articles_and_cli_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    save_cache = MagicMock()
    with (
        patch("digest.config.load_config", return_value=_mock_config()),
        patch("digest.radar.collect", AsyncMock(return_value=({"tech": [_Article()]}, {"hash": "new"}))),
        patch("digest.radar.summarize_all", AsyncMock(return_value=([_CategorySummary()], ""))),
        patch("digest.radar.pick_top_articles", AsyncMock(return_value=[])),
        patch("digest.radar.save_dedup_cache", save_cache),
    ):
        assert await main(["--radar-only"]) == 0
    save_cache.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("feed_failure", [False, True])
@pytest.mark.parametrize("dry_run", [False, True])
async def test_failed_runs_record_fetch_health_without_consuming_articles(
    feed_failure: bool, dry_run: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from digest.radar import AllFeedsFailedError

    monkeypatch.chdir(tmp_path)
    source_stats: dict = {}
    save_stats = MagicMock()
    save_cache = MagicMock()

    async def collect_stub(*args: object, **kwargs: object) -> tuple:
        metrics = kwargs["fetch_metrics"]
        assert isinstance(metrics, dict)
        metrics["test"] = SourceFetchMetrics(not feed_failure, 0 if feed_failure else 1, 10.0)
        if feed_failure:
            raise AllFeedsFailedError("all feeds unavailable")
        return {"tech": [_Article()]}, {"hash": "new"}

    with (
        patch("digest.config.load_config", return_value=_mock_config()),
        patch("digest.radar.collect", AsyncMock(side_effect=collect_stub)),
        patch("digest.radar.summarize_all", AsyncMock(return_value=([], ""))),
        patch("digest.radar.save_dedup_cache", save_cache),
        patch("digest.source_scorer.load_stats", return_value=source_stats),
        patch("digest.source_scorer.save_stats", save_stats),
        patch("digest.application.legacy._notify_summaries_failed", AsyncMock()),
    ):
        if feed_failure:
            with pytest.raises(AllFeedsFailedError):
                await run("config.yaml", dry_run, False, False)
        else:
            await run("config.yaml", dry_run, False, False)
    save_cache.assert_not_called()
    if dry_run:
        assert source_stats == {}
        save_stats.assert_not_called()
    else:
        save_stats.assert_called_once()
        assert source_stats["test"].total_fetches == 1
        assert source_stats["test"].successful_fetches == int(not feed_failure)
        assert source_stats["test"].articles_included_in_digest == 0


@pytest.mark.asyncio
async def test_required_telegram_failure_is_red_even_when_markdown_exists() -> None:
    stats = RunStats(
        feeds_fetched=1, new_articles=1, digest_length=100,
        telegram_sent=False, telegram_partial=False,
        markdown_saved=True, markdown_path="digest.md", required_delivery_failed=True,
    )
    with patch("digest.main.run", AsyncMock(return_value=stats)):
        assert await main([]) == 1


@pytest.mark.asyncio
async def test_votes_persist_with_adaptation_off_and_delivery_failure_managed_run_never_repolls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import datetime, timezone

    from digest.feedback import ArticleFeedback, load_feedback, save_feedback

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "synthetic-test-token")
    cfg = _mock_config()
    cfg.telegram.enabled = True
    assert not cfg.adaptive.enabled
    vote = ArticleFeedback("12345678", "test", -1, datetime.now(timezone.utc).isoformat())

    async def collect_vote(_token, _store, *, cache_dir):
        store = FeedbackStore(ratings=[vote], last_update_id=100)
        save_feedback(store, cache_dir, strict=True)
        return store

    article = _Article()
    card = ArticleSummary(article.title, article.link, "test", "tech", "Canonical summary.")
    with (
        patch("digest.config.load_config", return_value=cfg),
        patch("digest.feedback.collect_feedback", side_effect=collect_vote) as poll,
        patch("digest.radar.collect", AsyncMock(return_value=({"tech": [article]}, {}))) as collect,
        patch("digest.application.analysis.analyze_articles",
              AsyncMock(return_value=([_CategorySummary()], None, [card], None))),
        patch("digest.application.investigation.run_irritator",
              AsyncMock(return_value=([], [], IrritatorStatus("empty", "empty")))),
        patch("digest.delivery.write_digest", return_value=None),
        patch("digest.delivery.send_article_cards", AsyncMock(return_value=ArticleDeliveryResult(1, 0, 1))),
        patch("digest.delivery.send_counter_signals", AsyncMock()),
        patch("digest.application.run_state.process_pending_approvals") as approvals,
    ):
        result = await run("config.yaml", False, False, False)
        assert not result.telegram_sent and not result.markdown_saved
        assert collect.call_args.kwargs["effective_priorities"]["test"] == 2
        assert load_feedback(".cache", strict=True).ratings == [vote]
        assert load_feedback(".cache", strict=True).last_update_id == 100
        poll.assert_awaited_once()
        approvals.assert_not_called()
        poll.reset_mock()
        collect.return_value = ({}, {})
        await run("config.yaml", False, False, False, feedback_precollected=True)
        poll.assert_not_called()
        assert load_feedback(".cache", strict=True).ratings == [vote]


@pytest.mark.parametrize(("enabled", "decisions"), [(True, {}), (False, {}), (False, {"source": "approved"})])
def test_no_approval_reload_retains_config_and_execution(enabled: bool, decisions: dict[str, str]) -> None:
    from digest.application.run_state import apply_pending_approvals

    config, execution = _mock_config(), ModelExecution()
    store = FeedbackStore(source_decisions=decisions)
    with (patch("digest.config.load_config", side_effect=AssertionError("No reload")),
          patch.object(ModelExecution, "request_state", side_effect=AssertionError("No initialization"))):
        current_config, current_execution = apply_pending_approvals(
            config, "config.yaml", ".cache", store, enabled=enabled, execution=execution,
        )
    assert current_config is config and current_execution is execution


@pytest.mark.parametrize("persistence_failure", [False, True])
def test_approval_reload_starts_fresh_lazy_execution_even_when_config_is_unchanged(persistence_failure: bool) -> None:
    from digest.application.run_state import apply_pending_approvals

    config, execution = _mock_config(), ModelExecution()
    store = FeedbackStore(source_decisions={"source": "approved"})
    failure = OSError("state write failed") if persistence_failure else None
    with (patch("digest.application.run_state.process_pending_approvals", side_effect=failure),
          patch("digest.config.load_config", return_value=config) as reload,
          patch.object(ModelExecution, "request_state", side_effect=AssertionError("No initialization"))):
        current_config, current_execution = apply_pending_approvals(
            config, "config.yaml", ".cache", store, enabled=True, execution=execution,
        )
    reload.assert_called_once_with("config.yaml")
    assert current_config is config
    assert isinstance(current_execution, ModelExecution) and current_execution is not execution


def test_pending_source_approval_requires_current_identity_and_keeps_failed_decision(tmp_path: Path) -> None:
    from datetime import datetime, timedelta, timezone

    from digest.application.run_state import process_pending_approvals as _process_pending_approvals
    from digest.discovery import PendingSource, proposal_binding

    now = datetime.now(timezone.utc)
    fresh = PendingSource("Fresh", "https://example.com/fresh", "Tech", now.isoformat())
    failed = PendingSource("Retry", "https://example.com/retry", "Tech", now.isoformat())
    stale = PendingSource("Stale", "https://example.com/stale", "Tech", (now-timedelta(days=31)).isoformat())
    wrong = PendingSource("Wrong", "https://example.com/wrong", "Tech", now.isoformat(), "12345678")
    proposals = (fresh, failed, stale, wrong)
    store = FeedbackStore(
        source_decisions={x.source_hash: "approved" for x in proposals},
        source_decision_bindings={x.source_hash: proposal_binding(x) for x in proposals},
    )

    def apply(_path, proposal):
        if proposal is failed:
            raise OSError("synthetic write failure")

    with (
        patch("digest.discovery.load_pending", return_value=[fresh, failed, stale, wrong]),
        patch("digest.discovery.add_source_to_config", side_effect=apply) as add,
        patch("digest.discovery.save_pending"),
    ):
        _process_pending_approvals("config.yaml", str(tmp_path), store)
    assert [call.args[1] for call in add.call_args_list] == [fresh, failed]
    assert fresh.source_hash not in store.source_decisions
    assert store.source_decisions[failed.source_hash] == "approved"
    assert store.source_decision_bindings[failed.source_hash] == proposal_binding(failed)


@pytest.mark.asyncio
@pytest.mark.parametrize("write_fails", [False, True])
async def test_discovery_persists_unique_proposals_before_sending_instructions(
    write_fails: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from digest.discovery import ProposalDelivery, load_pending
    from digest.main import discover_sources
    from scripts.review_fixture import fixture_config

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GITHUB_RUN_ID", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "synthetic-test-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    url = "https://example.com/new"
    client = MagicMock()
    client.get = AsyncMock(return_value=MagicMock())

    async def send(proposal, token, owner, bot_username):
        assert load_pending(".cache") == [proposal]
        assert (token, owner, bot_username) == ("synthetic-test-token", "123", "digest_test_bot")
        return ProposalDelivery("confirmed", 1)

    config = fixture_config()
    config.telegram.enabled = True
    config.telegram.bot_username = "digest_test_bot"
    from digest.config import ProviderConfig
    config.llm.providers = [ProviderConfig("groq", "fixture-model", ["summarize"])]
    with ExitStack() as stack:
        stack.enter_context(patch("digest.config.load_config", return_value=config))
        stack.enter_context(patch("digest.llm.complete", AsyncMock(return_value=(
            f"FEED|{url}|tech|New\nFEED|{url}|tech|Duplicate", None,
        ))))
        stack.enter_context(patch("digest.discovery_feed.validate_feed_url", AsyncMock(return_value=url)))
        sent = stack.enter_context(patch("digest.discovery.send_source_approval_message", side_effect=send))
        if write_fails:
            stack.enter_context(patch("digest.discovery.atomic_json_write", side_effect=OSError("disk full")))
            with pytest.raises(OSError):
                await discover_sources("config.yaml")
            sent.assert_not_called()
        else:
            assert await discover_sources("config.yaml") == 0
            sent.assert_awaited_once()


def test_bound_rejection_removes_proposal_without_config_addition(tmp_path: Path) -> None:
    from datetime import datetime, timezone

    from digest.application.run_state import process_pending_approvals as _process_pending_approvals
    from digest.discovery import PendingSource, load_pending, proposal_binding, save_pending
    from digest.feedback import load_feedback, save_feedback

    proposal = PendingSource("Rejected", "https://example.com/no", "Tech", datetime.now(timezone.utc).isoformat())
    save_pending([proposal], str(tmp_path), strict=True)
    import json
    from dataclasses import asdict
    from datetime import timedelta
    expired = PendingSource("Expired", "https://example.com/expired", "Tech",
                            (datetime.now(timezone.utc)-timedelta(days=31)).isoformat())
    (tmp_path / "pending_sources.json").write_text(json.dumps({"pending": [asdict(proposal), asdict(expired)]}))
    store = FeedbackStore(
        source_decisions={proposal.source_hash: "rejected"},
        source_decision_bindings={proposal.source_hash: proposal_binding(proposal)},
    )
    save_feedback(store, str(tmp_path), strict=True)
    with patch("digest.discovery.add_source_to_config") as add:
        _process_pending_approvals("config.yaml", str(tmp_path), store)
    add.assert_not_called()
    assert load_pending(str(tmp_path)) == []
    assert load_feedback(str(tmp_path), strict=True).source_decisions == {}
    assert store.source_decisions == {} and store.source_decision_bindings == {}
    from digest.discovery import load_delivery
    assert {item["decision"] for item in load_delivery(str(tmp_path))["history"]} == {"rejected", "expired"}


@pytest.mark.parametrize("case", ["legacy", "changed_name", "changed_category", "changed_date", "duplicate", "future"])
def test_pending_decision_cannot_authorize_a_different_proposal(case: str, tmp_path: Path) -> None:
    from copy import deepcopy
    from datetime import datetime, timedelta, timezone

    from digest.application.run_state import process_pending_approvals as _process_pending_approvals
    from digest.discovery import PendingSource, proposal_binding

    now = datetime.now(timezone.utc)
    proposal = PendingSource("Original", "https://example.com/feed", "Tech", now.isoformat())
    store = FeedbackStore(
        source_decisions={proposal.source_hash: "approved"},
        source_decision_bindings={} if case == "legacy" else {proposal.source_hash: proposal_binding(proposal)},
    )
    original = deepcopy(store)
    if case == "changed_name":
        proposal.name = "Replacement"
    elif case == "changed_category":
        proposal.category = "Replacement"
    elif case == "changed_date":
        proposal.discovered_at = (now - timedelta(hours=1)).isoformat()
    elif case == "future":
        proposal.discovered_at = (now + timedelta(days=1)).isoformat()
        store.source_decision_bindings[proposal.source_hash] = proposal_binding(proposal)
        original = deepcopy(store)
    pending = [proposal, proposal] if case == "duplicate" else [proposal]
    with (
        patch("digest.discovery.load_pending", return_value=pending),
        patch("digest.discovery.add_source_to_config") as add,
        patch("digest.discovery.save_pending") as save,
    ):
        _process_pending_approvals("config.yaml", str(tmp_path), store)
    add.assert_not_called()
    save.assert_not_called()
    assert store == original


@pytest.mark.parametrize("failure", ["backup", "config", "pending", "feedback"])
def test_source_application_io_failure_preserves_durable_decision(failure: str, tmp_path: Path) -> None:
    from copy import deepcopy
    from datetime import datetime, timezone

    import yaml

    from digest.application.run_state import process_pending_approvals as _process_pending_approvals
    from digest.discovery import PendingSource, load_pending, proposal_binding, save_pending
    from digest.feedback import load_feedback, save_feedback

    proposal = PendingSource("New", "https://example.com/new", "Tech", datetime.now(timezone.utc).isoformat())
    config_path = tmp_path / "config.yaml"
    config_path.write_text("sources: []\n", encoding="utf-8")
    store = FeedbackStore(
        source_decisions={proposal.source_hash: "approved"},
        source_decision_bindings={proposal.source_hash: proposal_binding(proposal)},
    )
    original = deepcopy(store)
    save_pending([proposal], str(tmp_path), strict=True)
    save_feedback(store, str(tmp_path), strict=True)
    original_replace = Path.replace

    def fail_config_replace(path: Path, target: Path) -> Path:
        if path == config_path.with_suffix(".yaml.tmp"):
            raise OSError("synthetic config write failure")
        return original_replace(path, target)

    targets = {
        "backup": "digest.discovery.shutil.copy2", "config": "pathlib.Path.replace",
        "pending": "digest.discovery.atomic_json_write", "feedback": "digest.feedback.atomic_json_write",
    }
    with patch(targets[failure], autospec=True, side_effect=(
        fail_config_replace if failure == "config" else OSError("synthetic failure")
    )):
        if failure in ("pending", "feedback"):
            with pytest.raises(OSError):
                _process_pending_approvals(str(config_path), str(tmp_path), store)
        else:
            _process_pending_approvals(str(config_path), str(tmp_path), store)
    assert store == original and load_feedback(str(tmp_path), strict=True) == original
    if failure != "feedback":
        assert load_pending(str(tmp_path)) == [proposal]
    if failure in ("backup", "config"):
        assert yaml.safe_load(config_path.read_text()) == {"sources": []}
    else:
        assert yaml.safe_load(config_path.read_text())["sources"][0]["url"] == proposal.url


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["empty", "feeds", "analysis", "pending_write"])
async def test_source_application_precedes_collection_and_survives_unsuccessful_digest(
    failure: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import datetime, timezone

    from digest.discovery import PendingSource, load_pending, proposal_binding, save_pending
    from digest.feedback import load_feedback, save_feedback
    from digest.radar import AllFeedsFailedError

    monkeypatch.chdir(tmp_path)
    proposal = PendingSource("New", "https://example.com/new", "tech", datetime.now(timezone.utc).isoformat())
    Path("config.yaml").write_text("sources: []\n", encoding="utf-8")
    save_pending([proposal], ".cache", strict=True)
    save_feedback(FeedbackStore(
        source_decisions={proposal.source_hash: "approved"},
        source_decision_bindings={proposal.source_hash: proposal_binding(proposal)},
    ), ".cache", strict=True)
    initial = _mock_config()
    reloaded = _mock_config()
    reloaded.sources.append(_SourceCfg(name=proposal.name, url=proposal.url))

    async def collect(config, **kwargs):
        assert [source.name for source in config.sources] == ["test", "New"]
        if failure == "feeds":
            raise AllFeedsFailedError("synthetic feed failure")
        return ({"tech": [_Article()]} if failure == "analysis" else {}), {}

    with ExitStack() as stack:
        stack.enter_context(patch("digest.config.load_config", side_effect=[initial, reloaded]))
        stack.enter_context(patch("digest.radar.collect", side_effect=collect))
        stack.enter_context(patch("digest.application.analysis.analyze_articles",
              AsyncMock(return_value=([], None, [], None))))
        if failure == "pending_write":
            stack.enter_context(patch("digest.discovery.atomic_json_write", side_effect=OSError("disk full")))
        if failure == "feeds":
            with pytest.raises(AllFeedsFailedError):
                await run("config.yaml", False, False, False, feedback_precollected=True)
        else:
            result = await run("config.yaml", False, False, False, feedback_precollected=True)
            assert not result.telegram_sent and not result.markdown_saved
            assert result.feeds_fetched == 2
    durable = load_feedback(".cache", strict=True)
    assert durable.source_decisions == ({proposal.source_hash: "approved"} if failure == "pending_write" else {})
    assert load_pending(".cache") == ([proposal] if failure == "pending_write" else [])

@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["sent", "unknown", "persist_failure", "no_content", "corrupt_feedback"])
async def test_compact_issue_persists_only_confirmed_coverage_and_holds_uncertainty(
    case: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    from digest.delivery.issue_guard import load_guard, reserve
    from digest.delivery.telegram import IssueDeliveryResult
    from digest.feedback import load_feedback, save_feedback

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test")
    config_path = tmp_path / "config.yaml"
    config_path.write_text("stable fixture config")
    marker, expected = reserve(config_path)
    guard = load_guard(config_path, expected)  # Represents the managed persisted-hash handoff.
    if case == "corrupt_feedback":
        Path(".cache/feedback.json").write_text("broken preserved state")
    cfg = _mock_config()
    cfg.telegram.enabled = cfg.telegram.required = True
    cfg.telegram.delivery_mode = "compact"
    cfg.obsidian.enabled = True
    articles = [_Article(title="One"), _Article(title="Two", link="https://example.com/2")]
    hashes = [article_hash(item.title, item.link) for item in articles]
    cards = [ArticleSummary(item.title, item.link, item.source, "tech", "Complete text") for item in articles]
    cache = {"old": "old", **dict.fromkeys(hashes, "new")}
    confirmed = 1 if case == "unknown" else 2
    outcome = "unknown" if case == "unknown" else "sent"

    async def sender(*args, before_send, **kwargs):
        before_send()
        return IssueDeliveryResult(
            attempted=2, sent=confirmed, failed=2 - confirmed,
            delivered_hashes=set(hashes[:confirmed]),
            article_source_map={key[:8]: "test" for key in hashes[:confirmed]},
            outcome=outcome, total_chunks=2, attempted_chunks=2, confirmed_chunks=confirmed,
        )

    def save(store, directory, *, strict=False):
        if strict and case == "persist_failure":
            raise OSError("synthetic durable attribution failure after accepted POST")
        save_feedback(store, directory, strict=strict)

    with (
        patch("digest.config.load_config", return_value=cfg),
        patch("digest.radar.collect", AsyncMock(return_value=(
            {} if case == "no_content" else {"tech": articles}, cache,
        ))),
        patch("digest.application.analysis.analyze_articles",
              AsyncMock(return_value=([_CategorySummary()], None, cards, None))),
        patch("digest.application.investigation.run_irritator",
              AsyncMock(return_value=([], [], IrritatorStatus("empty", "empty")))),
        patch("digest.delivery.write_digest", return_value=Path("digest.md")),
        patch("digest.delivery.telegram.send_compact_issue", AsyncMock(side_effect=sender)) as send,
        patch("digest.delivery.send_article_cards", AsyncMock()) as old_send,
        patch("digest.application.legacy._legacy_delivery_extras", AsyncMock()) as extra,
        patch("digest.feedback.save_feedback", side_effect=save),
    ):
        if case in {"persist_failure", "corrupt_feedback"}:
            with pytest.raises(OSError if case == "persist_failure" else ValueError):
                await run(str(config_path), False, False, False, feedback_precollected=True, issue_guard=guard)
        else:
            result = await run(str(config_path), False, False, False, feedback_precollected=True, issue_guard=guard)
            assert result.telegram_sent is (case == "sent")
        old_send.assert_not_called()
        extra.assert_not_called()
    record = json.loads(marker.read_text())
    if case == "corrupt_feedback":
        send.assert_not_called()
        assert record["state"] == "not_sent"
        assert Path(".cache/feedback.json").read_text() == "broken preserved state"
        assert not Path(".cache/seen_articles.json").exists()
    elif case == "no_content":
        send.assert_not_called()
        assert record["state"] == "not_sent"
    elif case == "persist_failure":
        assert record["state"] == "unknown" and record["attempted_count"] is None
    else:
        assert record["state"] == ("confirmed" if case == "sent" else "unknown")
        assert set(json.loads(Path(".cache/seen_articles.json").read_text())) == {"old", *hashes[:confirmed]}
        assert load_feedback(".cache", strict=True).article_source_map == {
            key[:8]: "test" for key in hashes[:confirmed]
        }


@pytest.mark.asyncio
async def test_discovery_legacy_batch_pair_barrier_receipts_and_no_reoffer(tmp_path, monkeypatch):
    import hashlib
    from datetime import datetime, timedelta, timezone

    from digest.discovery import PendingSource, ProposalDelivery, load_delivery, load_pending
    from digest.main import discover_sources
    from scripts.review_fixture import fixture_config

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_RUN_ID", "fixture-run")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fixture-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "fixture-owner")
    now = datetime.now(timezone.utc)
    proposals = [PendingSource(f"Feed {n}", f"https://example.com/{n}", "New category", now.isoformat())
                 for n in range(3)]
    expired = PendingSource("Old", "https://example.com/old", "Old", (now-timedelta(days=31)).isoformat())
    # Write expired explicitly: save_pending itself prunes on write.
    import json
    from dataclasses import asdict
    Path(".cache").mkdir()
    Path(".cache/pending_sources.json").write_text(json.dumps({"pending": [asdict(p) for p in [expired, *proposals]]}))
    config = fixture_config()
    config.telegram.enabled = True
    config.telegram.bot_username = "fixture_bot"
    from digest.config import ProviderConfig
    config.llm.providers = [ProviderConfig("groq", "fixture-model", ["summarize"])]
    with (patch("digest.config.load_config", return_value=config),
          patch("digest.discovery_feed.validate_feed_url", AsyncMock(side_effect=lambda url: url)),
          patch("digest.llm.complete", AsyncMock(return_value=("", {}))) as model,
          patch("digest.discovery.send_source_approval_message", AsyncMock(side_effect=[
              ProposalDelivery("confirmed", 42), ProposalDelivery("unknown"), ProposalDelivery("rejected"),
          ])) as send):
        assert await discover_sources("config.yaml", phase="prepare") == 0
        model.assert_not_awaited()
        send.assert_not_awaited()
        with pytest.raises(ValueError, match="separate persisted"):
            await discover_sources("config.yaml")
        model.assert_not_awaited()
        assert load_pending(".cache") == proposals
        assert len(load_delivery(".cache")["history"]) == 1
        pending = Path(".cache/pending_sources.json")
        receipt = Path(".cache/discovery_delivery.json")
        ps, ds = [hashlib.sha256(p.read_bytes()).hexdigest() for p in [pending, receipt]]
        with pytest.raises(ValueError, match="pair hash mismatch"):
            await discover_sources("config.yaml", phase="send", pending_sha=ps, delivery_sha="wrong")
        send.assert_not_awaited()
        config.telegram.enabled = False
        assert await discover_sources("config.yaml", phase="send", pending_sha=ps, delivery_sha=ds) == 1
        send.assert_not_awaited()
        assert hashlib.sha256(receipt.read_bytes()).hexdigest() == ds
        config.telegram.enabled = True
        assert await discover_sources("config.yaml", phase="send", pending_sha=ps, delivery_sha=ds) == 1
        assert send.await_count == 3
        records = list(load_delivery(".cache")["deliveries"].values())
        assert [r["status"] for r in records] == ["confirmed", "unknown", "rejected"]
        assert records[0]["message_id"] == 42 and records[1]["message_id"] is None
        with pytest.raises(ValueError, match="pair hash mismatch"):
            await discover_sources("config.yaml", phase="send", pending_sha=ps, delivery_sha=ds)
        monkeypatch.setenv("GITHUB_RUN_ID", "next-fixture-run")
        model.return_value = ("FEED|https://example.com/old|Old|Old", {})
        assert await discover_sources("config.yaml", phase="prepare") == 0
        assert load_delivery(".cache")["batch"]["bindings"] == []
        assert all(p.url != expired.url for p in load_pending(".cache"))
        assert send.await_count == 3


@pytest.mark.asyncio
async def test_discovery_reserved_before_crash_holds_future_run(tmp_path, monkeypatch):
    from datetime import datetime, timezone

    from digest.discovery import PendingSource, load_delivery, save_pending
    from digest.main import discover_sources
    from scripts.review_fixture import fixture_config

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_RUN_ID", "prepare-only")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fixture-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "fixture-owner")
    proposal = PendingSource("Feed", "https://example.com/feed", "New", datetime.now(timezone.utc).isoformat())
    save_pending([proposal], ".cache")
    config = fixture_config()
    config.telegram.enabled = True
    from digest.config import ProviderConfig
    config.llm.providers = [ProviderConfig("groq", "fixture-model", ["summarize"])]
    with (patch("digest.config.load_config", return_value=config),
          patch("digest.discovery_feed.validate_feed_url", AsyncMock(side_effect=lambda url: url)),
          patch("digest.llm.complete", AsyncMock(side_effect=RuntimeError("unavailable"))),
          patch("digest.discovery.send_source_approval_message", AsyncMock()) as send):
        await discover_sources("config.yaml", phase="prepare")
        assert len(load_delivery(".cache")["batch"]["bindings"]) == 1
        assert load_delivery(".cache")["prepare_counts"]["generation_failed"] == 1
        monkeypatch.setenv("GITHUB_RUN_ID", "later-run")
        await discover_sources("config.yaml", phase="prepare")
        assert load_delivery(".cache")["batch"]["bindings"] == []
        send.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failures, expected_calls", [(0, 1), (1, 2), (2, 2)])
async def test_discovery_generation_preserves_bounded_configured_fallback(
    tmp_path, monkeypatch, failures, expected_calls,
):
    import httpx

    from digest.config import ProviderConfig
    from digest.discovery import load_delivery
    from digest.main import discover_sources
    from scripts.review_fixture import fixture_config

    monkeypatch.chdir(tmp_path)
    config = fixture_config()
    config.llm.providers = [
        ProviderConfig("gemini", "first", ["summarize"]),
        ProviderConfig("groq", "second", ["fallback"]),
        ProviderConfig("mistral", "third", ["fallback"]),
    ]
    config.llm.max_retries = 5
    config.llm.min_request_interval_seconds = 0
    calls = []

    async def provider_call(client, provider, messages, temperature, max_output_tokens):
        calls.append(provider.model)
        assert max_output_tokens == 2048
        if len(calls) <= failures:
            response = httpx.Response(503, request=httpx.Request("POST", "https://example.com/model"))
            raise httpx.HTTPStatusError("unavailable", request=response.request, response=response)
        return "", {}  # Valid empty ends generation without trying another provider.

    with patch("digest.config.load_config", return_value=config), \
            patch("digest.llm._call_provider", side_effect=provider_call):
        assert await discover_sources("config.yaml", phase="prepare") == 0
    assert calls == ["first", "second"][:expected_calls]
    counts = load_delivery(".cache")["prepare_counts"]
    assert counts["generation_failed"] == int(failures == 2)
    assert counts["suggested"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(("dry_run", "radar_only"), [(True, False), (False, True), (True, True)])
async def test_programmatic_preparation_rejects_preview_before_preparation_effects(
    dry_run: bool, radar_only: bool,
) -> None:
    with patch("digest.config.load_config", side_effect=AssertionError("No configuration or state work")):
        with pytest.raises(ValueError, match="cannot be combined with preview"):
            await run("config.yaml", dry_run, radar_only, False, prepare_only=True)
