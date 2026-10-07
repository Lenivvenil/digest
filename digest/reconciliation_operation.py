"""Local, unconnected execution of the fixed reconciliation response protocol.

The caller must independently verify an exact remote intent checkpoint before
uncached execution. A local atomic file and an active remote stage claim are NOT
that checkpoint. Neither this module nor its caller assertion verifies remote Git,
recovers a lost runner, grants a cycle allowance, or accepts publication work.
"""

from __future__ import annotations

import asyncio
import copy
import fcntl
import hashlib
import json
import math
import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import httpx

from digest import llm, model_budget, source_admission
from digest._util import atomic_json_write
from digest.config import Config, ProviderConfig
from digest.reading_brief import _can_fallback, _generation_timeout, _routes
from digest.reading_brief_state import BriefState, Route, Source, checksum, now
from digest.reading_brief_tokens import TokenProfileUnavailable
from digest.reading_reconciliation import (
    RESPONSE_INSTRUCTION,
    RESPONSE_VERSION,
    ReconciliationCompletion,
    ReconciliationInput,
    ReconciliationResponse,
    _reconciliation_messages,
    parse_reconciliation_response,
    verify_reconciliation_input,
)

OPERATION_VERSION = "article-reconciliation-operation-v1"
TEMPERATURE = 0.1
_OUTCOMES = {"reserved", "completed", "definite_failed", "unknown", "accepted", "accepted_invalid"}
_FINISH_REASONS = {
    "STOP",
    "stop",
    "length",
    "MAX_TOKENS",
    "tool_calls",
    "content_filter",
    "SAFETY",
    "RECITATION",
    "OTHER",
    "error",
}


@dataclass(frozen=True)
class PreparedRequest:
    route: Route
    request_sha256: str


@dataclass(frozen=True)
class IntentOrigin:
    cycle_id: str
    stage: str
    claim_sha: str


@dataclass(frozen=True)
class PreparedReconciliation:
    version: str
    article_identity: str
    input_sha256: str
    input_json: str
    requests: tuple[PreparedRequest, ...]
    origin: IntentOrigin
    manifest_sha256: str


@dataclass(frozen=True)
class ExactIntentCheckpoint:
    """Trusted caller assertion of a verified remote checkpoint, not its proof.

    The caller must have checked remote operation history before asserting first
    use, and verified that this complete manifest and stage claim were persisted
    together. A stage-budget-only commit is insufficient. All possible configured
    alternate routes are part of the manifest. No per-request push is implemented.
    """

    manifest_sha256: str
    cycle_id: str
    stage: str
    claim_sha: str
    checkpoint_reference: str


@dataclass(frozen=True)
class ExecutionBinding:
    cycle_id: str
    stage: str
    claim_sha: str
    execution_nonce: str
    checkpoint_reference: str


@dataclass
class OperationAttempt:
    kind: Literal["count", "generate"]
    request_index: int
    reserved_at: str
    status: str = "reserved"
    finished_at: str | None = None
    error_class: str | None = None
    exact_count: int | None = None
    response_sha256: str | None = None
    finish_reason: str | None = None
    usage: dict[str, int] = field(default_factory=dict)
    admission: source_admission.RequestAdmission | None = None


@dataclass
class OperationRecord:
    prepared: PreparedReconciliation
    execution: ExecutionBinding | None = None
    attempts: list[OperationAttempt] = field(default_factory=list)
    raw_response: str | None = None


@dataclass(frozen=True)
class OperationResult:
    status: Literal["completed", "pending"]
    response: ReconciliationResponse | None = None
    error_class: str | None = None
    cached: bool = False


