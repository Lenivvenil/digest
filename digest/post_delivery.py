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
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from digest._util import atomic_json_write
from digest.config import Config, load_config
from digest.review_checkpoint import load_review_checkpoint
from digest.review_resume import _safe_path
from digest.review_trial import _ALLOWED_MODELS

if TYPE_CHECKING:
    from digest.irritator.evidence_stage import EvidenceIrritatorResult


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


def _render_result(result: EvidenceIrritatorResult) -> str:
    lines = ['# Irritator: bounded post-delivery supplement', f'Status: {result.status}',
             f'Original evidence bundle: {result.bundle_id}',
             'Limited coverage: at most one narrative; RSS excerpts are not full-article verification.']
    for ranked in result.ranked_signals:
        lines.extend([f'\n## {ranked.signal.title}', ranked.signal.url,
                      f'Challenges or complicates: {ranked.narrative_claim}',
                      f'Score: {ranked.score}/10. {ranked.reasoning}'])
    lines.append('\n## Stage diagnostics\n' + json.dumps(asdict(result), ensure_ascii=False, indent=2))
    return '\n'.join(lines) + '\n'


async def _send_supplement(result: EvidenceIrritatorResult, config: Config) -> str:
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
    lines = [header, status,
             'Охват ограничен; RSS-выдержки, не полные статьи.' if russian
             else 'Limited coverage; RSS excerpts, not full articles.']
    if result.narratives:
        label = "Проверяем: " if russian else "Narrative checked: "
        lines.append(label + result.narratives[0].claim[:350])
    for ranked in result.ranked_signals[:2]:
        candidate = [ranked.signal.title[:180], ranked.signal.url,
                     ranked.reasoning[:400]]
        if len(escape_markdownv2('\n'.join(lines + candidate))) > 3600:
            break
        lines.extend(candidate)
    text = escape_markdownv2('\n'.join(lines))
    # Never retry an uncertain POST: Telegram has no idempotency key for sendMessage.
    async with httpx.AsyncClient() as client:
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
    from digest.irritator.evidence_stage import run_evidence_irritator

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
    if (type(record.get('schema_version')) is not int or record['schema_version'] != 1
            or record.get('execute_started')
            or record.get('checkpoint') != checkpoint.relative_to(Path.cwd().resolve()).as_posix()
            or record.get('bundle_id') != bundle.bundle_id
            or record.get('checkpoint_sha256') != hashlib.sha256(source_bytes).hexdigest()):
        raise ValueError('Invalid, changed or already executed post-delivery checkpoint.')
    record['execute_started'] = datetime.now(UTC).isoformat()
    atomic_json_write(marker, record)
    try:
        async with httpx.AsyncClient() as client:
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
    atomic_json_write(output, asdict(result))
    markdown.write_text(_render_result(result), encoding='utf-8')
    record['stage_status'] = result.status
    record['supplement_status'] = 'dispatching'
    atomic_json_write(marker, record)
    try:
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
