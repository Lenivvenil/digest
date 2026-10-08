"""Preparation and sender entrypoints around the immutable edition boundary."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

from digest.application.results import RunStats

if TYPE_CHECKING:
    from digest.adapters.models.execution import ModelExecution
    from digest.domain.delivery.outcomes import IssueDeliveryResult
    from digest.domain.editorial.attempts import ResolvedReview
    from digest.preparation import AcceptedPreparation, PreparationSnapshot

logger = logging.getLogger(__name__)
EditionStatus = Literal["held", "ready", "pending_window", "confirmed"]


def publish_outputs(**values: str) -> None:
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with Path(output).open("a", encoding="utf-8") as handle:
            for key, value in values.items():
                if "\n" in value or "\r" in value:
                    raise ValueError("Invalid edition output value.")
                handle.write(f"{key}={value}\n")


def _stats(status: str, *, feedback: int = 0, ready_sha: str = "") -> RunStats:
    return RunStats(
        0, 0, 0, False, False, False, "", feedback_collected=feedback, edition_status=status, ready_sha256=ready_sha
    )


@dataclass(frozen=True)
class ExistingEdition:
    """An inspected edition blocks fresh work, including conservative dispatch holds."""

    status: EditionStatus
    ready_sha256: str


class FreshPreparation(Enum):
    REQUIRED = "fresh_work_required"


@dataclass(frozen=True)
class IncompleteSelection:
    review_status: str


@dataclass(frozen=True)
class NoEdition:
    status: Literal["no_ready", "selection_incomplete"]
    review_status: str


@dataclass(frozen=True)
class FrozenPreparation:
    status: Literal["ready", "pending_window"]
    ready_sha256: str
    review_status: str
    digest_length: int
    archive: Path | None
    review_checkpoint: str


def preparation_stats(outcome: ExistingEdition | FrozenPreparation | NoEdition, feedback: int = 0) -> RunStats:
    """Project preparation evidence into the stable public reporting contract."""
    stats = _stats(
        outcome.status, feedback=feedback, ready_sha=outcome.ready_sha256 if not isinstance(outcome, NoEdition) else ""
    )
    if not isinstance(outcome, ExistingEdition):
        stats.review_status = outcome.review_status
    if isinstance(outcome, FrozenPreparation):
        stats.digest_length = outcome.digest_length
        stats.markdown_saved = outcome.archive is not None
        stats.markdown_path = str(outcome.archive) if outcome.archive is not None else ""
        stats.review_checkpoint = outcome.review_checkpoint
    return stats


def inspect_preparation(publication_date: date | None = None) -> ExistingEdition | FreshPreparation:
    from digest.application.prepared_delivery import inspect_edition

    _, digest, status = inspect_edition()
    if status == "confirmed" and publication_date and publication_date > datetime.now(timezone.utc).date():
        return FreshPreparation.REQUIRED
    if status in {"ready", "confirmed", "held", "pending_window"}:
        logger.info("Edition preparation: %s", status)
        return ExistingEdition(cast(EditionStatus, status), digest)
    return FreshPreparation.REQUIRED


def recover_preparation(
    publication_date: date | None = None,
) -> ExistingEdition | AcceptedPreparation | FreshPreparation:
    """Inspect frozen/held work before restoring canonical work; never collect or present."""
    from digest.preparation import load_accepted_preparation

    existing = inspect_preparation(publication_date)
    if isinstance(existing, ExistingEdition):
        return existing
    accepted = load_accepted_preparation(publication_date=publication_date)
    return accepted if accepted is not None else FreshPreparation.REQUIRED


def existing_preparation(
    config: Any,
    feedback: int = 0,
    publication_date: date | None = None,
) -> RunStats | None:
    """Compatibility projection of edition inspection."""
    existing = inspect_preparation(publication_date)
    return preparation_stats(existing, feedback) if isinstance(existing, ExistingEdition) else None


async def present_preparation(
    accepted: AcceptedPreparation,
    config: Any,
    *,
    execution: ModelExecution,
    verbose: bool = False,
    publication_date: date | None = None,
) -> FrozenPreparation | NoEdition:
    """Present exactly the canonical work verified at acceptance or recovery."""
    return await _present_snapshot(
        accepted.snapshot,
        config,
        execution=execution,
        verbose=verbose,
        publication_date=publication_date,
    )


async def finish_preparation(
    snapshot: PreparationSnapshot,
    config: Any,
    feedback: int = 0,
    *,
    execution: ModelExecution,
    verbose: bool = False,
    publication_date: date | None = None,
) -> RunStats:
    """Legacy snapshot API; ordinary preparation uses a verified accepted reference."""
    outcome = await _present_snapshot(
        snapshot,
        config,
        execution=execution,
        verbose=verbose,
        publication_date=publication_date,
    )
    return preparation_stats(outcome, feedback)


async def _present_snapshot(
    snapshot: PreparationSnapshot,
    config: Any,
    *,
    execution: ModelExecution,
    verbose: bool = False,
    publication_date: date | None = None,
) -> FrozenPreparation | NoEdition:
    """Shared presentation implementation; editorial acceptance belongs to the caller."""
    from digest.application.prepared_delivery import prepare_edition
    from digest.application.publication import assemble_publication
    from digest.delivery import write_digest
    from digest.preparation import clear_preparation

    review_status = snapshot.review_report.status if snapshot.review_report is not None else "not_requested"
    if not snapshot.top_articles:
        logger.info("Edition preparation: no selected articles; no ready edition created")
        return NoEdition("no_ready", review_status)
    publication = await assemble_publication(snapshot, config, execution=execution, verbose=verbose)
    archive = write_digest(
        publication.combined,
        config,
        top_articles=publication.cards,
        ranked_signals=list(publication.ranked_signals) or None,
        review_report=snapshot.review_report,
        irritator_status=publication.irritator_status,
        sources_count=snapshot.source_count,
        articles_count=snapshot.article_count,
        date=datetime.combine(publication_date, datetime.min.time(), timezone.utc) if publication_date else None,
    )
    # An enabled but failed archive is a preparation failure, never sender eligibility.
    if config.obsidian.enabled and archive is None:
        raise ValueError("Edition archive persistence failed; canonical preparation remains resumable.")
    references: dict[str, Any] = {}
    if archive is not None:
        paths = [archive]
        if snapshot.review_report is not None:
            from digest.adapters.storage.candidate_progress import (
                archive_candidate_accounting,
                candidate_accounting_sources,
            )

            paths.append(archive.with_suffix(".review.json"))
            accounting_path = archive_candidate_accounting(snapshot.review_report, archive)
            if accounting_path is not None:
                paths.append(accounting_path)
                paths.extend(candidate_accounting_sources(snapshot.review_report))
        for path in paths:
            relative = path.resolve().relative_to(Path.cwd().resolve()).as_posix()
            references[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    canonical, presentation = publication.metadata()
    _, digest = prepare_edition(
        publication.cards,
        config,
        notice=publication.combined,
        canonical_metadata=canonical,
        presentation_metadata=presentation,
        checkpoint_refs=references,
        producing_engine=_engine_provenance(),
        publication_date=publication_date,
    )
    clear_preparation()
    future_window = publication_date and publication_date > datetime.now(timezone.utc).date()
    return FrozenPreparation(
        "pending_window" if future_window else "ready",
        digest,
        review_status,
        len(publication.combined),
        archive,
        str(archive.with_suffix(".review.json")) if archive is not None and snapshot.review_report is not None else "",
    )


def _strict_cache(path: Path) -> dict[str, str]:
    from digest.adapters.storage.delivery_state import load_delivery_cache

    return load_delivery_cache(path)


def _merge_delivery(result: IssueDeliveryResult, manifest: dict[str, Any], config: Any) -> None:
    """Compatibility entrypoint for prepared outcome application."""
    from digest.application.delivery import PreparedOutcomePolicy, apply_confirmed_outcome

    if not result.delivered_hashes:
        return
    adaptive_enabled = getattr(getattr(config, "adaptive", None), "enabled", False)
    apply_confirmed_outcome(
        PreparedOutcomePolicy(
            outcome=result,
            cache_dir=".cache",
            publication_day=datetime.fromisoformat(manifest["window_start"]).date(),
            contributing_sources=manifest["canonical_metadata"]["contributing_sources"],
            adaptive_enabled=adaptive_enabled,
            enabled_sources=config.enabled_sources if adaptive_enabled else [],
        )
    )


async def delivery_phase(phase: str, config_path: str, ready_sha: str | None, claim_sha: str | None) -> int:
    from digest.adapters.storage.feedback import load_feedback
    from digest.application.prepared_delivery import claim_edition, inspect_edition, mark_applied, send_prepared_edition
    from digest.config import load_config

    config = load_config(config_path)
    manifest, actual_sha, status = inspect_edition()
    publish_outputs(edition_status=status, ready_sha256=actual_sha)
    if status == "confirmed":
        return 0
    if phase == "inspect":
        return 1 if status == "held" else 0
    if not config.telegram.enabled:
        raise ValueError("Telegram delivery is disabled; saved edition remains unsent.")
    if not ready_sha or ready_sha != actual_sha or manifest is None:
        raise ValueError("Sender requires the exact remotely persisted ready edition.")
    # Validate writable attribution state before claiming or attempting any POST.
    load_feedback(".cache", strict=True)
    _strict_cache(Path(".cache/seen_articles.json"))
    if phase == "claim":
        _, digest = claim_edition(ready_sha)
        publish_outputs(claim_sha256=digest)
        return 0
    if not claim_sha:
        raise ValueError("Sender requires the exact remotely persisted claim.")
    result = await send_prepared_edition(
        ready_sha,
        claim_sha,
        enabled=config.telegram.enabled,
        bot_username=config.telegram.bot_username,
    )
    _merge_delivery(result, manifest, config)
    mark_applied(ready_sha)
    checkpoint = ""
    if result.complete:
        checkpoint = next((name for name in manifest["checkpoint_refs"] if name.endswith(".review.json")), "")
    publish_outputs(edition_status="confirmed" if result.complete else result.outcome, review_checkpoint=checkpoint)
    return 0 if result.complete else 1


async def resume_preparation(
    config: Any,
    feedback: int,
    *,
    execution: ModelExecution,
    verbose: bool,
    publication_date: date | None = None,
) -> RunStats | None:
    from digest.preparation import AcceptedPreparation

    recovered = recover_preparation(publication_date)
    if isinstance(recovered, ExistingEdition):
        return preparation_stats(recovered, feedback)
    if isinstance(recovered, AcceptedPreparation):
        outcome = await present_preparation(
            recovered,
            config,
            verbose=verbose,
            publication_date=publication_date,
            execution=execution,
        )
        return preparation_stats(outcome, feedback)
    return None


def validate_cli(args: Any) -> None:
    if args.edition_date:
        if not args.prepare_edition or date.fromisoformat(args.edition_date).isoformat() != args.edition_date:
            raise ValueError("An intended UTC edition date requires --prepare-edition and YYYY-MM-DD.")
        if date.fromisoformat(args.edition_date) < datetime.now(timezone.utc).date():
            raise ValueError("Cannot prepare an edition for an elapsed UTC publication day.")
    if args.prepare_edition or args.edition_phase:
        if (
            args.check
            or args.discover
            or args.dry_run
            or args.radar_only
            or args.reserve_issue
            or args.issue_reservation_sha
            or args.prepare_edition
            and args.edition_phase
        ):
            raise ValueError("Edition phases cannot be combined with legacy dispatch or preview modes.")
        from digest.config import load_config

        if load_config(args.config).telegram.delivery_mode != "compact":
            raise ValueError("Prepared editions require compact delivery mode.")
    if (args.ready_sha or args.claim_sha) and not args.edition_phase:
        raise ValueError("Edition hashes require an explicit sender phase.")


def _engine_provenance() -> dict[str, Any]:
    from importlib.metadata import PackageNotFoundError, distribution

    from digest import __version__

    provenance: dict[str, Any] = {"version": __version__}
    try:
        raw = distribution("digest").read_text("direct_url.json")
        if raw:
            vcs = json.loads(raw).get("vcs_info", {})
            if isinstance(vcs.get("commit_id"), str):
                provenance["commit"] = vcs["commit_id"]
    except (PackageNotFoundError, ValueError):
        pass
    return provenance


def _acceptance(
    snapshot: PreparationSnapshot,
    result: ResolvedReview,
) -> Literal["selection", "abstention", "selection_incomplete"]:
    """Publication projection and editorial abstention remain distinct gates."""
    if snapshot.top_articles:
        return "selection"
    report = snapshot.review_report
    if report is None:
        return "selection_incomplete"
    return "abstention" if result.outcome == "primary_abstained" else "selection_incomplete"


def accept_preparation(
    snapshot: PreparationSnapshot,
    result: ResolvedReview,
    *,
    cache_dir: str,
    publication_date: date | None = None,
) -> AcceptedPreparation | IncompleteSelection:
    """Decide, save and verify the exact canonical work before any candidate handoff."""
    from digest.preparation import load_accepted_preparation, save_preparation

    if snapshot.review_report != result.report:
        raise ValueError("Preparation does not bind the resolved candidate review.")
    decision = _acceptance(snapshot, result)
    if decision == "selection_incomplete":
        review_status = snapshot.review_report.status if snapshot.review_report is not None else "not_requested"
        # Retain the existing validation read and clock boundary before fetch
        # statistics, including incomplete work. Presence never proves acceptance.
        load_accepted_preparation(cache_dir, publication_date=publication_date)
        return IncompleteSelection(review_status)
    path = save_preparation(snapshot, cache_dir=cache_dir, publication_date=publication_date)
    accepted = load_accepted_preparation(cache_dir, publication_date=publication_date)
    if accepted is None or accepted.path != path or accepted.snapshot != snapshot:
        raise ValueError("Accepted preparation readback differs from the saved canonical work; handoff blocked.")
    return accepted
