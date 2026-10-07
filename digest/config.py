"""Configuration loading and validation for the daily digest v2."""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from digest.source_scorer import SourceStateStore
from urllib.parse import urlparse

import yaml

logger = logging.getLogger(__name__)

VALID_PROVIDERS = {"anthropic", "gemini", "groq", "mistral", "deepseek"}
VALID_ROLES = {"summarize", "extract_narratives", "generate_queries", "rank_signals", "review_evidence", "fallback"}
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
    max_concurrent_requests: int = 4
    min_request_interval_seconds: float = 0.0
    max_retries: int = 0
    retry_max_wait_seconds: float = 60.0
    _runtime: Any = field(default=None, init=False, repr=False, compare=False)

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
    min_signal_score: int = 5
    check_liveness: bool = False
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
    trial_days: int = 7
    recency_hours: int = 24


@dataclass
class FiltersConfig:
    blocklist_keywords: list[str] = field(default_factory=list)


@dataclass
class TelegramConfig:
    enabled: bool = True
    required: bool = False
    split_messages: bool = True
    max_messages: int = 10
    bot_username: str = ""
    delivery_mode: str = "cards"


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


@dataclass(frozen=True)
class ReviewModelConfig:
    provider: str
    model: str


@dataclass
class ReviewConfig:
    enabled: bool = False
    primary: ReviewModelConfig = field(
        default_factory=lambda: ReviewModelConfig("gemini", "gemini-3.8-flash")
    )
    secondary: ReviewModelConfig = field(
        default_factory=lambda: ReviewModelConfig("groq", "openai/gpt-oss-120b")
    )
    tie_breaker: ReviewModelConfig | None = None
    max_evidence_articles: int = 20
    max_excerpt_chars: int = 500
    max_selections: int = 5  # Publication card cap; relevance is judged over the complete evidence packet.
    max_detailed_selections: int = 5  # Response detail budget, independent of publication capacity.
    max_output_tokens: int = 4096
    disagreement_threshold: float = 0.5
    review_led_only: bool = False


@dataclass(frozen=True)
class TranslationConfig:
    """Optional presentation-only translation; never an implicit provider fallback."""

    enabled: bool = False
    target_language: str = "ru"
    provider: str = ""
    model: str = ""
    max_calls: int = 1
    max_output_tokens: int = 2048
    timeout_seconds: float = 90.0
    max_input_chars: int = 12000


@dataclass(frozen=True)
class ReadingBriefConfig:
    """Opt-in full-source reading briefs on an explicitly configured route."""

    enabled: bool = False
    provider: str = ""
    model: str = ""
    max_output_tokens: int = 2048
    max_requests_per_run: int = 10


@dataclass
class DiscoveryConfig:
    """Exploration targets for source proposals, independent of active categories."""

    exploration_areas: list[str] = field(default_factory=lambda: [
        "fintech/banking/architecture", "science", "society/institutions", "history/culture", "environment", "design",
    ])


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
    review: ReviewConfig = field(default_factory=ReviewConfig)
    translation: TranslationConfig = field(default_factory=TranslationConfig)
    reading_brief: ReadingBriefConfig = field(default_factory=ReadingBriefConfig)
    discovery: DiscoveryConfig = field(default_factory=DiscoveryConfig)

    @property
    def enabled_sources(self) -> list[SourceConfig]:
        return [s for s in self.sources if s.enabled]

    def effective_sources(self, source_state: "SourceStateStore") -> list[SourceConfig]:
        """Return enabled sources excluding those demoted by runtime cache (ADR-0003)."""
        return [s for s in self.sources if s.enabled and not source_state.is_demoted(s.name)]


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


def _load_llm_limits(section: dict[str, Any]) -> dict[str, Any]:
    """Validate optional pacing/retry controls; old configs retain no retries."""
    values: dict[str, Any] = {}
    for name, default, low, high in [
        ("max_concurrent_requests", 4, 1, 20), ("max_retries", 0, 0, 3),
    ]:
        value = _safe_int(section.get(name, default), name, "llm")
        if not low <= value <= high:
            raise ValueError(f"llm.{name} must be between {low} and {high}.")
        values[name] = value
    for float_name, float_default, float_low, float_high in [
        ("min_request_interval_seconds", 0.0, 0.0, 120.0),
        ("retry_max_wait_seconds", 60.0, 0.0, 300.0),
    ]:
        float_value = _safe_float(section.get(float_name, float_default), float_name, "llm")
        if not math.isfinite(float_value) or not float_low <= float_value <= float_high:
            raise ValueError(f"llm.{float_name} must be between {float_low} and {float_high}.")
        values[float_name] = float_value
    return values


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

    # Warn about routing entries that reference providers not in providers list
    provider_names = {p.name for p in providers}
    for route in routing:
        if route.provider not in provider_names:
            logger.warning(
                "llm.routing references provider '%s' not in providers list — "
                "categories %s will fall back to role-based dispatch",
                route.provider,
                route.categories,
            )

    return LLMConfig(providers=providers, routing=routing, **_load_llm_limits(section))


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
    check_liveness = section.get("check_liveness", False)
    if not isinstance(check_liveness, bool):
        raise ValueError(
            f"Config field 'check_liveness' in section 'irritator' must be a boolean "
            f"(true or false without quotes), got {type(check_liveness).__name__} {check_liveness!r}. "
            f"Remove quotes around the value in your YAML."
        )
    return IrritatorConfig(
        max_narratives=max_narratives,
        queries_per_narrative=queries_per_narrative,
        top_signals=top_signals,
        min_signal_score=min_signal_score,
        check_liveness=check_liveness,
        sources=sources,
        reddit_subreddits=reddit_subreddits,
    )


