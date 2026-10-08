"""One report-only resume, with its attempt marker persisted before provider work.

The workflow must commit and push ``prepare``'s marker before calling ``execute``.
No feed collection, delivery, production cache writes or provider fallback occurs.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from digest._util import atomic_json_write
from digest.adapters.models.execution import ModelExecution
from digest.adapters.storage.review_checkpoints import load_review_checkpoint
from digest.application.review import run_evidence_review
from digest.application.review_request import build_review_messages
from digest.application.review_routes import ALLOWED_REVIEW_MODELS
from digest.config import Config, load_config
from digest.domain.editorial.reviews import (
    EvidenceBundle,
    ModelReview,
    ReviewReuseIdentity,
    reusable_model_review,
    review_prompt_hash,
)
from digest.presentation.review import render_review

_MAX_AGE = timedelta(hours=24)
_SUFFIX = ".review.json"


def _safe_path(path: Path) -> Path:
    resolved = path.resolve()
    try:
        relative = resolved.relative_to(Path.cwd().resolve()).as_posix()
    except ValueError as exc:
        raise ValueError("Resume paths must stay inside the current repository.") from exc
    if path.is_symlink() or not re.fullmatch(r"[A-Za-z0-9_./-]+", relative):
        raise ValueError("Resume paths must be plain safe repository-relative paths.")
    return resolved


def _siblings(checkpoint: Path) -> tuple[Path, Path, Path]:
    if not checkpoint.name.endswith(_SUFFIX):
        raise ValueError("Resume checkpoint must end in .review.json.")
    base = checkpoint.name.removesuffix(_SUFFIX)
    return (checkpoint.with_name(base + ".resume-attempted.json"),
            checkpoint.with_name(base + ".review-resumed.json"),
            checkpoint.with_name(base + ".review-resumed.md"))


def _config(path: Path) -> Config:
    config = load_config(path)
    if not config.review.enabled:
        raise ValueError("Resume requires review.enabled: true.")
    models = [config.review.primary, config.review.secondary, config.review.tie_breaker]
    if any((model.provider, model.model) not in ALLOWED_REVIEW_MODELS for model in models if model is not None):
        raise ValueError("Resume model is outside the approved free-route lineup.")
    config.llm.max_retries = 0
    config.llm.max_concurrent_requests = 1
    config.llm.min_request_interval_seconds = 65.0
    config.review.max_evidence_articles = min(config.review.max_evidence_articles, 20)
    config.review.max_excerpt_chars = min(config.review.max_excerpt_chars, 500)
    config.review.max_output_tokens = min(config.review.max_output_tokens, 4096)
    config.telegram.enabled = config.adaptive.enabled = config.obsidian.enabled = False
    return config


def _report_time(reviews: list[ModelReview]) -> datetime | None:
    times = []
    for review in reviews:
        timestamp = review.attempted_at or review.generated_at
        if not timestamp:
            continue
        try:
            stamp = datetime.fromisoformat(timestamp)
        except (TypeError, ValueError):
            continue
        if stamp.tzinfo is not None:
            times.append(stamp.astimezone(UTC))
    return max(times) if times else None


def _reusable_slots(bundle: EvidenceBundle, reviews: list[ModelReview], config: Config) -> set[str]:
    messages = build_review_messages(bundle, config.review, config.radar.language, sources=config.sources,
                                     closing=getattr(config, "closing", None))
    prompt_hash = review_prompt_hash(messages)
    models = {"primary": config.review.primary, "secondary": config.review.secondary,
              "third": config.review.tie_breaker}
    slots = set()
    for review in reviews:
        model = models.get(review.slot)
        identity = (ReviewReuseIdentity(review.slot, model.provider, model.model, bundle.bundle_id, prompt_hash)
                    if model is not None else None)
        if reusable_model_review(review, bundle, identity) is not None:
            slots.add(review.slot)
    return slots


def _read_checkpoint(path: Path, config: Config) -> tuple[bytes, EvidenceBundle, list[ModelReview]]:
    if path.stat().st_size > 256000:
        raise ValueError("Checkpoint exceeds 256000-byte budget.")
    content = path.read_bytes()
    bundle, reviews = load_review_checkpoint(path, config)
    if content != path.read_bytes():
        raise ValueError("Checkpoint changed while being read.")
    _reusable_slots(bundle, reviews, config)
    return content, bundle, reviews


def _github_output(checkpoint: Path | None) -> None:
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        value = checkpoint.relative_to(Path.cwd().resolve()).as_posix() if checkpoint else ""
        with Path(output).open("a", encoding="utf-8") as handle:
            handle.write(f"checkpoint={value}\n")


def prepare_resume(config_path: Path, reports_dir: Path, now: datetime | None = None) -> Path | None:
    """Select the newest eligible archive and claim its one scheduled attempt."""
    config = _config(config_path)
    reports = _safe_path(reports_dir)
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        raise ValueError("Resume clock must include a timezone.")
    candidates = []
    for candidate in reports.glob("*.review.json"):
        try:
            path = _safe_path(candidate)
            if not path.is_relative_to(reports):
                continue
            marker, output, markdown = _siblings(path)
            if any(sibling.exists() or sibling.is_symlink() for sibling in (marker, output, markdown)):
                continue
            content, bundle, reviews = _read_checkpoint(path, config)
            stamp = _report_time(reviews)
            if (json.loads(content).get("status") != "incomplete" or stamp is None
                    or not timedelta(0) <= current - stamp < _MAX_AGE):
                continue
            candidates.append((stamp, path, content, bundle.bundle_id))
        except (OSError, ValueError, TypeError):
            continue
    for _stamp, path, content, bundle_id in sorted(candidates, reverse=True):
        marker, _, _ = _siblings(path)
        if content != path.read_bytes():
            continue
        record = {
            "schema_version": 1, "checkpoint": path.relative_to(Path.cwd().resolve()).as_posix(),
            "checkpoint_sha256": hashlib.sha256(content).hexdigest(), "bundle_id": bundle_id,
            "prepared_at": current.astimezone(UTC).isoformat(), "execution_started_at": None,
        }
        try:
            with marker.open("x", encoding="utf-8") as handle:
                json.dump(record, handle, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
        except FileExistsError:
            continue
        _github_output(path)
        return path
    _github_output(None)
    return None


async def execute_resume(
    config_path: Path, checkpoint_path: Path, *, execution: ModelExecution,
) -> int:
    """Use at most two missing-slot attempts; never overwrite source evidence."""
    checkpoint = _safe_path(checkpoint_path)
    marker, output, markdown = _siblings(checkpoint)
    if not marker.is_file() or marker.is_symlink():
        raise ValueError("Resume requires its previously persisted attempt marker.")
    if any(path.exists() or path.is_symlink() for path in (output, markdown)):
        raise ValueError("Resume output already exists; another attempt is forbidden.")
    record: dict[str, Any] = json.loads(marker.read_text(encoding="utf-8"))
    if (record.get("schema_version") != 1 or record.get("execution_started_at")
            or record.get("checkpoint") != checkpoint.relative_to(Path.cwd().resolve()).as_posix()):
        raise ValueError("Invalid or already executed resume marker.")
    config = _config(config_path)
    content, bundle, cached = _read_checkpoint(checkpoint, config)
    if (record.get("checkpoint_sha256") != hashlib.sha256(content).hexdigest()
            or record.get("bundle_id") != bundle.bundle_id):
        raise ValueError("Checkpoint no longer matches its persisted attempt marker.")
    reusable = _reusable_slots(bundle, cached, config)
    limited_third = config.review.tie_breaker is not None and not ({"primary", "secondary"} & reusable)
    if limited_third:
        config.review.tie_breaker = None
    record["execution_started_at"] = datetime.now(UTC).isoformat()
    atomic_json_write(marker, record)
    report = await run_evidence_review(bundle, config, cached, execution=execution)
    if limited_third and report.third_model_reason == "third_model_not_configured":
        report.third_model_reason = "resume_request_budget_exhausted"
        report.status = "incomplete"
    payload = asdict(report)
    previous = json.loads(content)
    # Independent RSS review never upgrades source provenance. Preserve the
    # separately bound passages and any explicit missing-evidence marker as-is.
    for key in ("full_source_required", "full_source_evidence", "full_source_error", "reading_brief_status"):
        if key in previous:
            payload[key] = previous[key]
    with output.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    with markdown.open("x", encoding="utf-8") as handle:
        handle.write("# Report-only resumed review\n" + render_review(report) + "\n")
    return 0 if report.status == "complete" else 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare")
    prepare.add_argument("--config", type=Path, required=True)
    prepare.add_argument("--reports", type=Path, required=True)
    prepare.add_argument("--now", type=datetime.fromisoformat)
    execute = commands.add_parser("execute")
    execute.add_argument("--config", type=Path, required=True)
    execute.add_argument("--checkpoint", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "prepare":
        prepare_resume(args.config, args.reports, args.now)
        return 0
    return asyncio.run(execute_resume(args.config, args.checkpoint, execution=ModelExecution()))


if __name__ == "__main__":
    raise SystemExit(main())
