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
import time
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from digest.adapters.models.execution import ModelExecution
from digest.adapters.storage import post_delivery as storage
from digest.adapters.telegram.delivery import send_post_delivery_supplement
from digest.application.review_routes import ALLOWED_REVIEW_MODELS
from digest.config import Config, load_config
from digest.presentation.supplement import signal_text
from digest.review_checkpoint import load_review_checkpoint

if TYPE_CHECKING:
    from digest.irritator.evidence_stage import EvidenceIrritatorResult
    from digest.review import EvidenceBundle
    from digest.review_checkpoint import FullSourceEvidence


def _safe_path(path: Path) -> Path:
    return storage.safe_checkpoint_path(path)


def _paths(checkpoint: Path) -> tuple[Path, Path, Path]:
    if not checkpoint.name.endswith('.review.json'):
        raise ValueError('Expected a .review.json checkpoint.')
    stem = checkpoint.name.removesuffix('.review.json')
    return (checkpoint.with_name(stem + '.post-attempt.json'),
            checkpoint.with_name(stem + '.irritator.json'),
            checkpoint.with_name(stem + '.irritator.md'))


def _config(path: Path) -> Config:
    config = load_config(path)
    model = config.review.secondary
    if (model.provider, model.model) not in ALLOWED_REVIEW_MODELS:
        raise ValueError('Post-delivery model is outside the approved free-route lineup.')
    config.llm.max_retries = 0
    return config


def prepare_post_delivery(config_path: Path, checkpoint_path: Path) -> Path | None:
    checkpoint = _safe_path(checkpoint_path)
    config = _config(config_path)
    source_bytes = storage.read_checkpoint_bytes(checkpoint)
    bundle, _reviews = load_review_checkpoint(checkpoint, config)
    storage.require_unchanged_checkpoint(checkpoint, source_bytes)
    marker, report, markdown = _paths(checkpoint)
    if storage.attempt_artifacts_exist(marker, report, markdown):
        return None
    relative = storage.repository_relative_path(checkpoint)
    record = {
        'schema_version': 1, 'checkpoint': relative, 'bundle_id': bundle.bundle_id,
        'checkpoint_sha256': hashlib.sha256(source_bytes).hexdigest(),
        'prepared_at': datetime.now(UTC).isoformat(), 'execute_started': None,
        'supplement_status': 'not_attempted',
    }
    if not storage.create_attempt(marker, record):
        return None
    storage.append_github_output(relative, marker)
    return marker


def _render_result(
    result: EvidenceIrritatorResult, *, canonical: EvidenceIrritatorResult | None = None, notice: str = "",
) -> str:
    lines = ['# Irritator: bounded post-delivery supplement', f'Status: {result.status}',
             f'Original evidence bundle: {result.bundle_id}',
             result.coverage]
    if result.source_bundle_id is not None:
        lines.append(f'Full-source passage bundle: {result.source_bundle_id}')
    for narrative in result.narratives:
        lines.append(f'\nNarrative checked: {narrative.claim}')
    for ranked in result.ranked_signals:
        lines.append('\n' + signal_text(ranked))
    if notice:
        lines.append('\n' + notice)
    diagnostics = asdict(canonical or result)
    audit = diagnostics.pop('ranking_audit', None)
    if audit is not None:
        candidates = audit['candidates']
        admitted = sum(item['admission'] == 'admitted' for item in candidates)
        lines.append(
            f"\nPrivate ranking audit: {len(candidates)} validated candidates, {admitted} admitted; "
            f"response validated: {audit['response_validated']}. "
            "Candidate evidence and dispositions are in the companion .irritator.json archive."
        )
    lines.append('\n## Stage diagnostics\n' + json.dumps(diagnostics, ensure_ascii=False, indent=2))
    return '\n'.join(lines) + '\n'


def _source_provenance(
    checkpoint: Path, bundle: EvidenceBundle, config: Config, content: bytes,
) -> tuple[FullSourceEvidence | None, bool, str]:
    from digest.review_checkpoint import load_full_source_evidence

    payload = json.loads(content)
    required = (payload.get('full_source_required', False) is not False or 'full_source_evidence' in payload
                or getattr(getattr(config, 'reading_brief', None), 'enabled', False))
    try:
        source = load_full_source_evidence(checkpoint, bundle, config)
    except (ValueError, TypeError, KeyError) as exc:
        return None, True, type(exc).__name__
    return source, required, ''


