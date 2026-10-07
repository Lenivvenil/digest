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
import os
import time
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from digest._util import atomic_json_write
from digest.config import Config, load_config
from digest.delivery.supplement import signal_text, split_supplement
from digest.review_checkpoint import load_review_checkpoint
from digest.review_resume import _safe_path
from digest.review_trial import _ALLOWED_MODELS

if TYPE_CHECKING:
    from digest.irritator.evidence_stage import EvidenceIrritatorResult
    from digest.review import EvidenceBundle
    from digest.review_checkpoint import FullSourceEvidence


_SUPPLEMENT_DISPATCH_SECONDS = 30.0

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
    if (model.provider, model.model) not in _ALLOWED_MODELS:
        raise ValueError('Post-delivery model is outside the approved free-route lineup.')
    config.llm.max_retries = 0
    return config


def prepare_post_delivery(config_path: Path, checkpoint_path: Path) -> Path | None:
    checkpoint = _safe_path(checkpoint_path)
    config = _config(config_path)
    source_bytes = checkpoint.read_bytes()
    bundle, _reviews = load_review_checkpoint(checkpoint, config)
    if source_bytes != checkpoint.read_bytes():
        raise ValueError("Checkpoint changed while being read.")
    marker, report, markdown = _paths(checkpoint)
    if any(path.exists() or path.is_symlink() for path in (marker, report, markdown)):
        return None
    relative = checkpoint.relative_to(Path.cwd().resolve()).as_posix()
    record = {
        'schema_version': 1, 'checkpoint': relative, 'bundle_id': bundle.bundle_id,
        'checkpoint_sha256': hashlib.sha256(source_bytes).hexdigest(),
        'prepared_at': datetime.now(UTC).isoformat(), 'execute_started': None,
        'supplement_status': 'not_attempted',
    }
    try:
        with marker.open('x', encoding='utf-8') as handle:
            json.dump(record, handle, indent=2)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        return None
    output = os.environ.get('GITHUB_OUTPUT')
    if output:
        with Path(output).open('a', encoding='utf-8') as handle:
            handle.write(f'checkpoint={relative}\n')
            handle.write(f'marker={marker.relative_to(Path.cwd().resolve()).as_posix()}\n')
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


def _coverage_notice(result: EvidenceIrritatorResult, russian: bool) -> str:
    from digest.irritator.evidence_stage import FULL_SOURCE_COVERAGE

    if result.coverage == FULL_SOURCE_COVERAGE:
        return ('Охват ограничен выбранными отрывками полных статей; это не независимая проверка всех утверждений.'
                if russian else 'Limited coverage; selected full-source passages, not verification of every claim.')
    return ('Охват ограничен; RSS-выдержки, не полные статьи.' if russian
            else 'Limited coverage; RSS excerpts, not full articles.')


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
    from digest.delivery.telegram import escape_markdownv2

    token, chat = os.environ.get('TELEGRAM_BOT_TOKEN'), os.environ.get('TELEGRAM_CHAT_ID')
    if not config.telegram.enabled or not token or not chat:
        return 'not_configured'
    russian = config.radar.language == 'ru'
    header = ('Ирритатор: отдельная проверка одного нарратива' if russian
              else 'Irritator: separate check of one narrative')
    outcome = {'complete': 'проверка выполнена', 'empty': 'проверка выполнена, контрсигналов не найдено',
               'incomplete': 'проверка неполная', 'error': 'проверка не выполнена'}
    status = outcome.get(result.status, 'проверка неполная') if russian else result.status
    lines = [header, status, _coverage_notice(result, russian)]
    if result.narratives:
        label = "Проверяем: " if russian else "Narrative checked: "
        lines.extend(label + narrative.claim for narrative in result.narratives)
    lines.extend(signal_text(ranked, config.radar.language) for ranked in result.ranked_signals)
    if notice:
        lines.append(notice)
    chunks = split_supplement('\n\n'.join(lines), escape_markdownv2)
    # Never retry an uncertain POST: Telegram has no idempotency key for sendMessage.
    async with asyncio.timeout(_SUPPLEMENT_DISPATCH_SECONDS), httpx.AsyncClient() as client:
        for text in chunks:
            response = await client.post(f'https://api.telegram.org/bot{token}/sendMessage', json={
                'chat_id': chat, 'text': text, 'parse_mode': 'MarkdownV2',
                'disable_notification': True,
            }, timeout=30.0)
            response.raise_for_status()
            body = response.json()
            if not isinstance(body, dict) or body.get('ok') is not True:
                raise ValueError('Telegram did not confirm the supplement.')
    return 'sent'


