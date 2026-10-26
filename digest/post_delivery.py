"""Durable, one-attempt counter-signal supplement after confirmed primary delivery.

The workflow must commit/push prepare's marker before execute. Any uncertain
Telegram outcome is recorded and never automatically resent. Primary state is
never changed here.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import time
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from digest.adapters.models.execution import ModelExecution
from digest.adapters.storage import post_delivery as storage
from digest.adapters.storage.review_checkpoints import load_review_checkpoint
from digest.adapters.telegram.delivery import send_post_delivery_supplement
from digest.application.investigation_origin import freeze_delivered_input
from digest.application.native_investigation_origin import freeze_native_delivered_input
from digest.application.review_routes import ALLOWED_REVIEW_MODELS
from digest.config import Config, load_config
from digest.domain.investigation.delivered import (
    DeliveredInvestigationInput,
    DeliveredOrigin,
    NativeDeliveredInvestigationInput,
    validate_delivered_input,
)
from digest.domain.investigation.search_policy import build_search_policy
from digest.presentation.supplement import signal_text

if TYPE_CHECKING:
    from digest.domain.editorial.reviews import EvidenceBundle
    from digest.irritator.evidence_stage import EvidenceIrritatorResult
    from digest.review_checkpoint import FullSourceEvidence


def _safe_path(path: Path) -> Path:
    return storage.safe_checkpoint_path(path)


def _paths(checkpoint: Path) -> tuple[Path, Path, Path]:
    if not checkpoint.name.endswith(".review.json"):
        raise ValueError("Expected a .review.json checkpoint.")
    stem = checkpoint.name.removesuffix(".review.json")
    return (
        checkpoint.with_name(stem + ".post-attempt.json"),
        checkpoint.with_name(stem + ".irritator.json"),
        checkpoint.with_name(stem + ".irritator.md"),
    )


def _native_paths(checkpoint: Path, config: Config) -> tuple[Path, Path, Path]:
    from digest.adapters.storage.issue_paths import safe_issue_path

    safe_issue_path(checkpoint)
    directory = safe_issue_path(Path(config.obsidian.output_dir)).resolve()
    if (config.telegram.delivery_mode != "compact" or not config.obsidian.enabled
            or checkpoint.suffix != ".md" or checkpoint.parent != directory):
        raise ValueError("Native investigation requires an archive in the configured compact output directory.")
    stem = checkpoint.stem + ".native"
    return (
        checkpoint.with_name(stem + ".post-attempt.json"),
        checkpoint.with_name(stem + ".irritator.json"),
        checkpoint.with_name(stem + ".irritator.md"),
    )


def _native_execution_claim(marker: Path) -> Path:
    return marker.with_suffix(".execute.json")


def _config(path: Path) -> Config:
    config = load_config(path)
    model = config.review.secondary
    if (model.provider, model.model) not in ALLOWED_REVIEW_MODELS:
        raise ValueError("Post-delivery model is outside the approved free-route lineup.")
    config.llm.max_retries = 0
    return config


def prepare_post_delivery(config_path: Path, checkpoint_path: Path) -> Path | None:
    """Retained provider-review entrypoint; its serialized contract is unchanged."""
    return _prepare_post_delivery(config_path, checkpoint_path, native=False)


def prepare_native_post_delivery(config_path: Path, archive_path: Path) -> Path | None:
    """Reserve optional work from an actual delivered native Markdown archive."""
    return _prepare_post_delivery(config_path, archive_path, native=True)


def _prepare_post_delivery(
    config_path: Path, checkpoint_path: Path, *, native: bool, native_config: Config | None = None,
) -> Path | None:
    if native:
        from digest.adapters.storage.issue_paths import safe_issue_path

        safe_issue_path(checkpoint_path)
    checkpoint = _safe_path(checkpoint_path)
    if native_config is not None and not native:
        raise ValueError("Native semantic configuration cannot replace a retained provider route.")
    config = native_config if native_config is not None else _config(config_path)
    source_bytes = storage.read_checkpoint_bytes(checkpoint)
    delivered: DeliveredOrigin | None = None
    if native:
        marker, report, markdown = _native_paths(checkpoint, config)
        delivered, bundle = freeze_native_delivered_input(checkpoint, source_bytes)
        claim = _native_execution_claim(marker)
        if claim.exists() or claim.is_symlink():
            return None
    else:
        bundle, _reviews = load_review_checkpoint(checkpoint, config)
        marker, report, markdown = _paths(checkpoint)
    storage.require_unchanged_checkpoint(checkpoint, source_bytes)
    if storage.attempt_artifacts_exist(marker, report, markdown):
        return None
    if config.telegram.delivery_mode == "compact":
        from digest.application.supplement import pending_fragment

        pending, reason = pending_fragment(config.obsidian.output_dir, datetime.now(UTC).date())
        if pending is not None or reason != "no_pending_fragment":
            logging.getLogger(__name__).info("Optional investigation skipped: %s", reason)
            return None
    if not native and config.telegram.delivery_mode == "compact":
        delivered = freeze_delivered_input(checkpoint, source_bytes, bundle)
    storage.require_unchanged_checkpoint(checkpoint, source_bytes)
    relative = storage.repository_relative_path(checkpoint)
    record = {
        "schema_version": 2,
        "checkpoint": relative,
        "bundle_id": bundle.bundle_id,
        "search_policy": asdict(build_search_policy(
            config.irritator.sources, config.irritator.queries_per_narrative, research=delivered is not None,
        )),
        "checkpoint_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "prepared_at": datetime.now(UTC).isoformat(),
        "execute_started": None,
        "supplement_status": "not_attempted",
    }
    if delivered is not None:
        from digest._serialization import canonical_json_bytes

        record.update(
            schema_version=4 if native else 3,
            input_contract="native-delivered-card-v1" if native else "delivered-card-v1",
            delivered=asdict(delivered),
            delivered_sha256=hashlib.sha256(canonical_json_bytes(asdict(delivered))).hexdigest(),
        )
    if not storage.create_attempt(marker, record):
        return None
    storage.append_github_output(relative, marker)
    return marker


def _require_search_policy(record: object, config: Config, *, native: bool = False) -> None:
    """Hold old or changed attempts without assigning them today's search policy."""
    expected = asdict(build_search_policy(
        config.irritator.sources, config.irritator.queries_per_narrative,
        research=config.telegram.delivery_mode == "compact",
    ))
    if isinstance(record, dict):
        policy = record.get("search_policy")
        expected_schema = 4 if native else 3 if config.telegram.delivery_mode == "compact" else 2
        if (
            type(record.get("schema_version")) is int
            and record["schema_version"] == expected_schema
            and isinstance(policy, dict)
            and type(policy.get("max_queries")) is int
            and policy == expected
        ):
            return
    raise ValueError(
        "Post-delivery search policy is missing, changed or unsupported. Preserve existing attempt artifacts; "
        "prepare only a new checkpoint under the current policy."
    )


