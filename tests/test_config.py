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


def test_source_default_priority(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path, MINIMAL_CONFIG)
    config = load_config(cfg_path)
    assert config.sources[0].priority == 3


def test_source_explicit_priority(tmp_path: Path) -> None:
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
          - name: "High Priority Feed"
            url: "https://example.com/feed"
            category: "Test"
            enabled: true
            priority: 5
    """
    cfg_path = _write_config(tmp_path, content)
    config = load_config(cfg_path)
    assert config.sources[0].priority == 5


def test_source_priority_out_of_range(tmp_path: Path) -> None:
    for bad_priority in (0, 6):
        content = f"""
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
              - name: "Feed"
                url: "https://example.com/feed"
                category: "Test"
                enabled: true
                priority: {bad_priority}
        """
        cfg_path = _write_config(tmp_path, content)
        with pytest.raises(ValueError, match="priority.*must be between 1 and 5"):
            load_config(cfg_path)


def test_source_priority_bool_rejected(tmp_path: Path) -> None:
    """YAML boolean values (true/false) must be rejected as priority even though bool is a subclass of int."""
    for bad_priority in ("true", "false"):
        content = f"""
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
              - name: "Feed"
                url: "https://example.com/feed"
                category: "Test"
                enabled: true
                priority: {bad_priority}
        """
        cfg_path = _write_config(tmp_path, content)
        with pytest.raises(ValueError, match="priority.*must be an integer"):
            load_config(cfg_path)


def test_duplicate_source_names_rejected(tmp_path: Path) -> None:
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
          - name: "Feed A"
            url: "https://example.com/a"
            category: "Test"
            enabled: true
          - name: "Feed A"
            url: "https://example.com/b"
            category: "Test"
            enabled: true
    """
    cfg_path = _write_config(tmp_path, content)
    with pytest.raises(ValueError, match="Duplicate source name"):
        load_config(cfg_path)


def test_source_trial_defaults(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path, MINIMAL_CONFIG)
    config = load_config(cfg_path)
    assert config.sources[0].trial is False
    assert config.sources[0].trial_started is None
    assert config.sources[0].trial_days == 7


def test_source_trial_fields(tmp_path: Path) -> None:
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
          - name: "Trial Feed"
            url: "https://example.com/feed"
            category: "Test"
            enabled: true
            trial: true
            trial_started: "2026-03-10"
            trial_days: 14
    """
    cfg_path = _write_config(tmp_path, content)
    config = load_config(cfg_path)
    src = config.sources[0]
    assert src.trial is True
    assert src.trial_started == "2026-03-10"
    assert src.trial_days == 14


def test_adaptive_config_defaults(tmp_path: Path) -> None:
    cfg_path = _write_config(tmp_path, MINIMAL_CONFIG)
    config = load_config(cfg_path)
    assert config.adaptive.enabled is False
    assert config.adaptive.feedback_weight == 0.3
    assert config.adaptive.score_weight == 0.5
    assert config.adaptive.base_weight == 0.2
    assert config.adaptive.trial_slots == 2
    assert config.adaptive.min_priority == 1
    assert config.adaptive.max_priority == 5


def test_adaptive_config_explicit(tmp_path: Path) -> None:
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
        sources: []
        adaptive:
          enabled: true
          feedback_weight: 0.4
          score_weight: 0.4
          base_weight: 0.2
          trial_slots: 3
    """
    cfg_path = _write_config(tmp_path, content)
    config = load_config(cfg_path)
    assert config.adaptive.enabled is True
    assert config.adaptive.feedback_weight == 0.4
    assert config.adaptive.score_weight == 0.4
    assert config.adaptive.trial_slots == 3


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


def test_adaptive_min_priority_gte_max_rejected(tmp_path: Path) -> None:
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
        sources: []
        adaptive:
          enabled: true
          min_priority: 5
          max_priority: 1
    """
    cfg_path = _write_config(tmp_path, content)
    with pytest.raises(ValueError, match="min_priority.*must be less than.*max_priority"):
        load_config(cfg_path)


def test_adaptive_negative_trial_slots_rejected(tmp_path: Path) -> None:
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
        sources: []
        adaptive:
          enabled: true
          trial_slots: -1
    """
    cfg_path = _write_config(tmp_path, content)
    with pytest.raises(ValueError, match="trial_slots must be non-negative"):
        load_config(cfg_path)


