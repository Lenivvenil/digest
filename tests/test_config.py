"""Tests for config loading and validation."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from src.config import (
    Config,
    LLMConfig,
    ProviderConfig,
    RouteConfig,
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
        sources:
          - name: "Feed"
            url: "https://example.com/feed"
            category: "Test"
            enabled: true
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
        sources:
          - name: "Feed"
            url: "https://example.com/feed"
            category: "Test"
            enabled: true
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
        sources:
          - name: "Feed"
            url: "https://example.com/feed"
            category: "Test"
            enabled: true
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
        sources:
          - name: "Feed"
            url: "https://example.com/feed"
            category: "Test"
            enabled: true
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
        sources:
          - name: "Feed"
            url: "https://example.com/feed"
            category: "Test"
            enabled: true
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
            sources:
              - name: "Feed"
                url: "https://example.com/feed"
                category: "Test"
                enabled: true
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
        sources:
          - name: "Feed"
            url: "https://example.com/feed"
            category: "Test"
            enabled: true
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
        sources:
          - name: "Feed"
            url: "https://example.com/feed"
            category: "Test"
            enabled: true
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
        sources:
          - name: "Feed"
            url: "https://example.com/feed"
            category: "Test"
            enabled: true
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
        sources:
          - name: "Feed"
            url: "https://example.com/feed"
            category: "Test"
            enabled: true
        adaptive:
          enabled: true
          trial_slots: -1
    """
    cfg_path = _write_config(tmp_path, content)
    with pytest.raises(ValueError, match="trial_slots must be non-negative"):
        load_config(cfg_path)


def test_adaptive_trial_slots_equal_to_total_articles_accepted(tmp_path: Path) -> None:
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
          max_total_articles: 10
          summary_style: "analytical"
        sources:
          - name: "Feed"
            url: "https://example.com/feed"
            category: "Test"
            enabled: true
        adaptive:
          enabled: true
          trial_slots: 10
    """
    cfg_path = _write_config(tmp_path, content)
    config = load_config(cfg_path)
    assert config.adaptive.trial_slots == 10


def test_adaptive_trial_slots_exceeds_total_articles_rejected(tmp_path: Path) -> None:
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
          max_total_articles: 10
          summary_style: "analytical"
        sources:
          - name: "Feed"
            url: "https://example.com/feed"
            category: "Test"
            enabled: true
        adaptive:
          enabled: true
          trial_slots: 11
    """
    cfg_path = _write_config(tmp_path, content)
    with pytest.raises(ValueError, match="trial_slots.*must not exceed.*max_total_articles"):
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
        sources:
          - name: "Feed"
            url: "https://example.com/feed"
            category: "Test"
            enabled: true
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
        sources:
          - name: "Feed"
            url: "https://example.com/feed"
            category: "Test"
            enabled: true
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
        sources:
          - name: "Feed"
            url: "https://example.com/feed"
            category: "Test"
            enabled: true
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
        sources:
          - name: "Feed"
            url: "https://example.com/feed"
            category: "Test"
            enabled: true
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
        sources:
          - name: "Feed"
            url: "https://example.com/feed"
            category: "Test"
            enabled: true
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
        sources:
          - name: "Feed"
            url: "https://example.com/feed"
            category: "Test"
            enabled: true
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
        sources:
          - name: "Feed"
            url: "https://example.com/feed"
            category: "Test"
            enabled: true
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
        sources:
          - name: "Feed"
            url: "https://example.com/feed"
            category: "Test"
            enabled: true
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
        sources:
          - name: "Feed"
            url: "https://example.com/feed"
            category: "Test"
            enabled: true
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
        sources:
          - name: "Feed"
            url: "https://example.com/feed"
            category: "Test"
            enabled: true
        adaptive:
          enabled: true
          min_priority: 4
          max_priority: 5
    """
    cfg_path = _write_config(tmp_path, content)
    config = load_config(cfg_path)
    assert config.adaptive.min_priority == 4
    assert config.adaptive.max_priority == 5


def test_source_recency_hours_default(tmp_path: Path) -> None:
    """recency_hours should default to 24 when not specified."""
    cfg_path = _write_config(tmp_path, MINIMAL_CONFIG)
    config = load_config(cfg_path)
    assert config.sources[0].recency_hours == 24


def test_source_recency_hours_custom(tmp_path: Path) -> None:
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
          - name: "Weekly Feed"
            url: "https://example.com/feed"
            category: "Test"
            enabled: true
            recency_hours: 168
    """
    cfg_path = _write_config(tmp_path, content)
    config = load_config(cfg_path)
    assert config.sources[0].recency_hours == 168


def test_source_recency_hours_zero_rejected(tmp_path: Path) -> None:
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
            recency_hours: 0
    """
    cfg_path = _write_config(tmp_path, content)
    with pytest.raises(ValueError, match="recency_hours"):
        load_config(cfg_path)


def test_source_recency_hours_negative_rejected(tmp_path: Path) -> None:
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
            recency_hours: -1
    """
    cfg_path = _write_config(tmp_path, content)
    with pytest.raises(ValueError, match="recency_hours"):
        load_config(cfg_path)


