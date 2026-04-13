"""Source quality scoring: data models, statistics tracking, and scoring engine."""

from __future__ import annotations

import json
import logging
import re
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from src._util import atomic_json_write

if TYPE_CHECKING:
    from src.config import AdaptiveConfig, SourceConfig

logger = logging.getLogger(__name__)

STATS_FILE = "source_stats.json"
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
) -> tuple[list[str], list[str], list[str]]:
    """Evaluate trial sources and decide which to promote or demote.

    Returns (promote_names, demote_names, needs_start_names).
    """
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
        if source.trial_started is None:
            logger.info(
                "Trial source '%s' has no trial_started date; will initialize to %s",
                source.name,
                today,
            )
            needs_start.append(source.name)
            continue
        try:
            started_dt = datetime.strptime(source.trial_started, "%Y-%m-%d").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            logger.warning(
                "Invalid trial_started date for source '%s': %s",
                source.name,
                source.trial_started,
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


def _find_source_block(lines: list[str], source_name: str) -> tuple[int, int] | None:
    """Find the line range [start, end) of a source entry with the given name."""
    i = 0
    while i < len(lines):
        match = re.match(r"^(\s*)-\s+\w+\s*:", lines[i])
        if match:
            indent = len(match.group(1))
            start = i
            end = i + 1
            while end < len(lines):
                stripped = lines[end]
                if stripped.strip() == "" or stripped.lstrip().startswith("#"):
                    end += 1
                    continue
                next_match = re.match(r"^(\s*)-\s+\S", stripped)
                if next_match and len(next_match.group(1)) <= indent:
                    break
                key_match = re.match(r"^(\s*)\S", stripped)
                if key_match and len(key_match.group(1)) <= indent:
                    break
                end += 1

            for k in range(start, end):
                name_match = re.match(r"^\s*-?\s*name:\s*(.+?)\s*$", lines[k])
                if name_match and name_match.group(1).strip("\"'") == source_name:
                    return start, end

            i = end
        else:
            i += 1
    return None


def _set_field_in_block(
    lines: list[str], start: int, end: int, field_name: str, value: str
) -> list[str]:
    """Set or add a YAML field within a source block (lines[start:end])."""
    field_indent = "    "
    for k in range(start + 1, end):
        m = re.match(r"^(\s+)\w", lines[k])
        if m:
            field_indent = m.group(1)
            break

    for k in range(start, end):
        pattern = rf"^(\s+){re.escape(field_name)}\s*:.*$"
        if re.match(pattern, lines[k]):
            lines[k] = f"{field_indent}{field_name}: {value}"
            return lines

    lines.insert(end, f"{field_indent}{field_name}: {value}")
    return lines


def _remove_field_in_block(
    lines: list[str], start: int, end: int, field_name: str
) -> tuple[list[str], int]:
    """Remove a YAML field line from a source block. Returns updated lines and new end."""
    for k in range(start, end):
        if re.match(rf"^\s+{re.escape(field_name)}\s*:.*$", lines[k]):
            lines.pop(k)
            return lines, end - 1
    return lines, end


def apply_trial_decisions(
    config_path: str,
    promote: list[str],
    demote: list[str],
    needs_start: list[str] | None = None,
) -> None:
    """Update config.yaml: set trial=false for promoted, enabled=false for demoted,
    and initialize trial_started for new trial sources.
    """
    if needs_start is None:
        needs_start = []
    if not promote and not demote and not needs_start:
        return

    path = Path(config_path)
    with path.open("r", encoding="utf-8") as fh:
        lines = fh.read().splitlines()

    today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")

    for name in promote:
        block = _find_source_block(lines, name)
        if block is None:
            logger.warning("Cannot find source '%s' in config for promotion", name)
            continue
        start, end = block
        lines = _set_field_in_block(lines, start, end, "trial", "false")
        block = _find_source_block(lines, name)
        if block:
            start, end = block
            lines, end = _remove_field_in_block(lines, start, end, "trial_started")
            lines, end = _remove_field_in_block(lines, start, end, "trial_days")
        logger.info("Promoted trial source '%s' to permanent", name)

    for name in demote:
        block = _find_source_block(lines, name)
        if block is None:
            logger.warning("Cannot find source '%s' in config for demotion", name)
            continue
        start, end = block
        lines = _set_field_in_block(lines, start, end, "enabled", "false")
        logger.info("Demoted trial source '%s' (disabled)", name)

    for name in needs_start:
        block = _find_source_block(lines, name)
        if block is None:
            logger.warning("Cannot find source '%s' in config to set trial_started", name)
            continue
        start, end = block
        lines = _set_field_in_block(lines, start, end, "trial_started", today)
        logger.info("Initialized trial_started for '%s' to %s", name, today)

    bak_path = path.with_suffix(".yaml.bak")
    tmp_path = path.with_suffix(".yaml.tmp")
    try:
        shutil.copy2(path, bak_path)
    except OSError as exc:
        logger.warning("Could not create config backup '%s': %s", bak_path, exc)
        bak_path = None  # type: ignore[assignment]

    try:
        with tmp_path.open("w", encoding="utf-8") as fh:
            fh.write("\n".join(lines))
            if lines:
                fh.write("\n")
        tmp_path.replace(path)
        if bak_path is not None and bak_path.exists():
            bak_path.unlink(missing_ok=True)
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        if bak_path is not None and bak_path.exists():
            logger.error(
                "config.yaml write failed — backup preserved at '%s' for manual recovery.",
                bak_path,
            )
        raise