def _manifest(
    value: ReconciliationInput,
    source: Source,
    state: BriefState,
    config: Config,
    origin: IntentOrigin,
) -> PreparedReconciliation:
    verify_reconciliation_input(value, source, state)
    if (
        not isinstance(origin.cycle_id, str)
        or re.fullmatch(r"[1-9][0-9]{0,29}", origin.cycle_id) is None
        or origin.stage not in model_budget.STAGES
        or not isinstance(origin.claim_sha, str)
        or re.fullmatch(r"[0-9a-f]{64}", origin.claim_sha) is None
    ):
        raise ValueError("technical_operation_origin")
    routes = _routes(config)
    if not routes:
        raise ValueError("technical_unknown_profile")
    messages = _reconciliation_messages(value, RESPONSE_INSTRUCTION)
    manifest = PreparedReconciliation(
        OPERATION_VERSION,
        value.selection.identity,
        value.input_sha256,
        json.dumps(asdict(value), ensure_ascii=False, sort_keys=True),
        tuple(
            PreparedRequest(route, source_admission.request_sha256(route, messages, TEMPERATURE)) for route in routes
        ),
        origin,
        "",
    )
    return replace(manifest, manifest_sha256=checksum(asdict(manifest)))


def _path(state_dir: Path, identity: str) -> Path:
    if re.fullmatch(r"[0-9a-f]{32}", identity) is None:
        raise ValueError("technical_operation_identity")
    path = state_dir / "article_reconciliation" / f"{identity}.json"
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError("technical_operation_path")
    return path


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    # Nonblocking across await: concurrent callers hold rather than dispatch twice.
    lock = path.with_suffix(".lock")
    if lock.is_symlink():
        raise ValueError("technical_operation_path")
    with lock.open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("technical_operation_active") from exc
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def _save(path: Path, record: OperationRecord) -> None:
    if path.with_suffix(".json.tmp").is_symlink():
        raise ValueError("technical_operation_path")
    data = asdict(record)
    atomic_json_write(path, {"record": data, "sha256": checksum(data)})


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("technical_operation_integrity")
        result[key] = value
    return result


def _load(
    path: Path,
    value: ReconciliationInput,
    source: Source,
    state: BriefState,
    config: Config,
) -> OperationRecord:
    data = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
    if not isinstance(data, dict) or set(data) != {"record", "sha256"} or data["sha256"] != checksum(data["record"]):
        raise ValueError("technical_operation_integrity")
    raw = data["record"]
    if not isinstance(raw, dict) or set(raw) != {"prepared", "execution", "attempts", "raw_response"}:
        raise ValueError("technical_operation_integrity")
    origin = IntentOrigin(**raw["prepared"]["origin"])
    expected = _manifest(value, source, state, config, origin)
    if raw["prepared"] != json.loads(json.dumps(asdict(expected))):
        raise ValueError("technical_operation_binding")
    execution = ExecutionBinding(**raw["execution"]) if raw["execution"] is not None else None
    if execution is not None and (
        IntentOrigin(execution.cycle_id, execution.stage, execution.claim_sha) != origin
        or not isinstance(execution.execution_nonce, str)
        or re.fullmatch(r"[0-9a-f]{32}", execution.execution_nonce) is None
        or not isinstance(execution.checkpoint_reference, str)
        or not execution.checkpoint_reference.strip()
    ):
        raise ValueError("technical_operation_integrity")
    if not isinstance(raw["attempts"], list) or len(raw["attempts"]) > 2 * len(expected.requests):
        raise ValueError("technical_operation_integrity")
    attempts: list[OperationAttempt] = []
    for item in raw["attempts"]:
        admission = source_admission.RequestAdmission(**item["admission"]) if item["admission"] is not None else None
        attempt = OperationAttempt(**{**item, "admission": admission})
        if (
            attempt.kind not in {"count", "generate"}
            or attempt.status not in _OUTCOMES
            or type(attempt.request_index) is not int
            or not 0 <= attempt.request_index < len(expected.requests)
            or not isinstance(attempt.usage, dict)
            or any(
                key not in {"prompt_tokens", "completion_tokens", "total_tokens"} or type(count) is not int or count < 0
                for key, count in attempt.usage.items()
            )
        ):
            raise ValueError("technical_operation_integrity")
        if any(old.kind == attempt.kind and old.request_index == attempt.request_index for old in attempts):
            raise ValueError("technical_operation_integrity")
        _validate_attempt(attempt, attempts, expected)
        attempts.append(attempt)
    if attempts and execution is None:
        raise ValueError("technical_operation_integrity")
    completed = any(item.kind == "generate" and item.status == "completed" for item in attempts)
    if (completed and not isinstance(raw["raw_response"], str)) or (not completed and raw["raw_response"] is not None):
        raise ValueError("technical_operation_integrity")
    return OperationRecord(expected, execution, attempts, raw["raw_response"])


