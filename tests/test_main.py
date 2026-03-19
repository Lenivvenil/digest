"""Tests for src/main.py — entrypoint, CLI argument parsing, dry-run mode."""

from __future__ import annotations

import socket
import textwrap
import unittest.mock
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.collector import Article
from src.main import RunStats, _parse_args, main, run
from src.telegram import TelegramPartialDeliveryError


# ---------------------------------------------------------------------------
# Minimal config fixture
# ---------------------------------------------------------------------------

MINIMAL_CONFIG = textwrap.dedent(
    """\
    llm:
      provider: "anthropic"
      model: "claude-sonnet-4-20250514"
    delivery:
      telegram: false
      markdown_to_repo: false
      markdown_dir: "digests"
    digest:
      language: "ru"
      max_articles_per_source: 5
      max_total_articles: 30
      summary_style: "analytical"
    sources:
      - name: "Test Source"
        url: "https://example.com/feed.rss"
        category: "Test"
        enabled: true
    """
)

ADAPTIVE_CONFIG = textwrap.dedent(
    """\
    llm:
      provider: "anthropic"
      model: "claude-sonnet-4-20250514"
    delivery:
      telegram: false
      markdown_to_repo: false
      markdown_dir: "digests"
    adaptive:
      enabled: true
      feedback_weight: 0.3
      score_weight: 0.5
      base_weight: 0.2
      trial_slots: 2
    digest:
      language: "ru"
      max_articles_per_source: 5
      max_total_articles: 30
      summary_style: "analytical"
    sources:
      - name: "Test Source"
        url: "https://example.com/feed.rss"
        category: "Test"
        enabled: true
    """
)


@pytest.fixture()
def config_file(tmp_path: Path) -> Path:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(MINIMAL_CONFIG, encoding="utf-8")
    return cfg


@pytest.fixture()
def adaptive_config_file(tmp_path: Path) -> Path:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(ADAPTIVE_CONFIG, encoding="utf-8")
    return cfg


# ---------------------------------------------------------------------------
# Argument parsing tests
# ---------------------------------------------------------------------------


def test_parse_args_defaults() -> None:
    args = _parse_args([])
    assert args.config == "config.yaml"
    assert args.dry_run is False
    assert args.verbose is False


def test_parse_args_all_flags() -> None:
    args = _parse_args(["--config", "custom.yaml", "--dry-run", "--verbose"])
    assert args.config == "custom.yaml"
    assert args.dry_run is True
    assert args.verbose is True


def test_parse_args_config_only() -> None:
    args = _parse_args(["--config", "other.yaml"])
    assert args.config == "other.yaml"
    assert args.dry_run is False


# ---------------------------------------------------------------------------
# run() — dry-run mode
# ---------------------------------------------------------------------------


@pytest.fixture()
def sample_articles() -> dict[str, list[Article]]:
    from datetime import datetime, timezone

    art = Article(
        title="Test article",
        link="https://example.com/1",
        description="A test article description.",
        source="Test Source",
        category="Test",
        pub_date=datetime(2026, 3, 15, 6, 0, 0, tzinfo=timezone.utc),
    )
    return {"Test": [art]}


@pytest.mark.asyncio
async def test_run_dry_run_no_delivery(config_file: Path, sample_articles: dict) -> None:
    """In dry-run mode, Telegram and markdown writer must not be called."""
    with (
        patch("src.main.collect", new_callable=AsyncMock, return_value=(sample_articles, {})),
        patch("src.main.save_dedup_cache"),
        patch("src.main.get_provider") as mock_get_provider,
        patch("src.main.send_digest", new_callable=AsyncMock) as mock_send,
        patch("src.main.write_digest") as mock_write,
    ):
        mock_provider = MagicMock()
        mock_provider.summarize = AsyncMock(return_value="Test summary content")
        mock_get_provider.return_value = mock_provider

        stats = await run(config_path=str(config_file), dry_run=True)

    mock_send.assert_not_called()
    mock_write.assert_not_called()
    assert stats.telegram_sent is False
    assert stats.markdown_saved is False
    assert stats.digest_length == len("Test summary content")
    assert stats.new_articles == 1


