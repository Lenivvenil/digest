"""Strict delivery-state persistence without changing existing JSON records.

Each write is individually atomic and propagates failures. The application owns
their order; partially persisted outcomes require inspection, not replay.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING

from digest._util import atomic_json_write

if TYPE_CHECKING:
    from digest.source_scorer import SourceStateStore, SourceStats


def load_delivery_cache(path: Path) -> dict[str, str]:
    """Fail closed on malformed prepared-delivery deduplication state."""
    if not path.exists():
        return {}
    if path.is_symlink():
        raise ValueError("Delivery cache must not be a symlink.")
    raw = json.loads(path.read_text())
    if not isinstance(raw, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in raw.items()):
        raise ValueError("Invalid delivered article cache; sending blocked.")
    return raw


def save_delivery_cache(cache: dict[str, str], cache_dir: str) -> None:
    """Write confirmed article timestamps strictly, without cache pruning."""
    atomic_json_write(Path(cache_dir) / "seen_articles.json", cache)


def save_delivery_source_stats(stats: dict[str, SourceStats], cache_dir: str) -> None:
    """Write prepared delivery accounting strictly, without source pruning."""
    atomic_json_write(Path(cache_dir) / "source_stats.json", {name: asdict(value) for name, value in stats.items()})


def save_delivery_source_state(state: SourceStateStore, cache_dir: str) -> None:
    """Write prepared adaptive decisions strictly, retaining their schema."""
    atomic_json_write(Path(cache_dir) / "source_state.json", asdict(state))