def test_trial_days_zero_rejected(tmp_path: Path) -> None:
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
          - name: "Feed"
            url: "https://example.com/feed"
            category: "Test"
            enabled: true
            trial: true
            trial_days: 0
    """
    cfg_path = _write_config(tmp_path, content)
    with pytest.raises(ValueError, match="trial_days.*must be >= 1"):
        load_config(cfg_path)


def test_max_articles_per_source_zero_rejected(tmp_path: Path) -> None:
    content = """
        llm:
          provider: "anthropic"
          model: "claude-sonnet-4-20250514"
        delivery:
          telegram: false
          markdown_to_repo: false
        digest:
          language: "ru"
          max_articles_per_source: 0
          max_total_articles: 30
          summary_style: "analytical"
        sources: []
    """
    cfg_path = _write_config(tmp_path, content)
    with pytest.raises(ValueError, match="max_articles_per_source.*must be >= 1"):
        load_config(cfg_path)


def test_max_total_articles_negative_rejected(tmp_path: Path) -> None:
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
          max_total_articles: -1
          summary_style: "analytical"
        sources: []
    """
    cfg_path = _write_config(tmp_path, content)
    with pytest.raises(ValueError, match="max_total_articles.*must be >= 1"):
        load_config(cfg_path)


def test_adaptive_weights_not_summing_to_one_rejected(tmp_path: Path) -> None:
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
        sources: []
        adaptive:
          enabled: true
          feedback_weight: 0.5
          score_weight: 0.5
          base_weight: 0.5
    """
    cfg_path = _write_config(tmp_path, content)
    with pytest.raises(ValueError, match="Adaptive weights must sum to 1.0"):
        load_config(cfg_path)


def test_adaptive_negative_weight_rejected(tmp_path: Path) -> None:
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
        sources: []
        adaptive:
          enabled: true
          feedback_weight: -0.3
          score_weight: 1.1
          base_weight: 0.2
    """
    cfg_path = _write_config(tmp_path, content)
    with pytest.raises(ValueError, match="must be between 0.0 and 1.0"):
        load_config(cfg_path)


def test_adaptive_weight_above_one_rejected(tmp_path: Path) -> None:
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
        sources: []
        adaptive:
          enabled: true
          feedback_weight: 1.5
          score_weight: 0.0
          base_weight: 0.0
    """
    cfg_path = _write_config(tmp_path, content)
    with pytest.raises(ValueError, match="must be between 0.0 and 1.0"):
        load_config(cfg_path)


def test_adaptive_min_priority_below_one_rejected(tmp_path: Path) -> None:
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
        sources: []
        adaptive:
          enabled: true
          min_priority: -1
          max_priority: 5
    """
    cfg_path = _write_config(tmp_path, content)
    with pytest.raises(ValueError, match="min_priority must be >= 1"):
        load_config(cfg_path)


def test_adaptive_weights_exactly_sum_to_one(tmp_path: Path) -> None:
    """Weights that sum to exactly 1.0 should be accepted."""
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
        sources: []
        adaptive:
          enabled: true
          feedback_weight: 0.2
          score_weight: 0.3
          base_weight: 0.5
    """
    cfg_path = _write_config(tmp_path, content)
    config = load_config(cfg_path)
    assert config.adaptive.feedback_weight == 0.2
    assert config.adaptive.score_weight == 0.3
    assert config.adaptive.base_weight == 0.5


def test_adaptive_weight_at_boundary_zero(tmp_path: Path) -> None:
    """Weight of exactly 0.0 should be accepted."""
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
        sources: []
        adaptive:
          enabled: true
          feedback_weight: 0.0
          score_weight: 0.7
          base_weight: 0.3
    """
    cfg_path = _write_config(tmp_path, content)
    config = load_config(cfg_path)
    assert config.adaptive.feedback_weight == 0.0


def test_adaptive_weight_at_boundary_one(tmp_path: Path) -> None:
    """Weight of exactly 1.0 is technically allowed but weights must sum to 1.0."""
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
        sources: []
        adaptive:
          enabled: true
          feedback_weight: 1.0
          score_weight: 0.0
          base_weight: 0.0
    """
    cfg_path = _write_config(tmp_path, content)
    config = load_config(cfg_path)
    assert config.adaptive.feedback_weight == 1.0


def test_adaptive_min_equals_max_minus_one_allowed(tmp_path: Path) -> None:
    """min_priority = max_priority - 1 should be allowed (smallest valid range)."""
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
        sources: []
        adaptive:
          enabled: true
          min_priority: 4
          max_priority: 5
    """
    cfg_path = _write_config(tmp_path, content)
    config = load_config(cfg_path)
    assert config.adaptive.min_priority == 4
    assert config.adaptive.max_priority == 5