@pytest.mark.asyncio
async def test_run_no_articles(config_file: Path) -> None:
    """When no articles are found, summarizer and delivery must not be called."""
    with (
        patch("src.main.collect", new_callable=AsyncMock, return_value=({}, {})),
        patch("src.main.save_dedup_cache"),
        patch("src.main.get_provider") as mock_get_provider,
        patch("src.main.send_digest", new_callable=AsyncMock) as mock_send,
        patch("src.main.write_digest") as mock_write,
    ):
        stats = await run(config_path=str(config_file), dry_run=False)

    mock_get_provider.assert_not_called()
    mock_send.assert_not_called()
    mock_write.assert_not_called()
    assert stats.new_articles == 0
    assert stats.digest_length == 0


@pytest.mark.asyncio
async def test_run_no_delivery_does_not_save_cache(config_file: Path, sample_articles: dict) -> None:
    """When both Telegram and markdown are disabled, the dedup cache must NOT be saved."""
    with (
        patch("src.main.collect", new_callable=AsyncMock, return_value=(sample_articles, {})),
        patch("src.main.save_dedup_cache") as mock_save_cache,
        patch("src.main.get_provider") as mock_get_provider,
        patch("src.main.send_digest", new_callable=AsyncMock, return_value=False),
        patch("src.main.write_digest", return_value=None),
    ):
        mock_provider = MagicMock()
        mock_provider.summarize = AsyncMock(return_value="Summary")
        mock_get_provider.return_value = mock_provider

        stats = await run(config_path=str(config_file), dry_run=False)

    mock_save_cache.assert_not_called()
    assert stats.telegram_sent is False
    assert stats.markdown_saved is False


@pytest.mark.asyncio
async def test_run_full_pipeline(config_file: Path, sample_articles: dict, tmp_path: Path) -> None:
    """Full pipeline (no dry-run): Telegram send and markdown write are invoked."""
    markdown_path = tmp_path / "digests" / "2026-03-15.md"

    with (
        patch("src.main.collect", new_callable=AsyncMock, return_value=(sample_articles, {})),
        patch("src.main.save_dedup_cache") as mock_save_cache,
        patch("src.main.get_provider") as mock_get_provider,
        patch("src.main.send_digest", new_callable=AsyncMock, return_value=False) as mock_send,
        patch("src.main.write_digest", return_value=markdown_path) as mock_write,
    ):
        mock_provider = MagicMock()
        mock_provider.summarize = AsyncMock(return_value="Full summary")
        mock_get_provider.return_value = mock_provider

        stats = await run(config_path=str(config_file), dry_run=False)

    mock_send.assert_awaited_once()
    mock_write.assert_called_once()
    mock_save_cache.assert_called_once()
    assert stats.telegram_sent is False
    assert stats.markdown_saved is True
    assert stats.markdown_path == str(markdown_path)
    assert stats.digest_length == len("Full summary")


# ---------------------------------------------------------------------------
# main() exit codes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_main_missing_config() -> None:
    """Missing config file must return exit code 1."""
    exit_code = await main(["--config", "/nonexistent/path/config.yaml"])
    assert exit_code == 1


@pytest.mark.asyncio
async def test_main_dry_run_success(config_file: Path, sample_articles: dict) -> None:
    """Successful dry-run must return exit code 0."""
    with (
        patch("src.main.collect", new_callable=AsyncMock, return_value=(sample_articles, {})),
        patch("src.main.save_dedup_cache"),
        patch("src.main.get_provider") as mock_get_provider,
        patch("src.main.send_digest", new_callable=AsyncMock, return_value=False),
        patch("src.main.write_digest", return_value=None),
    ):
        mock_provider = MagicMock()
        mock_provider.summarize = AsyncMock(return_value="Dry run summary")
        mock_get_provider.return_value = mock_provider

        exit_code = await main(["--config", str(config_file), "--dry-run"])

    assert exit_code == 0


@pytest.mark.asyncio
async def test_main_llm_error_returns_exit_1(config_file: Path, sample_articles: dict) -> None:
    """LLM RuntimeError (all feeds failed) must cause exit code 1."""
    with (
        patch("src.main.collect", new_callable=AsyncMock, return_value=(sample_articles, {})),
        patch("src.main.save_dedup_cache"),
        patch("src.main.get_provider") as mock_get_provider,
    ):
        mock_provider = MagicMock()
        mock_provider.summarize = AsyncMock(side_effect=RuntimeError("LLM request failed after 2 attempts"))
        mock_get_provider.return_value = mock_provider

        exit_code = await main(["--config", str(config_file)])

    assert exit_code == 1