def _validate_attempt(
    attempt: OperationAttempt,
    previous: list[OperationAttempt],
    prepared: PreparedReconciliation,
) -> None:
    """Reject contradictory local evidence even if its outer checksum was rebuilt."""
    route = prepared.requests[attempt.request_index].route
    timestamp = datetime.fromisoformat(attempt.reserved_at)
    if timestamp.tzinfo is None or any(
        item.kind == "generate" and item.status != "definite_failed" for item in previous
    ):
        raise ValueError("technical_operation_integrity")
    if attempt.status == "reserved":
        if attempt.finished_at is not None or attempt.error_class is not None:
            raise ValueError("technical_operation_integrity")
    elif attempt.finished_at is None or datetime.fromisoformat(attempt.finished_at) < timestamp:
        raise ValueError("technical_operation_integrity")
    if attempt.status in {"completed", "accepted"} and attempt.error_class is not None:
        raise ValueError("technical_operation_integrity")
    if attempt.kind == "count":
        if (
            route.provider != "gemini"
            or attempt.status == "accepted"
            or attempt.admission is not None
            or attempt.response_sha256 is not None
            or attempt.finish_reason is not None
            or attempt.usage
        ):
            raise ValueError("technical_operation_integrity")
        if attempt.status == "completed":
            if type(attempt.exact_count) is not int or attempt.exact_count <= 0:
                raise ValueError("technical_operation_integrity")
        elif attempt.exact_count is not None:
            raise ValueError("technical_operation_integrity")
        return
    admission = attempt.admission
    if (
        attempt.exact_count is not None
        or admission is None
        or admission.status != "admitted"
        or admission.error_class is not None
        or admission.provider != route.provider
        or admission.model != route.model
        or admission.request_sha256 != prepared.requests[attempt.request_index].request_sha256
        or admission.output_reserve != route.max_output_tokens
        or admission.input_limit != route.input_tokens
    ):
        raise ValueError("technical_operation_integrity")
    if route.provider == "gemini":
        count = next(
            (item for item in previous if item.kind == "count" and item.request_index == attempt.request_index), None
        )
        if count is None or count.status != "completed" or admission.exact_count != count.exact_count:
            raise ValueError("technical_operation_integrity")
    if attempt.status in {"accepted", "accepted_invalid", "completed"}:
        invalid_transport = (
            attempt.status == "accepted_invalid"
            and attempt.error_class == "technical_invalid_transport_output"
            and attempt.response_sha256 is None
        )
        if not invalid_transport and (
            not isinstance(attempt.response_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", attempt.response_sha256) is None
        ):
            raise ValueError("technical_operation_integrity")
    elif attempt.response_sha256 is not None or attempt.finish_reason is not None or attempt.usage:
        raise ValueError("technical_operation_integrity")


def prepare_reconciliation_operation(
    value: ReconciliationInput,
    source: Source,
    state: BriefState,
    config: Config,
    state_dir: Path,
    *,
    claim: model_budget.StageClaim,
) -> PreparedReconciliation:
    """Create one local article record; never replace an existing operation.

    This only prepares data for a future verified remote checkpoint. It cannot
    authorize execution or establish that an absent record was never dispatched.
    The runtime must reconcile remotely retained article history before using it.
    Replacement, missing-runner recovery and next-cycle policy remain undecided.
    """
    prepared = _manifest(value, source, state, config, IntentOrigin(claim.cycle_id, claim.stage, claim.claim_sha))
    path = _path(state_dir, prepared.article_identity)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _locked(path):
        if path.exists():
            raise ValueError("technical_operation_exists")
        _save(path, OperationRecord(prepared))
    return prepared


def _execution(checkpoint: ExactIntentCheckpoint | None, prepared: PreparedReconciliation) -> ExecutionBinding:
    snapshot = model_budget.execution_from_env()
    if snapshot is None:
        raise model_budget.ModelBudgetError("Reconciliation requires an existing active shared execution.")
    stage = snapshot.stages[-1]
    if prepared.origin != IntentOrigin(snapshot.cycle_id, stage.stage, stage.claim_sha):
        raise ValueError("technical_checkpoint_transition_required")
    if (
        checkpoint is None
        or not isinstance(checkpoint, ExactIntentCheckpoint)
        or checkpoint.manifest_sha256 != prepared.manifest_sha256
        or checkpoint.cycle_id != snapshot.cycle_id
        or checkpoint.stage != stage.stage
        or checkpoint.claim_sha != stage.claim_sha
        or not isinstance(checkpoint.checkpoint_reference, str)
        or not checkpoint.checkpoint_reference.strip()
    ):
        raise ValueError("technical_intent_checkpoint")
    assert stage.execution_nonce is not None  # execution_from_env validates the original journal.
    return ExecutionBinding(
        snapshot.cycle_id, stage.stage, stage.claim_sha, stage.execution_nonce, checkpoint.checkpoint_reference
    )


def _completion(record: OperationRecord, attempt: OperationAttempt) -> ReconciliationCompletion:
    request = record.prepared.requests[attempt.request_index]
    return ReconciliationCompletion(
        RESPONSE_VERSION,
        record.prepared.input_sha256,
        request.route.provider,
        request.route.model,
        TEMPERATURE,
        request.route.max_output_tokens,
        request.request_sha256,
        attempt.response_sha256 or "",
        "completed",
        attempt.finish_reason,
    )


def _parse(
    record: OperationRecord,
    attempt: OperationAttempt,
    value: ReconciliationInput,
    source: Source,
    state: BriefState,
) -> ReconciliationResponse:
    if attempt.admission is None or record.raw_response is None:
        raise ValueError("technical_operation_integrity")
    return parse_reconciliation_response(
        record.raw_response, value, source, state, admission=attempt.admission, completion=_completion(record, attempt)
    )


def _reserve(
    path: Path,
    record: OperationRecord,
    kind: Literal["count", "generate"],
    index: int,
    admission: source_admission.RequestAdmission | None = None,
) -> OperationAttempt:
    attempt = OperationAttempt(kind, index, now(), admission=admission)
    record.attempts.append(attempt)
    _save(path, record)  # Local intent precedes adapter invocation, but is not a POST receipt.
    return attempt


def _failure(path: Path, record: OperationRecord, attempt: OperationAttempt, exc: Exception) -> None:
    definite = (
        _can_fallback(exc)
        or isinstance(exc, model_budget.ModelBudgetError)
        or isinstance(exc, RuntimeError)
        and str(exc) == "LLM request budget exhausted"
    )
    attempt.status = "definite_failed" if definite else "unknown"
    uncertainty = "technical_count_unknown" if attempt.kind == "count" else "technical_generation_unknown"
    attempt.error_class = "technical_request_rejected" if definite else uncertainty
    attempt.finished_at = now()
    _save(path, record)


def _previous(record: OperationRecord, kind: str, index: int) -> OperationAttempt | None:
    return next((item for item in record.attempts if item.kind == kind and item.request_index == index), None)


async def _admit(
    path: Path,
    record: OperationRecord,
    index: int,
    config: Config,
    messages: list[dict[str, str]],
    deadline: float,
) -> source_admission.RequestAdmission:
    request = record.prepared.requests[index]
    route = request.route
    admission = source_admission.RequestAdmission(
        route.provider, route.model, request.request_sha256, route.max_output_tokens, route.input_tokens
    )
    remaining = llm.request_budget_remaining(config)
    previous = _previous(record, "count", index)
    required = 2 if route.provider == "gemini" and previous is None else 1
    if remaining is None or remaining < required:
        raise model_budget.ModelBudgetError("Reconciliation holds capacity for counting and generation.")
    if route.provider == "groq":
        evidence = source_admission.estimate_request(route, messages)
        count = int(evidence["input_estimate"])
        admission = replace(admission, method="estimated", input_estimate=count, evidence=evidence)
    else:
        if previous is not None:
            if previous.status != "completed" or type(previous.exact_count) is not int or previous.exact_count <= 0:
                raise ValueError(previous.error_class or "technical_count_unknown")
            count = previous.exact_count
        else:
            wait = llm.request_wait_seconds(config)
            if time.monotonic() + wait + source_admission.COUNT_SECONDS >= deadline:
                raise TimeoutError("technical_deadline")
            attempt = _reserve(path, record, "count", index)
            try:
                async with asyncio.timeout(
                    min(wait + source_admission.COUNT_SECONDS, max(0, deadline - time.monotonic()))
                ):
                    count = await llm.count_gemini_tokens(
                        messages,
                        config,
                        provider_override=ProviderConfig(route.provider, route.model),
                        temperature=TEMPERATURE,
                        max_output_tokens=route.max_output_tokens,
                    )
            except (OSError, RuntimeError, ValueError, TypeError, KeyError, TimeoutError, httpx.HTTPError) as exc:
                _failure(path, record, attempt, exc)
                raise
            attempt.finished_at = now()
            if type(count) is not int or count <= 0:
                attempt.status = "accepted_invalid"
                attempt.error_class = "technical_count_unknown"
                _save(path, record)
                raise ValueError("technical_count_unknown")
            attempt.status = "completed"
            attempt.exact_count = count
            _save(path, record)
        admission = replace(admission, method="exact", exact_count=count)
    if count > route.input_tokens:
        raise ValueError("technical_admission_capacity")
    return replace(admission, status="admitted")


async def _generate(
    path: Path,
    record: OperationRecord,
    index: int,
    config: Config,
    messages: list[dict[str, str]],
    deadline: float,
    admission: source_admission.RequestAdmission,
    value: ReconciliationInput,
    source: Source,
    state: BriefState,
) -> ReconciliationResponse:
    request = record.prepared.requests[index]
    route = request.route
    if source_admission.request_sha256(route, messages, TEMPERATURE) != request.request_sha256:
        raise ValueError("technical_operation_binding")
    timeout = _generation_timeout(config, deadline)
    attempt = _reserve(path, record, "generate", index, admission)
    try:
        async with asyncio.timeout(max(0, deadline - time.monotonic())):
            text, usage = await llm.complete(
                llm.LLMRole.SUMMARIZE,
                messages,
                config,
                provider_override=ProviderConfig(route.provider, route.model),
                temperature=TEMPERATURE,
                max_output_tokens=route.max_output_tokens,
                request_timeout_seconds=timeout,
            )
    except (OSError, RuntimeError, ValueError, TypeError, KeyError, TimeoutError, httpx.HTTPError) as exc:
        _failure(path, record, attempt, exc)
        raise
    attempt.status = "accepted"
    attempt.finished_at = now()
    if not isinstance(text, str) or not isinstance(usage, dict):
        attempt.status = "accepted_invalid"
        attempt.error_class = "technical_invalid_transport_output"
        _save(path, record)
        raise ValueError("technical_invalid_transport_output")
    attempt.response_sha256 = hashlib.sha256(text.encode()).hexdigest()
    ending = usage.get("finish_reason")
    attempt.finish_reason = ending if isinstance(ending, str) and ending in _FINISH_REASONS else None
    attempt.usage = {
        key: usage[key]
        for key in ("prompt_tokens", "completion_tokens", "total_tokens")
        if type(usage.get(key)) is int and usage[key] >= 0
    }
    _save(path, record)  # A crash or invalid output cannot buy another generation.
    record.raw_response = text
    try:
        result = _parse(record, attempt, value, source, state)
    except (ValueError, TypeError, KeyError):
        attempt.status = "accepted_invalid"
        attempt.error_class = "technical_invalid_output"
        record.raw_response = None
        _save(path, record)
        raise ValueError("technical_invalid_output") from None
    attempt.status = "completed"
    _save(path, record)
    return result


async def _advance(
    path: Path,
    record: OperationRecord,
    config: Config,
    deadline: float,
    value: ReconciliationInput,
    source: Source,
    state: BriefState,
) -> OperationResult:
    # Shallow copies retain the initialized shared request runtime, without changing
    # retry or pacing policy for callers of other stages.
    llm.request_budget_remaining(config)
    call_config = copy.copy(config)
    call_config.llm = copy.copy(config.llm)
    call_config.llm.max_retries = 0
    messages = _reconciliation_messages(value, RESPONSE_INSTRUCTION)
    error = "technical_request_rejected"
    for index, request in enumerate(record.prepared.requests):
        if _previous(record, "generate", index) is not None:
            continue  # Definite failures also never retry this exact request in this operation.
        call_config.llm.min_request_interval_seconds = source_admission.request_interval(
            request.route.provider,
            config.llm.min_request_interval_seconds,
        )
        try:
            admission = await _admit(path, record, index, call_config, messages, deadline)
            response = await _generate(
                path, record, index, call_config, messages, deadline, admission, value, source, state
            )
            return OperationResult("completed", response)
        except model_budget.ModelBudgetError:
            return OperationResult("pending", error_class="technical_request_budget")
        except TimeoutError:
            generation = _previous(record, "generate", index)
            if generation is not None:
                return OperationResult("pending", error_class=generation.error_class or "technical_generation_unknown")
            count = _previous(record, "count", index)
            if count is None or count.status == "completed" or time.monotonic() >= deadline:
                return OperationResult("pending", error_class="technical_deadline")
            error = count.error_class or "technical_count_unknown"
        except TokenProfileUnavailable:
            error = "technical_tokenizer_profile"
        except (RuntimeError, ValueError, TypeError, KeyError, httpx.HTTPError) as exc:
            generation = _previous(record, "generate", index)
            if generation is not None and generation.status != "definite_failed":
                return OperationResult("pending", error_class=generation.error_class or "technical_generation_unknown")
            if generation is not None:
                error = generation.error_class or "technical_request_rejected"
            else:
                safe = {
                    "technical_count_unknown",
                    "technical_request_rejected",
                    "technical_admission_capacity",
                    "technical_operation_binding",
                }
                error = str(exc) if isinstance(exc, ValueError) and str(exc) in safe else "technical_count_unknown"
            # Only uncertain counting/local admission or known explicit generation
            # rejection reaches an alternate already frozen in the checkpoint.
    return OperationResult("pending", error_class=error)


async def execute_reconciliation_operation(
    value: ReconciliationInput,
    source: Source,
    state: BriefState,
    config: Config,
    state_dir: Path,
    *,
    deadline: float,
    checkpoint: ExactIntentCheckpoint | None,
) -> OperationResult:
    """Execute a prepared operation locally, without runtime/publication wiring.

    Exact completed cache is reusable without model allowance or a checkpoint.
    Uncached work requires the trusted remote-intent assertion AND current active
    shared-budget execution. Pending work cannot cross execution/cycle boundaries.
    Missing/corrupt state holds. This is not a runner-loss recovery protocol.
    """
    try:
        path = _path(state_dir, value.selection.identity)
        if not path.is_file():
            return OperationResult("pending", error_class="technical_operation_missing")
        with _locked(path):
            record = _load(path, value, source, state, config)
            prepared = record.prepared
            completed = [item for item in record.attempts if item.kind == "generate" and item.status == "completed"]
            if completed:
                if len(completed) != 1:
                    raise ValueError("technical_operation_integrity")
                return OperationResult("completed", _parse(record, completed[0], value, source, state), cached=True)
            if any(item.kind == "generate" and item.status != "definite_failed" for item in record.attempts):
                return OperationResult("pending", error_class="technical_generation_held")
            if (
                not isinstance(deadline, (int, float))
                or isinstance(deadline, bool)
                or not math.isfinite(deadline)
                or time.monotonic() >= deadline
            ):
                return OperationResult("pending", error_class="technical_deadline")
            execution = _execution(checkpoint, prepared)
            if record.execution is not None and record.execution != execution:
                return OperationResult("pending", error_class="technical_checkpoint_transition_required")
            record.execution = execution
            _save(path, record)
            return await _advance(path, record, config, deadline, value, source, state)
    except model_budget.ModelBudgetError:
        return OperationResult("pending", error_class="technical_request_budget")
    except (OSError, ValueError, TypeError, KeyError) as exc:
        known = {
            "technical_intent_checkpoint",
            "technical_operation_binding",
            "technical_operation_active",
            "technical_operation_path",
            "technical_unknown_profile",
            "technical_checkpoint_transition_required",
        }
        error = str(exc) if isinstance(exc, ValueError) and str(exc) in known else "technical_operation_integrity"
        return OperationResult("pending", error_class=error)