def _load_discovery(data: dict[str, Any]) -> DiscoveryConfig:
    section = data.get("discovery")
    if section is None:
        return DiscoveryConfig()
    if not isinstance(section, dict):
        raise ValueError("Config field 'discovery' must be a mapping.")
    areas = section.get("exploration_areas", DiscoveryConfig().exploration_areas)
    if (not isinstance(areas, list) or not 1 <= len(areas) <= 16
            or any(not isinstance(area, str) or not 1 <= len(area.strip()) <= 80 for area in areas)):
        raise ValueError("discovery.exploration_areas must contain 1–16 nonempty strings of at most 80 characters.")
    normalized = [area.strip() for area in areas]
    if len({area.casefold() for area in normalized}) != len(normalized):
        raise ValueError("discovery.exploration_areas must not contain duplicate areas.")
    return DiscoveryConfig(exploration_areas=normalized)


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
    bot_username = section.get("bot_username", "")
    if not isinstance(bot_username, str) or (bot_username and not re.fullmatch(r"[A-Za-z0-9_]{5,32}", bot_username)):
        raise ValueError("telegram.bot_username must be a plain bot username without @ or a URL.")
    delivery_mode = section.get("delivery_mode", "cards")
    if delivery_mode not in ("cards", "compact"):
        raise ValueError("telegram.delivery_mode must be cards or compact.")
    enabled = bool(section.get("enabled", True))
    required = section.get("required", False)
    if not isinstance(required, bool):
        raise ValueError("telegram.required must be a boolean.")
    if required and not enabled:
        raise ValueError("telegram.required cannot be true when telegram.enabled is false.")
    split_messages = bool(section.get("split_messages", True))
    max_messages = _safe_int(
        section.get("max_messages", 10), "max_messages", "telegram"
    )
    if max_messages < 1:
        raise ValueError(f"telegram.max_messages must be >= 1, got {max_messages}.")
    return TelegramConfig(
        bot_username=bot_username, delivery_mode=delivery_mode, enabled=enabled, required=required,
        split_messages=split_messages, max_messages=max_messages
    )


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


def _load_review(data: dict[str, Any]) -> ReviewConfig:
    section = data.get("review", {})
    if not isinstance(section, dict):
        raise ValueError("review must be a mapping.")
    defaults = ReviewConfig()
    enabled = section.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError("review.enabled must be a boolean.")
    review_led_only = section.get("review_led_only", False)
    if not isinstance(review_led_only, bool):
        raise ValueError("review.review_led_only must be a boolean.")

    def model_slot(name: str, default: ReviewModelConfig | None) -> ReviewModelConfig | None:
        raw = section.get(name)
        if raw is None:
            return default
        if not isinstance(raw, dict):
            raise ValueError(f"review.{name} must be a provider/model mapping.")
        provider = raw.get("provider")
        model = raw.get("model")
        if (not isinstance(provider, str) or provider not in VALID_PROVIDERS
                or not isinstance(model, str) or not model.strip()):
            raise ValueError(f"review.{name} needs a supported provider and non-empty model.")
        return ReviewModelConfig(str(provider), model.strip())

    primary = model_slot("primary", defaults.primary)
    secondary = model_slot("secondary", defaults.secondary)
    tie_breaker = model_slot("tie_breaker", None)
    assert primary is not None and secondary is not None
    slots = [primary, secondary] + ([tie_breaker] if tie_breaker else [])
    if len(set(slots)) != len(slots):
        raise ValueError("review slots must use distinct provider/model identities.")
    bounds = {"max_evidence_articles": (20, 1, 100), "max_excerpt_chars": (500, 50, 1000),
              "max_selections": (5, 1, 10), "max_detailed_selections": (5, 1, 10),
              "max_output_tokens": (4096, 128, 8192)}
    values: dict[str, int] = {}
    for key, (default, low, high) in bounds.items():
        if key == "max_detailed_selections" and isinstance(section.get(key), bool):
            raise ValueError("review.max_detailed_selections must be an integer.")
        value = _safe_int(section.get(key, default), key, "review")
        if not low <= value <= high:
            raise ValueError(f"review.{key} must be between {low} and {high}.")
        values[key] = value
    threshold = _safe_float(section.get("disagreement_threshold", 0.5), "disagreement_threshold", "review")
    if not math.isfinite(threshold) or not 0 <= threshold <= 1:
        raise ValueError("review.disagreement_threshold must be between 0 and 1.")
    return ReviewConfig(
        enabled=enabled, primary=primary, secondary=secondary, tie_breaker=tie_breaker,
        disagreement_threshold=threshold, review_led_only=review_led_only, **values,
    )


