"""Source quality scoring: data models, statistics tracking, and scoring engine."""

from __future__ import annotations

import json
import logging
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
            history = [
                DailySnapshot(
                    date=snap["date"],
                    articles_found=snap["articles_found"],
                    articles_included=snap["articles_included"],
                    fetch_ok=snap["fetch_ok"],
                )
                for snap in raw.get("history", [])
            ]
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
        return result
    except Exception as exc:
        logger.warning("Failed to load source stats: %s", exc)
        return {}


def save_stats(stats: dict[str, SourceStats], cache_dir: str) -> None:
    """Serialize source statistics to JSON."""
    path = Path(cache_dir) / STATS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
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
        # Rolling average for description length
        if avg_desc_len > 0:
            if s.avg_description_length == 0.0:
                s.avg_description_length = avg_desc_len
            else:
                s.avg_description_length = (
                    s.avg_description_length * 0.7 + avg_desc_len * 0.3
                )

    # Update existing snapshot if already recorded today; otherwise append new one.
    # Use max() to avoid overwriting a successful run's data with a later
    # partial failure, which would corrupt trend detection.
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

    # Cap history at HISTORY_MAX_DAYS
    if len(s.history) > HISTORY_MAX_DAYS:
        s.history = s.history[-HISTORY_MAX_DAYS:]


def calculate_score(stats: SourceStats) -> float:
    """Calculate composite quality score (0.0-1.0) for a source.

    Components:
    - reliability: successful_fetches / total_fetches
    - productivity: articles_included / articles_found
    - description quality: avg_description_length > 100
    - recency: last_seen within 3 days
    """
    if stats.total_fetches == 0:
        return 0.5  # New source with no history gets neutral score

    # Reliability (weight: 0.3)
    reliability = stats.successful_fetches / stats.total_fetches

    # Productivity (weight: 0.3) — based on last 7 snapshots to avoid
    # penalising high-frequency sources (e.g. HN: 30 found / 5 taken = 0.17).
    recent_snaps = stats.history[-7:] if stats.history else []
    recent_found = sum(s.articles_found for s in recent_snaps)
    recent_included = sum(s.articles_included for s in recent_snaps)
    if recent_found == 0:
        productivity = 0.0
    else:
        productivity = min(1.0, recent_included / recent_found)

    # Description quality (weight: 0.2)
    if stats.avg_description_length >= 100:
        desc_quality = 1.0
    else:
        desc_quality = stats.avg_description_length / 100.0

    # Recency (weight: 0.2)
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
    """Return source names where articles_found shows >50% increase.

    Compares the last `window` days against the previous `window` days.
    Sources with insufficient history (fewer than `window` snapshots in
    the recent window) are ignored.
    """
    trending: list[str] = []
    for name, s in stats.items():
        if len(s.history) < window:
            continue
        recent = s.history[-window:]
        previous = s.history[-2 * window : -window] if len(s.history) >= 2 * window else []
        recent_total = sum(snap.articles_found for snap in recent)
        previous_total = sum(snap.articles_found for snap in previous)
        if previous_total == 0:
            # No baseline to compare — cannot determine a trend
            continue
        increase = (recent_total - previous_total) / previous_total
        if increase > 0.5:
            trending.append(name)
    return trending


def calculate_effective_priorities(
    sources: list["SourceConfig"],
    stats: dict[str, SourceStats],
    feedback_scores: dict[str, float],
    adaptive_config: "AdaptiveConfig",
) -> dict[str, int]:
    """Compute effective priorities by combining base priority, quality score, and feedback.

    For each source:
      weighted = (base_priority/5)*base_weight + score*score_weight + feedback*feedback_weight
      scaled to min_priority..max_priority range, with +1 trend bonus (capped).
    """
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
        # Scale weighted (0.0-1.0) to priority range
        priority = round(min_p + weighted * p_range)
        # Apply trend bonus
        if source.name in trending:
            priority += 1
        priority = max(min_p, min(max_p, priority))
        result[source.name] = priority
    return result


def evaluate_trial_sources(
    sources: list["SourceConfig"],
    stats: dict[str, SourceStats],
    today: str,
) -> tuple[list[str], list[str], list[str]]:
    """Evaluate trial sources and decide which to promote or demote.

    Returns (promote_names, demote_names, needs_start_names):
    - promote: trials with score > 0.6 after trial_days elapsed
    - demote: trials with score < 0.3 after trial_days elapsed
    - needs_start: trials with no trial_started date (will be initialized)
    - others remain in trial
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
            # Auto-populate trial_started to today on first encounter
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
    """Find the line range [start, end) of a source entry with the given name.

    Handles both ``- name: X`` (name as first key) and ``- category: ...\n  name: X``
    (name appearing later in the block, e.g. when keys are alphabetically sorted).
    """
    import re as _re

    # First, find all list-item starts under sources
    i = 0
    while i < len(lines):
        # Look for lines that start a list item: "  - key: value"
        match = _re.match(r"^(\s*)-\s+\w+\s*:", lines[i])
        if match:
            indent = len(match.group(1))
            start = i
            # Find end of this block
            end = i + 1
            while end < len(lines):
                stripped = lines[end]
                if stripped.strip() == "" or stripped.lstrip().startswith("#"):
                    end += 1
                    continue
                next_match = _re.match(r"^(\s*)-\s+\S", stripped)
                if next_match and len(next_match.group(1)) <= indent:
                    break
                key_match = _re.match(r"^(\s*)\S", stripped)
                if key_match and len(key_match.group(1)) <= indent:
                    break
                end += 1

            # Check if this block contains `name: <source_name>`
            for k in range(start, end):
                # Match both `- name: X` (first key) and `  name: X` (continuation)
                name_match = _re.match(r"^\s*-?\s*name:\s*(.+?)\s*$", lines[k])
                if name_match and name_match.group(1).strip("\"'") == source_name:
                    return start, end

            i = end
        else:
            i += 1
    return None


def _set_field_in_block(
    lines: list[str], start: int, end: int, field: str, value: str
) -> list[str]:
    """Set or add a YAML field within a source block (lines[start:end]).

    If the field already exists, its value is replaced in-place.
    Otherwise, a new line is appended at the end of the block using
    the indentation of sibling fields.
    """
    import re as _re

    # Detect field indent from first non-name field in the block
    field_indent = "    "
    for k in range(start + 1, end):
        m = _re.match(r"^(\s+)\w", lines[k])
        if m:
            field_indent = m.group(1)
            break

    for k in range(start, end):
        pattern = rf"^(\s+){_re.escape(field)}\s*:.*$"
        if _re.match(pattern, lines[k]):
            lines[k] = f"{field_indent}{field}: {value}"
            return lines

    # Field not found — insert before end of block
    lines.insert(end, f"{field_indent}{field}: {value}")
    return lines


def _remove_field_in_block(
    lines: list[str], start: int, end: int, field: str
) -> tuple[list[str], int]:
    """Remove a YAML field line from a source block. Returns updated lines and new end."""
    import re as _re

    for k in range(start, end):
        if _re.match(rf"^\s+{_re.escape(field)}\s*:.*$", lines[k]):
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

    Uses line-by-line text editing to preserve comments, formatting, and field
    order in the hand-maintained config file.
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
        # Recalculate end after potential insertion
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

    tmp_path = path.with_suffix(".yaml.tmp")
    with tmp_path.open("w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
        if lines:
            fh.write("\n")
    tmp_path.replace(path)
