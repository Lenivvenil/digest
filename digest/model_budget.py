"""One ten-request model allowance shared by the fixed stages of a GitHub run.

A stage claim must be committed and verified remotely before its worker starts.
Requests are recorded locally before dispatch, including failures with no known
HTTP outcome. Only the original worker's local state, after that process exits,
can be finalized to release unused allowance. A lost workspace leaves its remote
claim active: fetching that claim is not evidence of zero requests.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import logging
import os
import re
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from digest._util import atomic_json_write

MAX_REQUESTS = 10
STAGES = ("prepare", "irritator", "comparison")
_SCHEMA_VERSION = 1
_MAX_BYTES = 32_768
logger = logging.getLogger(__name__)


class ModelBudgetError(ValueError):
    """The model allowance cannot safely authorize another request."""


@dataclass(frozen=True)
class RequestAttempt:
    provider: str
    model: str
    kind: str
    reserved_at: str


@dataclass(frozen=True)
class StageBudget:
    stage: str
    run_attempt: int
    allowance: int
    reserved_at: str
    nonce: str
    claim_sha: str
    begun_at: str | None = None
    execution_nonce: str | None = None
    attempts: tuple[RequestAttempt, ...] | None = None
    finalized_at: str | None = None


@dataclass(frozen=True)
class StageClaim:
    path: Path
    cycle_id: str
    stage: str
    claim_sha: str
    remaining: int


@dataclass(frozen=True)
class StageExecution(StageClaim):
    run_attempt: int
    execution_nonce: str


@dataclass(frozen=True)
class RequestReservation:
    remaining: int
    reserved_at: datetime
    previous_reserved_at: datetime | None


@dataclass(frozen=True)
class BudgetSnapshot:
    path: Path
    cycle_id: str
    limit: int
    stages: tuple[StageBudget, ...]

    @property
    def reserved_count(self) -> int:
        return sum(len(stage.attempts or ()) for stage in self.stages)

    @property
    def remaining(self) -> int:
        """Unused slots; an active stage still holds exclusive access to them."""
        return self.limit - self.reserved_count

    @property
    def active_stage(self) -> str | None:
        return self.stages[-1].stage if self.stages[-1].finalized_at is None else None

    @property
    def claim_sha(self) -> str:
        return self.stages[-1].claim_sha

    @property
    def last_reserved_at(self) -> datetime | None:
        for stage in reversed(self.stages):
            if stage.attempts:
                return datetime.fromisoformat(stage.attempts[-1].reserved_at)
        return None


def _safe(path: Path) -> Path:
    path = Path(os.path.abspath(path))
    if any(ord(character) < 32 for character in str(path)):
        raise ModelBudgetError("Model budget paths must not contain control characters.")
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ModelBudgetError("Model budget paths must not contain symlinks.")
    return path


def _path(cycle_id: str, cache_dir: str | Path) -> Path:
    if not isinstance(cycle_id, str) or re.fullmatch(r"[1-9][0-9]{0,29}", cycle_id) is None:
        raise ModelBudgetError("Model budget cycle must be the numeric GitHub run ID.")
    return _safe(Path(cache_dir) / "model_budgets" / f"{cycle_id}.json")


def _instant(now: datetime | None) -> datetime:
    instant = now or datetime.now(UTC)
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ModelBudgetError("Model request reservations require a timezone-aware time.")
    return instant.astimezone(UTC)


def _timestamp(value: object) -> str:
    if not isinstance(value, str):
        raise ModelBudgetError("Invalid model budget timestamp.")
    instant = datetime.fromisoformat(value)
    if instant.utcoffset() != timedelta(0) or instant.isoformat() != value:
        raise ModelBudgetError("Model budget timestamps must use canonical UTC.")
    return value


def _identifier(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/@+\-]{0,199}", value) is None:
        raise ModelBudgetError("Model request metadata must contain only bounded identifiers.")
    return value


def _stage_name(stage: object) -> str:
    if not isinstance(stage, str) or stage not in STAGES:
        raise ModelBudgetError("Model budget stage must be prepare, irritator, or comparison.")
    return stage


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _sha(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _claim_sha(cycle_id: str, stage: StageBudget) -> str:
    return _sha({"cycle_id": cycle_id, "stage": stage.stage, "run_attempt": stage.run_attempt,
                 "allowance": stage.allowance,
                 "reserved_at": stage.reserved_at, "nonce": stage.nonce})


def _object(value: object, fields: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise ModelBudgetError("Invalid model budget fields.")
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ModelBudgetError("Duplicate model budget JSON key.")
        result[key] = value
    return result


def _restore_stage(value: object, cycle_id: str) -> StageBudget:
    record = _object(value, {"stage", "run_attempt", "allowance", "reserved_at", "nonce", "claim_sha",
                             "begun_at", "execution_nonce", "attempts", "finalized_at"})
    if (type(record["allowance"]) is not int or not 0 <= record["allowance"] <= MAX_REQUESTS
            or type(record["run_attempt"]) is not int or record["run_attempt"] != 1
            or not isinstance(record["nonce"], str) or re.fullmatch(r"[0-9a-f]{32}", record["nonce"]) is None
            or not isinstance(record["claim_sha"], str)):
        raise ModelBudgetError("Invalid model stage allowance or claim.")
    begun = None if record["begun_at"] is None else _timestamp(record["begun_at"])
    if begun is None:
        if any(record[key] is not None for key in ("execution_nonce", "attempts", "finalized_at")):
            raise ModelBudgetError("Unstarted model stage has an unexpected usage journal.")
    elif (not isinstance(record["execution_nonce"], str)
          or re.fullmatch(r"[0-9a-f]{32}", record["execution_nonce"]) is None
          or not isinstance(record["attempts"], list) or len(record["attempts"]) > record["allowance"]):
        raise ModelBudgetError("Begun model stage is missing its exact execution journal.")
    previous = _timestamp(record["reserved_at"])
    if begun is not None:
        if datetime.fromisoformat(begun) < datetime.fromisoformat(previous):
            raise ModelBudgetError("Model stage began before its reservation.")
        previous = begun
    attempts: list[RequestAttempt] = []
    for raw in record["attempts"] or ():
        item = _object(raw, {"provider", "model", "kind", "reserved_at"})
        attempt = RequestAttempt(_identifier(item["provider"]), _identifier(item["model"]),
                                 _identifier(item["kind"]), _timestamp(item["reserved_at"]))
        if datetime.fromisoformat(attempt.reserved_at) < datetime.fromisoformat(previous):
            raise ModelBudgetError("Model request reservation times are out of order.")
        attempts.append(attempt)
        previous = attempt.reserved_at
    finalized = None if record["finalized_at"] is None else _timestamp(record["finalized_at"])
    if finalized is not None and datetime.fromisoformat(finalized) < datetime.fromisoformat(previous):
        raise ModelBudgetError("Model stage finalized before its request reservations.")
    stage = StageBudget(_stage_name(record["stage"]), record["run_attempt"], record["allowance"],
                        record["reserved_at"], record["nonce"], record["claim_sha"], begun,
                        record["execution_nonce"], tuple(attempts) if begun is not None else None, finalized)
    if stage.claim_sha != _claim_sha(cycle_id, stage):
        raise ModelBudgetError("Model stage claim hash mismatch.")
    return stage


def _load(path: Path, cycle_id: str) -> BudgetSnapshot:
    if not path.is_file() or path.stat().st_size > _MAX_BYTES:
        raise ModelBudgetError("Model budget state is missing or exceeds its size limit; model requests are held.")
    try:
        record = _object(json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object),
                         {"schema_version", "cycle_id", "limit", "stages", "sha256"})
        if (type(record["schema_version"]) is not int or record["schema_version"] != _SCHEMA_VERSION
                or record["cycle_id"] != cycle_id
                or type(record["limit"]) is not int or not 0 <= record["limit"] <= MAX_REQUESTS
                or not isinstance(record["stages"], list) or not 1 <= len(record["stages"]) <= len(STAGES)):
            raise ModelBudgetError("Invalid model budget envelope.")
        if record["sha256"] != _sha({key: value for key, value in record.items() if key != "sha256"}):
            raise ModelBudgetError("Model budget checksum mismatch.")
        stages = tuple(_restore_stage(value, cycle_id) for value in record["stages"])
        remaining = record["limit"]
        previous_index = -1
        previous_end: str | None = None
        for index, stage in enumerate(stages):
            stage_index = STAGES.index(stage.stage)
            if (stage_index <= previous_index or index == 0 and stage.stage != "prepare"
                    or stage.allowance != remaining
                    or index < len(stages) - 1 and stage.finalized_at is None
                    or previous_end is not None
                    and datetime.fromisoformat(stage.reserved_at) < datetime.fromisoformat(previous_end)):
                raise ModelBudgetError("Inconsistent model stage history or remaining allowance.")
            remaining -= len(stage.attempts or ())
            previous_index, previous_end = stage_index, stage.finalized_at
        return BudgetSnapshot(path, cycle_id, record["limit"], stages)
    except (ValueError, TypeError, KeyError, UnicodeError) as exc:
        raise ModelBudgetError("Invalid model budget state; model requests are held.") from exc


@contextmanager
def _locked(path: Path, *, create: bool = False) -> Iterator[None]:
    """Lock the existing budget directory inode, so no lockfile enters Git."""
    try:
        if create:
            path.parent.mkdir(parents=True, exist_ok=True)
        _safe(path)
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            _safe(path)
            yield
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise ModelBudgetError("Cannot read or persist model budget state; model requests are held.") from exc


def _write(snapshot: BudgetSnapshot) -> None:
    body = {"schema_version": _SCHEMA_VERSION, "cycle_id": snapshot.cycle_id, "limit": snapshot.limit,
            "stages": [asdict(stage) for stage in snapshot.stages]}
    record = {**body, "sha256": _sha(body)}
    if len(json.dumps(record, indent=2).encode("utf-8")) > _MAX_BYTES:
        raise ModelBudgetError("Model budget state exceeds its size limit.")
    _safe(snapshot.path.with_suffix(".json.tmp"))
    atomic_json_write(snapshot.path, record)
    with snapshot.path.open("rb") as stored:
        os.fsync(stored.fileno())
    directory = os.open(snapshot.path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def inspect_budget(cycle_id: str, cache_dir: str | Path = ".cache") -> BudgetSnapshot:
    """Read only the addressed cycle, rejecting absent or unverifiable state."""
    path = _path(cycle_id, cache_dir)
    with _locked(path):
        return _load(path, cycle_id)


def reserve_stage(
    cycle_id: str, stage: str, cache_dir: str | Path = ".cache", *, run_attempt: int,
    limit: int = MAX_REQUESTS, now: datetime | None = None,
) -> StageClaim:
    """Persist a new exclusive stage claim; callers must verify remote persistence.

    A missing cycle is new only in prepare on GitHub run attempt one. Repeated
    stages are never regranted, including after finalization or a workflow rerun.
    """
    path, stage, instant = _path(cycle_id, cache_dir), _stage_name(stage), _instant(now)
    if type(run_attempt) is not int or run_attempt < 1:
        raise ModelBudgetError("Model budget requires the GitHub run attempt.")
    if run_attempt != 1:
        raise ModelBudgetError("A workflow rerun cannot grant another cycle or stage allowance.")
    if type(limit) is not int or not 0 <= limit <= MAX_REQUESTS:
        raise ModelBudgetError("The cycle model allowance must be between zero and ten.")
    with _locked(path, create=stage == "prepare" and run_attempt == 1):
        if not path.exists():
            if stage != "prepare" or run_attempt != 1:
                raise ModelBudgetError("Missing cycle usage cannot grant a new model allowance.")
            snapshot = BudgetSnapshot(path, cycle_id, limit, ())
        else:
            snapshot = _load(path, cycle_id)
            if snapshot.active_stage is not None:
                raise ModelBudgetError("A prior model stage is active or its usage is unknown; requests are held.")
            if STAGES.index(stage) <= STAGES.index(snapshot.stages[-1].stage):
                raise ModelBudgetError("This model stage already ran or was passed; reruns cannot regrant it.")
            if instant < datetime.fromisoformat(snapshot.stages[-1].finalized_at or ""):
                raise ModelBudgetError("New model stage time precedes the previous stage completion.")
        record = StageBudget(stage, run_attempt, snapshot.remaining, instant.isoformat(), secrets.token_hex(16), "")
        record = replace(record, claim_sha=_claim_sha(cycle_id, record))
        _write(replace(snapshot, stages=(*snapshot.stages, record)))
        return StageClaim(path, cycle_id, stage, record.claim_sha, record.allowance)


def _active(snapshot: BudgetSnapshot, stage: str, claim_sha: str) -> StageBudget:
    current = snapshot.stages[-1]
    if (current.stage != _stage_name(stage) or current.claim_sha != claim_sha
            or current.finalized_at is not None):
        raise ModelBudgetError("Model request does not match the current active cycle and stage claim.")
    return current


def begin_stage(
    cycle_id: str, stage: str, claim_sha: str, *, run_attempt: int,
    cache_dir: str | Path = ".cache", now: datetime | None = None,
) -> StageExecution:
    """Bind one local execution journal after the remote claim barrier succeeds.

    This local-only marker is not pushed before execution. The remote reservation
    therefore cannot masquerade as an empty journal if the worker state is lost.
    """
    path, instant = _path(cycle_id, cache_dir), _instant(now)
    with _locked(path):
        snapshot = _load(path, cycle_id)
        current = _active(snapshot, stage, claim_sha)
        if type(run_attempt) is not int or current.run_attempt != run_attempt or current.begun_at is not None:
            raise ModelBudgetError("Model stage already began or belongs to another workflow attempt.")
        if instant < datetime.fromisoformat(current.reserved_at):
            raise ModelBudgetError("Model stage cannot begin before its reservation.")
        nonce = secrets.token_hex(16)
        updated = replace(current, begun_at=instant.isoformat(), execution_nonce=nonce, attempts=())
        _write(replace(snapshot, stages=(*snapshot.stages[:-1], updated)))
        return StageExecution(path, cycle_id, stage, claim_sha, snapshot.remaining, run_attempt, nonce)


def _execution(snapshot: BudgetSnapshot, stage: str, claim_sha: str,
               run_attempt: int, execution_nonce: str) -> StageBudget:
    current = _active(snapshot, stage, claim_sha)
    if (type(run_attempt) is not int or current.run_attempt != run_attempt or current.begun_at is None
            or current.attempts is None or not execution_nonce or current.execution_nonce != execution_nonce):
        raise ModelBudgetError("The original model execution journal is missing or does not match; requests are held.")
    return current


def reserve_request(
    cycle_id: str, stage: str, claim_sha: str, *, provider: str, model: str, kind: str,
    run_attempt: int, execution_nonce: str,
    cache_dir: str | Path = ".cache", now: datetime | None = None,
) -> RequestReservation:
    """Spend one slot durably before credentials, HTTP, fallback, or token counting.

    This records an attempted request, never confirmation that a POST occurred.
    There is no refund path: an unknown transport outcome still spends its slot.
    """
    path = _path(cycle_id, cache_dir)
    provider, model, kind = _identifier(provider), _identifier(model), _identifier(kind)
    with _locked(path):
        instant = _instant(now)
        attempt = RequestAttempt(provider, model, kind, instant.isoformat())
        snapshot = _load(path, cycle_id)
        current = _execution(snapshot, stage, claim_sha, run_attempt, execution_nonce)
        if snapshot.remaining <= 0:
            raise ModelBudgetError("The shared cycle model request allowance is exhausted.")
        previous = snapshot.last_reserved_at
        if instant < datetime.fromisoformat(current.begun_at or "") or previous is not None and instant < previous:
            raise ModelBudgetError("Model request time precedes the last reservation.")
        updated = replace(current, attempts=(*(current.attempts or ()), attempt))
        _write(replace(snapshot, stages=(*snapshot.stages[:-1], updated)))
        return RequestReservation(snapshot.remaining - 1, instant, previous)


def finalize_stage(
    cycle_id: str, stage: str, claim_sha: str, cache_dir: str | Path = ".cache", *, run_attempt: int,
    execution_nonce: str, now: datetime | None = None,
) -> BudgetSnapshot:
    """Release verified unused slots after the original worker process has exited.

    The orchestrator must call this in that worker's original workspace, after
    process completion. Never finalize a fetched active claim after lost usage.
    This function cannot establish process completion or reconstruct lost usage.
    """
    path, instant = _path(cycle_id, cache_dir), _instant(now)
    with _locked(path):
        snapshot = _load(path, cycle_id)
        current = _execution(snapshot, stage, claim_sha, run_attempt, execution_nonce)
        last = snapshot.last_reserved_at
        if instant < datetime.fromisoformat(current.begun_at or "") or last is not None and instant < last:
            raise ModelBudgetError("Model stage cannot finalize before its request reservations.")
        updated = replace(current, finalized_at=instant.isoformat())
        result = replace(snapshot, stages=(*snapshot.stages[:-1], updated))
        _write(result)
        return result


def _execution_environment() -> tuple[str, str, str, int, str] | None:
    prefix = "DIGEST_MODEL_BUDGET_"
    names = ("CYCLE", "STAGE", "CLAIM_SHA", "ATTEMPT", "EXECUTION_NONCE")
    required = os.environ.get(prefix + "REQUIRED")
    if required not in (None, "0", "1"):
        raise ModelBudgetError("DIGEST_MODEL_BUDGET_REQUIRED must be zero or one; model requests are held.")
    enabled = required == "1" or any(prefix + name in os.environ for name in names)
    if not enabled:
        return None
    values = [os.environ.get(prefix + name, "") for name in names]
    if not all(values) or re.fullmatch(r"[1-9][0-9]*", values[3]) is None:
        raise ModelBudgetError("Required model cycle execution credentials are missing; model requests are held.")
    if (os.environ.get("GITHUB_RUN_ID", values[0]) != values[0]
            or os.environ.get("GITHUB_RUN_ATTEMPT", values[3]) != values[3]):
        raise ModelBudgetError("Model execution credentials differ from the immutable GitHub run identity.")
    return values[0], values[1], values[2], int(values[3]), values[4]


def execution_from_env(cache_dir: str | Path = ".cache") -> BudgetSnapshot | None:
    """Inspect only the verified current local execution, without granting allowance."""
    fields = _execution_environment()
    if fields is None:
        return None
    cycle, stage, claim, attempt, nonce = fields
    path = _path(cycle, cache_dir)
    with _locked(path):
        snapshot = _load(path, cycle)
        _execution(snapshot, stage, claim, attempt, nonce)
        return snapshot


def reserve_request_from_env(
    *, provider: str, model: str, kind: str, cache_dir: str | Path = ".cache", now: datetime | None = None,
) -> RequestReservation | None:
    """Use only explicit execution ownership; never initialize missing usage."""
    fields = _execution_environment()
    if fields is None:
        return None
    cycle, stage, claim, attempt, nonce = fields
    return reserve_request(cycle, stage, claim, run_attempt=attempt, execution_nonce=nonce,
                           provider=provider, model=model, kind=kind, cache_dir=cache_dir, now=now)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("reserve", "begin", "finalize", "inspect"))
    parser.add_argument("--cycle-id", default=os.environ.get("GITHUB_RUN_ID"))
    parser.add_argument("--stage", choices=STAGES)
    parser.add_argument("--claim-sha")
    parser.add_argument("--execution-nonce")
    parser.add_argument("--cache-dir", default=".cache")
    parser.add_argument("--run-attempt", type=int, default=os.environ.get("GITHUB_RUN_ATTEMPT"))
    parser.add_argument("--limit", type=int, default=MAX_REQUESTS)
    args = parser.parse_args(argv)
    if args.cycle_id is None:
        parser.error("--cycle-id or GITHUB_RUN_ID is required")
    if args.command != "inspect" and args.stage is None:
        parser.error("--stage is required")
    if args.command != "inspect" and args.run_attempt is None:
        parser.error("--run-attempt or GITHUB_RUN_ATTEMPT is required")
    if args.command in ("begin", "finalize") and args.claim_sha is None:
        parser.error("--claim-sha is required")
    if args.command == "finalize" and args.execution_nonce is None:
        parser.error("--execution-nonce is required")
    try:
        result: StageClaim | BudgetSnapshot
        if args.command == "reserve":
            result = reserve_stage(args.cycle_id, args.stage, args.cache_dir,
                                   run_attempt=args.run_attempt, limit=args.limit)
        elif args.command == "begin":
            result = begin_stage(args.cycle_id, args.stage, args.claim_sha,
                                 cache_dir=args.cache_dir, run_attempt=args.run_attempt)
        elif args.command == "finalize":
            result = finalize_stage(args.cycle_id, args.stage, args.claim_sha, args.cache_dir,
                                    run_attempt=args.run_attempt, execution_nonce=args.execution_nonce)
        else:
            result = inspect_budget(args.cycle_id, args.cache_dir)
        output = f"claim_sha={result.claim_sha}\npath={result.path}\nremaining={result.remaining}\n"
        if isinstance(result, BudgetSnapshot):
            output += f"active_stage={result.active_stage or ''}\n"
        if isinstance(result, StageExecution):
            output += f"execution_nonce={result.execution_nonce}\n"
        if destination := os.environ.get("GITHUB_OUTPUT"):
            with Path(destination).open("a", encoding="utf-8") as handle:
                handle.write(output)
        logger.info("Model budget %s: cycle=%s remaining=%d", args.command, args.cycle_id, result.remaining)
        return 0
    except (ModelBudgetError, OSError) as exc:
        logger.error("Model budget held: %s", exc)
        return 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(main())
