"""Local bare-Git and fake-transport proofs of the connected prepare-stage barrier."""

from __future__ import annotations

import asyncio
import copy
import json
import subprocess
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import yaml

from digest import llm, model_budget
from digest import reconciliation_checkpoint as checkpoint
from digest.candidate_review import CandidateProgress
from digest.config import Config, ProviderConfig
from digest.reading_preparation import prepare_selected_sources, setup_reading_budget
from tests.test_candidate_review import NOW
from tests.test_main_reading_brief import generate, saved_selection
from tests.test_reading_brief import fetched


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], stderr=subprocess.PIPE).decode().strip()


@dataclass
class Runtime:
    root: Path
    remote: Path
    config: Config
    progress: CandidateProgress
    execution: model_budget.StageExecution
    calls: list[httpx.Request]

    async def prepare(self, seconds: float = 1000) -> Path | None:
        return await checkpoint.prepare_batch(self.progress, self.config, "config.yaml", time.monotonic() + seconds)

    def persist(self) -> None:
        git(self.root, "add", ".cache")
        git(self.root, "commit", "-m", "persist local outcomes")
        git(self.root, "push", "origin", "HEAD:main")


def bind(monkeypatch: pytest.MonkeyPatch, execution: model_budget.StageExecution) -> None:
    values = {
        "REQUIRED": "1",
        "CYCLE": execution.cycle_id,
        "STAGE": "prepare",
        "CLAIM_SHA": execution.claim_sha,
        "ATTEMPT": "1",
        "EXECUTION_NONCE": execution.execution_nonce,
    }
    for key, value in values.items():
        monkeypatch.setenv("DIGEST_MODEL_BUDGET_" + key, value)
    monkeypatch.setenv("GITHUB_RUN_ID", execution.cycle_id)
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")


@pytest.fixture
async def runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Runtime:
    root, remote = tmp_path / "runtime", tmp_path / "remote.git"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.email", "test@example.invalid")
    git(root, "config", "user.name", "Offline test")
    git(root, "init", "--bare", str(remote))
    git(root, "remote", "add", "origin", str(remote))
    monkeypatch.chdir(root)
    monkeypatch.setenv("GEMINI_API_KEY", "fake-test-key")
    monkeypatch.setenv("DIGEST_RECONCILIATION_CHECKPOINT_REQUIRED", "1")
    config, progress, packet, report = await saved_selection(Path(".cache"), all_selected=True)
    config.reading_brief = replace(config.reading_brief, max_requests_per_run=6)
    config.radar.language = "en"
    config.review.review_led_only = True
    config.llm.min_request_interval_seconds = 0
    config.llm.providers = [ProviderConfig("gemini", "gemini-3.8-flash", ["summarize"])]
    Path("config.yaml").write_text(yaml.safe_dump(asdict(fresh(config))))
    claim = model_budget.reserve_stage("123456789", "prepare", run_attempt=1)
    git(root, "add", "config.yaml", ".cache")
    git(root, "commit", "-m", "initial claim")
    git(root, "push", "origin", "HEAD:main")
    execution = model_budget.begin_stage(claim.cycle_id, "prepare", claim.claim_sha, run_attempt=1)
    bind(monkeypatch, execution)
    setup_reading_budget(config)
    with (
        patch(
            "digest.reading_brief.fetch_article",
            AsyncMock(return_value=fetched("Complete source finding.\n\nQUALIFICATION: only the selected pilot.")),
        ),
        patch("digest.llm.count_gemini_tokens", AsyncMock(return_value=100)),
        patch("digest.llm.complete", side_effect=generate),
    ):
        result = await prepare_selected_sources(
            progress, packet, report, config, Path(".cache"), time.monotonic() + 1000
        )
    assert result.technical_complete == 2
    # Charge the preceding RSS/source work through the real shared reservation point.
    for _ in range(2):
        llm._reserve_request(llm._request_state(config), "gemini", "gemini-3.8-flash", "generate")
    calls: list[httpx.Request] = []

    def transport(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path.endswith(":countTokens"):
            return httpx.Response(200, json={"totalTokens": 200})
        body = json.loads(request.content)
        value = json.loads(body["contents"][0]["parts"][0]["text"])
        # The exact reconciliation message carries its sparse evidence envelope.
        ids = [item["id"] for item in value["evidence"]]
        qualifications = sorted({span for page in value["pages"] for span in page["qualification_span_ids"]})
        raw = json.dumps(
            {
                "selected_span_ids": ids[:1],
                "qualification_span_ids": qualifications,
                "reading_angle": {"text": "The finding applies only to the pilot.", "span_ids": ids},
                "abstain": False,
            }
        )
        return httpx.Response(
            200, json={"candidates": [{"content": {"parts": [{"text": raw}]}, "finishReason": "STOP"}]}
        )

    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *args, **kwargs: original(*args, **kwargs, transport=httpx.MockTransport(transport), trust_env=False),
    )
    return Runtime(root, remote, config, progress, execution, calls)