def _delivered_input(record: dict[str, Any], bundle: EvidenceBundle) -> DeliveredOrigin | None:
    if record["schema_version"] == 2:
        return None
    from digest._serialization import canonical_json_bytes, restore_dataclass

    native = record["schema_version"] == 4
    contract = "native-delivered-card-v1" if native else "delivered-card-v1"
    if record.get("input_contract") != contract or hashlib.sha256(
        canonical_json_bytes(record.get("delivered"))
    ).hexdigest() != record.get("delivered_sha256"):
        raise ValueError("Invalid delivered investigation input contract or binding.")
    origin: DeliveredOrigin = restore_dataclass(
        record["delivered"], NativeDeliveredInvestigationInput if native else DeliveredInvestigationInput,
    )
    validate_delivered_input(origin, bundle)
    if origin.checkpoint_sha256 != record.get("checkpoint_sha256"):
        raise ValueError("Delivered investigation checkpoint binding changed.")
    return origin


def _render_result(
    result: EvidenceIrritatorResult,
    *,
    canonical: EvidenceIrritatorResult | None = None,
    notice: str = "",
) -> str:
    lines = [
        "# Irritator: bounded post-delivery supplement",
        f"Status: {result.status}",
        f"Original evidence bundle: {result.bundle_id}",
        result.coverage,
    ]
    if result.source_bundle_id is not None:
        lines.append(f"Full-source passage bundle: {result.source_bundle_id}")
    for narrative in result.narratives:
        lines.append(f"\nNarrative checked: {narrative.claim}")
    for ranked in result.ranked_signals:
        lines.append("\n" + signal_text(ranked))
    if notice:
        lines.append("\n" + notice)
    diagnostics = asdict(canonical or result)
    audit = diagnostics.pop("ranking_audit", None)
    if audit is not None:
        candidates = audit["candidates"]
        admitted = sum(item["admission"] == "admitted" for item in candidates)
        lines.append(
            f"\nPrivate ranking audit: {len(candidates)} validated candidates, {admitted} admitted; "
            f"response validated: {audit['response_validated']}. "
            "Candidate evidence and dispositions are in the companion .irritator.json archive."
        )
    lines.append(
        "\n## Stage diagnostics\n\n```json\n" + json.dumps(diagnostics, ensure_ascii=False, indent=2) + "\n```"
    )
    return "\n".join(lines) + "\n"


