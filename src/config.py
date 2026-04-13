"""Configuration loading and validation for the daily digest v2."""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

logger = logging.getLogger(__name__)

VALID_PROVIDERS = {"anthropic", "gemini", "groq", "mistral", "deepseek"}
VALID_ROLES = {"summarize", "extract_narratives", "generate_queries", "rank_signals", "fallback"}
VALID_SUMMARY_STYLES = {"analytical", "brief", "detailed"}
VALID_LANGUAGES = {"ru", "en"}
VALID_SOURCES = {"hackernews", "reddit", "arxiv", "devto", "lobsters"}


@dataclass
class ProviderConfig:
    name: str
    model: str
    role: list[str] = field(default_factory=list)


@dataclass
class RouteConfig:
    categories: list[str]
    provider: str
    model: str


@dataclass
class LLMConfig:
    providers: list[ProviderConfig]
    routing: list[RouteConfig] = field(default_factory=list)

    @property
    def provider(self) -> str:
        """Primary provider name (backward-compat)."""
        return self.providers[0].name

    @property
    def model(self) -> str:
        """Primary model name (backward-compat)."""
        return self.providers[0].model


@dataclass
class RadarConfig:
    language: str = "ru"
    max_articles_per_category: int = 5
    summary_style: str = "analytical"
    perspectives: bool = False


@dataclass
class IrritatorConfig:
    max_narratives: int = 5
    queries_per_narrative: int = 3
    top_signals: int = 3
    min_signal_score: int = 7
    sources: list[str] = field(default_factory=lambda: list(VALID_SOURCES))
    reddit_subreddits: list[str] = field(
        default_factory=lambda: [
            "programming",
            "ExperiencedDevs",
            "softwarearchitecture",
            "banking",
            "fintech",
        ]
    )


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
    recency_hours: int = 24


@dataclass
class FiltersConfig:
    blocklist_keywords: list[str] = field(default_factory=list)


@dataclass
class TelegramConfig:
    enabled: bool = True
    split_messages: bool = True


@dataclass
class ObsidianConfig:
    enabled: bool = True
    output_dir: str = "digests"


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
    radar: RadarConfig
    irritator: IrritatorConfig
    sources: list[SourceConfig]
    filters: FiltersConfig
    telegram: TelegramConfig
    obsidian: ObsidianConfig
    adaptive: AdaptiveConfig = field(
        default_factory=lambda: AdaptiveConfig(enabled=False)
    )

    @property
    def enabled_sources(self) -> list[SourceConfig]:
        return [s for s in self.sources if s.enabled]


