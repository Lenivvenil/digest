"""Pending proposal file codec; storage retention is distinct from decision eligibility."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from digest._util import atomic_json_write
from digest.domain.catalog.proposals import PendingSource, source_hash

logger = logging.getLogger(__name__)
PENDING_FILE = "pending_sources.json"


def load_pending(cache_dir: str, *, strict: bool = False) -> list[PendingSource]:
    """Load proposals; strict mode raises on unreadable or malformed cache data."""
    path = Path(cache_dir) / PENDING_FILE
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict) or not isinstance(data.get("pending"), list):
            raise ValueError("Pending source cache must contain a pending list.")
        sources: list[PendingSource] = []
        for item in data["pending"]:
            try:
                if not isinstance(item, dict) or any(
                    not isinstance(item.get(key), str) or not item[key].strip()
                    for key in ("name", "url", "category", "discovered_at")
                ):
                    raise ValueError("Pending source entries must contain nonempty string identity fields.")
                datetime.fromisoformat(item["discovered_at"])
                hash8 = item.get("source_hash", source_hash(item["url"]))
                if not isinstance(hash8, str) or not re.fullmatch(r"[0-9a-f]{8}", hash8):
                    raise ValueError("Pending source hashes must contain eight lowercase hexadecimal characters.")
                sources.append(
                    PendingSource(
                        name=item["name"],
                        url=item["url"],
                        category=item["category"],
                        discovered_at=item["discovered_at"],
                        source_hash=hash8,
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                if strict:
                    raise
                logger.warning("Skipping malformed pending source entry: %s", exc)
        return sources
    except FileNotFoundError:
        return []
    except Exception as exc:
        logger.warning("Failed to load pending sources: %s", exc)
        if strict:
            raise
        return []


def save_pending(
    sources: list[PendingSource], cache_dir: str, *, strict: bool = False,
) -> None:
    """Save pending sources, pruning old entries; optionally propagate I/O errors."""
    path = Path(cache_dir) / PENDING_FILE
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=30)
    pruned: list[PendingSource] = []
    for s in sources:
        try:
            ts = datetime.fromisoformat(s.discovered_at)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts >= cutoff:
                pruned.append(s)
        except ValueError:
            pruned.append(s)
    data = {"pending": [asdict(s) for s in pruned]}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json_write(path, data)
    except Exception as exc:
        logger.warning("Failed to save pending sources: %s", exc)
        if strict:
            raise
