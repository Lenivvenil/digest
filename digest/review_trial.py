"""One isolated, bounded review trial. Never invokes delivery or persists runtime state.

Run from a read-only-permission GitHub job with Gemini/Groq secrets only. Writes
reports solely to the explicit output directory; production config is read-only.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import tempfile
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from digest.adapters.models.execution import ModelExecution
from digest.application.review import run_blind_review, run_evidence_review
from digest.application.review_routes import ALLOWED_REVIEW_MODELS as _ALLOWED_MODELS
from digest.config import load_config
from digest.presentation.review import render_review
from digest.radar.collector import collect
from digest.review_checkpoint import load_review_checkpoint


async def run_trial(
    config_path: Path, output_dir: Path, resume_path: Path | None = None, *, execution: ModelExecution,
) -> int:
    config_path, output_dir = config_path.resolve(), output_dir.resolve()
    if resume_path is not None:
        resume_path = resume_path.resolve()
        if resume_path == output_dir / "review.json":
            raise ValueError("Resume output must not overwrite the original checkpoint.")
    if any((output_dir / name).exists() for name in ("review.json", "review.md", "trial-metadata.json")):
        raise ValueError("Trial output already exists; choose a fresh output directory.")
    config = load_config(config_path)
    if not config.review.enabled:
        raise ValueError("The trial requires review.enabled: true.")
    models = [config.review.primary, config.review.secondary]
    if config.review.tie_breaker is not None:
        models.append(config.review.tie_breaker)
    if any((m.provider, m.model) not in _ALLOWED_MODELS for m in models):
        raise ValueError("Trial model is outside the explicitly approved free-route lineup.")
    runner_temp = os.environ.get("RUNNER_TEMP")
    if runner_temp and not output_dir.is_relative_to(Path(runner_temp).resolve()):
        raise ValueError("GitHub trial output must stay under RUNNER_TEMP.")

    # Hard caps for this one experiment. No retries and no provider fallback.
    config.llm.max_retries = 0
    config.llm.max_concurrent_requests = 1
    config.llm.min_request_interval_seconds = 20.0
    config.review.max_evidence_articles = min(config.review.max_evidence_articles, 10)
    config.review.max_excerpt_chars = min(config.review.max_excerpt_chars, 400)
    config.review.max_selections = min(config.review.max_selections, 3)
    config.review.max_output_tokens = min(config.review.max_output_tokens, 4096)
    config.telegram.enabled = False
    config.adaptive.enabled = False
    config.obsidian.enabled = False

    # The collector's relative .cache lookup sees an empty temporary directory.
    # No cache save, feedback polling, approvals, delivery or git operation occurs.
    previous_cwd = Path.cwd()
    try:
        with tempfile.TemporaryDirectory(prefix="digest-blind-trial-") as isolated:
            os.chdir(isolated)
            if resume_path is not None:
                bundle, cached_reviews = load_review_checkpoint(resume_path, config)
                report = await run_evidence_review(bundle, config, cached_reviews, execution=execution)
            else:
                articles, _unpersisted_cache = await collect(config)
                if not articles:
                    raise ValueError("No source evidence collected; no model calls made.")
                report = await run_blind_review(articles, config, execution=execution)
    finally:
        os.chdir(previous_cwd)

    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "review.json").write_text(json.dumps(asdict(report), ensure_ascii=False, indent=2) + "\n")
    (output_dir / "review.md").write_text("# Controlled blind-review trial\n" + render_review(report) + "\n")
    metadata = {
        "created_at": datetime.now(UTC).isoformat(),
        "source_checkpoint_sha256": (hashlib.sha256(resume_path.read_bytes()).hexdigest()
                                     if resume_path is not None else None),
        "resumed": resume_path is not None,
        "reused_slots": [r.slot for r in report.reviews if r.reused_from_checkpoint],
        "new_attempt_slots": [r.slot for r in report.reviews if not r.reused_from_checkpoint],
        "trial_only": True, "production_state_written": False, "telegram_used": False,
        "max_model_requests": 3, "model_retries": 0,
        "max_evidence_articles": config.review.max_evidence_articles,
        "max_excerpt_chars": config.review.max_excerpt_chars,
        "max_output_tokens_per_request": config.review.max_output_tokens,
        "free_tier_note": "Uses existing configured accounts; no billing change or paid-provider fallback.",
    }
    (output_dir / "trial-metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")
    print(f"Trial review: {report.status}; slots={len(report.reviews)}; overlap={report.selection_overlap}")
    for review in report.reviews:
        print(f"{review.slot}: {review.provider}/{review.model}: {review.status}; usage={review.usage}")
    return 0 if report.status == "complete" else 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--resume", type=Path, help="Prior report JSON; reuse valid matching slots only")
    args = parser.parse_args()
    return asyncio.run(run_trial(args.config, args.output, args.resume, execution=ModelExecution()))


if __name__ == "__main__":
    raise SystemExit(main())