def fresh(config: Config) -> Config:
    result = copy.copy(config)
    result.llm = copy.copy(config.llm)
    result.llm._runtime = None
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize("new_process", [False, True])
async def test_batched_checkpoint_execute_finalize_persist_and_cache(runtime: Runtime, new_process: bool) -> None:
    path = await runtime.prepare()
    assert path is not None
    initial = git(runtime.root, "rev-list", "--count", "main")
    results = await checkpoint.checkpoint_and_execute(path, fresh(runtime.config) if new_process else runtime.config)
    assert [result.status for result in results] == ["completed", "completed"]
    assert len(runtime.calls) == 4
    assert int(git(runtime.root, "rev-list", "--count", "main")) == int(initial) + 1
    assert model_budget.inspect_budget(runtime.execution.cycle_id).reserved_count == 6
    assert not Path(".cache/prepared_edition.json").exists()
    await checkpoint.finalize_batch(path, runtime.config)
    runtime.persist()
    assert model_budget.inspect_budget(runtime.execution.cycle_id).active_stage is None
    outcome = checkpoint._json(Path(".cache/reconciliation_outcomes/123456789.json"))
    assert outcome["unstarted"] == {}
    # Final persistence binds changed operation bytes; initial pre-dispatch hashes are not reused.
    from digest import reconciliation_operation as operation

    for item in checkpoint._load_batch(path).items:
        value, source, state = checkpoint._input(item.handoff, Path(".cache"))
        result = await operation.execute_reconciliation_operation(
            value, source, state, runtime.config, Path(".cache"), checkpoint=None, deadline=0
        )
        assert result.cached and result.status == "completed"
    assert len(runtime.calls) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        "intent",
        "source",
        "nonce",
        "protocol",
        "journal",
        "head",
        "remote",
        "source_state",
        "seen_articles",
    ],
)
async def test_changed_checkpoint_holds_before_http(
    runtime: Runtime,
    change: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = await runtime.prepare()
    assert path is not None
    batch = checkpoint._load_batch(path)
    if change in {"intent", "source"}:
        reference = next(
            item
            for item in batch.files
            if (
                "article_reconciliation/" in item.path if change == "intent" else "reading_briefs/sources/" in item.path
            )
        )
        Path(reference.path).write_bytes(Path(reference.path).read_bytes() + b" ")
    elif change in {"source_state", "seen_articles"}:
        reference = next(item for item in batch.files if item.path == f".cache/{change}.json")
        assert reference.sha256 is None
        Path(reference.path).write_text("{}")
    elif change == "nonce":
        monkeypatch.setenv("DIGEST_MODEL_BUDGET_EXECUTION_NONCE", "0" * 32)
    elif change == "protocol":
        raw = checkpoint._json(path)
        raw["batch"]["protocol"] = 999
        raw["sha256"] = checkpoint.checksum(raw["batch"])
        path.write_text(json.dumps(raw))
    elif change == "journal":
        Path(batch.local_budget.path).unlink()
    else:
        Path("unrelated.txt").write_text("other work")
        git(runtime.root, "add", "unrelated.txt")
        git(runtime.root, "commit", "-m", "unrelated")
        if change == "remote":
            git(runtime.root, "push", "origin", "HEAD:main")
    with pytest.raises((ValueError, OSError)):
        await checkpoint.checkpoint_and_execute(path, fresh(runtime.config))
    assert not runtime.calls


@pytest.mark.asyncio
async def test_fresh_checkout_cannot_execute_or_finalize(runtime: Runtime, monkeypatch: pytest.MonkeyPatch) -> None:
    path = await runtime.prepare()
    assert path is not None
    # Publish the exact intent/source batch, excluding the active local journal.
    batch = checkpoint._load_batch(path)
    git(runtime.root, "add", str(path), *(item.path for item in batch.files if item.sha256 is not None))
    git(runtime.root, "commit", "-m", "intent checkpoint without active journal")
    git(runtime.root, "push", "origin", "HEAD:main")
    clone = runtime.root.parent / "clone"
    git(runtime.root, "clone", "-b", "main", str(runtime.remote), str(clone))
    monkeypatch.chdir(clone)
    for action in (checkpoint.checkpoint_and_execute, checkpoint.finalize_batch):
        with pytest.raises((ValueError, OSError)):
            await action(path, fresh(runtime.config))
    assert model_budget.inspect_budget(runtime.execution.cycle_id).active_stage == "prepare"
    from digest import reconciliation_operation as operation

    for item in batch.items:
        value, source, state = checkpoint._input(item.handoff, Path(".cache"))
        proof = operation.ExactIntentCheckpoint(
            item.manifest_sha256, batch.cycle_id, "prepare", batch.claim_sha, git(clone, "rev-parse", "HEAD")
        )
        result = await operation.execute_reconciliation_operation(
            value,
            source,
            state,
            runtime.config,
            Path(".cache"),
            deadline=time.monotonic() + 1000,
            checkpoint=proof,
        )
        assert result.error_class == "technical_request_budget"
    with pytest.raises(model_budget.ModelBudgetError):
        model_budget.finalize_stage(
            batch.cycle_id, "prepare", batch.claim_sha, run_attempt=1, execution_nonce=batch.execution_nonce
        )
    assert not runtime.calls


@pytest.mark.asyncio
async def test_failed_push_holds_and_proved_unstarted_can_rebind(
    runtime: Runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = await runtime.prepare()
    assert path is not None
    hook = runtime.remote / "hooks/pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    with pytest.raises(ValueError, match="checkpoint_git"):
        await checkpoint.checkpoint_and_execute(path, fresh(runtime.config))
    assert not runtime.calls
    await checkpoint.finalize_batch(path, runtime.config)
    hook.unlink()
    runtime.persist()
    claim = model_budget.reserve_stage("123456790", "prepare", run_attempt=1)
    runtime.persist()
    execution = model_budget.begin_stage(claim.cycle_id, "prepare", claim.claim_sha, run_attempt=1)
    bind(monkeypatch, execution)
    later = fresh(runtime.config)
    setup_reading_budget(later)
    new_path = await checkpoint.prepare_batch(runtime.progress, later, "config.yaml", time.monotonic() + 1000)
    assert new_path is not None
    assert len(list(Path(".cache/article_reconciliation/revisions").glob("*.json"))) == 2
    results = await checkpoint.checkpoint_and_execute(new_path, fresh(later))
    assert all(result.status == "completed" for result in results)
    assert len(runtime.calls) == 4


@pytest.mark.asyncio
async def test_small_cap_and_pacing_survive_fresh_runtime(runtime: Runtime, monkeypatch: pytest.MonkeyPatch) -> None:
    llm.set_request_limit(runtime.config, 3)
    llm._request_state(runtime.config).next_request_at = time.monotonic() + 65
    path = await runtime.prepare()
    assert path is not None
    batch = checkpoint._load_batch(path)
    assert batch.remaining == 1 and batch.not_before_unix > time.time() + 60
    restored = fresh(runtime.config)
    # One Gemini slot cannot buy count+generate; this must hold without HTTP.
    results = await checkpoint.checkpoint_and_execute(path, restored)
    assert all(result.status == "pending" for result in results)
    assert not runtime.calls and llm.request_budget_remaining(restored) == 1
    assert llm.request_wait_seconds(restored) > 60


@pytest.mark.asyncio
async def test_disabled_or_exhausted_window_creates_no_intent(
    runtime: Runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert await runtime.prepare(seconds=30) is None
    monkeypatch.delenv("DIGEST_RECONCILIATION_CHECKPOINT_REQUIRED")
    assert await runtime.prepare() is None
    assert not Path(".cache/article_reconciliation").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("delete_remote", [False, True])
async def test_missing_local_retained_operation_is_never_recreated(runtime: Runtime, delete_remote: bool) -> None:
    path = await runtime.prepare()
    assert path is not None
    batch = checkpoint._load_batch(path)
    git(runtime.root, "add", str(path), *(item.path for item in batch.files if item.sha256 is not None))
    git(runtime.root, "commit", "-m", "retain intents without the active journal")
    git(runtime.root, "push", "origin", "HEAD:main")
    for item in checkpoint._load_batch(path).items:
        Path(f".cache/article_reconciliation/{item.identity}.json").unlink()
    if delete_remote:
        git(runtime.root, "add", "-u", ".cache/article_reconciliation")
        git(runtime.root, "commit", "-m", "remove retained intents from the remote tip")
        git(runtime.root, "push", "origin", "HEAD:main")
        assert not git(runtime.root, "ls-tree", "origin/main", ".cache/article_reconciliation")
        assert git(runtime.root, "log", "-1", "--format=%H", "origin/main", "--", ".cache/article_reconciliation")
    assert await runtime.prepare() is None
    assert not list(Path(".cache/article_reconciliation").glob("*.json"))
    assert not runtime.calls


@pytest.mark.asyncio
async def test_partial_batch_rebinds_only_proved_unstarted_peer(
    runtime: Runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = await runtime.prepare()
    assert path is not None
    from digest import reconciliation_operation as operation

    original = operation.execute_reconciliation_operation
    calls = 0

    async def elapsed(*args: Any, **kwargs: Any) -> operation.OperationResult:
        nonlocal calls
        calls += 1
        if calls > 1:
            kwargs["deadline"] = 0
        return await original(*args, **kwargs)

    with patch.object(operation, "execute_reconciliation_operation", elapsed):
        results = await checkpoint.checkpoint_and_execute(path, fresh(runtime.config))
    assert [result.status for result in results] == ["completed", "pending"]
    assert len(runtime.calls) == 2
    await checkpoint.finalize_batch(path, runtime.config)
    runtime.persist()
    outcome = checkpoint._json(Path(".cache/reconciliation_outcomes/123456789.json"))
    assert len(outcome["unstarted"]) == 1
    claim = model_budget.reserve_stage("123456790", "prepare", run_attempt=1)
    runtime.persist()
    execution = model_budget.begin_stage(claim.cycle_id, "prepare", claim.claim_sha, run_attempt=1)
    bind(monkeypatch, execution)
    later = fresh(runtime.config)
    setup_reading_budget(later)
    next_batch = await checkpoint.prepare_batch(runtime.progress, later, "config.yaml", time.monotonic() + 1000)
    assert next_batch is not None and len(checkpoint._load_batch(next_batch).items) == 1
    assert checkpoint._load_batch(next_batch).items[0].identity == next(iter(outcome["unstarted"]))
    old_spent = model_budget.inspect_budget("123456789").reserved_count
    assert old_spent == 4  # Two preceding source reservations plus A's count/generation.
    assert (await checkpoint.checkpoint_and_execute(next_batch, fresh(later)))[0].status == "completed"
    assert len(runtime.calls) == 4
    assert model_budget.inspect_budget("123456789").reserved_count == old_spent
    assert model_budget.inspect_budget("123456790").reserved_count == 2  # Only B's count/generation.


@pytest.mark.asyncio
async def test_actual_fresh_python_process_uses_shared_budget(runtime: Runtime) -> None:
    import os
    import sys

    path = await runtime.prepare()
    assert path is not None
    script = """
import asyncio, json, sys
from pathlib import Path
import httpx
from digest.config import load_config
from digest import reconciliation_checkpoint as checkpoint
from digest import model_budget
calls = []
def transport(request):
    calls.append(str(request.url))
    if request.url.path.endswith(":countTokens"):
        return httpx.Response(200, json={"totalTokens": 200})
    data = json.loads(json.loads(request.content)["contents"][0]["parts"][0]["text"])
    ids = [span["id"] for span in data["evidence"]]
    quals = sorted({span for page in data["pages"] for span in page["qualification_span_ids"]})
    text = json.dumps({"selected_span_ids": ids[:1], "qualification_span_ids": quals,
        "reading_angle": {"text": "Only the pilot is covered.", "span_ids": ids}, "abstain": False})
    return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": "STOP"}]})
original = httpx.AsyncClient
httpx.AsyncClient = lambda *a, **kw: original(*a, **kw, transport=httpx.MockTransport(transport), trust_env=False)
results = asyncio.run(checkpoint.checkpoint_and_execute(Path(sys.argv[1]), load_config("config.yaml")))
assert all(result.status == "completed" for result in results), results
assert len(calls) == 4, calls
assert model_budget.inspect_budget("123456789").reserved_count == 6
"""
    environment = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        script,
        str(path),
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    assert process.returncode == 0, (stdout.decode(), stderr.decode())
    assert not runtime.calls
    assert model_budget.inspect_budget(runtime.execution.cycle_id).reserved_count == 6


@pytest.mark.asyncio
async def test_remote_change_after_push_holds_even_when_checkpoint_reachable(
    runtime: Runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = await runtime.prepare()
    assert path is not None
    original = checkpoint._git
    operation_path = next(
        item.path for item in checkpoint._load_batch(path).files if "article_reconciliation/" in item.path
    )

    async def git_with_race(*args: str, **kwargs: Any) -> bytes:
        result = await original(*args, **kwargs)
        if args[0] == "push":
            clone = runtime.root.parent / "writer"
            git(runtime.root, "clone", "-b", "main", str(runtime.remote), str(clone))
            git(clone, "config", "user.name", "Other writer")
            git(clone, "config", "user.email", "other@example.invalid")
            changed = clone / operation_path
            changed.write_bytes(changed.read_bytes() + b" ")
            git(clone, "add", operation_path)
            git(clone, "commit", "-m", "changed referenced evidence")
            git(clone, "push", "origin", "HEAD:main")
        return result

    monkeypatch.setattr(checkpoint, "_git", git_with_race)
    with pytest.raises(ValueError, match="remote_changed"):
        await checkpoint.checkpoint_and_execute(path, fresh(runtime.config))
    assert not runtime.calls


@pytest.mark.asyncio
async def test_push_succeeded_but_result_unknown_never_dispatches(
    runtime: Runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = await runtime.prepare()
    assert path is not None
    original = checkpoint._git

    async def unknown(*args: str, **kwargs: Any) -> bytes:
        result = await original(*args, **kwargs)
        if args[0] == "push":
            raise TimeoutError("push result lost")
        return result

    monkeypatch.setattr(checkpoint, "_git", unknown)
    with pytest.raises(TimeoutError, match="result lost"):
        await checkpoint.checkpoint_and_execute(path, fresh(runtime.config))
    assert not runtime.calls
    await checkpoint.finalize_batch(path, runtime.config)
    assert model_budget.inspect_budget(runtime.execution.cycle_id).active_stage is None


@pytest.mark.asyncio
async def test_git_cancellation_kills_and_waits_for_process_group(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio
    import signal

    started = asyncio.Event()
    killed: list[tuple[int, int]] = []

    class Child:
        pid = 12345
        returncode = None
        waited = False

        async def communicate(self) -> tuple[bytes, bytes]:
            started.set()
            await asyncio.Event().wait()
            return b"", b""

        async def wait(self) -> None:
            self.waited = True

    child = Child()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=child))
    monkeypatch.setattr(checkpoint.os, "killpg", lambda pid, signal: killed.append((pid, signal)))
    task = asyncio.create_task(checkpoint._git("fetch", deadline=time.monotonic() + 20))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert killed == [(12345, signal.SIGKILL)] and child.waited


@pytest.mark.asyncio
@pytest.mark.parametrize("already_ready", [False, True])
async def test_main_preparation_emits_executable_batch_in_both_reading_branches(
    runtime: Runtime,
    monkeypatch: pytest.MonkeyPatch,
    already_ready: bool,
) -> None:
    import shutil

    from digest.feedback import FeedbackStore
    from digest.main import _run
    from digest.radar.collector import SourceCollectionOutcome

    runtime.config.review.review_led_only = True
    if not already_ready:
        for name in ("reading_briefs", "reading_bindings", "reading_handoffs"):
            shutil.rmtree(Path(".cache") / name)
    output = runtime.root / "step-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))

    async def collect(_config: Any, **kwargs: Any) -> tuple[dict[str, Any], dict[str, str]]:
        kwargs["inventory"].sources = [SourceCollectionOutcome("A", "https://a.example/feed", "tech", 0)]
        return {}, {}

    with (
        patch("digest.candidate_review._instant", return_value=NOW),
        patch("digest.config.load_config", return_value=runtime.config),
        patch("digest.main._collect_run_feedback", AsyncMock(return_value=(FeedbackStore(), True, 0))),
        patch("digest.radar.collect", side_effect=collect),
        patch(
            "digest.reading_brief.fetch_article",
            AsyncMock(return_value=fetched("Complete source finding.\n\nQUALIFICATION: only the selected pilot.")),
        ),
        patch("digest.llm.count_gemini_tokens", AsyncMock(return_value=100)),
        patch("digest.llm.complete", side_effect=generate),
        patch("digest.edition_runtime.finish_preparation", side_effect=AssertionError("No publication")),
    ):
        stats = await _run("config.yaml", False, False, False, prepare_only=True, feedback_precollected=True)
    batch_path = Path(
        next(
            line.split("=", 1)[1]
            for line in output.read_text().splitlines()
            if line.startswith("reconciliation_batch=")
        )
    )
    assert batch_path.is_file() and not stats.telegram_sent
    results = await checkpoint.checkpoint_and_execute(batch_path, fresh(runtime.config))
    assert len(results) == 2 and all(result.status == "completed" for result in results)
    assert len(runtime.calls) == 4


@pytest.mark.asyncio
async def test_quota_stopped_peer_with_no_adapter_intent_can_rebind(
    runtime: Runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm.set_request_limit(runtime.config, 4)
    path = await runtime.prepare()
    assert path is not None
    results = await checkpoint.checkpoint_and_execute(path, fresh(runtime.config))
    assert [result.status for result in results] == ["completed", "pending"]
    assert results[1].error_class == "technical_request_budget" and len(runtime.calls) == 2
    await checkpoint.finalize_batch(path, runtime.config)
    runtime.persist()
    outcome = checkpoint._json(Path(".cache/reconciliation_outcomes/123456789.json"))
    assert len(outcome["unstarted"]) == 1
    claim = model_budget.reserve_stage("123456790", "prepare", run_attempt=1)
    runtime.persist()
    execution = model_budget.begin_stage(claim.cycle_id, "prepare", claim.claim_sha, run_attempt=1)
    bind(monkeypatch, execution)
    later = fresh(runtime.config)
    setup_reading_budget(later)
    new_path = await checkpoint.prepare_batch(runtime.progress, later, "config.yaml", time.monotonic() + 1000)
    assert new_path is not None and len(checkpoint._load_batch(new_path).items) == 1
    assert (await checkpoint.checkpoint_and_execute(new_path, fresh(later)))[0].status == "completed"


@pytest.mark.asyncio
async def test_original_workspace_repairs_outcome_write_after_finalization(
    runtime: Runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = await runtime.prepare()
    assert path is not None
    original = checkpoint.atomic_json_write

    def fail_once(target: Path, data: Any) -> None:
        if "reconciliation_outcomes" in str(target):
            raise OSError("fixture disk interrupted")
        original(target, data)

    with patch.object(checkpoint, "atomic_json_write", fail_once):
        with pytest.raises(OSError, match="disk interrupted"):
            await checkpoint.finalize_batch(path, runtime.config)
    assert model_budget.inspect_budget(runtime.execution.cycle_id).active_stage is None
    await checkpoint.finalize_batch(path, runtime.config)
    await checkpoint.finalize_batch(path, runtime.config)
    outcome = checkpoint._json(Path(".cache/reconciliation_outcomes/123456789.json"))
    assert len(outcome["unstarted"]) == 2
    with pytest.raises(model_budget.ModelBudgetError):
        await checkpoint.checkpoint_and_execute(path, fresh(runtime.config))
    assert not runtime.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "cancel", "batch_write"])
async def test_preparation_failure_restores_uncheckpointed_intents(
    runtime: Runtime,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    original = checkpoint._remote_file
    calls = 0

    async def fail_later(commit: str, path: str, deadline: float) -> bytes | None:
        nonlocal calls
        if "article_reconciliation" in path:
            calls += 1
            if calls == 2:
                if failure == "timeout":
                    raise TimeoutError("fixture history timeout")
                if failure == "cancel":
                    raise asyncio.CancelledError
        return await original(commit, path, deadline)

    monkeypatch.setattr(checkpoint, "_remote_file", fail_later)
    if failure == "batch_write":
        original_write = checkpoint.atomic_json_write

        def failed_write(path: Path, data: Any) -> None:
            if "reconciliation_batches" in str(path):
                raise OSError("fixture batch write")
            original_write(path, data)

        monkeypatch.setattr(checkpoint, "atomic_json_write", failed_write)
    with pytest.raises((OSError, asyncio.CancelledError)):
        await runtime.prepare()
    assert not list(Path(".cache/article_reconciliation").glob("*.json"))
    assert not list(Path(".cache/reconciliation_batches").glob("*.json"))
    assert not runtime.calls
    assert model_budget.inspect_budget(runtime.execution.cycle_id).reserved_count == 2


@pytest.mark.asyncio
async def test_accidentally_staged_active_budget_holds_before_publication(runtime: Runtime) -> None:
    path = await runtime.prepare()
    assert path is not None
    before = git(runtime.root, "rev-parse", "origin/main")
    git(runtime.root, "add", str(runtime.execution.path))
    with pytest.raises(ValueError, match="dirty_index"):
        await checkpoint.checkpoint_and_execute(path, fresh(runtime.config))
    assert git(runtime.root, "rev-parse", "origin/main") == before
    assert not runtime.calls


@pytest.mark.asyncio
async def test_historical_collection_audit_does_not_expand_checkpoint_dependencies(runtime: Runtime) -> None:
    from digest import candidate_storage
    from digest.candidate_review import _digest

    root = Path(".cache")
    frozen = candidate_storage.freeze_packet(runtime.progress.packets[0], {}, root)
    historical = {
        "observations": [{"article_reference": {"occurrence_sha256": f"{index:064x}"}} for index in range(100)]
    }
    body = checkpoint._json(frozen)
    body["summary"]["current_collection"] = historical
    body["sha256"] = candidate_storage.digest({key: value for key, value in body.items() if key != "sha256"})
    frozen.write_text(json.dumps(body))
    working = checkpoint._json(root / "candidate_progress.json")
    working["candidate_accounting"]["latest_collection_json"] = json.dumps(historical)
    working["sha256"] = _digest(working["candidate_accounting"])
    (root / "candidate_progress.json").write_text(json.dumps(working))
    # Missing historical bodies and one corrupt unrelated object are irrelevant.
    (root / "candidate_sources" / ("0" * 64 + ".json")).write_text("corrupt unrelated history")
    path = await runtime.prepare()
    assert path is not None
    references = checkpoint._load_batch(path).files
    assert not any(item.path.endswith("0" * 64 + ".json") for item in references)
    assert all(
        result.status == "completed" for result in await checkpoint.checkpoint_and_execute(path, fresh(runtime.config))
    )


@pytest.mark.asyncio
async def test_corrupt_active_candidate_source_still_holds_checkpoint(runtime: Runtime) -> None:
    raw = checkpoint._json(Path(".cache/candidate_progress.json"))
    reference = next(iter(raw["candidate_accounting"]["candidates"].values()))["article_ref"]
    Path(f".cache/candidate_sources/{reference}.json").write_text("corrupt active evidence")
    with pytest.raises(ValueError):
        await runtime.prepare()
    assert not Path(".cache/article_reconciliation").exists()
    assert not runtime.calls
