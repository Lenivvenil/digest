"""Configuration loading and validation for the daily digest."""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

VALID_PROVIDERS = {"anthropic", "gemini", "groq"}
VALID_SUMMARY_STYLES = {"analytical", "brief", "detailed"}
VALID_LANGUAGES = {"ru", "en"}


@dataclass
class LLMConfig:
    provider: str
    model: str


@dataclass
class DeliveryConfig:
    telegram: bool
    markdown_to_repo: bool
    markdown_dir: str


@dataclass
class DigestConfig:
    language: str
    max_articles_per_source: int
    max_total_articles: int
    summary_style: str


@dataclass
class SourceConfig:
    name: str
    url: str
    category: str
    enabled: bool
    priority: int = 3
    trial: bool = False
    trial_started: str | None = None
    trial_days: int = 7


@dataclass
class AdaptiveConfig:
    enabled: bool
    feedback_weight: float = 0.3
    score_weight: float = 0.5
    base_weight: float = 0.2
    trial_slots: int = 2
    min_priority: int = 1
    max_priority: int = 5


@dataclass
class Config:
    llm: LLMConfig
    delivery: DeliveryConfig
    digest: DigestConfig
    sources: list[SourceConfig] = field(default_factory=list)
    adaptive: AdaptiveConfig = field(
        default_factory=lambda: AdaptiveConfig(enabled=False)
    )

    @property
    def enabled_sources(self) -> list[SourceConfig]:
        return [s for s in self.sources if s.enabled]


def _require(data: dict[str, Any], key: str, section: str) -> Any:
    if key not in data:
        raise ValueError(
            f"Missing required config field '{key}' in section '{section}'. "
            f"See config.yaml for reference."
        )
    return data[key]


def _require_bool(data: dict[str, Any], key: str, section: str) -> bool:
    value = _require(data, key, section)
    if not isinstance(value, bool):
        raise ValueError(
            f"Config field '{key}' in section '{section}' must be a boolean "
            f"(true or false without quotes), got {type(value).__name__} {value!r}. "
            f"Remove quotes around the value in your YAML."
        )
    return value


def _load_llm(data: dict[str, Any]) -> LLMConfig:
    section = _require(data, "llm", "root")
    provider = _require(section, "provider", "llm")
    if provider not in VALID_PROVIDERS:
        raise ValueError(
            f"Invalid llm.provider '{provider}'. "
            f"Must be one of: {', '.join(sorted(VALID_PROVIDERS))}."
        )
    model = _require(section, "model", "llm")
    return LLMConfig(provider=provider, model=model)


def _load_delivery(data: dict[str, Any]) -> DeliveryConfig:
    section = _require(data, "delivery", "root")
    telegram = _require_bool(section, "telegram", "delivery")
    markdown_to_repo = _require_bool(section, "markdown_to_repo", "delivery")
    markdown_dir = section.get("markdown_dir", "digests")
    return DeliveryConfig(
        telegram=telegram,
        markdown_to_repo=markdown_to_repo,
        markdown_dir=str(markdown_dir),
    )


def _load_digest(data: dict[str, Any]) -> DigestConfig:
    section = _require(data, "digest", "root")
    language = section.get("language", "ru")
    if language not in VALID_LANGUAGES:
        raise ValueError(
            f"Invalid digest.language '{language}'. "
            f"Must be one of: {', '.join(sorted(VALID_LANGUAGES))}."
        )
    max_articles_per_source = section.get("max_articles_per_source", 5)
    max_total_articles = section.get("max_total_articles", 30)
    if int(max_articles_per_source) < 1:
        raise ValueError(
            f"Config field 'max_articles_per_source' must be >= 1, "
            f"got {max_articles_per_source}."
        )
    if int(max_total_articles) < 1:
        raise ValueError(
            f"Config field 'max_total_articles' must be >= 1, "
            f"got {max_total_articles}."
        )
    summary_style = section.get("summary_style", "analytical")
    if summary_style not in VALID_SUMMARY_STYLES:
        raise ValueError(
            f"Invalid digest.summary_style '{summary_style}'. "
            f"Must be one of: {', '.join(sorted(VALID_SUMMARY_STYLES))}."
        )
    return DigestConfig(
        language=language,
        max_articles_per_source=int(max_articles_per_source),
        max_total_articles=int(max_total_articles),
        summary_style=summary_style,
    )


