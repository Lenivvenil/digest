"""Preparation and sender entrypoints around the immutable edition boundary."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import asdict, replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from digest.application.results import RunStats

if TYPE_CHECKING:
    from digest.adapters.models.execution import ModelExecution
    from digest.domain.delivery.outcomes import IssueDeliveryResult
    from digest.preparation import PreparationSnapshot

logger = logging.getLogger(__name__)


def publish_outputs(**values: str) -> None:
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with Path(output).open("a", encoding="utf-8") as handle:
            for key, value in values.items():
                if "\n" in value or "\r" in value:
                    raise ValueError("Invalid edition output value.")
                handle.write(f"{key}={value}\n")


def _stats(status: str, *, feedback: int = 0, ready_sha: str = "") -> RunStats:
    return RunStats(0, 0, 0, False, False, False, "", feedback_collected=feedback,
                    edition_status=status, ready_sha256=ready_sha)


def existing_preparation(
    config: Any, feedback: int = 0, publication_date: date | None = None,
) -> RunStats | None:
    from digest.delivery.edition import inspect_edition

    _, digest, status = inspect_edition()
    if status == "confirmed" and publication_date and publication_date > datetime.now(timezone.utc).date():
        return None
    if status in {"ready", "confirmed", "held", "pending_window"}:
        logger.info("Edition preparation: %s", status)
        return _stats(status, feedback=feedback, ready_sha=digest)
    return None


async def finish_preparation(
    snapshot: PreparationSnapshot, config: Any, feedback: int = 0, *, execution: ModelExecution, verbose: bool = False,
    publication_date: date | None = None, selection_complete: bool = True,
) -> RunStats:
    """Resume only presentation; accepted canonical work is already saved."""
    from digest.application.investigation import run_irritator
    from digest.application.presentation import deferred_review_status, publication_presentation
    from digest.application.source_attribution import main_attribution_occurrences
    from digest.closing import attribute_closing_card
    from digest.delivery import write_digest
    from digest.delivery.edition import prepare_edition
    from digest.delivery.telegram import _render_compact_issue
    from digest.domain.catalog.occurrences import occurrence_sha256
    from digest.irritator import IrritatorStatus
    from digest.preparation import clear_preparation
    from digest.presentation.source_attribution import attribute_source_card
    from digest.translation import ClosingPresentation, translate_publication_with_closing

    stats = _stats("no_ready" if selection_complete else "selection_incomplete", feedback=feedback)
    stats.review_status = snapshot.review_report.status if snapshot.review_report is not None else "not_requested"
    if not snapshot.top_articles:
        if not selection_complete:
            logger.error("Selection did not complete; candidate evidence remains pending and no edition is ready.")
        else:
            logger.info("Edition preparation: no selected articles; no ready edition created")
        return stats
    main_attribution = main_attribution_occurrences(
        snapshot.review_report, snapshot.top_articles, getattr(config, "sources", ()),
        closing_snapshot=getattr(snapshot, "closing", None) is not None,
    )
    ranked: list[Any] = []
    if config.review.enabled and config.review.review_led_only:
        status = IrritatorStatus(deferred_review_status(config.radar.language), "deferred")
    else:
        _, ranked, status = await run_irritator(snapshot.summaries, config, verbose, execution=execution)
    closing = getattr(snapshot, "closing", None)
    closing_presentation: ClosingPresentation | None = None
    if closing is not None and closing.status == "selected":
        if closing.card is None:
            raise ValueError("Selected closing decision is missing its canonical card.")
        text, cards, ranked, closing_presentation = await translate_publication_with_closing(
            snapshot.combined, snapshot.top_articles, ranked, closing.card, config, Path(".cache/translations"),
            selection_binding=asdict(closing), execution=execution,
        )
    else:
        text, cards, ranked = await publication_presentation(
            snapshot.combined, snapshot.top_articles, ranked, config, Path(".cache/translations"), False,
            execution=execution,
        )
        if closing is not None:
            closing_presentation = ClosingPresentation(closing.status, closing.reason)
    # Credits stay outside canonical text and model/cache inputs, but enter both
    # archive and frozen delivery. Missing provenance was checked before calls.
    from digest.radar.collector import article_hash

    cards = [attribute_source_card(card, occurrence, occurrence_sha256(occurrence))[0]
             if (occurrence := main_attribution.get(article_hash(card.title, card.link))) is not None else card
             for card in cards]
    # Never discard a required main card to satisfy optional placement. Hold the
    # accepted preparation before archive/freeze/send when its credit would split.
    _, main_ranges = _render_compact_issue(cards, config, text)
    if any(item.full_hash in main_attribution and len(item.covering_chunks) != 1 for item in main_ranges):
        raise ValueError("Main source attribution spans delivery chunks; accepted preparation retained. "
                         "Review its presentation before resuming; no article was sent or discarded.")
    if closing_presentation is not None and closing_presentation.card is not None:
        attributed = attribute_closing_card(closing, closing_presentation.card) if closing is not None else None
        if attributed is None:
            closing_presentation = replace(
                closing_presentation, status="incomplete", reason="attribution_unavailable", card=None,
            )
        else:
            assembled = [*cards, attributed]
            try:
                _, ranges = _render_compact_issue(assembled, config, text)
            except ValueError as exc:
                logger.warning("Closing presentation omitted after render preflight: %s", exc)
                closing_presentation = replace(
                    closing_presentation, status="incomplete", reason="rendering_failed", card=None,
                )
            else:
                # A visible partial article cannot lose its required credit if
                # a later chunk fails. Omit optional content; leave main intact.
                if any((item.full_hash in main_attribution or item is ranges[-1])
                       and len(item.covering_chunks) != 1 for item in ranges):
                    closing_presentation = replace(
                        closing_presentation, status="incomplete", reason="attribution_split", card=None,
                    )
                else:
                    closing_presentation = replace(closing_presentation, card=attributed)
                    cards = assembled
    archive = write_digest(
        text, config, top_articles=cards, ranked_signals=ranked or None,
        review_report=snapshot.review_report, irritator_status=status,
        sources_count=snapshot.source_count, articles_count=snapshot.article_count,
        date=datetime.combine(publication_date, datetime.min.time(), timezone.utc) if publication_date else None,
    )
    # An enabled but failed archive is a preparation failure, never sender eligibility.
    if config.obsidian.enabled and archive is None:
        raise ValueError("Edition archive persistence failed; canonical preparation remains resumable.")
    references: dict[str, Any] = {}
    if archive is not None:
        paths = [archive]
        if snapshot.review_report is not None:
            from digest.candidate_review import archive_candidate_accounting, candidate_accounting_sources

            paths.append(archive.with_suffix(".review.json"))
            accounting_path = archive_candidate_accounting(snapshot.review_report, archive)
            if accounting_path is not None:
                paths.append(accounting_path)
                paths.extend(candidate_accounting_sources(snapshot.review_report))
        for path in paths:
            relative = path.resolve().relative_to(Path.cwd().resolve()).as_posix()
            references[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    canonical: dict[str, Any] = {
        "combined": snapshot.combined,
        "cards": [asdict(card) for card in snapshot.top_articles],
        "contributing_sources": snapshot.contributing_sources,
        "source_count": snapshot.source_count,
        "article_count": snapshot.article_count,
    }
    presentation: dict[str, Any] = {"combined": text, "cards": [asdict(card) for card in cards]}
    if closing is not None and closing_presentation is not None:
        canonical["closing"] = asdict(closing)
        presentation["closing"] = asdict(closing_presentation)
    _, digest = prepare_edition(
        cards, config, notice=text, canonical_metadata=canonical,
        presentation_metadata=presentation,
        checkpoint_refs=references, producing_engine=_engine_provenance(), publication_date=publication_date,
    )
    clear_preparation()
    future_window = publication_date and publication_date > datetime.now(timezone.utc).date()
    status_name = "pending_window" if future_window else "ready"
    stats.edition_status = status_name
    stats.ready_sha256 = digest
    stats.digest_length = len(text)
    stats.markdown_saved = archive is not None
    stats.markdown_path = str(archive) if archive is not None else ""
    stats.review_checkpoint = (str(archive.with_suffix(".review.json"))
                               if archive is not None and snapshot.review_report is not None else "")
    return stats


def _strict_cache(path: Path) -> dict[str, str]:
    from digest.adapters.storage.delivery_state import load_delivery_cache

    return load_delivery_cache(path)


def _merge_delivery(result: IssueDeliveryResult, manifest: dict[str, Any], config: Any) -> None:
    """Compatibility entrypoint for prepared outcome application."""
    from digest.application.delivery import PreparedOutcomePolicy, apply_confirmed_outcome

    if not result.delivered_hashes:
        return
    adaptive_enabled = getattr(getattr(config, "adaptive", None), "enabled", False)
    apply_confirmed_outcome(PreparedOutcomePolicy(
        outcome=result,
        cache_dir=".cache",
        publication_day=datetime.fromisoformat(manifest["window_start"]).date(),
        contributing_sources=manifest["canonical_metadata"]["contributing_sources"],
        adaptive_enabled=adaptive_enabled,
        enabled_sources=config.enabled_sources if adaptive_enabled else [],
    ))


async def delivery_phase(phase: str, config_path: str, ready_sha: str | None, claim_sha: str | None) -> int:
    from digest.adapters.storage.feedback import load_feedback
    from digest.config import load_config
    from digest.delivery.edition import claim_edition, inspect_edition, mark_applied, send_prepared_edition

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
        ready_sha, claim_sha, enabled=config.telegram.enabled, bot_username=config.telegram.bot_username,
    )
    _merge_delivery(result, manifest, config)
    mark_applied(ready_sha)
    checkpoint = ""
    if result.complete:
        checkpoint = next((name for name in manifest["checkpoint_refs"] if name.endswith(".review.json")), "")
    publish_outputs(edition_status="confirmed" if result.complete else result.outcome,
                    review_checkpoint=checkpoint)
    return 0 if result.complete else 1


async def resume_preparation(
    config: Any, feedback: int, *, execution: ModelExecution, verbose: bool, publication_date: date | None = None,
) -> RunStats | None:
    from digest.preparation import load_preparation

    existing = existing_preparation(config, feedback, publication_date)
    if existing is not None:
        return existing
    snapshot = load_preparation(publication_date=publication_date)
    if snapshot is not None:
        return await finish_preparation(
            snapshot, config, feedback, verbose=verbose, publication_date=publication_date, execution=execution,
        )
    return None


def validate_cli(args: Any) -> None:
    if args.edition_date:
        if not args.prepare_edition or date.fromisoformat(args.edition_date).isoformat() != args.edition_date:
            raise ValueError("An intended UTC edition date requires --prepare-edition and YYYY-MM-DD.")
        if date.fromisoformat(args.edition_date) < datetime.now(timezone.utc).date():
            raise ValueError("Cannot prepare an edition for an elapsed UTC publication day.")
    if args.prepare_edition or args.edition_phase:
        if (args.check or args.discover or args.dry_run or args.radar_only or args.reserve_issue
                or args.issue_reservation_sha or args.prepare_edition and args.edition_phase):
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


def save_accepted_preparation(
    snapshot: PreparationSnapshot, *, cache_dir: str, publication_date: date | None = None,
) -> None:
    from digest.preparation import save_preparation

    # A failed primary is missing work, not a reusable empty editorial decision.
    report = snapshot.review_report
    accepted = bool(snapshot.top_articles) or bool(
        report and any(review.slot == "primary" and review.status == "abstained" for review in report.reviews)
    )
    if accepted:
        save_preparation(snapshot, cache_dir=cache_dir, publication_date=publication_date)
