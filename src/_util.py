"""Shared utilities for the digest package."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def atomic_json_write(path: Path, data: Any) -> None:
    """Write *data* as JSON to *path* atomically (write-to-tmp-then-rename).

    Prevents data corruption if the process crashes mid-write.
    tmp.replace(path) is atomic on POSIX.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    tmp.replace(path)


def cleanup_stale_tmp(directory: Path, max_age_seconds: int = 300) -> None:
    """Remove orphaned .tmp files older than *max_age_seconds*.

    These can accumulate if the process crashes between write and rename
    in atomic_json_write().
    """
    now = time.time()
    for tmp in directory.glob("*.tmp"):
        try:
            age = now - tmp.stat().st_mtime
            if age > max_age_seconds:
                tmp.unlink(missing_ok=True)
                logger.info("Removed stale tmp file: %s (age %.0fs)", tmp, age)
        except OSError:
            pass
