"""Comment-preserving, idempotent trial-source additions to runtime YAML."""

from __future__ import annotations

import logging
import shutil
import textwrap
from pathlib import Path
from typing import Any

import yaml

from digest.domain.catalog.proposals import PendingSource

logger = logging.getLogger(__name__)


def add_source_to_config(config_path: str, source: PendingSource) -> None:
    """Atomically add an idempotent trial source to the YAML sources list."""
    path = Path(config_path)
    with path.open("r", encoding="utf-8") as fh:
        content = fh.read()
    config = yaml.safe_load(content)
    if not isinstance(config, dict):
        raise ValueError("Cannot add a source: config must be a YAML mapping.")
    sources = config.setdefault("sources", [])
    if not isinstance(sources, list) or any(not isinstance(item, dict) for item in sources):
        raise ValueError("Cannot add a source: config.sources must be a list of mappings.")
    if any(item.get("url") == source.url for item in sources):
        logger.info("Source '%s' already exists in config (URL match), skipping", source.name)
        return
    entry = {
        "name": source.name,
        "url": source.url,
        "category": source.category,
        "enabled": True,
        "priority": 3,
        "trial": True,
        "trial_days": 14,
    }
    new_content = _insert_source_entry(content, entry)
    # Check the text edit before touching the file, including unusual YAML styles.
    updated = yaml.safe_load(new_content)
    expected = {**config, "sources": [*sources, entry]}
    if updated != expected:
        raise ValueError("Cannot safely insert a source into this YAML config layout.")

    bak_path = path.with_suffix(".yaml.bak")
    tmp_path = path.with_suffix(".yaml.tmp")
    try:
        shutil.copy2(path, bak_path)
    except OSError as exc:
        logger.error("Cannot create config backup, aborting source addition: %s", exc)
        raise

    try:
        with tmp_path.open("w", encoding="utf-8") as fh:
            fh.write(new_content)
        tmp_path.replace(path)
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        if bak_path.exists():
            logger.error(
                "config.yaml write failed — backup preserved at '%s' for manual recovery.",
                bak_path,
            )
        raise
    try:
        bak_path.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("Source added, but config backup cleanup failed: %s", exc)
    logger.info("Added trial source '%s' to config", source.name)


def _insert_source_entry(content: str, entry: dict[str, Any]) -> str:
    """Locate the sources sequence with YAML nodes and preserve existing text."""
    root = yaml.compose(content)
    if not isinstance(root, yaml.MappingNode):
        raise ValueError("Cannot add a source: config must be a YAML mapping.")
    matches = [value for key, value in root.value if key.value == "sources"]
    block = yaml.safe_dump([entry], sort_keys=False, allow_unicode=True)
    if not matches:
        return content.rstrip("\n") + "\nsources:\n" + textwrap.indent(block, "  ")
    if len(matches) != 1 or not isinstance(matches[0], yaml.SequenceNode):
        raise ValueError("Cannot add a source: config must have one sources list.")
    node = matches[0]
    if node.flow_style:
        # An inline sources list can be extended without rewriting its comments.
        index = node.end_mark.index - 1
        if content[index : index + 1] != "]":
            raise ValueError("Cannot safely insert a source into this YAML config layout.")
        serialized = yaml.safe_dump(entry, default_flow_style=True, sort_keys=False, allow_unicode=True).strip()
        separator = ", " if node.value else ""
        return content[:index] + separator + serialized + content[index:]
    if content[node.start_mark.index : node.start_mark.index + 1] != "-":
        raise ValueError("Cannot safely insert a source into an aliased or anchored sources list.")
    index = node.end_mark.index
    # The end mark may include indentation preceding the next top-level key.
    line_start = index - node.end_mark.column
    if not content[line_start:index].strip():
        index = line_start
    prefix = content[:index]
    if prefix and not prefix.endswith("\n"):
        prefix += "\n"
    return prefix + textwrap.indent(block, " " * node.start_mark.column) + content[index:]