def _source_provenance(
    checkpoint: Path,
    bundle: EvidenceBundle,
    config: Config,
    content: bytes,
) -> tuple[FullSourceEvidence | None, bool, str]:
    from digest.review_checkpoint import load_full_source_evidence

    payload = json.loads(content)
    required = (
        payload.get("full_source_required", False) is not False
        or "full_source_evidence" in payload
        or getattr(getattr(config, "reading_brief", None), "enabled", False)
    )
    try:
        source = load_full_source_evidence(checkpoint, bundle, config)
    except (ValueError, TypeError, KeyError) as exc:
        return None, True, type(exc).__name__
    return source, required, ""


async def _send_supplement(result: EvidenceIrritatorResult, config: Config, *, notice: str = "") -> str:
    return await send_post_delivery_supplement(result, config, notice=notice)


async def execute_post_delivery(
    config_path: Path,
    checkpoint_path: Path,
    *,
    execution: ModelExecution,
) -> int:
    return await _execute_post_delivery(config_path, checkpoint_path, execution=execution, native=False)


async def execute_native_post_delivery(
    config_path: Path, archive_path: Path, *, execution: ModelExecution,
) -> int:
    """Execute the separate native-origin contract; ordinary delivery is never called."""
    return await _execute_post_delivery(config_path, archive_path, execution=execution, native=True)


