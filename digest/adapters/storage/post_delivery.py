"""Concrete checkpoint, attempt-marker and archive storage for post-delivery work.

The initial attempt is an exclusive, flushed and fsynced file with a trailing
newline. Later records use the existing atomic JSON replacement independently;
neither mechanism makes the marker and archives a multi-file transaction.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from digest._util import atomic_json_write


def safe_checkpoint_path(path: Path) -> Path:
    """Preserve the repository-relative path guard used by post-delivery prepare."""
    resolved = path.resolve()
    try:
        relative = repository_relative_path(resolved)
    except ValueError as exc:
        raise ValueError("Resume paths must stay inside the current repository.") from exc
    if path.is_symlink() or not re.fullmatch(r"[A-Za-z0-9_./-]+", relative):
        raise ValueError("Resume paths must be plain safe repository-relative paths.")
    return resolved


def repository_relative_path(path: Path) -> str:
    return path.relative_to(Path.cwd().resolve()).as_posix()


def read_checkpoint_bytes(checkpoint: Path) -> bytes:
    return checkpoint.read_bytes()


def require_unchanged_checkpoint(checkpoint: Path, content: bytes) -> None:
    if content != checkpoint.read_bytes():
        raise ValueError("Checkpoint changed while being read.")


def attempt_artifacts_exist(marker: Path, result: Path, markdown: Path) -> bool:
    return any(path.exists() or path.is_symlink() for path in (marker, result, markdown))


def load_attempt(marker: Path, result: Path, markdown: Path) -> dict[str, Any]:
    """Require an existing marker and no result before reading the attempt."""
    if not marker.is_file() or marker.is_symlink():
        raise ValueError("Persisted post-delivery attempt marker is required.")
    if any(path.exists() or path.is_symlink() for path in (result, markdown)):
        raise ValueError("Post-delivery result already exists; refusing another attempt.")
    record: dict[str, Any] = json.loads(marker.read_text())
    return record


def create_attempt(marker: Path, record: dict[str, Any]) -> bool:
    """Exclusively persist the initial claim, leaving a concurrent winner intact."""
    try:
        with marker.open("x", encoding="utf-8") as handle:
            json.dump(record, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        return False
    return True


def save_attempt(marker: Path, record: dict[str, Any]) -> None:
    atomic_json_write(marker, record)


def save_result(result: Path, payload: dict[str, Any]) -> None:
    atomic_json_write(result, payload)


def save_markdown_archive(markdown: Path, content: str) -> None:
    markdown.write_text(content, encoding="utf-8")


def save_failure_archives(result: Path, markdown: Path, payload: dict[str, Any]) -> None:
    """Retain the unexpected-failure JSON and diagnostic archive write order."""
    atomic_json_write(result, payload)
    markdown.write_text("# Irritator incomplete\n" + json.dumps(payload, indent=2) + "\n")


def append_github_output(checkpoint: str, marker: Path) -> None:
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with Path(output).open("a", encoding="utf-8") as handle:
            handle.write(f"checkpoint={checkpoint}\n")
            handle.write(f"marker={repository_relative_path(marker)}\n")
