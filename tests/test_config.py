"""Tests for config loading and validation."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from src.config import (
    Config,
    load_config,
)


def _write_config(tmp_path: Path, content: str) -> Path:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(textwrap.dedent(content), encoding="utf-8")
    return cfg


MINIMAL_CONFIG = """
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
      - name: "Test Feed"
        url: "https://example.com/feed"
        category: "Test"
        enabled: true
"""


def test_load_minimal_config(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path, MINIMAL_CONFIG)
    config = load_config(cfg_path)

    assert isinstance(config, Config)
    assert config.llm.provider == "anthropic"
    assert config.llm.model == "claude-sonnet-4-20250514"
    assert config.delivery.telegram is False
    assert config.delivery.markdown_to_repo is False
    assert config.delivery.markdown_dir == "digests"
    assert config.digest.language == "ru"
    assert config.digest.max_articles_per_source == 5
    assert config.digest.max_total_articles == 30
    assert config.digest.summary_style == "analytical"
    assert len(config.sources) == 1
    assert config.sources[0].name == "Test Feed"
    assert config.sources[0].enabled is True


def test_enabled_sources_filter(tmp_path: Path) -> None:
    content = """
        llm:
          provider: "gemini"
          model: "gemini-2.5-flash"
        delivery:
          telegram: false
          markdown_to_repo: false
        digest:
          language: "en"
          max_articles_per_source: 3
          max_total_articles: 20
          summary_style: "brief"
        sources:
          - name: "Enabled"
            url: "https://example.com/a"
            category: "Cat"
            enabled: true
          - name: "Disabled"
            url: "https://example.com/b"
            category: "Cat"
            enabled: false
    """
    cfg_path = _write_config(tmp_path, content)
    config = load_config(cfg_path)
    assert len(config.sources) == 2
    assert len(config.enabled_sources) == 1
    assert config.enabled_sources[0].name == "Enabled"


def test_default_markdown_dir(tmp_path: Path) -> None:
    content = """
        llm:
          provider: "groq"
          model: "llama-3.3-70b-versatile"
        delivery:
          telegram: false
          markdown_to_repo: false
        digest:
          language: "ru"
          max_articles_per_source: 5
          max_total_articles: 30
          summary_style: "analytical"
        sources:
          - name: "Feed"
            url: "https://example.com/feed"
            category: "Test"
            enabled: true
    """
    cfg_path = _write_config(tmp_path, content)
    config = load_config(cfg_path)
    assert config.delivery.markdown_dir == "digests"


def test_file_not_found() -> None:
    with pytest.raises(FileNotFoundError, match="Config file not found"):
        load_config("/nonexistent/path/config.yaml")


def test_missing_llm_section(tmp_path: Path) -> None:
    content = """
        delivery:
          telegram: false
          markdown_to_repo: false
        digest:
          language: "ru"
          max_articles_per_source: 5
          max_total_articles: 30
          summary_style: "analytical"
        sources: []
    """
    cfg_path = _write_config(tmp_path, content)
    with pytest.raises(ValueError, match="Missing required config field 'llm'"):
        load_config(cfg_path)


def test_missing_provider(tmp_path: Path) -> None:
    content = """
        llm:
          model: "claude-sonnet-4-20250514"
        delivery:
          telegram: false
          markdown_to_repo: false
        digest:
          language: "ru"
          max_articles_per_source: 5
          max_total_articles: 30
          summary_style: "analytical"
        sources: []
    """
    cfg_path = _write_config(tmp_path, content)
    with pytest.raises(ValueError, match="Missing required config field 'provider'"):
        load_config(cfg_path)


def test_invalid_provider(tmp_path: Path) -> None:
    content = """
        llm:
          provider: "openai"
          model: "gpt-4"
        delivery:
          telegram: false
          markdown_to_repo: false
        digest:
          language: "ru"
          max_articles_per_source: 5
          max_total_articles: 30
          summary_style: "analytical"
        sources: []
    """
    cfg_path = _write_config(tmp_path, content)
    with pytest.raises(ValueError, match="Invalid llm.provider"):
        load_config(cfg_path)


def test_invalid_summary_style(tmp_path: Path) -> None:
    content = """
        llm:
          provider: "anthropic"
          model: "claude-sonnet-4-20250514"
        delivery:
          telegram: false
          markdown_to_repo: false
        digest:
          language: "ru"
          max_articles_per_source: 5
          max_total_articles: 30
          summary_style: "fancy"
        sources: []
    """
    cfg_path = _write_config(tmp_path, content)
    with pytest.raises(ValueError, match="Invalid digest.summary_style"):
        load_config(cfg_path)


def test_invalid_language(tmp_path: Path) -> None:
    content = """
        llm:
          provider: "anthropic"
          model: "claude-sonnet-4-20250514"
        delivery:
          telegram: false
          markdown_to_repo: false
        digest:
          language: "de"
          max_articles_per_source: 5
          max_total_articles: 30
          summary_style: "brief"
        sources: []
    """
    cfg_path = _write_config(tmp_path, content)
    with pytest.raises(ValueError, match="Invalid digest.language"):
        load_config(cfg_path)


def test_missing_source_url(tmp_path: Path) -> None:
    content = """
        llm:
          provider: "anthropic"
          model: "claude-sonnet-4-20250514"
        delivery:
          telegram: false
          markdown_to_repo: false
        digest:
          language: "ru"
          max_articles_per_source: 5
          max_total_articles: 30
          summary_style: "analytical"
        sources:
          - name: "No URL"
            category: "Test"
            enabled: true
    """
    cfg_path = _write_config(tmp_path, content)
    with pytest.raises(ValueError, match="Missing required config field 'url'"):
        load_config(cfg_path)


def test_all_providers_accepted(tmp_path: Path) -> None:
    for provider in ("anthropic", "gemini", "groq"):
        content = f"""
            llm:
              provider: "{provider}"
              model: "some-model"
            delivery:
              telegram: false
              markdown_to_repo: false
            digest:
              language: "ru"
              max_articles_per_source: 5
              max_total_articles: 30
              summary_style: "analytical"
            sources: []
        """
        cfg_path = _write_config(tmp_path, content)
        config = load_config(cfg_path)
        assert config.llm.provider == provider


def test_default_summary_style_and_language(tmp_path: Path) -> None:
    content = """
        llm:
          provider: "anthropic"
          model: "claude-haiku-4-5-20251001"
        delivery:
          telegram: false
          markdown_to_repo: false
        digest:
          language: "ru"
          max_articles_per_source: 5
          max_total_articles: 30
        sources: []
    """
    cfg_path = _write_config(tmp_path, content)
    config = load_config(cfg_path)
    assert config.digest.summary_style == "analytical"