def _safe_int(value: Any, field_name: str, section: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as err:
        raise ValueError(
            f"Config field '{field_name}' in section '{section}' must be an integer, "
            f"got {type(value).__name__} {value!r}."
        ) from err


def _safe_float(value: Any, field_name: str, section: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as err:
        raise ValueError(
            f"Config field '{field_name}' in section '{section}' must be a number, "
            f"got {type(value).__name__} {value!r}."
        ) from err


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
    raw_providers = _require(section, "providers", "llm")
    if not isinstance(raw_providers, list) or len(raw_providers) == 0:
        raise ValueError(
            "Config field 'providers' in section 'llm' must be a non-empty list. "
            "See config.yaml for reference."
        )
    providers: list[ProviderConfig] = []
    seen_names: set[str] = set()
    for i, item in enumerate(raw_providers):
        if not isinstance(item, dict):
            raise ValueError(f"providers[{i}] in section 'llm' must be a mapping.")
        name = str(_require(item, "name", f"llm.providers[{i}]"))
        if name not in VALID_PROVIDERS:
            raise ValueError(
                f"Invalid llm.providers[{i}].name '{name}'. "
                f"Must be one of: {', '.join(sorted(VALID_PROVIDERS))}."
            )
        if name in seen_names:
            raise ValueError(
                f"Duplicate provider name '{name}' in llm.providers. "
                "Each provider may appear at most once."
            )
        seen_names.add(name)
        model = str(_require(item, "model", f"llm.providers[{i}]"))
        raw_role = item.get("role", [])
        if not isinstance(raw_role, list):
            raise ValueError(
                f"Config field 'role' in llm.providers[{i}] must be a list."
            )
        roles: list[str] = []
        for r in raw_role:
            r_str = str(r)
            if r_str not in VALID_ROLES:
                raise ValueError(
                    f"Invalid role '{r_str}' in llm.providers[{i}].role. "
                    f"Must be one of: {', '.join(sorted(VALID_ROLES))}."
                )
            roles.append(r_str)
        providers.append(ProviderConfig(name=name, model=model, role=roles))
    # Validate: at least one provider handles 'summarize' or 'fallback'
    all_roles: set[str] = {r for p in providers for r in p.role}
    if not (all_roles & {"summarize", "fallback"}):
        raise ValueError(
            "At least one provider must have role 'summarize' or 'fallback'. "
            "Check llm.providers[].role in config.yaml."
        )

    # Load routing (optional)
    routing: list[RouteConfig] = []
    if "routing" in section:
        raw_routing = section["routing"]
        if not isinstance(raw_routing, list):
            raise ValueError("Config field 'routing' in section 'llm' must be a list.")
        seen_categories: set[str] = set()
        for i, item in enumerate(raw_routing):
            if not isinstance(item, dict):
                raise ValueError(f"routing[{i}] in section 'llm' must be a mapping.")
            categories_raw = _require(item, "categories", f"llm.routing[{i}]")
            if not isinstance(categories_raw, list):
                raise ValueError(f"llm.routing[{i}].categories must be a list.")
            categories = [str(c) for c in categories_raw]
            for cat in categories:
                if cat in seen_categories:
                    raise ValueError(
                        f"Duplicate category '{cat}' in llm.routing. "
                        "Each category may appear in at most one route."
                    )
                seen_categories.add(cat)
            route_provider = str(_require(item, "provider", f"llm.routing[{i}]"))
            if route_provider not in VALID_PROVIDERS:
                raise ValueError(
                    f"Invalid provider '{route_provider}' in llm.routing[{i}]. "
                    f"Must be one of: {', '.join(sorted(VALID_PROVIDERS))}."
                )
            route_model = str(_require(item, "model", f"llm.routing[{i}]"))
            routing.append(
                RouteConfig(
                    categories=categories,
                    provider=route_provider,
                    model=route_model,
                )
            )

    return LLMConfig(providers=providers, routing=routing)


def _load_radar(data: dict[str, Any]) -> RadarConfig:
    section = data.get("radar")
    if section is None:
        return RadarConfig()
    if not isinstance(section, dict):
        raise ValueError("Config field 'radar' must be a mapping.")
    language = str(section.get("language", "ru"))
    if language not in VALID_LANGUAGES:
        raise ValueError(
            f"Invalid radar.language '{language}'. "
            f"Must be one of: {', '.join(sorted(VALID_LANGUAGES))}."
        )
    max_articles = _safe_int(
        section.get("max_articles_per_category", 5),
        "max_articles_per_category",
        "radar",
    )
    if max_articles < 1:
        raise ValueError(
            f"radar.max_articles_per_category must be >= 1, got {max_articles}."
        )
    summary_style = str(section.get("summary_style", "analytical"))
    if summary_style not in VALID_SUMMARY_STYLES:
        raise ValueError(
            f"Invalid radar.summary_style '{summary_style}'. "
            f"Must be one of: {', '.join(sorted(VALID_SUMMARY_STYLES))}."
        )
    perspectives = bool(section.get("perspectives", False))
    return RadarConfig(
        language=language,
        max_articles_per_category=max_articles,
        summary_style=summary_style,
        perspectives=perspectives,
    )


def _load_irritator(data: dict[str, Any]) -> IrritatorConfig:
    section = data.get("irritator")
    if section is None:
        return IrritatorConfig()
    if not isinstance(section, dict):
        raise ValueError("Config field 'irritator' must be a mapping.")
    max_narratives = _safe_int(
        section.get("max_narratives", 5), "max_narratives", "irritator"
    )
    if max_narratives < 1:
        raise ValueError(
            f"irritator.max_narratives must be >= 1, got {max_narratives}."
        )
    queries_per_narrative = _safe_int(
        section.get("queries_per_narrative", 3), "queries_per_narrative", "irritator"
    )
    if queries_per_narrative < 1:
        raise ValueError(
            f"irritator.queries_per_narrative must be >= 1, got {queries_per_narrative}."
        )
    top_signals = _safe_int(
        section.get("top_signals", 3), "top_signals", "irritator"
    )
    if top_signals < 1:
        raise ValueError(
            f"irritator.top_signals must be >= 1, got {top_signals}."
        )
    min_signal_score = _safe_int(
        section.get("min_signal_score", 7), "min_signal_score", "irritator"
    )
    if min_signal_score < 1 or min_signal_score > 10:
        raise ValueError(
            f"irritator.min_signal_score must be between 1 and 10, got {min_signal_score}."
        )
    raw_sources = section.get("sources", list(VALID_SOURCES))
    if not isinstance(raw_sources, list):
        raise ValueError("irritator.sources must be a list.")
    for s in raw_sources:
        if str(s) not in VALID_SOURCES:
            raise ValueError(
                f"Invalid irritator source '{s}'. "
                f"Must be one of: {', '.join(sorted(VALID_SOURCES))}."
            )
    sources = [str(s) for s in raw_sources]
    raw_subreddits = section.get(
        "reddit_subreddits",
        ["programming", "ExperiencedDevs", "softwarearchitecture", "banking", "fintech"],
    )
    if not isinstance(raw_subreddits, list):
        raise ValueError("irritator.reddit_subreddits must be a list.")
    reddit_subreddits = [str(s) for s in raw_subreddits]
    return IrritatorConfig(
        max_narratives=max_narratives,
        queries_per_narrative=queries_per_narrative,
        top_signals=top_signals,
        min_signal_score=min_signal_score,
        sources=sources,
        reddit_subreddits=reddit_subreddits,
    )


def _load_sources(data: dict[str, Any]) -> list[SourceConfig]:
    raw_sources = _require(data, "sources", "root")
    if not isinstance(raw_sources, list):
        raise ValueError("Config field 'sources' must be a list.")
    sources: list[SourceConfig] = []
    for i, item in enumerate(raw_sources):
        if not isinstance(item, dict):
            raise ValueError(f"Source at index {i} must be a mapping.")
        name = str(_require(item, "name", f"sources[{i}]"))
        if not name.strip():
            raise ValueError(f"Source name at index {i} must not be empty.")
        url = str(_require(item, "url", f"sources[{i}]"))
        if urlparse(url).scheme not in ("http", "https"):
            raise ValueError(
                f"Source '{name}' has an invalid URL scheme. "
                f"Only http and https are allowed, got: {url!r}."
            )
        category = str(_require(item, "category", f"sources[{i}]"))
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
        raw_recency = item.get("recency_hours", 24)
        if isinstance(raw_recency, bool) or not isinstance(raw_recency, int):
            raise ValueError(
                f"Config field 'recency_hours' in sources[{i}] must be an integer, "
                f"got {type(raw_recency).__name__} {raw_recency!r}."
            )
        if raw_recency < 1:
            raise ValueError(
                f"Config field 'recency_hours' in sources[{i}] must be >= 1, "
                f"got {raw_recency}."
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
                name=name,
                url=url,
                category=category,
                enabled=raw_enabled,
                priority=raw_priority,
                trial=raw_trial,
                trial_started=trial_started,
                trial_days=raw_trial_days,
                recency_hours=raw_recency,
            )
        )
    if not sources:
        raise ValueError("Config must contain at least one source.")
    seen_names: set[str] = set()
    for source in sources:
        if source.name in seen_names:
            raise ValueError(
                f"Duplicate source name {source.name!r}. "
                "Each source must have a unique name."
            )
        seen_names.add(source.name)
    return sources


def _load_filters(data: dict[str, Any]) -> FiltersConfig:
    section = data.get("filters")
    if section is None:
        return FiltersConfig()
    if not isinstance(section, dict):
        raise ValueError("Config field 'filters' must be a mapping.")
    raw_keywords = section.get("blocklist_keywords", [])
    if not isinstance(raw_keywords, list):
        raise ValueError("filters.blocklist_keywords must be a list.")
    return FiltersConfig(blocklist_keywords=[str(k) for k in raw_keywords])


def _load_telegram(data: dict[str, Any]) -> TelegramConfig:
    section = data.get("telegram")
    if section is None:
        return TelegramConfig()
    if not isinstance(section, dict):
        raise ValueError("Config field 'telegram' must be a mapping.")
    enabled = bool(section.get("enabled", True))
    split_messages = bool(section.get("split_messages", True))
    return TelegramConfig(enabled=enabled, split_messages=split_messages)


def _load_obsidian(data: dict[str, Any]) -> ObsidianConfig:
    section = data.get("obsidian")
    if section is None:
        return ObsidianConfig()
    if not isinstance(section, dict):
        raise ValueError("Config field 'obsidian' must be a mapping.")
    enabled = bool(section.get("enabled", True))
    output_dir = str(section.get("output_dir", "digests"))
    return ObsidianConfig(enabled=enabled, output_dir=output_dir)


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
    feedback_weight = _safe_float(section.get("feedback_weight", 0.3), "feedback_weight", "adaptive")
    score_weight = _safe_float(section.get("score_weight", 0.5), "score_weight", "adaptive")
    base_weight = _safe_float(section.get("base_weight", 0.2), "base_weight", "adaptive")
    min_priority = _safe_int(section.get("min_priority", 1), "min_priority", "adaptive")
    max_priority = _safe_int(section.get("max_priority", 5), "max_priority", "adaptive")

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

    trial_slots = _safe_int(section.get("trial_slots", 2), "trial_slots", "adaptive")
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
        try:
            data = yaml.safe_load(fh)
        except yaml.YAMLError as exc:
            raise ValueError(f"Invalid YAML in config file: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("Config file must be a YAML mapping at the top level.")

    llm = _load_llm(data)
    radar = _load_radar(data)
    irritator = _load_irritator(data)
    sources = _load_sources(data)
    filters = _load_filters(data)
    telegram = _load_telegram(data)
    obsidian = _load_obsidian(data)
    adaptive = _load_adaptive(data)

    logger.info(
        "Config loaded: providers=%s, sources=%d (%d enabled), adaptive=%s",
        [p.name for p in llm.providers],
        len(sources),
        sum(1 for s in sources if s.enabled),
        adaptive.enabled,
    )
    return Config(
        llm=llm,
        radar=radar,
        irritator=irritator,
        sources=sources,
        filters=filters,
        telegram=telegram,
        obsidian=obsidian,
        adaptive=adaptive,
    )
