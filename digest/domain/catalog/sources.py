"""Declared source settings, fetch statistics and retained trial lifecycle values."""

from __future__ import annotations

from dataclasses import dataclass, field

SOURCE_STATE_SCHEMA_VERSION = 1
HISTORY_MAX_DAYS = 30


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
class AdaptiveConfig:
    enabled: bool
    feedback_weight: float = 0.3
    score_weight: float = 0.5
    base_weight: float = 0.2
    trial_slots: int = 2
    min_priority: int = 1
    max_priority: int = 5


@dataclass
class DailySnapshot:
    date: str
    articles_found: int
    articles_included: int
    fetch_ok: bool


@dataclass
class SourceStats:
    name: str
    total_fetches: int = 0
    successful_fetches: int = 0
    total_articles_found: int = 0
    articles_included_in_digest: int = 0
    avg_description_length: float = 0.0
    last_seen: str | None = None
    history: list[DailySnapshot] = field(default_factory=list)


@dataclass
class SourceStateEntry:
    trial_started: str | None = None
    graduated: bool = False
    demoted: bool = False


@dataclass
class SourceStateStore:
    schema_version: int = SOURCE_STATE_SCHEMA_VERSION
    sources: dict[str, SourceStateEntry] = field(default_factory=dict)

    def _entry(self, name: str) -> SourceStateEntry:
        if name not in self.sources:
            self.sources[name] = SourceStateEntry()
        return self.sources[name]

    def is_demoted(self, name: str) -> bool:
        return self.sources.get(name, SourceStateEntry()).demoted

    def is_graduated(self, name: str) -> bool:
        return self.sources.get(name, SourceStateEntry()).graduated

    def get_trial_started(self, name: str) -> str | None:
        return self.sources.get(name, SourceStateEntry()).trial_started

    def set_trial_started(self, name: str, date: str) -> None:
        self._entry(name).trial_started = date

    def mark_graduated(self, name: str) -> None:
        entry = self._entry(name)
        entry.graduated = True
        entry.trial_started = None

    def mark_demoted(self, name: str) -> None:
        self._entry(name).demoted = True