async def _send_supplement(result: EvidenceIrritatorResult, config: Config, *, notice: str = "") -> str:
    return await send_post_delivery_supplement(result, config, notice=notice)


async def execute_post_delivery(
    config_path: Path, checkpoint_path: Path, *, execution: ModelExecution,
) -> int:
    from digest.irritator.evidence_stage import MAX_SECONDS, run_evidence_irritator

    checkpoint = _safe_path(checkpoint_path)
    marker, output, markdown = _paths(checkpoint)
    record = storage.load_attempt(marker, output, markdown)
    config = _config(config_path)
    source_bytes = storage.read_checkpoint_bytes(checkpoint)
    bundle, _reviews = load_review_checkpoint(checkpoint, config)
    storage.require_unchanged_checkpoint(checkpoint, source_bytes)
    source_evidence, require_full_source, source_error = _source_provenance(checkpoint, bundle, config, source_bytes)
    storage.require_unchanged_checkpoint(checkpoint, source_bytes)
    if (type(record.get('schema_version')) is not int or record['schema_version'] != 1
            or record.get('execute_started')
            or record.get('checkpoint') != storage.repository_relative_path(checkpoint)
            or record.get('bundle_id') != bundle.bundle_id
            or record.get('checkpoint_sha256') != hashlib.sha256(source_bytes).hexdigest()):
        raise ValueError('Invalid, changed or already executed post-delivery checkpoint.')
    record['execute_started'] = datetime.now(UTC).isoformat()
    if require_full_source:
        record['full_source_required'] = True
        if source_error:
            record['full_source_error'] = source_error
    storage.save_attempt(marker, record)
    translation_enabled = config.translation.enabled and config.translation.target_language != 'en'
    extra = min(config.translation.timeout_seconds, 45.0) if translation_enabled else 0.0
    processing_deadline = time.monotonic() + MAX_SECONDS + extra
    try:
        async with httpx.AsyncClient() as client:
            if require_full_source or source_evidence is not None:
                result = await run_evidence_irritator(bundle, config, client, source_evidence=source_evidence,
                                                     require_full_source=require_full_source, execution=execution)
            else:
                result = await run_evidence_irritator(bundle, config, client, execution=execution)
    except Exception as exc:
        # Unexpected implementation failures still leave an explicit durable outcome.
        payload = {'status': 'error', 'bundle_id': bundle.bundle_id, 'error': type(exc).__name__,
                   'coverage': 'one narrative maximum', 'stage': 'unexpected_failure'}
        storage.save_failure_archives(output, markdown, payload)
        record['stage_status'] = 'error'
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
                result, config, checkpoint.parent / '.translations', deadline=processing_deadline,
                execution=execution,
            )
        except Exception as exc:
            translation = TranslationResult({}, 'fallback', reasons=[type(exc).__name__])
        notice = translation.notice
        record['translation_status'] = translation.status
        record['translation_reasons'] = translation.reasons
        if translation.status == 'translated':
            presentation_config = replace(config, radar=replace(config.radar,
                                          language=config.translation.target_language))
    storage.save_markdown_archive(markdown, _render_result(presented, canonical=result, notice=notice))
    record['stage_status'] = result.status
    if getattr(config.telegram, 'delivery_mode', 'cards') == 'compact':
        record['supplement_status'] = 'archive_only'
        record['finished_at'] = datetime.now(UTC).isoformat()
        storage.save_attempt(marker, record)
        return 0 if result.status in {'complete', 'empty'} else 2
    record['supplement_status'] = 'dispatching'
    storage.save_attempt(marker, record)
    try:
        if translation_enabled:
            record['supplement_status'] = await _send_supplement(presented, presentation_config, notice=notice)
        else:
            record['supplement_status'] = await _send_supplement(result, config)
    except Exception as exc:
        record['supplement_status'] = 'unknown'
        record['send_error'] = type(exc).__name__
    record['finished_at'] = datetime.now(UTC).isoformat()
    storage.save_attempt(marker, record)
    return 0 if result.status in {'complete', 'empty'} and record['supplement_status'] == 'sent' else 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['prepare', 'execute'])
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--checkpoint', required=True, type=Path)
    args = parser.parse_args()
    if args.command == 'prepare':
        prepare_post_delivery(args.config, args.checkpoint)
        return 0
    return asyncio.run(execute_post_delivery(args.config, args.checkpoint, execution=ModelExecution()))


if __name__ == '__main__':
    raise SystemExit(main())
