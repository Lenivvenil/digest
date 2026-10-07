"""Validate and select a digest application, with a distinct outer guard lifetime."""
from __future__ import annotations

import time
from collections.abc import Callable
from datetime import date
from typing import TYPE_CHECKING

from digest.application import legacy
from digest.application.results import Preview, RunStats

if TYPE_CHECKING:
    from digest.config import Config
    from digest.delivery.issue_guard import IssueGuard


def _validate_compact_run(
    compact: bool, dry_run: bool, radar_only: bool, guard: IssueGuard | None, prepare_only: bool,
) -> None:
    if compact and not dry_run and not radar_only and guard is None and not prepare_only:
        raise ValueError("Compact publication requires an externally persisted issue reservation.")


def _validate_closing_mode(config: Config, prepare_only: bool) -> None:
    if getattr(getattr(config, "closing", None), "enabled", False) and not prepare_only:
        raise ValueError("Closing items require immutable edition preparation (--prepare-edition).")


async def _run(
    config_path: str, dry_run: bool, radar_only: bool, verbose: bool, *, feedback_precollected: bool = False,
    issue_guard: IssueGuard | None = None, prepare_only: bool = False, edition_date: date | None = None,
    emit_preview: Callable[[Preview], None],
) -> RunStats:
    """Resolve the application once; each workflow owns its execution sequence."""
    from digest.application.preparation import prepare_edition, prepare_sources
    from digest.config import load_config
    from digest.reading_preparation import validate_reading_mode

    if prepare_only and (dry_run or radar_only):
        raise ValueError("Edition preparation cannot be combined with preview modes.")
    started_at = time.monotonic()
    config = load_config(config_path)
    validate_reading_mode(config, prepare_only)
    _validate_closing_mode(config, prepare_only)
    compact = getattr(config.telegram, "delivery_mode", "cards") == "compact"
    _validate_compact_run(compact, dry_run, radar_only, issue_guard, prepare_only)
    if prepare_only:
        prepare = prepare_sources if config.reading_brief.enabled else prepare_edition
        return await prepare(config, config_path, verbose=verbose, feedback_precollected=feedback_precollected,
                             publication_date=edition_date, started_at=started_at)
    return await legacy.run_legacy(
        config, config_path, dry_run, radar_only, verbose, started_at=started_at,
        feedback_precollected=feedback_precollected, issue_guard=issue_guard, emit_preview=emit_preview,
    )


async def run(
    config_path: str, dry_run: bool, radar_only: bool, verbose: bool, *, feedback_precollected: bool = False,
    issue_guard: IssueGuard | None = None, prepare_only: bool = False, edition_date: date | None = None,
    emit_preview: Callable[[Preview], None],
) -> RunStats:
    """Finalize coarse issue state even when analysis or delivery exits early."""
    try:
        return await _run(config_path, dry_run, radar_only, verbose,
                          feedback_precollected=feedback_precollected, issue_guard=issue_guard,
                          prepare_only=prepare_only, edition_date=edition_date, emit_preview=emit_preview)
    finally:
        if issue_guard is not None:
            if issue_guard.state == "reserved":
                issue_guard.finish("not_sent")
            elif issue_guard.state == "sending":
                issue_guard.finish("unknown")