async def execute_post_delivery(config_path: Path, checkpoint_path: Path) -> int:
    from digest.irritator.evidence_stage import MAX_SECONDS, run_evidence_irritator

    checkpoint = _safe_path(checkpoint_path)
    marker, output, markdown = _paths(checkpoint)
    if not marker.is_file() or marker.is_symlink():
        raise ValueError('Persisted post-delivery attempt marker is required.')
    if any(path.exists() or path.is_symlink() for path in (output, markdown)):
        raise ValueError('Post-delivery result already exists; refusing another attempt.')
    record: dict[str, Any] = json.loads(marker.read_text())
    config = _config(config_path)
    source_bytes = checkpoint.read_bytes()
    bundle, _reviews = load_review_checkpoint(checkpoint, config)
    if source_bytes != checkpoint.read_bytes():
        raise ValueError("Checkpoint changed while being read.")
    source_evidence, require_full_source, source_error = _source_provenance(checkpoint, bundle, config, source_bytes)
    if source_bytes != checkpoint.read_bytes():
        raise ValueError("Checkpoint changed while being read.")
    if (type(record.get('schema_version')) is not int or record['schema_version'] != 1
            or record.get('execute_started')
            or record.get('checkpoint') != checkpoint.relative_to(Path.cwd().resolve()).as_posix()
            or record.get('bundle_id') != bundle.bundle_id
            or record.get('checkpoint_sha256') != hashlib.sha256(source_bytes).hexdigest()):
        raise ValueError('Invalid, changed or already executed post-delivery checkpoint.')
    record['execute_started'] = datetime.now(UTC).isoformat()
    if require_full_source:
        record['full_source_required'] = True
        if source_error:
            record['full_source_error'] = source_error
    atomic_json_write(marker, record)
    translation_enabled = config.translation.enabled and config.translation.target_language != 'en'
    extra = min(config.translation.timeout_seconds, 45.0) if translation_enabled else 0.0
    processing_deadline = time.monotonic() + MAX_SECONDS + extra
    try:
        async with httpx.AsyncClient() as client:
            if require_full_source or source_evidence is not None:
                result = await run_evidence_irritator(bundle, config, client, source_evidence=source_evidence,
                                                     require_full_source=require_full_source)
            else:
                result = await run_evidence_irritator(bundle, config, client)
    except Exception as exc:
        # Unexpected implementation failures still leave an explicit durable outcome.
        payload = {'status': 'error', 'bundle_id': bundle.bundle_id, 'error': type(exc).__name__,
                   'coverage': 'one narrative maximum', 'stage': 'unexpected_failure'}
        atomic_json_write(output, payload)
        markdown.write_text('# Irritator incomplete\n' + json.dumps(payload, indent=2) + '\n')
        record['stage_status'] = 'error'
        atomic_json_write(marker, record)
        return 2
    # Persist original analysis before optional presentation. Translation never
    # replaces source evidence or the original stage result.
    atomic_json_write(output, asdict(result))
    presented, presentation_config, notice = result, config, ""
    if translation_enabled:
        from digest.translation import TranslationResult, translate_supplement_presentation

        try:
            presented, translation = await translate_supplement_presentation(
                result, config, checkpoint.parent / '.translations', deadline=processing_deadline,
            )
        except Exception as exc:
            translation = TranslationResult({}, 'fallback', reasons=[type(exc).__name__])
        notice = translation.notice
        record['translation_status'] = translation.status
        record['translation_reasons'] = translation.reasons
        if translation.status == 'translated':
            presentation_config = replace(config, radar=replace(config.radar,
                                          language=config.translation.target_language))
    markdown.write_text(_render_result(presented, canonical=result, notice=notice), encoding='utf-8')
    record['stage_status'] = result.status
    if getattr(config.telegram, 'delivery_mode', 'cards') == 'compact':
        record['supplement_status'] = 'archive_only'
        record['finished_at'] = datetime.now(UTC).isoformat()
        atomic_json_write(marker, record)
        return 0 if result.status in {'complete', 'empty'} else 2
    record['supplement_status'] = 'dispatching'
    atomic_json_write(marker, record)
    try:
        if translation_enabled:
            record['supplement_status'] = await _send_supplement(presented, presentation_config, notice=notice)
        else:
            record['supplement_status'] = await _send_supplement(result, config)
    except Exception as exc:
        record['supplement_status'] = 'unknown'
        record['send_error'] = type(exc).__name__
    record['finished_at'] = datetime.now(UTC).isoformat()
    atomic_json_write(marker, record)
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
    return asyncio.run(execute_post_delivery(args.config, args.checkpoint))


if __name__ == '__main__':
    raise SystemExit(main())