@pytest.mark.asyncio
async def test_main_telegram_failure_still_exit_0(
    config_file: Path, sample_articles: dict, tmp_path: Path
) -> None:
    """Telegram delivery failure is non-critical — exit code must still be 0."""
    markdown_path = tmp_path / "2026-03-15.md"

    with (
        patch("src.main.collect", new_callable=AsyncMock, return_value=(sample_articles, {})),
        patch("src.main.save_dedup_cache"),
        patch("src.main.get_provider") as mock_get_provider,
        patch("src.main.send_digest", new_callable=AsyncMock, side_effect=Exception("Network error")),
        patch("src.main.write_digest", return_value=markdown_path),
    ):
        mock_provider = MagicMock()
        mock_provider.summarize = AsyncMock(return_value="Summary")
        mock_get_provider.return_value = mock_provider

        exit_code = await main(["--config", str(config_file)])

    assert exit_code == 0


@pytest.mark.asyncio
async def test_run_partial_telegram_no_markdown_skips_cache(
    config_file: Path, sample_articles: dict
) -> None:
    """Partial Telegram delivery without markdown: cache must NOT be saved so articles retry."""
    with (
        patch("src.main.collect", new_callable=AsyncMock, return_value=(sample_articles, {})),
        patch("src.main.save_dedup_cache") as mock_save_cache,
        patch("src.main.get_provider") as mock_get_provider,
        patch(
            "src.main.send_digest",
            new_callable=AsyncMock,
            side_effect=TelegramPartialDeliveryError("second chunk failed"),
        ),
        patch("src.main.write_digest", return_value=None),
    ):
        mock_provider = MagicMock()
        mock_provider.summarize = AsyncMock(return_value="Summary")
        mock_get_provider.return_value = mock_provider

        stats = await run(config_path=str(config_file), dry_run=False)

    mock_save_cache.assert_not_called()
    assert stats.telegram_sent is False
    assert stats.telegram_partial is True


@pytest.mark.asyncio
async def test_run_partial_telegram_with_markdown_saves_cache(
    config_file: Path, sample_articles: dict, tmp_path: Path
) -> None:
    """Partial Telegram delivery with markdown fallback: cache IS saved (full content available)."""
    markdown_path = tmp_path / "digest.md"

    with (
        patch("src.main.collect", new_callable=AsyncMock, return_value=(sample_articles, {})),
        patch("src.main.save_dedup_cache") as mock_save_cache,
        patch("src.main.get_provider") as mock_get_provider,
        patch(
            "src.main.send_digest",
            new_callable=AsyncMock,
            side_effect=TelegramPartialDeliveryError("second chunk failed"),
        ),
        patch("src.main.write_digest", return_value=markdown_path),
    ):
        mock_provider = MagicMock()
        mock_provider.summarize = AsyncMock(return_value="Summary")
        mock_get_provider.return_value = mock_provider

        stats = await run(config_path=str(config_file), dry_run=False)

    mock_save_cache.assert_called_once()
    assert stats.telegram_sent is False
    assert stats.telegram_partial is True


@pytest.mark.asyncio
async def test_main_partial_telegram_no_markdown_exits_1(
    config_file: Path, sample_articles: dict
) -> None:
    """Partial Telegram delivery with no markdown fallback must exit code 1."""
    with (
        patch("src.main.collect", new_callable=AsyncMock, return_value=(sample_articles, {})),
        patch("src.main.save_dedup_cache"),
        patch("src.main.get_provider") as mock_get_provider,
        patch(
            "src.main.send_digest",
            new_callable=AsyncMock,
            side_effect=TelegramPartialDeliveryError("second chunk failed"),
        ),
        patch("src.main.write_digest", return_value=None),
    ):
        mock_provider = MagicMock()
        mock_provider.summarize = AsyncMock(return_value="Summary")
        mock_get_provider.return_value = mock_provider

        exit_code = await main(["--config", str(config_file)])

    assert exit_code == 1


