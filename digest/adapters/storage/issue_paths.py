"""Unchanged symlink guard for compact and prepared delivery files."""

import os
from pathlib import Path


def safe_issue_path(path: Path) -> Path:
    path = Path(os.path.abspath(path))
    if any(item.is_symlink() for item in (path, *path.parents)):
        raise ValueError("Compact issue paths must not contain symlinks.")
    return path
