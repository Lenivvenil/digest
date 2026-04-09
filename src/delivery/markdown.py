"""Markdown file output for Obsidian."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _build_frontmatter(
    date_str: str,
    sources_count: int,
    articles_count: int,
) -> str:
    """Build YAML frontmatter for the digest file."""
    return (
        "---\n"
        f"title: Daily Digest {date_str}\n"
        f"date: {date_str}\n"
        f"sources_count: {sources_count}\n"
        f"articles_count: {articles_count}\n"
        "tags: [digest, daily]\n"
        "---\n"
    )


def _build_counter_signals_section(ranked_signals: list[Any]) -> str:
    """Build Obsidian callout block for counter-signals."""
    if not ranked_signals:
        return ""

    lines = ["\n\n## \u26a0\ufe0f Counter-Signals\n"]
    for r in ranked_signals:
        lines.append(
            f"> [!warning] [{r.signal.title}]({r.signal.url}) "
            f"\\[{r.score}/10\\]\n"
            f"> {r.reasoning}\n"
            f"> *Narrative: {r.narrative_claim[:100]}*\n"
        )
    return "\n".join(lines)


def write_digest(
    summary: str,
    config: Any,
    *,
    ranked_signals: list[Any] | None = None,
    date: datetime | None = None,
    sources_count: int = 0,
    articles_count: int = 0,
) -> Path | None:
    """Write digest markdown file to the configured output directory.

    Returns the file path on success, None on error.
    """
    if not config.obsidian.enabled:
        logger.debug("Obsidian output disabled in config")
        return None

    dt = date or datetime.now(timezone.utc)
    date_str = dt.strftime("%Y-%m-%d")
    output_dir = Path(config.obsidian.output_dir)

    frontmatter = _build_frontmatter(date_str, sources_count, articles_count)
    content = f"{frontmatter}\n{summary}\n"

    if ranked_signals:
        content += _build_counter_signals_section(ranked_signals)

    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        file_path = output_dir / f"{date_str}.md"
        file_path.write_text(content, encoding="utf-8")
        logger.info("Digest written to %s", file_path)
        return file_path
    except OSError as exc:
        logger.error("Failed to write digest file: %s", exc)
        return None
