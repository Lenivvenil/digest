"""Public Python compatibility entrypoints and command-line dispatch.

Applications own execution and effects; this shell supplies terminal/reporting
adapters and deliberately dispatches through its public wrapper names.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import TYPE_CHECKING, Any

from digest.application import discovery, execution
from digest.application.results import RunStats as RunStats
from digest.cli import arguments, diagnostics, reporting

if TYPE_CHECKING:
    from digest.delivery.issue_guard import IssueGuard


async def _run(
    config_path: str, dry_run: bool, radar_only: bool, verbose: bool, *, feedback_precollected: bool = False,
    issue_guard: IssueGuard | None = None, prepare_only: bool = False, edition_date: date | None = None,
) -> RunStats:
    """Delegate to execution._run, preserving its guard-cleanup contract."""
    return await execution._run(
        config_path, dry_run, radar_only, verbose, feedback_precollected=feedback_precollected,
        issue_guard=issue_guard, prepare_only=prepare_only, edition_date=edition_date,
        emit_preview=reporting.emit_preview,
    )


async def run(
    config_path: str, dry_run: bool, radar_only: bool, verbose: bool, *, feedback_precollected: bool = False,
    issue_guard: IssueGuard | None = None, prepare_only: bool = False, edition_date: date | None = None,
) -> RunStats:
    """Delegate to execution.run, preserving its guard-cleanup contract."""
    return await execution.run(
        config_path, dry_run, radar_only, verbose, feedback_precollected=feedback_precollected,
        issue_guard=issue_guard, prepare_only=prepare_only, edition_date=edition_date,
        emit_preview=reporting.emit_preview,
    )


async def check_config(config_path: str) -> int:
    """Run the explicit CLI diagnostics command."""
    return await diagnostics.check_config(config_path)


async def discover_sources(
    config_path: str, *, phase: str = "all", pending_sha: str | None = None,
    delivery_sha: str | None = None,
) -> int:
    """Report durable preparation before dispatching the same discovery session."""
    session = discovery.start_session(config_path, phase)
    counts = None
    if phase in {"prepare", "all"}:
        prepared = await discovery.prepare_and_reserve(session)
        # This write is an effect barrier, including local all: failure prevents send.
        reporting.publish_discovery_prepared(prepared)
        pending_sha, delivery_sha, counts = prepared.pending_sha, prepared.delivery_sha, prepared.counts
        if phase == "prepare":
            result = discovery.DiscoveryResult("prepare", counts)
            reporting.print_discovery(result)
            return result.exit_code
    if phase in {"send", "all"}:
        result = await discovery.send_reserved(session, pending_sha, delivery_sha, counts)
        reporting.print_discovery(result)
        return result.exit_code
    return 0


async def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint. Returns exit code (0 = success, 1 = critical failure)."""
    args = arguments.parse_args(argv)
    reporting.setup_logging(args.verbose)

    try:
        from digest.edition_runtime import validate_cli
        validate_cli(args)
        if args.edition_phase:
            from digest.edition_runtime import delivery_phase
            return await delivery_phase(args.edition_phase, args.config, args.ready_sha, args.claim_sha)
        if ((args.discovery_phase != "all" or args.discovery_pending_sha or args.discovery_delivery_sha)
                and not args.discover):
            raise ValueError("Discovery phases require --discover.")
        if args.reserve_issue or args.issue_reservation_sha:
            if args.check or args.discover or args.dry_run or args.radar_only:
                raise ValueError("Issue reservation cannot be combined with check, discovery or preview modes.")
            from digest.config import load_config
            if load_config(args.config).telegram.delivery_mode != "compact":
                raise ValueError("Issue reservation requires telegram.delivery_mode: compact.")
        if args.reserve_issue:
            if args.issue_reservation_sha:
                raise ValueError("Reserve and publish are separate persistence phases.")
            from digest.delivery.issue_guard import reserve
            path, digest = reserve(args.config)
            reporting.publish_issue_reservation(path, digest)
            return 0
        if args.check:
            return await check_config(args.config)
        if args.discover:
            return await discover_sources(args.config, phase=args.discovery_phase,
                                          pending_sha=args.discovery_pending_sha,
                                          delivery_sha=args.discovery_delivery_sha)

        issue_guard = None
        if args.issue_reservation_sha:
            from digest.delivery.issue_guard import load_guard
            issue_guard = load_guard(args.config, args.issue_reservation_sha)
        run_options: dict[str, Any] = {"feedback_precollected": args.feedback_precollected}
        if args.prepare_edition:
            run_options["prepare_only"] = True
            run_options["edition_date"] = date.fromisoformat(args.edition_date) if args.edition_date else None
        if issue_guard is not None:
            run_options["issue_guard"] = issue_guard
        stats = await run(args.config, args.dry_run, args.radar_only, args.verbose, **run_options)
        reporting.print_stats(stats)
        if args.prepare_edition:
            from digest.edition_runtime import publish_outputs
            publish_outputs(edition_status=stats.edition_status or "no_ready", ready_sha256=stats.ready_sha256)
            return 1 if stats.edition_status in {"held", "selection_incomplete"} else 0

        if stats.required_delivery_failed:
            logging.getLogger(__name__).error("Required Telegram article delivery did not complete.")
            return 1
        if stats.telegram_partial and not stats.markdown_saved:
            return 1
        if (
            not args.dry_run
            and not args.radar_only
            and stats.new_articles > 0
            and not (stats.telegram_sent or stats.markdown_saved or stats.telegram_partial)
        ):
            logging.getLogger(__name__).error(
                "No delivery channel produced output despite %d new articles — "
                "check Telegram credentials and obsidian config.",
                stats.new_articles,
            )
            return 1
        if not args.dry_run and not args.radar_only:
            reporting.publish_review_checkpoint(stats)
        return 0
    except FileNotFoundError as exc:
        logging.getLogger(__name__).error("Config file not found: %s", exc)
        return 1
    except ValueError as exc:
        logging.getLogger(__name__).error("Configuration error: %s", exc)
        return 1
    except Exception as exc:
        logging.getLogger(__name__).error("Unexpected error: %s", exc, exc_info=True)
        return 1
