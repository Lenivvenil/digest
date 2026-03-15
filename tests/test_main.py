"""Tests for src/main.py — entrypoint, CLI argument parsing, dry-run mode."""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.collector import Article
from src.main import _parse_args, main, run


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


@pytest.fixture()
def config_file(tmp_path: Path) -> Path:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(MINIMAL_CONFIG, encoding="utf-8")
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
        patch("src.main.collect", new_callable=AsyncMock, return_value=sample_articles),
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
    assert stats.articles_collected == 1


@pytest.mark.asyncio
async def test_run_no_articles(config_file: Path) -> None:
    """When no articles are found, summarizer and delivery must not be called."""
    with (
        patch("src.main.collect", new_callable=AsyncMock, return_value={}),
        patch("src.main.get_provider") as mock_get_provider,
        patch("src.main.send_digest", new_callable=AsyncMock) as mock_send,
        patch("src.main.write_digest") as mock_write,
    ):
        stats = await run(config_path=str(config_file), dry_run=False)

    mock_get_provider.assert_not_called()
    mock_send.assert_not_called()
    mock_write.assert_not_called()
    assert stats.articles_collected == 0
    assert stats.digest_length == 0


@pytest.mark.asyncio
async def test_run_full_pipeline(config_file: Path, sample_articles: dict, tmp_path: Path) -> None:
    """Full pipeline (no dry-run): Telegram send and markdown write are invoked."""
    markdown_path = tmp_path / "digests" / "2026-03-15.md"

    with (
        patch("src.main.collect", new_callable=AsyncMock, return_value=sample_articles),
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
        patch("src.main.collect", new_callable=AsyncMock, return_value=sample_articles),
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
        patch("src.main.collect", new_callable=AsyncMock, return_value=sample_articles),
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
        patch("src.main.collect", new_callable=AsyncMock, return_value=sample_articles),
        patch("src.main.get_provider") as mock_get_provider,
        patch("src.main.send_digest", new_callable=AsyncMock, side_effect=Exception("Network error")),
        patch("src.main.write_digest", return_value=markdown_path),
    ):
        mock_provider = MagicMock()
        mock_provider.summarize = AsyncMock(return_value="Summary")
        mock_get_provider.return_value = mock_provider

        exit_code = await main(["--config", str(config_file)])

    assert exit_code == 0
