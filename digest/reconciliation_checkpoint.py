"""One candidate-bound Git barrier inside the existing preparation execution.

This is a technical response checkpoint, never publication acceptance or runner
recovery. The active local budget journal is never part of the mid-stage commit.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import math
import os
import signal
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from digest import llm, model_budget
from digest import reconciliation_operation as operation
from digest._serialization import restore_dataclass as _restore
from digest._serialization import unique_object as _unique_object
from digest._util import atomic_json_write
from digest.adapters.models.execution import ModelExecution
from digest.adapters.storage.checkpoints import safe_checkpoint_path as _safe
from digest.candidate_review import CandidateProgress, load_candidate_progress
from digest.config import Config, load_config
from digest.reading_brief_state import BriefState, Source, checksum, load_source, load_state, state_root
from digest.reading_preparation import (
    ReadingBinding,
    _binding,
    _currently_selected,
    _handoff_body,
    _hash,
    reading_deadline,
)
from digest.reading_reconciliation import ReconciliationInput, build_reconciliation_input

RECONCILIATION_CHECKPOINT_PROTOCOL = 1
REMOTE = "refs/remotes/origin/main"
MINIMUM_WINDOW = 80.0
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FileReference:
    path: str
    sha256: str | None


@dataclass(frozen=True)
class BatchItem:
    identity: str
    handoff: str
    manifest_sha256: str


@dataclass(frozen=True)
class ReconciliationBatch:
    protocol: int
    cycle_id: str
    claim_sha: str
    execution_nonce: str
    baseline: str
    deadline_unix: float
    not_before_unix: float
    remaining: int
    local_budget: FileReference
    remote_budget_sha256: str
    items: tuple[BatchItem, ...]
    files: tuple[FileReference, ...]


def _relative(path: Path) -> str:
    safe = _safe(path)
    relative = safe.relative_to(Path.cwd().resolve()).as_posix()
    if relative.startswith(".git/") or any(ord(char) < 32 for char in relative):
        raise ValueError("technical_checkpoint_path")
    return relative


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _reference(path: Path) -> FileReference:
    relative = _relative(path)
    return FileReference(relative, _digest(path.read_bytes()) if path.is_file() else None)


def _json(path: Path) -> Any:
    return json.loads(_safe(path).read_text(), object_pairs_hook=_unique_object)


async def _git(*args: str, deadline: float, optional: bool = False) -> bytes:
    remaining = min(20.0, deadline - time.monotonic())
    if remaining <= 0:
        raise TimeoutError("technical_deadline")
    process = await asyncio.create_subprocess_exec(
        "git",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(), remaining)
    except BaseException:
        if process.returncode is None:
            os.killpg(process.pid, signal.SIGKILL)
        await process.wait()
        raise
    if process.returncode and not optional:
        raise ValueError("technical_checkpoint_git")
    return output if process.returncode == 0 else b""


async def _fetch(deadline: float) -> str:
    if (await _git("rev-parse", "--is-shallow-repository", deadline=deadline)).strip() != b"false":
        raise ValueError("technical_checkpoint_history")
    await _git("fetch", "--no-tags", "origin", "+refs/heads/main:" + REMOTE, deadline=deadline)
    return (await _git("rev-parse", REMOTE, deadline=deadline)).decode().strip()


async def _remote_file(commit: str, path: str, deadline: float) -> bytes | None:
    exists = await _git("ls-tree", commit, "--", path, deadline=deadline)
    return await _git("show", f"{commit}:{path}", deadline=deadline) if exists else None


def eligible_handoffs(progress: CandidateProgress, state_dir: Path) -> tuple[str, ...]:
    """Use current candidate proofs, including earlier deferred source handoffs."""
    found: set[str] = set()
    for packet in progress.packets:
        report = packet.report
        if report is None:
            continue
        for article in packet.articles:
            from digest.radar.collector import article_hash

            identity = article_hash(article.title, article.link)
            candidate = progress.candidates.get(identity)
            if candidate is None or not _currently_selected(candidate, progress, packet, report):
                continue
            try:
                expected = _binding(packet, report, identity)
                raw = _json(state_dir / "reading_bindings" / f"{identity}.json")
                binding: ReadingBinding = _restore(raw["binding"], ReadingBinding)
                if (
                    raw["sha256"] != _hash(asdict(binding))
                    or replace(binding, evidence_origin="current_selection_binding") != expected
                    or _hash(asdict(candidate.article)) != binding.occurrence_sha256
                ):
                    continue
                state = load_state(state_dir, identity)
                if state.status not in {"ready", "abstained"}:
                    continue
                body = _handoff_body(binding, state, state_dir)
                path = state_dir / "reading_handoffs" / f"{_hash(body)}.json"
                if _json(path) == body:
                    found.add(_relative(path))
            except (OSError, ValueError, KeyError, TypeError):
                continue
    return tuple(sorted(found))


def _input(handoff: str, state_dir: Path) -> tuple[ReconciliationInput, Source, BriefState]:
    raw = _json(Path(handoff))
    binding: ReadingBinding = _restore(raw["binding"], ReadingBinding)
    state = load_state(state_dir, binding.identity)
    if raw != _handoff_body(binding, state, state_dir):
        raise ValueError("technical_checkpoint_evidence")
    saved = _json(state_dir / "reading_bindings" / f"{binding.identity}.json")
    if saved != {"binding": asdict(binding), "sha256": _hash(asdict(binding))}:
        raise ValueError("technical_checkpoint_evidence")
    source = load_source(state_dir, state)
    return build_reconciliation_input(source, state), source, state


def _candidate_files(state_dir: Path) -> set[Path]:
    """Follow the active storage graph, without dereferencing historical audit IDs."""
    load_candidate_progress(state_dir)
    path = state_dir / "candidate_progress.json"
    paths = {path}
    body = _json(path)["candidate_accounting"]
    article_refs = set()
    for candidate in body["candidates"].values():
        article_refs.add(candidate["article_ref"])
        article_refs.update(candidate["occurrence_refs"])
    for packet in body["packets"]:
        if "packet_ref" in packet:
            path = state_dir / "candidate_reports" / f"{packet['packet_ref']}.json"
            paths.add(path)
            saved = _json(path)["packet"]
        else:
            saved = packet["packet"]
        article_refs.update(saved["article_refs"])
    paths.update(state_dir / "candidate_sources" / f"{reference}.json" for reference in article_refs)
    return paths


def _active() -> model_budget.BudgetSnapshot:
    snapshot = model_budget.execution_from_env()
    if snapshot is None or snapshot.active_stage != "prepare":
        raise ValueError("technical_checkpoint_execution")
    return snapshot


async def _rebind_unstarted(
    record: operation.OperationRecord,
    value: ReconciliationInput,
    source: Source,
    state: BriefState,
    config: Config,
    state_dir: Path,
    claim: model_budget.StageClaim,
    baseline: str,
    deadline: float,
    rollback: dict[Path, bytes | None],
) -> tuple[operation.PreparedReconciliation, set[Path]]:
    """Bind a finalized per-item zero-attempt proof without refunding its peers."""
    if record.attempts:
        raise ValueError("technical_checkpoint_transition_required")
    old_cycle = record.prepared.origin.cycle_id
    old_path = state_dir / "reconciliation_batches" / f"{old_cycle}.json"
    old_budget = model_budget.inspect_budget(old_cycle, state_dir)
    for path in (old_path, old_budget.path):
        if await _remote_file(baseline, _relative(path), deadline) != path.read_bytes():
            raise ValueError("technical_checkpoint_unstarted_proof_missing")
    old = _load_batch(old_path)
    if record.execution is not None and record.execution.execution_nonce != old.execution_nonce:
        raise ValueError("technical_checkpoint_transition_required")
    stage = next(item for item in old_budget.stages if item.stage == "prepare")
    outcome_path = state_dir / "reconciliation_outcomes" / f"{old_cycle}.json"
    if await _remote_file(baseline, _relative(outcome_path), deadline) != outcome_path.read_bytes():
        raise ValueError("technical_checkpoint_unstarted_proof_missing")
    outcome = _json(outcome_path)
    if (
        stage.finalized_at is None
        or stage.claim_sha != old.claim_sha
        or stage.execution_nonce != old.execution_nonce
        or outcome.get("prepare_stage_sha256") != checksum(asdict(stage))
        or outcome.get("batch_sha256") != _digest(old_path.read_bytes())
        or outcome.get("unstarted", {}).get(record.prepared.article_identity)
        != _digest(operation._path(state_dir, record.prepared.article_identity).read_bytes())
        or not any(item.manifest_sha256 == record.prepared.manifest_sha256 for item in old.items)
    ):
        raise ValueError("technical_checkpoint_transition_required")
    archive = _safe(state_dir / "article_reconciliation" / "revisions" / f"{record.prepared.manifest_sha256}.json")
    archive.parent.mkdir(exist_ok=True)
    path = operation._path(state_dir, record.prepared.article_identity)
    with operation._locked(path):
        if operation._load(path, value, source, state, config) != record:
            raise ValueError("technical_checkpoint_changed")
        if archive.exists() and archive.read_bytes() != path.read_bytes():
            raise ValueError("technical_checkpoint_changed")
        if not archive.exists():
            archive.write_bytes(path.read_bytes())
        prepared = operation._manifest(
            value, source, state, config, operation.IntentOrigin(claim.cycle_id, claim.stage, claim.claim_sha)
        )
        rollback[path] = path.read_bytes()
        operation._save(path, operation.OperationRecord(prepared))
    return prepared, {archive, old_path, old_budget.path, outcome_path}


async def prepare_batch(
    progress: CandidateProgress,
    config: Config,
    config_path: str,
    deadline: float,
    *,
    state_dir: Path = Path(".cache"),
    execution: ModelExecution,
) -> Path | None:
    rollback: dict[Path, bytes | None] = {}
    try:
        return await _prepare_batch(progress, config, config_path, deadline, state_dir, rollback, execution=execution)
    except BaseException:
        # Preparation cannot dispatch. Undo only this invocation's uncheckpointed
        # intents on a timeout/cancellation, preserving any prior exact record.
        for path, previous in reversed(rollback.items()):
            if previous is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(previous)
        raise


async def _prepare_batch(
    progress: CandidateProgress,
    config: Config,
    config_path: str,
    deadline: float,
    state_dir: Path,
    rollback: dict[Path, bytes | None],
    *,
    execution: ModelExecution,
) -> Path | None:
    if os.environ.get("DIGEST_RECONCILIATION_CHECKPOINT_REQUIRED") != "1" or not config.reading_brief.enabled:
        return None
    handoffs = eligible_handoffs(progress, state_dir)
    if not handoffs or deadline - time.monotonic() < MINIMUM_WINDOW:
        return None
    snapshot = _active()
    remaining = llm.request_budget_remaining(config, execution)
    if remaining is None or remaining <= 0:
        return None
    baseline = await _fetch(deadline)
    if (await _git("rev-parse", "HEAD", deadline=deadline)).decode().strip() != baseline:
        raise ValueError("technical_checkpoint_remote_changed")
    if deadline - time.monotonic() < MINIMUM_WINDOW:
        return None
    stage = snapshot.stages[-1]
    assert stage.execution_nonce is not None
    claim = model_budget.StageClaim(snapshot.path, snapshot.cycle_id, "prepare", stage.claim_sha, snapshot.remaining)
    items: list[BatchItem] = []
    remote_budget = await _remote_file(baseline, _relative(snapshot.path), deadline)
    reservation = replace(stage, begun_at=None, execution_nonce=None, attempts=None)
    expected = {
        "schema_version": 1,
        "cycle_id": snapshot.cycle_id,
        "limit": snapshot.limit,
        "stages": [asdict(reservation)],
    }
    if remote_budget is None or json.loads(remote_budget) != {**expected, "sha256": model_budget._sha(expected)}:
        raise ValueError("technical_checkpoint_reservation_changed")
    paths = _candidate_files(state_dir) | {
        Path(config_path),
        state_dir / "seen_articles.json",
        state_dir / "source_state.json",
    }
    for handoff in handoffs:
        path = Path(handoff)
        try:
            value, source, state = _input(handoff, state_dir)
            path = operation._path(state_dir, value.selection.identity)
            retained = await _remote_file(baseline, _relative(path), deadline)
            if not path.exists():
                history = await _git("log", "-1", "--format=%H", baseline, "--", _relative(path), deadline=deadline)
                if retained is not None or history:
                    raise ValueError("technical_checkpoint_history_missing")
                rollback[path] = None
                prepared = operation.prepare_reconciliation_operation(
                    value,
                    source,
                    state,
                    config,
                    state_dir,
                    claim=claim,
                )
            else:
                record = operation._load(path, value, source, state, config)
                if retained is not None and retained != path.read_bytes():
                    raise ValueError("technical_checkpoint_history_changed")
                if any(attempt.kind == "generate" and attempt.status == "completed" for attempt in record.attempts):
                    continue
                if record.prepared.origin != operation.IntentOrigin(snapshot.cycle_id, "prepare", stage.claim_sha):
                    if retained is None:
                        raise ValueError("technical_checkpoint_unstarted_proof_missing")
                    prepared, prior_paths = await _rebind_unstarted(
                        record,
                        value,
                        source,
                        state,
                        config,
                        state_dir,
                        claim,
                        baseline,
                        deadline,
                        rollback,
                    )
                    paths.update(prior_paths)
                else:
                    if record.execution is not None or record.attempts:
                        raise ValueError("technical_checkpoint_execution_held")
                    prepared = record.prepared
            items.append(BatchItem(value.selection.identity, handoff, prepared.manifest_sha256))
            paths.update(
                {
                    path,
                    Path(handoff),
                    state_dir / "reading_bindings" / f"{value.selection.identity}.json",
                    state_root(state_dir) / f"{value.selection.identity}.json",
                    state_root(state_dir) / "sources" / f"{state.source_sha256}.json",
                }
            )
        except TimeoutError:
            raise
        except (OSError, ValueError, TypeError, KeyError) as exc:
            if path in rollback:
                previous = rollback.pop(path)
                if previous is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_bytes(previous)
            logger.warning("Reconciliation candidate held: %s", exc)
    if not items:
        return None
    runtime = llm._request_state(config, execution)
    offset = time.time() - time.monotonic()
    batch = ReconciliationBatch(
        RECONCILIATION_CHECKPOINT_PROTOCOL,
        snapshot.cycle_id,
        stage.claim_sha,
        stage.execution_nonce,
        baseline,
        deadline + offset,
        max(time.time(), runtime.next_request_at + offset),
        remaining,
        _reference(snapshot.path),
        _digest(remote_budget),
        tuple(items),
        tuple(sorted((_reference(path) for path in paths), key=lambda item: item.path)),
    )
    path = _safe(state_dir / "reconciliation_batches" / f"{snapshot.cycle_id}.json")
    if path.exists():
        raise ValueError("technical_checkpoint_batch_exists")
    path.parent.mkdir(exist_ok=True)
    rollback[path] = None
    atomic_json_write(path, {"batch": asdict(batch), "sha256": checksum(asdict(batch))})
    return path


async def prepare_current_batch(
    progress: CandidateProgress | None,
    config: Config,
    config_path: str,
    started: float,
    *,
    execution: ModelExecution,
) -> None:
    if progress is None or not getattr(getattr(config, "reading_brief", None), "enabled", False):
        return
    try:
        path = await prepare_batch(
            progress, config, config_path, reading_deadline(config, started), execution=execution,
        )
        if path is not None and (output := os.environ.get("GITHUB_OUTPUT")):
            with Path(output).open("a") as stream:
                stream.write(f"reconciliation_batch={_relative(path)}\n")
    except (OSError, ValueError, TypeError, KeyError, TimeoutError) as exc:
        logger.warning("Reconciliation checkpoint held: %s", exc)


def _load_batch(path: Path) -> ReconciliationBatch:
    raw = _json(path)
    if set(raw) != {"batch", "sha256"} or raw["sha256"] != checksum(raw["batch"]):
        raise ValueError("technical_checkpoint_integrity")
    batch: ReconciliationBatch = _restore(raw["batch"], ReconciliationBatch)
    if (
        batch.protocol != RECONCILIATION_CHECKPOINT_PROTOCOL
        or not batch.items
        or any(not math.isfinite(value) for value in (batch.deadline_unix, batch.not_before_unix))
        or type(batch.remaining) is not int
        or not 0 <= batch.remaining <= 10
    ):
        raise ValueError("technical_checkpoint_protocol")
    return batch


def _verify_local(batch: ReconciliationBatch) -> None:
    snapshot = _active()
    stage = snapshot.stages[-1]
    if (snapshot.cycle_id, stage.claim_sha, stage.execution_nonce) != (
        batch.cycle_id,
        batch.claim_sha,
        batch.execution_nonce,
    ):
        raise ValueError("technical_checkpoint_execution")
    if _reference(snapshot.path) != batch.local_budget:
        raise ValueError("technical_checkpoint_changed")
    for reference in batch.files:
        if _reference(Path(reference.path)) != reference:
            raise ValueError("technical_checkpoint_changed")


async def checkpoint_and_execute(
    path: Path, config: Config, *, execution: ModelExecution,
) -> tuple[operation.OperationResult, ...]:
    batch = _load_batch(path)
    deadline = time.monotonic() + batch.deadline_unix - time.time()
    if managed := os.environ.get("PREPARATION_DEADLINE"):
        deadline = min(deadline, time.monotonic() + int(managed) - time.time())
    if deadline - time.monotonic() < 1:
        raise TimeoutError("technical_deadline")
    _verify_local(batch)
    batch_hash = _digest(path.read_bytes())
    if (await _git("rev-parse", "HEAD", deadline=deadline)).decode().strip() != batch.baseline or await _fetch(
        deadline
    ) != batch.baseline:
        raise ValueError("technical_checkpoint_remote_changed")
    references = (*batch.files, _reference(path))
    if any(item.path == batch.local_budget.path for item in references):
        raise ValueError("technical_checkpoint_active_budget_staged")
    if await _git("diff", "--cached", "--name-only", deadline=deadline):
        raise ValueError("technical_checkpoint_dirty_index")
    await _git("add", "--", *(item.path for item in references if item.sha256 is not None), deadline=deadline)
    await _git("commit", "-m", "reading: checkpoint exact reconciliation batch", deadline=deadline)
    commit = (await _git("rev-parse", "HEAD", deadline=deadline)).decode().strip()
    remote_reservation = FileReference(batch.local_budget.path, batch.remote_budget_sha256)
    retained = await _remote_file(commit, remote_reservation.path, deadline)
    if retained is None or _digest(retained) != remote_reservation.sha256:
        raise ValueError("technical_checkpoint_active_budget_staged")
    await _git("push", "origin", "HEAD:refs/heads/main", deadline=deadline)
    tip = await _fetch(deadline)
    await _git("merge-base", "--is-ancestor", commit, tip, deadline=deadline)
    for reference in (*references, remote_reservation):
        data = await _remote_file(tip, reference.path, deadline)
        if (None if data is None else _digest(data)) != reference.sha256:
            raise ValueError("technical_checkpoint_remote_changed")
    _verify_local(batch)
    if _digest(path.read_bytes()) != batch_hash:
        raise ValueError("technical_checkpoint_changed")
    eligible = set(eligible_handoffs(load_candidate_progress(), Path(".cache")))
    if any(item.handoff not in eligible for item in batch.items):
        raise ValueError("technical_checkpoint_ineligible")
    runtime = llm._request_state(config, execution)
    llm.set_request_limit(config, execution, runtime.requests_attempted + batch.remaining)
    runtime.next_request_at = max(runtime.next_request_at, time.monotonic() + batch.not_before_unix - time.time())
    results = []
    for item in batch.items:
        value, source, state = _input(item.handoff, Path(".cache"))
        assertion = operation.ExactIntentCheckpoint(
            item.manifest_sha256,
            batch.cycle_id,
            "prepare",
            batch.claim_sha,
            commit,
        )
        results.append(
            await operation.execute_reconciliation_operation(
                value,
                source,
                state,
                config,
                Path(".cache"),
                deadline=deadline,
                checkpoint=assertion,
                execution=execution,
            )
        )
    return tuple(results)


async def finalize_batch(path: Path, config: Config) -> None:
    """Finalize the original local journal, then bind actual per-operation outcomes."""
    batch = _load_batch(path)
    snapshot = model_budget.inspect_budget(batch.cycle_id)
    stage = snapshot.stages[-1]
    if model_budget._execution_environment() != (
        batch.cycle_id,
        "prepare",
        batch.claim_sha,
        stage.run_attempt,
        batch.execution_nonce,
    ):
        raise ValueError("technical_checkpoint_execution")
    if (snapshot.cycle_id, stage.claim_sha, stage.execution_nonce) != (
        batch.cycle_id,
        batch.claim_sha,
        batch.execution_nonce,
    ):
        raise ValueError("technical_checkpoint_execution")
    unstarted = {}
    records = {}
    for item in batch.items:
        value, source, state = _input(item.handoff, Path(".cache"))
        operation_path = operation._path(Path(".cache"), item.identity)
        record = operation._load(operation_path, value, source, state, config)
        if record.prepared.manifest_sha256 != item.manifest_sha256:
            raise ValueError("technical_checkpoint_changed")
        records[item.identity] = _digest(operation_path.read_bytes())
        if not record.attempts and (
            record.execution is None or record.execution.execution_nonce == batch.execution_nonce
        ):
            unstarted[item.identity] = records[item.identity]
    finalized = (
        snapshot
        if stage.finalized_at is not None
        else model_budget.finalize_stage(
            batch.cycle_id,
            "prepare",
            batch.claim_sha,
            run_attempt=stage.run_attempt,
            execution_nonce=batch.execution_nonce,
        )
    )
    outcome = Path(".cache/reconciliation_outcomes") / f"{batch.cycle_id}.json"
    outcome.parent.mkdir(exist_ok=True)
    body = {
        "batch_sha256": _digest(path.read_bytes()),
        "prepare_stage_sha256": checksum(asdict(finalized.stages[-1])),
        "records": records,
        "unstarted": unstarted,
    }
    if outcome.exists() and _json(outcome) != body:
        raise ValueError("technical_checkpoint_outcome_changed")
    atomic_json_write(outcome, body)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch", required=True)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--finalize", action="store_true")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        execution = ModelExecution()
        if args.finalize:
            asyncio.run(finalize_batch(Path(args.batch), config))
            return 0
        results = asyncio.run(checkpoint_and_execute(Path(args.batch), config, execution=execution))
        logger.info(
            "Reconciliation technical candidates: %d completed; %d held.",
            sum(result.status == "completed" for result in results),
            sum(result.status == "pending" for result in results),
        )
        return 0
    except (OSError, ValueError, TypeError, KeyError, TimeoutError) as exc:
        logger.error("Reconciliation checkpoint held: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
