"""Tests for configuration loading and validation (src/config.py v2)."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from digest.config import (
    Config,
    LLMConfig,
    load_config,
)


def _write_config(tmp_path: Path, content: str) -> str:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(textwrap.dedent(content), encoding="utf-8")
    return str(cfg)


MINIMAL_CONFIG = """
    llm:
      providers:
        - name: groq
          model: llama-3.3-70b-versatile
          role: [summarize]
    sources:
      - name: Test Feed
        url: https://example.com/feed
        category: Test
        enabled: true
"""


def test_load_minimal_config(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path, MINIMAL_CONFIG)
    config = load_config(cfg_path)

    assert isinstance(config, Config)
    assert isinstance(config.llm, LLMConfig)
    assert len(config.llm.providers) == 1
    assert config.llm.providers[0].name == "groq"
    assert config.llm.providers[0].model == "llama-3.3-70b-versatile"
    assert config.llm.providers[0].role == ["summarize"]
    assert len(config.sources) == 1
    assert config.sources[0].name == "Test Feed"


def test_defaults_applied(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path, MINIMAL_CONFIG)
    config = load_config(cfg_path)

    assert config.radar.language == "ru"
    assert config.radar.max_articles_per_category == 5
    assert config.radar.summary_style == "analytical"
    assert config.radar.perspectives is False
    assert config.irritator.max_narratives == 5
    assert config.irritator.min_signal_score == 7
    assert config.telegram.enabled is True
    assert config.telegram.split_messages is True
    assert config.obsidian.enabled is True
    assert config.obsidian.output_dir == "digests"
    assert config.filters.blocklist_keywords == []


def test_provider_roles(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path, """
        llm:
          providers:
            - name: groq
              model: llama
              role: [summarize, extract_narratives]
            - name: gemini
              model: gemini-2.5-flash
              role: [generate_queries, rank_signals]
            - name: deepseek
              model: deepseek-chat
              role: [fallback]
        sources:
          - name: Feed
            url: https://example.com/feed
            category: Tech
            enabled: true
    """)
    config = load_config(cfg_path)
    assert len(config.llm.providers) == 3
    assert config.llm.providers[0].role == ["summarize", "extract_narratives"]
    assert config.llm.providers[2].role == ["fallback"]


def test_irritator_custom_config(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path, """
        llm:
          providers:
            - name: groq
              model: llama
              role: [summarize]
        irritator:
          max_narratives: 3
          queries_per_narrative: 2
          top_signals: 5
          min_signal_score: 8
          sources: [hackernews, arxiv]
          reddit_subreddits: [python, rust]
        sources:
          - name: Feed
            url: https://example.com/feed
            category: Tech
            enabled: true
    """)
    config = load_config(cfg_path)
    assert config.irritator.max_narratives == 3
    assert config.irritator.queries_per_narrative == 2
    assert config.irritator.top_signals == 5
    assert config.irritator.min_signal_score == 8
    assert config.irritator.sources == ["hackernews", "arxiv"]
    assert config.irritator.reddit_subreddits == ["python", "rust"]


def test_filters_and_delivery(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path, """
        llm:
          providers:
            - name: groq
              model: llama
              role: [fallback]
        filters:
          blocklist_keywords: [trump, election, sports]
        telegram:
          enabled: false
          split_messages: false
        obsidian:
          enabled: false
          output_dir: my_notes
        sources:
          - name: Feed
            url: https://example.com/feed
            category: Tech
            enabled: true
    """)
    config = load_config(cfg_path)
    assert config.filters.blocklist_keywords == ["trump", "election", "sports"]
    assert config.telegram.enabled is False
    assert config.telegram.split_messages is False
    assert config.obsidian.enabled is False
    assert config.obsidian.output_dir == "my_notes"


def test_source_fields(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path, """
        llm:
          providers:
            - name: groq
              model: llama
              role: [summarize]
        sources:
          - name: High Priority Feed
            url: https://example.com/feed
            category: Architecture
            enabled: true
            priority: 5
            recency_hours: 168
          - name: Disabled Feed
            url: https://example.org/rss
            category: Cloud
            enabled: false
    """)
    config = load_config(cfg_path)
    assert len(config.sources) == 2
    assert config.sources[0].priority == 5
    assert config.sources[0].recency_hours == 168
    assert config.sources[1].enabled is False
    assert config.sources[1].priority == 3  # default
    assert config.sources[1].recency_hours == 24  # default


def test_enabled_sources_property(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path, """
        llm:
          providers:
            - name: groq
              model: llama
              role: [summarize]
        sources:
          - name: Active
            url: https://example.com/feed
            category: Tech
            enabled: true
          - name: Inactive
            url: https://example.org/feed
            category: Tech
            enabled: false
    """)
    config = load_config(cfg_path)
    assert len(config.enabled_sources) == 1
    assert config.enabled_sources[0].name == "Active"


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


def test_missing_llm_section(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path, """
        sources:
          - name: Feed
            url: https://example.com/feed
            category: Tech
            enabled: true
    """)
    with pytest.raises(ValueError, match="llm"):
        load_config(cfg_path)


def test_invalid_provider_name(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path, """
        llm:
          providers:
            - name: invalidprovider
              model: some-model
              role: [summarize]
        sources:
          - name: Feed
            url: https://example.com/feed
            category: Tech
            enabled: true
    """)
    with pytest.raises(ValueError, match="invalidprovider"):
        load_config(cfg_path)


def test_invalid_role(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path, """
        llm:
          providers:
            - name: groq
              model: llama
              role: [summarize, invalid_role]
        sources:
          - name: Feed
            url: https://example.com/feed
            category: Tech
            enabled: true
    """)
    with pytest.raises(ValueError, match="invalid_role"):
        load_config(cfg_path)


def test_no_summarize_or_fallback_role(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path, """
        llm:
          providers:
            - name: groq
              model: llama
              role: [generate_queries]
        sources:
          - name: Feed
            url: https://example.com/feed
            category: Tech
            enabled: true
    """)
    with pytest.raises(ValueError, match="summarize.*fallback|fallback.*summarize"):
        load_config(cfg_path)


def test_duplicate_provider(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path, """
        llm:
          providers:
            - name: groq
              model: llama
              role: [summarize]
            - name: groq
              model: llama2
              role: [fallback]
        sources:
          - name: Feed
            url: https://example.com/feed
            category: Tech
            enabled: true
    """)
    with pytest.raises(ValueError, match="[Dd]uplicate"):
        load_config(cfg_path)


def test_invalid_radar_language(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path, """
        llm:
          providers:
            - name: groq
              model: llama
              role: [summarize]
        radar:
          language: fr
        sources:
          - name: Feed
            url: https://example.com/feed
            category: Tech
            enabled: true
    """)
    with pytest.raises(ValueError, match="fr"):
        load_config(cfg_path)


def test_invalid_summary_style(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path, """
        llm:
          providers:
            - name: groq
              model: llama
              role: [summarize]
        radar:
          summary_style: verbose
        sources:
          - name: Feed
            url: https://example.com/feed
            category: Tech
            enabled: true
    """)
    with pytest.raises(ValueError, match="verbose"):
        load_config(cfg_path)


def test_invalid_irritator_source(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path, """
        llm:
          providers:
            - name: groq
              model: llama
              role: [summarize]
        irritator:
          sources: [hackernews, twitter]
        sources:
          - name: Feed
            url: https://example.com/feed
            category: Tech
            enabled: true
    """)
    with pytest.raises(ValueError, match="twitter"):
        load_config(cfg_path)


def test_irritator_signal_score_out_of_range(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path, """
        llm:
          providers:
            - name: groq
              model: llama
              role: [summarize]
        irritator:
          min_signal_score: 11
        sources:
          - name: Feed
            url: https://example.com/feed
            category: Tech
            enabled: true
    """)
    with pytest.raises(ValueError, match="11"):
        load_config(cfg_path)


def test_missing_sources(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path, """
        llm:
          providers:
            - name: groq
              model: llama
              role: [summarize]
    """)
    with pytest.raises(ValueError, match="sources"):
        load_config(cfg_path)


def test_empty_sources(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path, """
        llm:
          providers:
            - name: groq
              model: llama
              role: [summarize]
        sources: []
    """)
    with pytest.raises(ValueError):
        load_config(cfg_path)


def test_invalid_url_scheme(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path, """
        llm:
          providers:
            - name: groq
              model: llama
              role: [summarize]
        sources:
          - name: Bad Feed
            url: ftp://example.com/feed
            category: Tech
            enabled: true
    """)
    with pytest.raises(ValueError, match="[Uu]RL|scheme"):
        load_config(cfg_path)


def test_priority_out_of_range(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path, """
        llm:
          providers:
            - name: groq
              model: llama
              role: [summarize]
        sources:
          - name: Feed
            url: https://example.com/feed
            category: Tech
            enabled: true
            priority: 6
    """)
    with pytest.raises(ValueError, match="priority"):
        load_config(cfg_path)


def test_duplicate_source_name(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path, """
        llm:
          providers:
            - name: groq
              model: llama
              role: [summarize]
        sources:
          - name: Same Feed
            url: https://example.com/feed
            category: Tech
            enabled: true
          - name: Same Feed
            url: https://example.org/feed
            category: Cloud
            enabled: true
    """)
    with pytest.raises(ValueError, match="[Dd]uplicate"):
        load_config(cfg_path)


def test_file_not_found() -> None:
    with pytest.raises(FileNotFoundError):
        load_config("/nonexistent/path/config.yaml")


def test_invalid_yaml(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("key: [unclosed bracket", encoding="utf-8")
    with pytest.raises(ValueError, match="[Yy]AML|yaml"):
        load_config(str(cfg_path))


def test_non_mapping_yaml(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("- item1\n- item2\n", encoding="utf-8")
    with pytest.raises(ValueError, match="mapping"):
        load_config(str(cfg_path))


def test_effective_sources_filters_demoted(tmp_path: Path) -> None:
    """Config.effective_sources excludes sources marked demoted in cache."""
    from digest.source_scorer import SourceStateStore

    cfg_path = _write_config(tmp_path, """
        llm:
          providers:
            - name: groq
              model: llama-3.3-70b-versatile
              role: [summarize]
        sources:
          - name: GoodFeed
            url: https://example.com/feed
            category: Test
            enabled: true
          - name: DemotedFeed
            url: https://example.com/other
            category: Test
            enabled: true
    """)
    config = load_config(cfg_path)

    store = SourceStateStore()
    store.mark_demoted("DemotedFeed")

    effective = config.effective_sources(store)
    assert len(effective) == 1
    assert effective[0].name == "GoodFeed"


def test_effective_sources_empty_state_same_as_enabled(tmp_path: Path) -> None:
    """With empty cache, effective_sources equals enabled_sources."""
    from digest.source_scorer import SourceStateStore

    cfg_path = _write_config(tmp_path, MINIMAL_CONFIG)
    config = load_config(cfg_path)

    store = SourceStateStore()
    assert config.effective_sources(store) == config.enabled_sources