def _load_sources(data: dict[str, Any]) -> list[SourceConfig]:
    raw_sources = _require(data, "sources", "root")
    if not isinstance(raw_sources, list):
        raise ValueError("Config field 'sources' must be a list.")
    sources: list[SourceConfig] = []
    for i, item in enumerate(raw_sources):
        if not isinstance(item, dict):
            raise ValueError(f"Source at index {i} must be a mapping.")
        name = _require(item, "name", f"sources[{i}]")
        url = _require(item, "url", f"sources[{i}]")
        category = _require(item, "category", f"sources[{i}]")
        raw_enabled = item.get("enabled", True)
        if not isinstance(raw_enabled, bool):
            raise ValueError(
                f"Config field 'enabled' in sources[{i}] must be a boolean "
                f"(true or false without quotes), got {type(raw_enabled).__name__} {raw_enabled!r}."
            )
        raw_priority = item.get("priority", 3)
        if isinstance(raw_priority, bool) or not isinstance(raw_priority, int):
            raise ValueError(
                f"Config field 'priority' in sources[{i}] must be an integer, "
                f"got {type(raw_priority).__name__} {raw_priority!r}."
            )
        if raw_priority < 1 or raw_priority > 5:
            raise ValueError(
                f"Config field 'priority' in sources[{i}] must be between 1 and 5, "
                f"got {raw_priority!r}."
            )
        raw_trial = item.get("trial", False)
        if not isinstance(raw_trial, bool):
            raise ValueError(
                f"Config field 'trial' in sources[{i}] must be a boolean "
                f"(true or false without quotes), got {type(raw_trial).__name__} {raw_trial!r}."
            )
        trial_started = item.get("trial_started", None)
        if trial_started is not None:
            trial_started = str(trial_started)
        raw_trial_days = item.get("trial_days", 7)
        if isinstance(raw_trial_days, bool) or not isinstance(raw_trial_days, int):
            raise ValueError(
                f"Config field 'trial_days' in sources[{i}] must be an integer, "
                f"got {type(raw_trial_days).__name__} {raw_trial_days!r}."
            )
        if raw_trial_days < 1:
            raise ValueError(
                f"Config field 'trial_days' in sources[{i}] must be >= 1, "
                f"got {raw_trial_days}."
            )
        sources.append(
            SourceConfig(
                name=str(name),
                url=str(url),
                category=str(category),
                enabled=raw_enabled,
                priority=raw_priority,
                trial=raw_trial,
                trial_started=trial_started,
                trial_days=raw_trial_days,
            )
        )
    seen_names: set[str] = set()
    for source in sources:
        if source.name in seen_names:
            raise ValueError(
                f"Duplicate source name {source.name!r}. "
                "Each source must have a unique name."
            )
        seen_names.add(source.name)
    return sources


def _load_adaptive(data: dict[str, Any]) -> AdaptiveConfig:
    section = data.get("adaptive")
    if section is None:
        return AdaptiveConfig(enabled=False)
    if not isinstance(section, dict):
        raise ValueError("Config field 'adaptive' must be a mapping.")
    enabled = section.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError(
            f"Config field 'enabled' in section 'adaptive' must be a boolean, "
            f"got {type(enabled).__name__} {enabled!r}."
        )
    feedback_weight = float(section.get("feedback_weight", 0.3))
    score_weight = float(section.get("score_weight", 0.5))
    base_weight = float(section.get("base_weight", 0.2))
    min_priority = int(section.get("min_priority", 1))
    max_priority = int(section.get("max_priority", 5))

    for wname, wval in [
        ("feedback_weight", feedback_weight),
        ("score_weight", score_weight),
        ("base_weight", base_weight),
    ]:
        if not math.isfinite(wval) or wval < 0.0 or wval > 1.0:
            raise ValueError(
                f"adaptive.{wname} must be between 0.0 and 1.0, got {wval}."
            )

    if min_priority < 1:
        raise ValueError(
            f"adaptive.min_priority must be >= 1, got {min_priority}."
        )
    if max_priority < 1:
        raise ValueError(
            f"adaptive.max_priority must be >= 1, got {max_priority}."
        )
    if min_priority >= max_priority:
        raise ValueError(
            f"adaptive.min_priority ({min_priority}) must be less than "
            f"adaptive.max_priority ({max_priority})."
        )

    weight_sum = feedback_weight + score_weight + base_weight
    if abs(weight_sum - 1.0) > 0.01:
        raise ValueError(
            f"Adaptive weights must sum to 1.0 (got {weight_sum:.2f}): "
            f"feedback_weight={feedback_weight}, score_weight={score_weight}, "
            f"base_weight={base_weight}."
        )

    trial_slots = int(section.get("trial_slots", 2))
    if trial_slots < 0:
        raise ValueError(
            f"adaptive.trial_slots must be non-negative, got {trial_slots}."
        )

    return AdaptiveConfig(
        enabled=enabled,
        feedback_weight=feedback_weight,
        score_weight=score_weight,
        base_weight=base_weight,
        trial_slots=trial_slots,
        min_priority=min_priority,
        max_priority=max_priority,
    )


def load_config(config_path: str | Path = "config.yaml") -> Config:
    """Load and validate configuration from a YAML file.

    Raises:
        FileNotFoundError: if the config file does not exist.
        ValueError: if required fields are missing or invalid.
    """
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(
            f"Config file not found: {path.resolve()}. "
            "Copy config.yaml from the repository root and edit it."
        )
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ValueError("Config file must be a YAML mapping at the top level.")

    llm = _load_llm(data)
    delivery = _load_delivery(data)
    digest = _load_digest(data)
    sources = _load_sources(data)
    adaptive = _load_adaptive(data)

    # Validate cross-section constraints
    if adaptive.enabled and adaptive.trial_slots > digest.max_total_articles:
        raise ValueError(
            f"adaptive.trial_slots ({adaptive.trial_slots}) must not exceed "
            f"digest.max_total_articles ({digest.max_total_articles})."
        )

    logger.info(
        "Config loaded: provider=%s, sources=%d (%d enabled), adaptive=%s",
        llm.provider,
        len(sources),
        sum(1 for s in sources if s.enabled),
        adaptive.enabled,
    )
    return Config(
        llm=llm,
        delivery=delivery,
        digest=digest,
        sources=sources,
        adaptive=adaptive,
    )