@pytest.mark.asyncio
async def test_main_partial_telegram_with_markdown_exits_0(
    config_file: Path, sample_articles: dict, tmp_path: Path
) -> None:
    """Partial Telegram delivery with markdown fallback must still exit code 0."""
    markdown_path = tmp_path / "2026-03-15.md"

    with (
        patch("src.main.collect", new_callable=AsyncMock, return_value=(sample_articles, {})),
        patch("src.main.save_dedup_cache"),
        patch("src.main.get_provider") as mock_get_provider,
        patch(
            "src.main.send_digest",
            new_callable=AsyncMock,
            side_effect=TelegramPartialDeliveryError("second chunk failed"),
        ),
        patch("src.main.write_digest", return_value=markdown_path),
    ):
        mock_provider = MagicMock()
        mock_provider.summarize = AsyncMock(return_value="Summary")
        mock_get_provider.return_value = mock_provider

        exit_code = await main(["--config", str(config_file)])

    assert exit_code == 0


# ---------------------------------------------------------------------------
# Adaptive integration tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_adaptive_loads_and_saves_stats_feedback(
    adaptive_config_file: Path, sample_articles: dict
) -> None:
    """When adaptive is enabled and delivery succeeds, run() saves stats and feedback."""
    with (
        patch("src.main.load_stats", return_value={}) as mock_load_stats,
        patch("src.main.save_stats") as mock_save_stats,
        patch("src.main.load_feedback") as mock_load_feedback,
        patch("src.main.save_feedback") as mock_save_feedback,
        patch("src.main.collect_feedback", new_callable=AsyncMock) as mock_collect_fb,
        patch("src.main.calculate_effective_priorities", return_value={"Test Source": 3}) as mock_calc,
        patch("src.main.collect", new_callable=AsyncMock, return_value=(sample_articles, {})),
        patch("src.main.save_dedup_cache"),
        patch("src.main.get_provider") as mock_get_provider,
        patch("src.main.send_digest", new_callable=AsyncMock, return_value=True),
        patch("src.main.write_digest", return_value=None),
        patch("src.main.evaluate_trial_sources", return_value=([], [], [])),
        patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "test-token"}),
    ):
        from src.feedback import FeedbackStore

        mock_load_feedback.return_value = FeedbackStore()
        mock_collect_fb.return_value = FeedbackStore()
        mock_provider = MagicMock()
        mock_provider.summarize = AsyncMock(return_value="Summary")
        mock_get_provider.return_value = mock_provider

        await run(config_path=str(adaptive_config_file), dry_run=False)

    mock_load_stats.assert_called_once_with(".cache")
    mock_load_feedback.assert_called_once_with(".cache")
    mock_collect_fb.assert_awaited_once()
    mock_calc.assert_called_once()
    mock_save_stats.assert_called_once()
    # save_feedback is called twice: once immediately after polling (to
    # persist last_update_id) and once after delivery (to persist
    # last_digest_sources / digest_sources_map).
    assert mock_save_feedback.call_count == 2


@pytest.mark.asyncio
async def test_run_adaptive_delivery_failure_skips_stats_and_trials(
    adaptive_config_file: Path, sample_articles: dict
) -> None:
    """When delivery fails, source stats are still saved and trials must NOT be evaluated."""
    with (
        patch("src.main.load_stats", return_value={}),
        patch("src.main.save_stats") as mock_save_stats,
        patch("src.main.load_feedback") as mock_load_feedback,
        patch("src.main.save_feedback") as mock_save_feedback,
        patch("src.main.collect_feedback", new_callable=AsyncMock) as mock_collect_fb,
        patch("src.main.calculate_effective_priorities", return_value={"Test Source": 3}),
        patch("src.main.collect", new_callable=AsyncMock, return_value=(sample_articles, {})),
        patch("src.main.save_dedup_cache"),
        patch("src.main.get_provider") as mock_get_provider,
        patch("src.main.send_digest", new_callable=AsyncMock, return_value=False),
        patch("src.main.write_digest", return_value=None),
        patch("src.main.evaluate_trial_sources") as mock_eval,
        patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "test-token"}),
    ):
        from src.feedback import FeedbackStore

        mock_load_feedback.return_value = FeedbackStore()
        mock_collect_fb.return_value = FeedbackStore()
        mock_provider = MagicMock()
        mock_provider.summarize = AsyncMock(return_value="Summary")
        mock_get_provider.return_value = mock_provider

        await run(config_path=str(adaptive_config_file), dry_run=False)

    # Stats are always saved to accumulate quality data even on delivery failure
    mock_save_stats.assert_called_once()
    # Feedback is saved twice: once immediately after polling (to persist
    # last_update_id) and once after delivery attempt (to persist any
    # digest_sources_map updates).
    assert mock_save_feedback.call_count == 2
    # Trial evaluation must NOT run when delivery fails
    mock_eval.assert_not_called()