# ---------------------------------------------------------------------------
# Sprint 5: validation hardening
# ---------------------------------------------------------------------------


def test_yaml_error(tmp_path: Path) -> None:
    """Invalid YAML raises ValueError with a descriptive message."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text("key: [unclosed bracket", encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid YAML"):
        load_config(cfg)


def test_non_numeric_int(tmp_path: Path) -> None:
    """Non-integer value for max_articles_per_source raises ValueError."""
    content = MINIMAL_CONFIG.replace(
        "max_articles_per_source: 5", 'max_articles_per_source: "not_a_number"'
    )
    cfg_path = _write_config(tmp_path, content)
    with pytest.raises(ValueError, match="max_articles_per_source"):
        load_config(cfg_path)


def test_file_scheme_rejected(tmp_path: Path) -> None:
    """A source URL with file:// scheme raises ValueError."""
    content = MINIMAL_CONFIG.replace(
        'url: "https://example.com/feed"', 'url: "file:///etc/passwd"'
    )
    cfg_path = _write_config(tmp_path, content)
    with pytest.raises(ValueError, match="URL scheme"):
        load_config(cfg_path)


def test_empty_name(tmp_path: Path) -> None:
    """A source with an empty name raises ValueError."""
    content = MINIMAL_CONFIG.replace('name: "Test Feed"', 'name: ""')
    cfg_path = _write_config(tmp_path, content)
    with pytest.raises(ValueError, match="empty"):
        load_config(cfg_path)


def test_empty_sources(tmp_path: Path) -> None:
    """An empty sources list raises ValueError."""
    old_sources = (
        "    sources:\n      - name: \"Test Feed\"\n        url: \"https://example.com/feed\"\n"
        "        category: \"Test\"\n        enabled: true"
    )
    content = MINIMAL_CONFIG.replace(old_sources, "    sources: []")
    cfg_path = _write_config(tmp_path, content)
    with pytest.raises(ValueError, match="at least one source"):
        load_config(cfg_path)


# ---------------------------------------------------------------------------
# Sprint 6: providers list + routing
# ---------------------------------------------------------------------------

PROVIDERS_BASE = """
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
          - name: "Feed"
            url: "https://example.com/feed"
            category: "Test"
            enabled: true
"""


def test_providers_list_loads(tmp_path: Path) -> None:
    """New providers list format is parsed into LLMConfig.providers."""
    content = """
        llm:
          providers:
            - name: "gemini"
              model: "gemini-2.5-flash"
            - name: "groq"
              model: "llama-3.3-70b-versatile"
    """ + PROVIDERS_BASE
    cfg_path = _write_config(tmp_path, content)
    config = load_config(cfg_path)
    assert isinstance(config.llm, LLMConfig)
    assert len(config.llm.providers) == 2
    assert config.llm.providers[0].name == "gemini"
    assert config.llm.providers[0].model == "gemini-2.5-flash"
    assert config.llm.providers[1].name == "groq"
    # Backward-compat properties read from providers[0]
    assert config.llm.provider == "gemini"
    assert config.llm.model == "gemini-2.5-flash"


def test_legacy_format_backward_compat(tmp_path: Path) -> None:
    """Old provider/model keys auto-convert to providers list with one entry."""
    content = """
        llm:
          provider: "anthropic"
          model: "claude-sonnet-4-20250514"
    """ + PROVIDERS_BASE
    cfg_path = _write_config(tmp_path, content)
    config = load_config(cfg_path)
    assert len(config.llm.providers) == 1
    assert config.llm.providers[0] == ProviderConfig(name="anthropic", model="claude-sonnet-4-20250514")
    assert config.llm.provider == "anthropic"
    assert config.llm.model == "claude-sonnet-4-20250514"


def test_providers_list_empty_rejected(tmp_path: Path) -> None:
    """Empty providers list raises ValueError."""
    content = """
        llm:
          providers: []
    """ + PROVIDERS_BASE
    cfg_path = _write_config(tmp_path, content)
    with pytest.raises(ValueError, match="providers"):
        load_config(cfg_path)


def test_routing_loads(tmp_path: Path) -> None:
    """routing section with valid categories and providers is parsed correctly."""
    content = """
        llm:
          providers:
            - name: "gemini"
              model: "gemini-2.5-flash"
          routing:
            - categories: ["AI & LLM", "AI Engineering"]
              provider: "groq"
              model: "llama-3.3-70b-versatile"
    """ + PROVIDERS_BASE
    cfg_path = _write_config(tmp_path, content)
    config = load_config(cfg_path)
    assert len(config.llm.routing) == 1
    route = config.llm.routing[0]
    assert isinstance(route, RouteConfig)
    assert route.categories == ["AI & LLM", "AI Engineering"]
    assert route.provider == "groq"
    assert route.model == "llama-3.3-70b-versatile"


