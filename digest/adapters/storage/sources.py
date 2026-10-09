"""Source JSON codecs with explicit strict prepared reads and permissive legacy I/O.

Setup, pruning and encoding precede the caught atomic-write failure boundary.
Prepared delivery reuses the declared values and encoders, without reconstructing
invalid existing accounting history before strict writes.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Any

from digest._serialization import restore_dataclass, unique_object
from digest._util import atomic_json_write
from digest.adapters.storage.issue_paths import safe_issue_path
from digest.domain.catalog.sources import (
    SOURCE_STATE_SCHEMA_VERSION,
    DailySnapshot,
    SourceConfig,
    SourceStateEntry,
    SourceStateStore,
    SourceStats,
)

logger = logging.getLogger(__name__)
STATS_FILE = "source_stats.json"
SOURCE_STATE_FILE = "source_state.json"
CATEGORY_MAP_FILE = "source_category_map.json"


def encode_source_state(store: SourceStateStore) -> dict[str, Any]:
    """Retain the existing field and source insertion order."""
    return {
        "schema_version": store.schema_version,
        "sources": {name: asdict(entry) for name, entry in store.sources.items()},
    }


def encode_source_stats(stats: dict[str, SourceStats]) -> dict[str, Any]:
    return {name: asdict(value) for name, value in stats.items()}


def encode_source_category_map(sources: list[SourceConfig]) -> dict[str, str]:
    return {source.name: source.category for source in sources}


def _strict_source_record(path: Path) -> dict[str, Any] | None:
    """Only missing files are fresh state; unsafe paths and read errors hold work."""
    path = safe_issue_path(path)
    try:
        content = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    value = json.loads(content, object_pairs_hook=unique_object)
    if not isinstance(value, dict):
        raise ValueError("Invalid source accounting object; prepared application blocked.")
    return value


def _accounting_day(value: str | None) -> None:
    if value is not None and date.fromisoformat(value).isoformat() != value:
        raise ValueError("Invalid source accounting calendar day.")


def load_source_state(cache_dir: str, *, strict: bool = False) -> SourceStateStore:
    """Restore source lifecycle; strict prepared reads never reset corrupt history."""
    path = Path(cache_dir) / SOURCE_STATE_FILE
    if strict:
        value = _strict_source_record(path)
        if value is None:
            return SourceStateStore()
        store: SourceStateStore = restore_dataclass(value, SourceStateStore)
        if store.schema_version != SOURCE_STATE_SCHEMA_VERSION:
            raise ValueError("Unsupported source lifecycle schema; prepared application blocked.")
        for entry in store.sources.values():
            _accounting_day(entry.trial_started)
        return store
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
    data = encode_source_state(store)
    try:
        atomic_json_write(path, data)
    except Exception as exc:
        logger.warning("Failed to save source_state.json: %s", exc)


def save_source_category_map(sources: list[SourceConfig], cache_dir: str) -> None:
    """Persist {source_name: category} mapping for /bubble analytics."""
    path = Path(cache_dir) / CATEGORY_MAP_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    data = encode_source_category_map(sources)
    try:
        atomic_json_write(path, data)
    except Exception as exc:
        logger.warning("Failed to save source_category_map.json: %s", exc)


def load_source_category_map(cache_dir: str) -> dict[str, str]:
    """Load {source_name: category} from cache. Returns empty dict if missing."""
    path = Path(cache_dir) / CATEGORY_MAP_FILE
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()}
    except Exception as exc:
        logger.warning("Failed to load source_category_map.json: %s", exc)
    return {}


def load_stats(cache_dir: str, *, strict: bool = False) -> dict[str, SourceStats]:
    """Restore statistics; legacy reads retain their permissive partial-load policy."""
    path = Path(cache_dir) / STATS_FILE
    if strict:
        value = _strict_source_record(path)
        if value is None:
            return {}
        stats: dict[str, SourceStats] = restore_dataclass(value, dict[str, SourceStats])
        for source in stats.values():
            if min(source.total_fetches, source.successful_fetches, source.total_articles_found,
                   source.articles_included_in_digest, source.avg_description_length) < 0:
                raise ValueError("Negative source accounting value; prepared application blocked.")
            _accounting_day(source.last_seen)
            for snapshot in source.history:
                _accounting_day(snapshot.date)
                if min(snapshot.articles_found, snapshot.articles_included) < 0:
                    raise ValueError("Negative source history value; prepared application blocked.")
        return stats
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
    data = encode_source_stats(stats)
    try:
        atomic_json_write(path, data)
    except Exception as exc:
        logger.warning("Failed to save source stats: %s", exc)