@pytest.mark.asyncio
async def test_run_skips_adaptive_when_disabled(
    config_file: Path, sample_articles: dict
) -> None:
    """When adaptive is disabled, run() does not compute effective priorities."""
    with (
        patch("src.main.load_stats", return_value={}) as mock_load_stats,
        patch("src.main.save_stats"),
        patch("src.main.load_feedback") as mock_load_feedback,
        patch("src.main.save_feedback"),
        patch("src.main.calculate_effective_priorities") as mock_calc,
        patch("src.main.collect", new_callable=AsyncMock, return_value=(sample_articles, {})),
        patch("src.main.save_dedup_cache"),
        patch("src.main.get_provider") as mock_get_provider,
        patch("src.main.send_digest", new_callable=AsyncMock, return_value=False),
        patch("src.main.write_digest", return_value=None),
    ):
        from src.feedback import FeedbackStore

        mock_load_feedback.return_value = FeedbackStore()
        mock_provider = MagicMock()
        mock_provider.summarize = AsyncMock(return_value="Summary")
        mock_get_provider.return_value = mock_provider

        await run(config_path=str(config_file), dry_run=False)

    mock_load_stats.assert_called_once()
    mock_load_feedback.assert_called_once()
    mock_calc.assert_not_called()


@pytest.mark.asyncio
async def test_run_adaptive_trial_evaluation(
    adaptive_config_file: Path, sample_articles: dict, tmp_path: Path
) -> None:
    """When adaptive enabled, trial sources are evaluated and decisions applied after delivery."""
    markdown_path = tmp_path / "digests" / "test.md"

    with (
        patch("src.main.load_stats", return_value={}),
        patch("src.main.save_stats"),
        patch("src.main.load_feedback") as mock_load_feedback,
        patch("src.main.save_feedback"),
        patch("src.main.collect_feedback", new_callable=AsyncMock) as mock_collect_fb,
        patch("src.main.calculate_effective_priorities", return_value={"Test Source": 3}),
        patch("src.main.collect", new_callable=AsyncMock, return_value=(sample_articles, {})),
        patch("src.main.save_dedup_cache"),
        patch("src.main.get_provider") as mock_get_provider,
        patch("src.main.send_digest", new_callable=AsyncMock, return_value=False),
        patch("src.main.write_digest", return_value=markdown_path),
        patch("src.main.evaluate_trial_sources", return_value=(["SourceA"], ["SourceB"], [])) as mock_eval,
        patch("src.main.apply_trial_decisions") as mock_apply,
        patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "test-token"}),
    ):
        from src.feedback import FeedbackStore

        mock_load_feedback.return_value = FeedbackStore()
        mock_collect_fb.return_value = FeedbackStore()
        mock_provider = MagicMock()
        mock_provider.summarize = AsyncMock(return_value="Summary")
        mock_get_provider.return_value = mock_provider

        stats = await run(config_path=str(adaptive_config_file), dry_run=False)

    mock_eval.assert_called_once()
    mock_apply.assert_called_once_with(
        str(adaptive_config_file), ["SourceA"], ["SourceB"], needs_start=[]
    )
    assert stats.sources_promoted == 1
    assert stats.sources_demoted == 1


def test_parse_args_discover_flag() -> None:
    args = _parse_args(["--discover"])
    assert args.discover is True


def test_parse_args_discover_default() -> None:
    args = _parse_args([])
    assert args.discover is False


def test_run_stats_new_fields() -> None:
    """RunStats includes adaptive-related fields with defaults."""
    stats = RunStats(
        feeds_fetched=5,
        new_articles=10,
        digest_length=1000,
        telegram_sent=True,
        telegram_partial=False,
        markdown_saved=True,
        markdown_path="digests/test.md",
    )
    assert stats.sources_promoted == 0
    assert stats.sources_demoted == 0
    assert stats.feedback_collected == 0