async def _execute_post_delivery(
    config_path: Path, checkpoint_path: Path, *, execution: ModelExecution, native: bool,
) -> int:
    from digest.irritator.evidence_stage import MAX_SECONDS, run_evidence_irritator

    if native:
        from digest.adapters.storage.issue_paths import safe_issue_path

        safe_issue_path(checkpoint_path)
    checkpoint = _safe_path(checkpoint_path)
    config = _config(config_path)
    marker, output, markdown = _native_paths(checkpoint, config) if native else _paths(checkpoint)
    record = storage.load_attempt(marker, output, markdown)
    _require_search_policy(record, config, native=native)
    source_bytes = storage.read_checkpoint_bytes(checkpoint)
    current_origin: NativeDeliveredInvestigationInput | None = None
    if native:
        current_origin, bundle = freeze_native_delivered_input(checkpoint, source_bytes)
    else:
        bundle, _reviews = load_review_checkpoint(checkpoint, config)
    storage.require_unchanged_checkpoint(checkpoint, source_bytes)
    delivered = _delivered_input(record, bundle)
    if native and delivered != current_origin:
        raise ValueError("Native delivered investigation origin changed since prepare.")
    # Native source packets explicitly contain RSS evidence, not a provider-owned
    # full-source checkpoint. The shared research stage may acquire documents later.
    source_evidence, require_full_source, source_error = (
        (None, False, "") if native else _source_provenance(checkpoint, bundle, config, source_bytes)
    )
    storage.require_unchanged_checkpoint(checkpoint, source_bytes)
    if (
        record.get("execute_started", False) is not None
        or record.get("checkpoint") != storage.repository_relative_path(checkpoint)
        or record.get("bundle_id") != bundle.bundle_id
        or record.get("checkpoint_sha256") != hashlib.sha256(source_bytes).hexdigest()
    ):
        raise ValueError("Invalid, changed or already executed post-delivery checkpoint.")
    if native and not storage.create_native_execution_claim(_native_execution_claim(marker), {
        "schema": "native-investigation-execution/v1", "checkpoint": record["checkpoint"],
        "delivered_sha256": record["delivered_sha256"],
    }):
        raise ValueError("Native investigation execution already claimed; refusing another attempt.")
    record["execute_started"] = datetime.now(UTC).isoformat()
    if require_full_source:
        record["full_source_required"] = True
        if source_error:
            record["full_source_error"] = source_error
    storage.save_attempt(marker, record)
    translation_enabled = config.translation.enabled and config.translation.target_language != "en"
    extra = min(config.translation.timeout_seconds, 45.0) if translation_enabled else 0.0
    processing_deadline = time.monotonic() + MAX_SECONDS + extra
    try:
        async with httpx.AsyncClient() as client:
            if delivered is not None:
                result = await run_evidence_irritator(
                    bundle,
                    config,
                    client,
                    source_evidence=source_evidence,
                    require_full_source=require_full_source,
                    execution=execution,
                    delivered=delivered,
                )
            elif require_full_source or source_evidence is not None:
                result = await run_evidence_irritator(
                    bundle,
                    config,
                    client,
                    source_evidence=source_evidence,
                    require_full_source=require_full_source,
                    execution=execution,
                )
            else:
                result = await run_evidence_irritator(bundle, config, client, execution=execution)
    except Exception as exc:
        # Unexpected implementation failures still leave an explicit durable outcome.
        payload = {
            "status": "error",
            "bundle_id": bundle.bundle_id,
            "error": type(exc).__name__,
            "coverage": "one narrative maximum",
            "stage": "unexpected_failure",
        }
        storage.save_failure_archives(output, markdown, payload)
        record["stage_status"] = "error"
        storage.save_attempt(marker, record)
        return 2
    # Persist original analysis before optional presentation. Translation never
    # replaces source evidence or the original stage result.
    storage.save_result(output, asdict(result))
    presented, presentation_config, notice = result, config, ""
    if translation_enabled:
        from digest.translation import TranslationResult, translate_supplement_presentation

        try:
            presented, translation = await translate_supplement_presentation(
                result,
                config,
                checkpoint.parent / ".translations",
                deadline=processing_deadline,
                execution=execution,
            )
        except Exception as exc:
            translation = TranslationResult({}, "fallback", reasons=[type(exc).__name__])
        from digest.presentation import publication_language

        notice = translation.notice_for(publication_language(config))
        record["translation_status"] = translation.status
        record["translation_reasons"] = translation.reasons
        if translation.status == "translated":
            presentation_config = replace(
                config, radar=replace(config.radar, language=config.translation.target_language)
            )
    storage.save_markdown_archive(markdown, _render_result(presented, canonical=result, notice=notice))
    record["stage_status"] = result.status
    if getattr(config.telegram, "delivery_mode", "cards") == "compact":
        from digest.application.supplement import prepare_fragment

        assert delivered is not None
        prepare_fragment(
            record,
            output,
            result,
            presented,
            delivered,
            language=(config.translation.target_language if config.translation.enabled else config.radar.language),
            notice=notice,
            bundle=bundle,
            source=source_evidence,
        )
        record["finished_at"] = datetime.now(UTC).isoformat()
        storage.save_attempt(marker, record)
        return 0 if result.status in {"complete", "empty"} else 2
    record["supplement_status"] = "dispatching"
    storage.save_attempt(marker, record)
    try:
        if translation_enabled:
            record["supplement_status"] = await _send_supplement(presented, presentation_config, notice=notice)
        else:
            record["supplement_status"] = await _send_supplement(result, config)
    except Exception as exc:
        record["supplement_status"] = "unknown"
        record["send_error"] = type(exc).__name__
    record["finished_at"] = datetime.now(UTC).isoformat()
    storage.save_attempt(marker, record)
    return 0 if result.status in {"complete", "empty"} and record["supplement_status"] == "sent" else 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["prepare", "execute", "native-prepare", "native-execute"])
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--checkpoint", required=True, type=Path)
    args = parser.parse_args()
    if args.command in {"prepare", "native-prepare"}:
        prepare = prepare_native_post_delivery if args.command == "native-prepare" else prepare_post_delivery
        prepare(args.config, args.checkpoint)
        return 0
    execute = execute_native_post_delivery if args.command == "native-execute" else execute_post_delivery
    return asyncio.run(execute(args.config, args.checkpoint, execution=ModelExecution()))


if __name__ == "__main__":
    raise SystemExit(main())