def test_routing_duplicate_category_rejected(tmp_path: Path) -> None:
    """Same category appearing in two routes raises ValueError."""
    content = """
        llm:
          providers:
            - name: "gemini"
              model: "gemini-2.5-flash"
          routing:
            - categories: ["AI & LLM"]
              provider: "groq"
              model: "llama-3.3-70b-versatile"
            - categories: ["AI & LLM"]
              provider: "gemini"
              model: "gemini-2.5-flash"
    """ + PROVIDERS_BASE
    cfg_path = _write_config(tmp_path, content)
    with pytest.raises(ValueError, match="[Dd]uplicate.*categor|categor.*[Dd]uplicate"):
        load_config(cfg_path)


def test_routing_invalid_provider_rejected(tmp_path: Path) -> None:
    """Provider name not in VALID_PROVIDERS raises ValueError."""
    content = """
        llm:
          providers:
            - name: "gemini"
              model: "gemini-2.5-flash"
          routing:
            - categories: ["AI & LLM"]
              provider: "openai"
              model: "gpt-4"
    """ + PROVIDERS_BASE
    cfg_path = _write_config(tmp_path, content)
    with pytest.raises(ValueError, match="[Ii]nvalid.*provider|provider.*[Ii]nvalid"):
        load_config(cfg_path)


def test_routing_optional(tmp_path: Path) -> None:
    """Config without routing key works fine and defaults to empty list."""
    content = """
        llm:
          providers:
            - name: "gemini"
              model: "gemini-2.5-flash"
    """ + PROVIDERS_BASE
    cfg_path = _write_config(tmp_path, content)
    config = load_config(cfg_path)
    assert config.llm.routing == []


def test_mistral_deepseek_accepted(tmp_path: Path) -> None:
    """mistral and deepseek are valid provider names."""
    for provider in ("mistral", "deepseek"):
        content = f"""
        llm:
          providers:
            - name: "{provider}"
              model: "some-model"
        """ + PROVIDERS_BASE
        cfg_path = _write_config(tmp_path, content)
        load_config(cfg_path)


# ---------------------------------------------------------------------------
# HTML source type validation tests
# ---------------------------------------------------------------------------

_HTML_SOURCE_BASE = """
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
"""


def test_html_source_type_rss_default(tmp_path: Path) -> None:
    """Source without 'type' field defaults to 'rss'."""
    cfg_path = _write_config(tmp_path, MINIMAL_CONFIG)
    config = load_config(cfg_path)
    assert config.sources[0].type == "rss"
    assert config.sources[0].selectors is None


def test_html_source_type_html_valid(tmp_path: Path) -> None:
    """Source with type='html' and valid selectors loads correctly."""
    content = _HTML_SOURCE_BASE + """
    sources:
      - name: "DBS Newsroom"
        url: "https://www.dbs.com/newsroom"
        category: "Banking & Fintech"
        enabled: true
        type: html
        selectors:
          article: ".news-item"
          title: "h3"
          link: "a[href]"
          description: "p.summary"
    """
    cfg_path = _write_config(tmp_path, content)
    config = load_config(cfg_path)
    src = config.sources[0]
    assert src.type == "html"
    assert src.selectors == {
        "article": ".news-item",
        "title": "h3",
        "link": "a[href]",
        "description": "p.summary",
    }


def test_html_source_type_invalid_value(tmp_path: Path) -> None:
    """Source with type='xml' raises ValueError."""
    content = _HTML_SOURCE_BASE + """
    sources:
      - name: "Bad"
        url: "https://example.com"
        category: "Test"
        enabled: true
        type: xml
    """
    cfg_path = _write_config(tmp_path, content)
    with pytest.raises(ValueError, match="must be 'rss' or 'html'"):
        load_config(cfg_path)


def test_html_source_missing_selectors(tmp_path: Path) -> None:
    """Source with type='html' but no selectors raises ValueError."""
    content = _HTML_SOURCE_BASE + """
    sources:
      - name: "NoSelectors"
        url: "https://example.com/newsroom"
        category: "Test"
        enabled: true
        type: html
    """
    cfg_path = _write_config(tmp_path, content)
    with pytest.raises(ValueError, match="selectors.*missing"):
        load_config(cfg_path)


def test_html_source_missing_article_selector(tmp_path: Path) -> None:
    """Source with type='html' but missing 'article' key raises ValueError."""
    content = _HTML_SOURCE_BASE + """
    sources:
      - name: "NoArticle"
        url: "https://example.com/newsroom"
        category: "Test"
        enabled: true
        type: html
        selectors:
          title: "h3"
    """
    cfg_path = _write_config(tmp_path, content)
    with pytest.raises(ValueError, match="'article'"):
        load_config(cfg_path)


def test_html_source_missing_title_selector(tmp_path: Path) -> None:
    """Source with type='html' but missing 'title' key raises ValueError."""
    content = _HTML_SOURCE_BASE + """
    sources:
      - name: "NoTitle"
        url: "https://example.com/newsroom"
        category: "Test"
        enabled: true
        type: html
        selectors:
          article: ".item"
    """
    cfg_path = _write_config(tmp_path, content)
    with pytest.raises(ValueError, match="'title'"):
        load_config(cfg_path)
