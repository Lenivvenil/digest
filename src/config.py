"""Configuration loading and validation for the daily digest."""

from __future__ import annotations

import logging
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


@dataclass
class Config:
    llm: LLMConfig
    delivery: DeliveryConfig
    digest: DigestConfig
    sources: list[SourceConfig] = field(default_factory=list)

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
        sources.append(
            SourceConfig(
                name=str(name),
                url=str(url),
                category=str(category),
                enabled=raw_enabled,
            )
        )
    return sources


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

    logger.info(
        "Config loaded: provider=%s, sources=%d (%d enabled)",
        llm.provider,
        len(sources),
        sum(1 for s in sources if s.enabled),
    )
    return Config(llm=llm, delivery=delivery, digest=digest, sources=sources)
