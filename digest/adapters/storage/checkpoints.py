"""Filesystem path guards for retained checkpoints."""

from __future__ import annotations

import os
from pathlib import Path


def safe_checkpoint_path(path: Path) -> Path:
    path = Path(os.path.abspath(path))
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError("Preparation checkpoint paths must not contain symlinks.")
    return path