def _load_translation(
    data: dict[str, Any], llm: LLMConfig, radar: RadarConfig, review: ReviewConfig,
) -> TranslationConfig:
    if "translation" not in data:
        return TranslationConfig()  # Legacy generation language and call count are unchanged.
    section = data["translation"]
    if not isinstance(section, dict):
        raise ValueError("translation must be a mapping.")
    known = {"enabled", "target_language", "provider", "model", "max_calls", "max_output_tokens",
             "timeout_seconds", "max_input_chars"}
    if set(section) - known:
        raise ValueError("Unknown translation setting.")
    enabled = section.get("enabled", False)
    if type(enabled) is not bool:
        raise ValueError("translation.enabled must be a boolean.")
    target = section.get("target_language", "ru")
    if not isinstance(target, str) or target not in VALID_LANGUAGES:
        raise ValueError("translation.target_language must be en or ru.")
    provider, model = section.get("provider", ""), section.get("model", "")
    if not isinstance(provider, str) or not isinstance(model, str):
        raise ValueError("translation.provider and model must be strings.")
    values: dict[str, int] = {}
    for key, default, lower, upper in (("max_calls", 1, 1, 10), ("max_output_tokens", 2048, 256, 4096),
                                       ("max_input_chars", 12000, 256, 32000)):
        value = section.get(key, default)
        if type(value) is not int or not lower <= value <= upper:
            raise ValueError(f"translation.{key} must be an integer between {lower} and {upper}.")
        values[key] = value
    timeout = section.get("timeout_seconds", 90.0)
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 180:
        raise ValueError("translation.timeout_seconds must be finite and within (0, 180].")
    # A new explicit presentation contract defaults to canonical English, while old
    # configs without the section and explicit language settings retain their behavior.
    radar_section = data.get("radar") or {}
    if "language" not in radar_section:
        radar.language = "en"
    if enabled:
        if radar.language != "en":
            raise ValueError("Translation requires radar.language: en canonical text. "
                             "Keep translation absent for legacy direct Russian generation.")
        configured = {(p.name, p.model) for p in llm.providers}
        review_section = data.get("review", {})
        for slot in ("primary", "secondary", "tie_breaker"):
            route = getattr(review, slot)
            # Defaults are not operator-configured routes. Only validated explicit
            # mappings can authorize reuse; ordinary provider roles stay unchanged.
            if isinstance(review_section.get(slot), dict) and route is not None:
                configured.add((route.provider, route.model))
        if not provider or not model or (provider, model) not in configured:
            raise ValueError("Translation requires an explicit provider/model already present in "
                             "llm.providers or an explicitly configured review route.")
    return TranslationConfig(enabled=enabled, target_language=target, provider=provider, model=model,
                             timeout_seconds=float(timeout), **values)


def _load_reading_brief(
    data: dict[str, Any], llm: LLMConfig, radar: RadarConfig, review: ReviewConfig,
) -> ReadingBriefConfig:
    section = data.get("reading_brief", {})
    if not isinstance(section, dict):
        raise ValueError("reading_brief must be a mapping.")
    enabled = section.get("enabled", False)
    if type(enabled) is not bool:
        raise ValueError("reading_brief.enabled must be a boolean.")
    provider, model = section.get("provider", ""), section.get("model", "")
    if not isinstance(provider, str) or not isinstance(model, str):
        raise ValueError("reading_brief provider/model must be strings.")
    values: dict[str, int] = {}
    for key, default, lower, upper in (("max_output_tokens", 2048, 256, 4096),
                                       ("max_requests_per_run", 10, 1, 10)):
        value = section.get(key, default)
        if type(value) is not int or not lower <= value <= upper:
            raise ValueError(f"reading_brief.{key} must be an integer between {lower} and {upper}.")
        values[key] = value
    if enabled:
        if radar.language != "en" or not review.enabled or not review.review_led_only:
            raise ValueError("Reading briefs require canonical English and review-led primary selection.")
        configured = {(p.name, p.model) for p in llm.providers}
        review_section = data.get("review", {})
        for slot in ("primary", "secondary", "tie_breaker"):
            route = getattr(review, slot)
            if isinstance(review_section.get(slot), dict) and route is not None:
                configured.add((route.provider, route.model))
        if not provider or not model or (provider, model) not in configured:
            raise ValueError("Reading briefs require an explicit existing configured provider/model route.")
    return ReadingBriefConfig(enabled=enabled, provider=provider, model=model, **values)


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
    review = _load_review(data)
    translation = _load_translation(data, llm, radar, review)
    reading_brief = _load_reading_brief(data, llm, radar, review)
    discovery = _load_discovery(data)

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
        review=review,
        translation=translation,
        reading_brief=reading_brief,
        discovery=discovery,
    )
