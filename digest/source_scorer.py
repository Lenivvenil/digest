"""Source quality scoring: data models, statistics tracking, and scoring engine."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from digest._util import atomic_json_write

if TYPE_CHECKING:
    from digest.config import AdaptiveConfig, SourceConfig

logger = logging.getLogger(__name__)

STATS_FILE = "source_stats.json"
SOURCE_STATE_FILE = "source_state.json"
SOURCE_STATE_SCHEMA_VERSION = 1
HISTORY_MAX_DAYS = 30


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


def load_source_state(cache_dir: str) -> SourceStateStore:
    """Load source runtime state from JSON cache. Return empty store if missing or corrupt."""
    path = Path(cache_dir) / SOURCE_STATE_FILE
    if not path.exists():
        return SourceStateStore()
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            logger.warning("Invalid source_state.json format, expected dict — starting fresh")
            return SourceStateStore()
        version = data.get("schema_version", 0)
        if version != SOURCE_STATE_SCHEMA_VERSION:
            logger.warning(
                "source_state.json schema_version=%s unsupported (expected %s) — starting fresh",
                version,
                SOURCE_STATE_SCHEMA_VERSION,
            )
            return SourceStateStore()
        sources: dict[str, SourceStateEntry] = {}
        for name, raw in data.get("sources", {}).items():
            try:
                sources[name] = SourceStateEntry(
                    trial_started=raw.get("trial_started"),
                    graduated=bool(raw.get("graduated", False)),
                    demoted=bool(raw.get("demoted", False)),
                )
            except (KeyError, TypeError, AttributeError) as exc:
                logger.warning("Skipping malformed source_state entry '%s': %s", name, exc)
        return SourceStateStore(schema_version=version, sources=sources)
    except json.JSONDecodeError as exc:
        logger.warning("Corrupted source_state.json — starting fresh: %s", exc)
        return SourceStateStore()
    except Exception as exc:
        logger.warning("Failed to load source_state.json: %s", exc)
        return SourceStateStore()


def save_source_state(store: SourceStateStore, cache_dir: str) -> None:
    """Persist source runtime state to JSON cache atomically."""
    path = Path(cache_dir) / SOURCE_STATE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "schema_version": store.schema_version,
        "sources": {
            name: asdict(entry)
            for name, entry in store.sources.items()
        },
    }
    try:
        atomic_json_write(path, data)
    except Exception as exc:
        logger.warning("Failed to save source_state.json: %s", exc)


def load_stats(cache_dir: str) -> dict[str, SourceStats]:
    """Load source statistics from JSON file. Return empty dict if missing."""
    path = Path(cache_dir) / STATS_FILE
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            logger.warning("Invalid source stats format, expected dict")
            return {}
        result: dict[str, SourceStats] = {}
        for name, raw in data.items():
            try:
                history: list[DailySnapshot] = []
                for j, snap in enumerate(raw.get("history", [])):
                    try:
                        history.append(
                            DailySnapshot(
                                date=snap["date"],
                                articles_found=snap["articles_found"],
                                articles_included=snap["articles_included"],
                                fetch_ok=snap["fetch_ok"],
                            )
                        )
                    except (KeyError, TypeError) as exc:
                        logger.warning(
                            "Skipping malformed snapshot at index %d for source '%s': %s",
                            j,
                            name,
                            exc,
                        )
                result[name] = SourceStats(
                    name=raw.get("name", name),
                    total_fetches=raw.get("total_fetches", 0),
                    successful_fetches=raw.get("successful_fetches", 0),
                    total_articles_found=raw.get("total_articles_found", 0),
                    articles_included_in_digest=raw.get("articles_included_in_digest", 0),
                    avg_description_length=raw.get("avg_description_length", 0.0),
                    last_seen=raw.get("last_seen"),
                    history=history,
                )
            except (KeyError, TypeError, AttributeError) as exc:
                logger.warning("Skipping malformed source stats entry '%s': %s", name, exc)
        return result
    except json.JSONDecodeError as exc:
        logger.warning("Corrupted stats JSON in %s, starting fresh: %s", path, exc)
        return {}
    except Exception as exc:
        logger.warning("Failed to load source stats: %s", exc)
        return {}


def save_stats(
    stats: dict[str, SourceStats],
    cache_dir: str,
    active_sources: set[str] | None = None,
) -> None:
    """Serialize source statistics to JSON.

    If active_sources is provided, entries for sources not in the set are
    pruned before saving.
    """
    path = Path(cache_dir) / STATS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    if active_sources is not None:
        stale = [name for name in stats if name not in active_sources]
        for name in stale:
            del stats[name]
            logger.info("Pruned stale source stats entry: '%s'", name)
    data = {name: asdict(s) for name, s in stats.items()}
    try:
        atomic_json_write(path, data)
    except Exception as exc:
        logger.warning("Failed to save source stats: %s", exc)


def update_stats(
    stats: dict[str, SourceStats],
    source_name: str,
    fetch_ok: bool,
    articles_found: int,
    articles_included: int,
    avg_desc_len: float,
) -> None:
    """Update statistics for a source after a fetch. Caps history at 30 days."""
    today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")

    if source_name not in stats:
        stats[source_name] = SourceStats(name=source_name)

    s = stats[source_name]
    s.total_fetches += 1
    if fetch_ok:
        s.successful_fetches += 1
        s.total_articles_found += articles_found
        s.articles_included_in_digest += articles_included
        s.last_seen = today
        if avg_desc_len > 0:
            if s.avg_description_length == 0.0:
                s.avg_description_length = avg_desc_len
            else:
                s.avg_description_length = (
                    s.avg_description_length * 0.7 + avg_desc_len * 0.3
                )

    if s.history and s.history[-1].date == today:
        snap = s.history[-1]
        snap.articles_found = max(snap.articles_found, articles_found)
        snap.articles_included = max(snap.articles_included, articles_included)
        snap.fetch_ok = snap.fetch_ok or fetch_ok
    else:
        s.history.append(
            DailySnapshot(
                date=today,
                articles_found=articles_found,
                articles_included=articles_included,
                fetch_ok=fetch_ok,
            )
        )

    if len(s.history) > HISTORY_MAX_DAYS:
        s.history = s.history[-HISTORY_MAX_DAYS:]


def calculate_score(stats: SourceStats) -> float:
    """Calculate composite quality score (0.0-1.0) for a source."""
    if stats.total_fetches == 0:
        return 0.5

    reliability = stats.successful_fetches / stats.total_fetches

    recent_snaps = stats.history[-7:] if stats.history else []
    recent_found = sum(s.articles_found for s in recent_snaps)
    recent_included = sum(s.articles_included for s in recent_snaps)
    if recent_found == 0:
        if stats.total_articles_found > 0:
            productivity = min(1.0, stats.articles_included_in_digest / stats.total_articles_found)
        else:
            productivity = 0.0
    else:
        productivity = min(1.0, recent_included / recent_found)

    if stats.avg_description_length >= 100:
        desc_quality = 1.0
    else:
        desc_quality = stats.avg_description_length / 100.0

    if stats.last_seen is None:
        recency = 0.0
    else:
        try:
            last = datetime.strptime(stats.last_seen, "%Y-%m-%d").replace(
                tzinfo=timezone.utc
            )
            days_ago = (datetime.now(tz=timezone.utc) - last).days
            if days_ago <= 3:
                recency = 1.0
            else:
                recency = max(0.0, 1.0 - (days_ago - 3) / 7.0)
        except ValueError:
            recency = 0.0

    score = (
        reliability * 0.3
        + productivity * 0.3
        + desc_quality * 0.2
        + recency * 0.2
    )
    return min(1.0, max(0.0, score))


def detect_trending_sources(
    stats: dict[str, SourceStats], window: int = 7
) -> list[str]:
    """Return source names where articles_found shows >50% increase."""
    trending: list[str] = []
    for name, s in stats.items():
        if len(s.history) < window:
            continue
        recent = s.history[-window:]
        previous = s.history[-2 * window : -window] if len(s.history) >= 2 * window else []
        recent_total = sum(snap.articles_found for snap in recent)
        previous_total = sum(snap.articles_found for snap in previous)
        if previous_total == 0:
            continue
        increase = (recent_total - previous_total) / previous_total
        if increase > 0.5:
            trending.append(name)
    return trending


def calculate_effective_priorities(
    sources: list[SourceConfig],
    stats: dict[str, SourceStats],
    feedback_scores: dict[str, float],
    adaptive_config: AdaptiveConfig,
) -> dict[str, int]:
    """Compute effective priorities by combining base priority, quality score, and feedback."""
    trending = detect_trending_sources(stats)
    min_p = adaptive_config.min_priority
    max_p = adaptive_config.max_priority
    p_range = max_p - min_p

    result: dict[str, int] = {}
    for source in sources:
        base_norm = source.priority / 5.0
        score = calculate_score(stats[source.name]) if source.name in stats else 0.5
        feedback = feedback_scores.get(source.name, 0.5)

        weighted = (
            base_norm * adaptive_config.base_weight
            + score * adaptive_config.score_weight
            + feedback * adaptive_config.feedback_weight
        )
        priority = round(min_p + weighted * p_range)
        if source.name in trending:
            priority += 1
        priority = max(min_p, min(max_p, priority))
        result[source.name] = priority
    return result


def evaluate_trial_sources(
    sources: list[SourceConfig],
    stats: dict[str, SourceStats],
    today: str,
    source_state: SourceStateStore | None = None,
) -> tuple[list[str], list[str], list[str]]:
    """Evaluate trial sources and decide which to promote or demote.

    Returns (promote_names, demote_names, needs_start_names).
    trial_started is read from source_state; sources already graduated or demoted are skipped.
    """
    if source_state is None:
        source_state = SourceStateStore()

    promote: list[str] = []
    demote: list[str] = []

    try:
        today_dt = datetime.strptime(today, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        logger.warning("Invalid today date for trial evaluation: %s", today)
        return [], [], []

    needs_start: list[str] = []

    for source in sources:
        if not source.trial:
            continue
        if source_state.is_graduated(source.name) or source_state.is_demoted(source.name):
            continue
        trial_started = source_state.get_trial_started(source.name)
        if trial_started is None:
            logger.info(
                "Trial source '%s' has no trial_started date; will initialize to %s",
                source.name,
                today,
            )
            needs_start.append(source.name)
            continue
        try:
            started_dt = datetime.strptime(trial_started, "%Y-%m-%d").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            logger.warning(
                "Invalid trial_started date for source '%s': %s",
                source.name,
                trial_started,
            )
            continue

        elapsed = (today_dt - started_dt).days
        if elapsed < source.trial_days:
            continue

        score = calculate_score(stats[source.name]) if source.name in stats else 0.5
        if score > 0.6:
            promote.append(source.name)
        elif score < 0.3:
            demote.append(source.name)

    return promote, demote, needs_start


def apply_trial_decisions_to_cache(
    store: SourceStateStore,
    promote: list[str],
    demote: list[str],
    today: str,
    needs_start: list[str] | None = None,
) -> SourceStateStore:
    """Apply trial promotion/demotion decisions to the source state cache.

    Graduated sources: mark_graduated (trial_started cleared).
    Demoted sources: mark_demoted.
    needs_start sources: initialize trial_started to today.
    """
    if needs_start is None:
        needs_start = []
    for name in promote:
        store.mark_graduated(name)
        logger.info("Graduated trial source '%s' to permanent", name)
    for name in demote:
        store.mark_demoted(name)
        logger.info("Demoted trial source '%s'", name)
    for name in needs_start:
        store.set_trial_started(name, today)
        logger.info("Initialized trial_started for '%s' to %s", name, today)
    return store
