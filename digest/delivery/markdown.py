"""Markdown file output for Obsidian."""

from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from digest._util import atomic_json_write
from digest.delivery.supplement import signal_text

if TYPE_CHECKING:
    from digest.irritator import IrritatorStatus
    from digest.review import BlindReviewReport

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


def _build_top_articles_section(top_articles: list[Any]) -> str:
    """Build Obsidian callout block for per-article LLM summaries."""
    if not top_articles:
        return ""

    lines = ["\n\n## Top Articles\n"]
    for a in top_articles:
        lines.append(
            f"> [!note] [{a.title}]({a.link})\n"
            f"> {a.summary}\n"
            f"> *{a.source} · {a.category}*\n"
        )
    return "\n".join(lines)


def _build_counter_signals_section(ranked_signals: list[Any]) -> str:
    """Build Obsidian callout block for counter-signals."""
    if not ranked_signals:
        return ""

    lines = ["\n\n## \u26a0\ufe0f Counter-Signals\n"]
    for ranked in ranked_signals:
        lines.append(
            "> [!warning]\n" + "\n".join(f"> {line}" for line in signal_text(ranked).split("\n")) + "\n"
        )
    return "\n".join(lines)


def write_digest(
    summary: str,
    config: Any,
    *,
    top_articles: list[Any] | None = None,
    ranked_signals: list[Any] | None = None,
    review_report: BlindReviewReport | None = None,
    irritator_status: IrritatorStatus | None = None,
    date: datetime | None = None,
    sources_count: int = 0,
    articles_count: int = 0,
) -> Path | None:
    """Write digest markdown file to the configured output directory.

    Same-day runs use numbered files so a retry cannot overwrite earlier output.
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

    if top_articles:
        content += _build_top_articles_section(top_articles)

    if ranked_signals:
        content += _build_counter_signals_section(ranked_signals)

    if irritator_status is not None:
        content += f"\n\nIrritator status: {irritator_status.level} — {irritator_status.text}\n"

    if review_report is not None:
        from digest.review import render_review

        content += render_review(review_report) + "\n"

    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        run_number = 1
        while True:
            suffix = "" if run_number == 1 else f"-{run_number}"
            file_path = output_dir / f"{date_str}{suffix}.md"
            try:
                # Exclusive creation preserves previous runs, even during a race.
                handle = file_path.open("x", encoding="utf-8")
            except FileExistsError:
                run_number += 1
                continue
            try:
                with handle:
                    handle.write(content)
            except OSError:
                file_path.unlink(missing_ok=True)
                raise
            if review_report is not None:
                atomic_json_write(file_path.with_suffix(".review.json"), asdict(review_report))
            logger.info("Digest written to %s", file_path)
            return file_path
    except OSError as exc:
        logger.error("Failed to write digest file: %s", exc)
        return None