# ---------------------------------------------------------------------------
# discover_sources tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_discover_sources_parses_llm_response(config_file: Path) -> None:
    """discover_sources should parse FEED| lines and validate URLs."""
    from src.main import _ValidatedURL, discover_sources

    llm_response = (
        "Here are some suggestions:\n"
        "FEED|https://example.com/feed1.xml|Security|Security Weekly\n"
        "FEED|https://example.com/feed2.xml|Cloud|Cloud Blog\n"
        "Some other text\n"
    )

    fake_addrinfos = [
        (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", 0)),
    ]

    def fake_validate(url: str) -> _ValidatedURL | None:
        if url.startswith("https://example.com/"):
            return _ValidatedURL(
                url=url, hostname="example.com", pinned_addrinfos=fake_addrinfos
            )
        return None

    mock_client = AsyncMock()
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_client.get = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("src.main.get_provider") as mock_get_provider,
        patch("httpx.AsyncClient", return_value=mock_client),
        patch("src.main._validate_url", side_effect=fake_validate),
    ):
        mock_provider = MagicMock()
        mock_provider.summarize = AsyncMock(return_value=llm_response)
        mock_get_provider.return_value = mock_provider

        result = await discover_sources(config_path=str(config_file))

    assert result == 0
    assert mock_client.get.await_count == 2


@pytest.mark.asyncio
async def test_discover_sources_no_suggestions(config_file: Path) -> None:
    """discover_sources should handle empty LLM response gracefully."""
    from src.main import discover_sources

    with patch("src.main.get_provider") as mock_get_provider:
        mock_provider = MagicMock()
        mock_provider.summarize = AsyncMock(return_value="No good feeds found.")
        mock_get_provider.return_value = mock_provider

        result = await discover_sources(config_path=str(config_file))

    assert result == 0


@pytest.mark.asyncio
async def test_discover_sources_blocks_unsafe_urls(config_file: Path) -> None:
    """discover_sources must not fetch private/localhost URLs from LLM output."""
    from src.main import _ValidatedURL, discover_sources

    llm_response = (
        "FEED|http://169.254.169.254/latest/meta-data/|Cloud|Metadata\n"
        "FEED|http://localhost:6379/|Internal|Redis\n"
        "FEED|https://example.com/feed.xml|Tech|Safe Feed\n"
    )

    fake_addrinfos = [
        (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", 0)),
    ]

    def fake_validate(url: str) -> _ValidatedURL | None:
        if "169.254" in url or "localhost" in url:
            return None
        return _ValidatedURL(
            url=url, hostname="example.com", pinned_addrinfos=fake_addrinfos
        )

    mock_client = AsyncMock()
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_client.get = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("src.main.get_provider") as mock_get_provider,
        patch("httpx.AsyncClient", return_value=mock_client),
        patch("src.main._validate_url", side_effect=fake_validate),
    ):
        mock_provider = MagicMock()
        mock_provider.summarize = AsyncMock(return_value=llm_response)
        mock_get_provider.return_value = mock_provider

        result = await discover_sources(config_path=str(config_file))

    assert result == 0
    # Only the safe URL should be fetched; the two unsafe ones are blocked
    assert mock_client.get.await_count == 1


