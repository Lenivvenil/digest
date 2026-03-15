"""Markdown file output for Obsidian — saves digest as a dated markdown file."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from src.config import Config

logger = logging.getLogger(__name__)


def write_digest(
    summary: str,
    config: Config,
    *,
    date: datetime | None = None,
    sources_count: int = 0,
    articles_count: int = 0,
) -> Path | None:
    """Write digest to a markdown file with YAML frontmatter.

    Returns the path to the written file, or None if delivery is disabled.

    Args:
        summary: LLM-generated summary (already markdown).
        config: Loaded application config.
        date: Date to use for the filename and frontmatter. Defaults to today UTC.
        sources_count: Number of sources fetched (for frontmatter metadata).
        articles_count: Number of articles included (for frontmatter metadata).
    """
    if not config.delivery.markdown_to_repo:
        logger.debug("Markdown delivery disabled, skipping file write.")
        return None

    if date is None:
        date = datetime.now(timezone.utc)

    date_str = date.strftime("%Y-%m-%d")
    output_dir = Path(config.delivery.markdown_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    file_path = output_dir / f"{date_str}.md"

    frontmatter = _build_frontmatter(
        date_str=date_str,
        sources_count=sources_count,
        articles_count=articles_count,
        llm_provider=config.llm.provider,
    )

    content = frontmatter + "\n" + summary + "\n"
    file_path.write_text(content, encoding="utf-8")

    logger.info("Digest written to %s (%d bytes)", file_path, len(content))
    return file_path


def _build_frontmatter(
    *,
    date_str: str,
    sources_count: int,
    articles_count: int,
    llm_provider: str,
) -> str:
    """Build YAML frontmatter block for the digest file."""
    title = f"Daily Digest {date_str}"
    lines = [
        "---",
        f"title: {title}",
        f"date: {date_str}",
        f"sources_count: {sources_count}",
        f"articles_count: {articles_count}",
        f"llm_provider: {llm_provider}",
        "tags: [digest, daily]",
        "---",
    ]
    return "\n".join(lines) + "\n"
