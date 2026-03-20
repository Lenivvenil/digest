"""Shared utilities for the digest package."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def atomic_json_write(path: Path, data: Any) -> None:
    """Write *data* as JSON to *path* atomically (write-to-tmp-then-rename).

    Prevents data corruption if the process crashes mid-write.
    tmp.replace(path) is atomic on POSIX.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    tmp.replace(path)