def test_validate_url_blocks_private_ips() -> None:
    from unittest.mock import patch

    from src.main import _validate_url

    fake_addrinfos = [
        (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", 0)),
    ]

    with patch("socket.getaddrinfo", return_value=fake_addrinfos):
        # Safe URLs return a _ValidatedURL with pinned addrinfos
        result = _validate_url("https://example.com/feed")
        assert result is not None
        assert result.url == "https://example.com/feed"
        assert result.hostname == "example.com"
        assert len(result.pinned_addrinfos) > 0
    # Unsafe URLs return None (no DNS needed for these checks)
    assert _validate_url("http://localhost/foo") is None
    assert _validate_url("http://127.0.0.1/foo") is None
    assert _validate_url("http://169.254.169.254/latest") is None
    assert _validate_url("http://10.0.0.1/internal") is None
    assert _validate_url("http://192.168.1.1/admin") is None
    assert _validate_url("ftp://example.com/feed") is None
    assert _validate_url("http://metadata.google.internal/v1") is None
    assert _validate_url("") is None
    # Multicast and CGNAT addresses must be blocked
    assert _validate_url("http://224.0.0.1/feed") is None
    assert _validate_url("http://100.64.0.1/feed") is None
    # Out-of-range port must not crash, just return None
    assert _validate_url("https://example.com:99999/feed") is None


def test_pin_dns_overrides_resolution() -> None:
    """_pin_dns should override socket.getaddrinfo for the pinned hostname."""
    import socket

    from src.main import _pin_dns

    fake_addrinfos = [
        (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", 0)),
    ]

    with _pin_dns("pintest.example.com", fake_addrinfos):
        result = socket.getaddrinfo("pintest.example.com", 443)
        assert len(result) == 1
        assert result[0][4] == ("93.184.216.34", 443)

    # After context exit, original resolution is restored — verify by checking
    # the patched getaddrinfo is no longer in effect (calling with a different
    # host should not return our fake addrinfos)
    with unittest.mock.patch("socket.getaddrinfo", return_value=[
        (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("1.2.3.4", 80)),
    ]) as mock_gai:
        original_result = socket.getaddrinfo("example.com", 80)
        assert len(original_result) > 0
        mock_gai.assert_called_once()


def test_pin_dns_overrides_resolution_bytes_host() -> None:
    """_pin_dns should also pin when host is passed as bytes (httpx/httpcore path)."""
    import socket

    from src.main import _pin_dns

    fake_addrinfos = [
        (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", 0)),
    ]

    with _pin_dns("pintest.example.com", fake_addrinfos):
        result = socket.getaddrinfo(b"pintest.example.com", 443)
        assert len(result) == 1
        assert result[0][4] == ("93.184.216.34", 443)


def test_prune_digest_sources_map_under_limit() -> None:
    """Pruning should not modify map when it's under max_entries."""
    from src.main import _prune_digest_sources_map

    mapping = {
        "2026-03-01": ["Feed1"],
        "2026-03-05": ["Feed2"],
        "2026-03-10": ["Feed3"],
    }
    _prune_digest_sources_map(mapping, max_entries=5)
    # All 3 entries should remain (3 < 5)
    assert len(mapping) == 3


def test_prune_digest_sources_map_exceeds_limit() -> None:
    """Pruning should remove oldest entries when exceeding max_entries."""
    from src.main import _prune_digest_sources_map

    mapping = {
        "2026-03-01": ["Feed1"],
        "2026-03-02": ["Feed2"],
        "2026-03-03": ["Feed3"],
        "2026-03-04": ["Feed4"],
        "2026-03-05": ["Feed5"],
    }
    _prune_digest_sources_map(mapping, max_entries=3)
    # Should keep only the 3 newest entries (by key sort order)
    assert len(mapping) == 3
    # Oldest two entries (2026-03-01, 2026-03-02) should be removed
    assert "2026-03-01" not in mapping
    assert "2026-03-02" not in mapping
    # Newest three should remain
    assert "2026-03-03" in mapping
    assert "2026-03-04" in mapping
    assert "2026-03-05" in mapping


def test_prune_digest_sources_map_preserves_newest() -> None:
    """Pruning should preserve the entries with highest sort order (newest)."""
    from src.main import _prune_digest_sources_map

    mapping = {
        "2026-01-01": ["Old"],
        "2026-03-18_120000": ["Recent1"],
        "2026-03-19_090000": ["Recent2"],
        "2026-03-20_140000": ["Recent3"],
    }
    _prune_digest_sources_map(mapping, max_entries=2)
    # Should keep 2 newest (highest sort order)
    assert len(mapping) == 2
    assert "2026-03-19_090000" in mapping
    assert "2026-03-20_140000" in mapping
    assert "2026-01-01" not in mapping


@pytest.mark.asyncio
async def test_discover_sources_malformed_lines(config_file: Path) -> None:
    """discover_sources should skip lines with wrong number of fields."""
    from src.main import _ValidatedURL, discover_sources

    llm_response = (
        "FEED|only-two-parts|Category\n"
        "FEED|https://ok.com/feed|Cat|Name\n"
        "NOT_FEED|something\n"
    )

    fake_addrinfos = [
        (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", 0)),
    ]

    def fake_validate(url: str) -> _ValidatedURL | None:
        if url.startswith("https://"):
            return _ValidatedURL(
                url=url, hostname="ok.com", pinned_addrinfos=fake_addrinfos
            )
        return None

    mock_client = AsyncMock()
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_client.get = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("src.main.get_provider") as mock_get_provider,
        patch("httpx.AsyncClient", return_value=mock_client),
        patch("src.main._validate_url", side_effect=fake_validate),
    ):
        mock_provider = MagicMock()
        mock_provider.summarize = AsyncMock(return_value=llm_response)
        mock_get_provider.return_value = mock_provider

        result = await discover_sources(config_path=str(config_file))

    assert result == 0
    # Only one valid FEED line -> only 1 GET request
    assert mock_client.get.await_count == 1
