"""Native semantic phase fixtures: no provider API, real search or transport calls."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from digest._serialization import canonical_json_bytes
from digest.application import native_irritator as native
from digest.application import supplement
from digest.config import Config
from digest.domain.investigation.signals import Signal
from digest.irritator.documents import DocumentSnapshot, SourceRequestBudget
from tests.test_irritator_research import BODY, LINK, _snapshot
from tests.test_native_editorial import CACHE, NOW
from tests.test_native_editorial import isolated as isolated
from tests.test_native_fragment_destination import _clock
from tests.test_native_investigation_origin import _confirmed

CONFIG = Path("config.yaml")


@pytest.fixture(autouse=True)
def no_api_or_transport(monkeypatch: pytest.MonkeyPatch, isolated: None) -> None:
    def denied(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("Native semantic fixtures must never call a model API or delivery adapter")

    for target in (
        "digest.irritator.evidence_stage.complete", "digest.translation.complete",
        "digest.post_delivery._send_supplement",
    ):
        monkeypatch.setattr(target, denied)


def _setup(
    monkeypatch: pytest.MonkeyPatch, *, presentation: bool = False, translation_limit: int | None = None,
) -> tuple[Config, Path, Path]:
    _clock(monkeypatch, NOW)
    from digest import post_delivery

    monkeypatch.setattr(native, "datetime", post_delivery.datetime)
    cfg, archive = _confirmed()
    cfg.irritator.sources = ["devto"]
    cfg.translation = replace(cfg.translation, enabled=presentation, target_language="ru")
    if translation_limit is not None:
        cfg.translation = replace(cfg.translation, max_input_chars=translation_limit)
    if presentation:
        cfg.radar.language = "en"
    monkeypatch.setattr(native, "load_config", lambda _: cfg)
    session = native.begin(CONFIG, archive)
    assert session is not None
    return cfg, archive, session


async def _advance(session: Path) -> dict[str, Any]:
    """Explicit synthetic persistence handshake; never an implicit acquisition retry."""
    result = await native.advance(CONFIG, session)
    for _ in range(2):
        if result["status"] != "acquisition_pending":
            return result
        intent = native.claim_acquisition(Path(result["request"]), result["request_sha256"])
        # The real controller persists/readbacks this intent before supplying its
        # hash. This offline fixture proves only the local effect-order contract.
        result = await native.advance(CONFIG, session, acquisition_intent_sha256=native._sha(intent.read_bytes()))
    if result["status"] == "acquisition_pending":
        raise AssertionError("Native research exceeded its two declared acquisition phases")
    return result


def _messages(pending: dict[str, Any]) -> dict[str, Any]:
    request = json.loads(Path(pending["request"]).read_bytes())
    return json.loads(request["input"]["messages"][1]["content"])


def _semantic_result(pending: dict[str, Any]) -> dict[str, Any]:
    data = _messages(pending)
    if pending["phase"] == "narrative":
        card = data["delivered_cards"][0]
        item = next(item for item in data["evidence"]["items"] if item["evidence_id"] == card["target_card_id"])
        return {"narratives": [{
            "claim": "A conditional source interpretation.", "category": item["category"],
            "implicit_assumptions": [], "why_worth_challenging": "Check documented conditions.",
            "evidence_ids": [item["evidence_id"]], "quotes": {item["evidence_id"]: item["excerpt"]},
            "target_card_id": card["target_card_id"],
            "delivered_quote": {"field": "summary", "text": card["summary"]},
        }], "limitations": []}
    if pending["phase"] == "research_plan":
        return {
            "question": "Which client authentication policies can the protocol preserve?",
            "evidence_needed": "The protocol's documented authentication requirements.",
            "queries": [{"query": "protocol client authentication", "intent": "Find the specification.",
                         "sources": ["devto"]}],
            "document_ids": [], "read_query_index": 0, "limitations": [],
        }
    if pending["phase"] == "ranking":
        signal = data["signals"][0]
        return {"rankings": [{
            "url": signal["url"], "score": 8, "relation": "complicates",
            "finding": "The specification documents a client authentication condition.",
            "applicability": "If that policy is required, the protocol cannot preserve it.",
            "material_change": "Check the documented requirement before relying on the reported result.",
            "target_quote": data["narrative"]["claim"],
            "evidence_quote_ids": [signal["snippet"][0]["id"]],
        }], "limitations": []}
    assert pending["phase"] == "presentation"
    return {"translations": [{"id": name, "text": "Представление: " + value}
                             for name, value in data["fields"].items()]}


def _response(
    pending: dict[str, Any], result: dict[str, Any] | None = None, *, controller: str = "synthetic-controller/run-1",
) -> tuple[Path, Path]:
    request = Path(pending["request"])
    intent = native.claim_phase(request, pending["request_sha256"], controller)
    value = {
        "schema": "native-irritator-response/v1", "phase": pending["phase"],
        "request_sha256": pending["request_sha256"], "intent_sha256": native._sha(intent.read_bytes()),
        "controller_ref": controller, "result": _semantic_result(pending) if result is None else result,
    }
    incoming = request.with_name(f"{pending['phase']}.incoming.json")
    incoming.write_bytes(canonical_json_bytes(value))
    return incoming, intent


def _accept(pending: dict[str, Any], result: dict[str, Any] | None = None) -> Path:
    incoming, _ = _response(pending, result)
    return native.accept_response(Path(pending["request"]), incoming, pending["request_sha256"])


def _acquisitions(monkeypatch: pytest.MonkeyPatch, *, search_starts: int = 1) -> dict[str, Any]:
    calls: list[tuple[str, int]] = []

    async def original(url: str, *, budget: SourceRequestBudget) -> DocumentSnapshot:
        budget.consume()
        calls.append(("original", budget.requests_started))
        return _snapshot(url=url, text="Original synthetic source context.")

    async def discover(*args: Any, **kwargs: Any) -> list[Signal]:
        for _ in range(search_starts):
            kwargs["before_request"]()
        calls.append(("search", search_starts))
        return [Signal(LINK.url, LINK.title, "Discovery lead, not admissible document evidence.", "devto", "", 0)]

    async def external(url: str, *, budget: SourceRequestBudget) -> DocumentSnapshot:
        assert url == LINK.url
        budget.consume()
        calls.append(("external", budget.requests_started))
        return _snapshot(text=BODY)

    original_mock = AsyncMock(side_effect=original)
    search_mock = AsyncMock(side_effect=discover)
    external_mock = AsyncMock(side_effect=external)
    monkeypatch.setattr(native, "acquire_document", original_mock)
    monkeypatch.setattr("digest.irritator.evidence_stage.search_devto", search_mock)
    monkeypatch.setattr("digest.irritator.evidence_stage.acquire_document", external_mock)
    for source in ("arxiv", "hackernews"):
        monkeypatch.setattr("digest.irritator.evidence_stage.search_" + source,
                            AsyncMock(side_effect=AssertionError("Unselected source must not be searched")))
    return {"original": original_mock, "search": search_mock, "external": external_mock, "calls": calls}


async def _to_plan(monkeypatch: pytest.MonkeyPatch) -> tuple[Config, Path, Path, dict[str, Any], dict[str, Any]]:
    cfg, archive, session = _setup(monkeypatch)
    acquired = _acquisitions(monkeypatch)
    pending = await _advance(session)
    assert pending["phase"] == "narrative"
    _accept(pending)
    plan = await _advance(session)
    assert plan["phase"] == "research_plan"
    return cfg, archive, session, plan, acquired


@pytest.mark.parametrize("presentation", [False, True])
@pytest.mark.parametrize("search_starts", [1, 7])
async def test_native_semantic_phases_parse_replay_and_create_existing_fragment(
    monkeypatch: pytest.MonkeyPatch, presentation: bool, search_starts: int,
) -> None:
    _, _, session = _setup(monkeypatch, presentation=presentation)
    acquired = _acquisitions(monkeypatch, search_starts=search_starts)
    primary = {str(path): path.read_bytes() for path in CACHE.rglob("*") if path.is_file()}
    seen = []
    while True:
        pending = await _advance(session)
        if pending["status"] != "native_pending":
            break
        seen.append(pending["phase"])
        # Replaying a pending phase regenerates no native judgment or acquisition.
        counts = [acquired[name].await_count for name in ("original", "search", "external")]
        assert await _advance(session) == pending
        assert [acquired[name].await_count for name in ("original", "search", "external")] == counts
        _accept(pending)
    assert seen == ["narrative", "research_plan", "ranking"] + (["presentation"] if presentation else [])
    assert pending["schema"] == "native-irritator-complete/v1" and pending["status"] == "complete"
    assert pending["physical_starts"] == search_starts + 2 <= 9
    assert acquired["calls"] == [("original", 1), ("search", search_starts), ("external", search_starts + 2)]
    assert all(acquired[name].await_count == 1 for name in ("original", "search", "external"))
    complete_before = {str(path): path.read_bytes() for path in Path("digests").rglob("*") if path.is_file()}
    assert await _advance(session) == pending
    assert {str(path): path.read_bytes() for path in Path("digests").rglob("*") if path.is_file()} == complete_before
    assert all(acquired[name].await_count == 1 for name in ("original", "search", "external"))
    root = json.loads(session.read_bytes())
    marker = json.loads(Path(root["attempt"]).read_bytes())
    assert marker["supplement_status"] == "pending"
    fragment, reason = supplement.pending_fragment("digests", (NOW + timedelta(days=1)).date(), now=NOW)
    assert fragment is not None and reason == "eligible"
    result = json.loads(Path(fragment.fragment.result).read_bytes())
    assert result["source_request_starts"] == search_starts + 2 and len(result["documents"]) == 2
    assert result["ranked_signals"][0]["signal"]["snippet"] == BODY
    assert result["schema_version"] == 3
    provenance = result["native_provenance"]
    assert provenance["schema"] == "native-irritator-provenance/v1"
    assert provenance["session"]["sha256"] == native._sha(session.read_bytes())
    for reference in [provenance["session"], *provenance["phases"]]:
        assert native._sha(Path(reference["path"]).read_bytes()) == reference["sha256"]
    assert all(not item["provider"] and not item["model"] and not item["usage"] for item in result["diagnostics"])
    if presentation:
        assert "Представление:" in fragment.fragment.text
        assert "Представление:" not in result["narratives"][0]["claim"]
    assert {str(path): path.read_bytes() for path in CACHE.rglob("*") if path.is_file()} == primary


async def test_begin_and_native_phase_intents_are_exclusive(monkeypatch: pytest.MonkeyPatch) -> None:
    _, archive, session = _setup(monkeypatch)
    with pytest.raises(ValueError, match="already exists"):
        native.begin(CONFIG, archive)
    pending = await _advance(session)
    incoming, intent = _response(pending)
    before = intent.read_bytes()
    with pytest.raises(FileExistsError):
        native.claim_phase(Path(pending["request"]), pending["request_sha256"], "synthetic-controller/run-2")
    assert intent.read_bytes() == before
    accepted = native.accept_response(Path(pending["request"]), incoming, pending["request_sha256"])
    assert native.accept_response(Path(pending["request"]), incoming, pending["request_sha256"]) == accepted


@pytest.mark.parametrize("mutation", ["phase", "request", "intent", "controller", "schema", "result_type", "extra"])
async def test_response_envelope_binding_holds_before_acceptance(
    monkeypatch: pytest.MonkeyPatch, mutation: str,
) -> None:
    _, _, session = _setup(monkeypatch)
    pending = await _advance(session)
    incoming, intent = _response(pending)
    value = json.loads(incoming.read_bytes())
    field = {"phase": "phase", "request": "request_sha256", "intent": "intent_sha256",
             "controller": "controller_ref", "schema": "schema"}.get(mutation)
    if field:
        value[field] = "wrong" if mutation in {"phase", "controller", "schema"} else "f" * 64
    elif mutation == "result_type":
        value["result"] = []
    else:
        value["extra"] = True
    incoming.write_bytes(canonical_json_bytes(value))
    retained = intent.read_bytes()
    with pytest.raises(ValueError):
        native.accept_response(Path(pending["request"]), incoming, pending["request_sha256"])
    assert intent.read_bytes() == retained
    assert not incoming.with_name("narrative.response.json").exists()


async def test_invalid_semantic_response_is_retained_and_cannot_be_replaced(monkeypatch: pytest.MonkeyPatch) -> None:
    _, _, session = _setup(monkeypatch)
    acquired = _acquisitions(monkeypatch)
    pending = await _advance(session)
    saved = _accept(pending, {"narratives": [], "limitations": []})
    before = saved.read_bytes()
    for _ in range(2):
        with pytest.raises(ValueError, match="empty result"):
            await _advance(session)
    replacement = json.loads(before)
    replacement["result"] = _semantic_result(pending)
    incoming = saved.with_name("replacement.incoming.json")
    incoming.write_bytes(canonical_json_bytes(replacement))
    with pytest.raises(ValueError, match="changed"):
        native.accept_response(Path(pending["request"]), incoming, pending["request_sha256"])
    assert saved.read_bytes() == before
    assert all(acquired[name].await_count == 0 for name in ("original", "search", "external"))


@pytest.mark.parametrize("target", ["session", "claim", "attempt", "archive", "request", "intent", "response"])
async def test_changed_continuation_anchors_hold_without_new_acquisition(
    monkeypatch: pytest.MonkeyPatch, target: str,
) -> None:
    _, archive, session, pending, acquired = await _to_plan(monkeypatch)
    root = json.loads(session.read_bytes())
    phase_dir = Path(root["phases"])
    path = {"session": session, "claim": Path(root["claim"]), "attempt": Path(root["attempt"]), "archive": archive,
            "request": phase_dir / "narrative.request.json", "intent": phase_dir / "narrative.intent.json",
            "response": phase_dir / "narrative.response.json"}[target]
    if target == "response":
        raw = json.loads(path.read_bytes())
        raw["result"]["narratives"][0]["claim"] += " Changed."
        path.write_bytes(canonical_json_bytes(raw))
    else:
        path.write_bytes(path.read_bytes() + b" ")
    before = {str(item): item.read_bytes() for item in Path("digests").rglob("*") if item.is_file()}
    with pytest.raises(ValueError):
        await _advance(session)
    assert {str(item): item.read_bytes() for item in Path("digests").rglob("*") if item.is_file()} == before
    assert acquired["original"].await_count == 1
    assert acquired["search"].await_count == acquired["external"].await_count == 0
    assert pending["phase"] == "research_plan"


async def test_changed_configuration_holds_exact_session_without_regeneration(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg, _, session = _setup(monkeypatch)
    pending = await _advance(session)
    cfg.irritator.queries_per_narrative += 1
    with pytest.raises(ValueError, match="configuration changed"):
        await _advance(session)
    assert Path(pending["request"]).is_file()


@pytest.mark.parametrize("phase", ["original_document", "research_evidence"])
async def test_unknown_acquisition_intent_never_repeats_source_work(
    monkeypatch: pytest.MonkeyPatch, phase: str,
) -> None:
    _, _, session = _setup(monkeypatch)
    acquired = _acquisitions(monkeypatch)
    pending = await _advance(session)
    _accept(pending)
    if phase == "research_evidence":
        pending = await _advance(session)
        assert pending["phase"] == "research_plan"
        _accept(pending)
        target = "digest.irritator.evidence_stage._collect_research_evidence"
    else:
        target = "digest.application.native_irritator.acquire_document"
    failure = AsyncMock(side_effect=RuntimeError("Synthetic unknown acquisition"))
    monkeypatch.setattr(target, failure)
    with pytest.raises(RuntimeError, match="unknown acquisition"):
        await _advance(session)
    root = json.loads(session.read_bytes())
    intent = Path(root["phases"]) / f"{phase}.intent.json"
    assert intent.is_file()
    retained = intent.read_bytes()
    with pytest.raises(ValueError, match="incomplete intent"):
        await _advance(session)
    with pytest.raises(FileExistsError):
        await native.advance(CONFIG, session, acquisition_intent_sha256=native._sha(intent.read_bytes()))
    assert failure.await_count == 1 and intent.read_bytes() == retained
    if phase == "research_evidence":
        assert acquired["original"].await_count == 1


@pytest.mark.parametrize("kind", ["session_symlink", "request_symlink", "response_symlink", "outside_session"])
async def test_native_phase_paths_reject_symlinks_and_repository_escape(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, kind: str,
) -> None:
    _, _, session = _setup(monkeypatch)
    pending = await _advance(session)
    if kind == "session_symlink":
        link = session.with_name("linked.phases.json")
        link.symlink_to(session.name)
        with pytest.raises(ValueError):
            await native.advance(CONFIG, link)
    elif kind == "outside_session":
        path = tmp_path.parent / (tmp_path.name + "-session.json")
        path.write_bytes(session.read_bytes())
        with pytest.raises(ValueError):
            await native.advance(CONFIG, path)
    elif kind == "request_symlink":
        request = Path(pending["request"])
        real = request.with_name("request.saved.json")
        request.rename(real)
        request.symlink_to(real.name)
        with pytest.raises(ValueError):
            native.claim_phase(request, pending["request_sha256"], "synthetic-controller")
    else:
        incoming, _ = _response(pending)
        link = incoming.with_name("linked.response.json")
        link.symlink_to(incoming.name)
        with pytest.raises(ValueError):
            native.accept_response(Path(pending["request"]), link, pending["request_sha256"])


async def _complete_native(monkeypatch: pytest.MonkeyPatch) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    _, _, session = _setup(monkeypatch)
    acquired = _acquisitions(monkeypatch)
    for _ in range(5):
        result = await _advance(session)
        if result["status"] != "native_pending":
            return session, result, acquired
        _accept(result)
    raise AssertionError("Synthetic native pipeline exceeded its declared phase count")


@pytest.mark.parametrize(
    "mutation", ["schema", "session", "physical_count", "missing_artifact", "artifact_hash", "extra"],
)
async def test_native_completion_record_rejects_tampering_without_reopening_work(
    monkeypatch: pytest.MonkeyPatch, mutation: str,
) -> None:
    session, _, acquired = await _complete_native(monkeypatch)
    root = json.loads(session.read_bytes())
    path = Path(root["phases"]) / "complete.json"
    value = json.loads(path.read_bytes())
    if mutation == "schema":
        value["schema"] = "unknown/v1"
    elif mutation == "session":
        value["session_sha256"] = "f" * 64
    elif mutation == "physical_count":
        value["physical_starts"] = 10
    elif mutation == "missing_artifact":
        value["artifacts"].pop()
    elif mutation == "artifact_hash":
        value["artifacts"][0]["sha256"] = "f" * 64
    else:
        value["unrecognized"] = True
    path.write_bytes(canonical_json_bytes(value))
    before = {str(item): item.read_bytes() for item in Path("digests").rglob("*") if item.is_file()}
    with pytest.raises(ValueError):
        await _advance(session)
    assert {str(item): item.read_bytes() for item in Path("digests").rglob("*") if item.is_file()} == before
    assert all(acquired[name].await_count == 1 for name in ("original", "search", "external"))


@pytest.mark.parametrize("count", [-1, True, 10])
async def test_replayed_acquisition_rejects_invalid_cumulative_starts(
    monkeypatch: pytest.MonkeyPatch, count: Any,
) -> None:
    _, _, session, _, acquired = await _to_plan(monkeypatch)
    root = json.loads(session.read_bytes())
    path = Path(root["phases"]) / "original_document.acquired.json"
    reference = json.loads(path.read_bytes())
    result_path = Path(reference["result"]["path"])
    value = json.loads(result_path.read_bytes())
    value["physical_starts_after"] = count
    result_path.write_bytes(canonical_json_bytes(value))
    reference["result"]["sha256"] = native._sha(result_path.read_bytes())
    path.write_bytes(canonical_json_bytes(reference))
    with pytest.raises(ValueError, match="cumulative request count"):
        await _advance(session)
    assert acquired["original"].await_count == 1
    assert acquired["search"].await_count == acquired["external"].await_count == 0


@pytest.mark.parametrize("phase", ["original_document", "research_evidence"])
async def test_completed_fragment_binds_native_acquisition_content(monkeypatch: pytest.MonkeyPatch, phase: str) -> None:
    from digest.adapters.storage import post_delivery as post_storage

    session, _, _ = await _complete_native(monkeypatch)
    root = json.loads(session.read_bytes())
    record = json.loads(Path(root["attempt"]).read_bytes())
    acquisition = json.loads((Path(root["phases"]) / f"{phase}.acquired.json").read_bytes())
    content = Path(acquisition["result"]["path"])
    content.write_bytes(content.read_bytes() + b" ")
    fragment = record["fragment"]
    with pytest.raises(ValueError):
        post_storage.read_projection(fragment["projection"], fragment["projection_sha256"])


@pytest.mark.parametrize("boundary", ["result", "markdown", "marker", "complete"])
async def test_native_finalization_interruption_retains_hold_without_repeating_work(
    monkeypatch: pytest.MonkeyPatch, boundary: str,
) -> None:
    _, _, session = _setup(monkeypatch)
    acquired = _acquisitions(monkeypatch)
    for expected in ("narrative", "research_plan", "ranking"):
        pending = await _advance(session)
        assert pending["phase"] == expected
        _accept(pending)
    if boundary == "complete":
        original = native._exclusive

        def write(path: Path, value: dict[str, Any]) -> str:
            if path.name == "complete.json":
                raise OSError("Synthetic finalize interruption")
            return original(path, value)

        monkeypatch.setattr(native, "_exclusive", write)
    else:
        target = {"result": "_save_result", "markdown": "_save_markdown", "marker": "_save_attempt"}[boundary]

        def interrupted(*args: Any, **kwargs: Any) -> Any:
            raise OSError("Synthetic finalize interruption")

        monkeypatch.setattr(native, target, interrupted)
    with pytest.raises(OSError, match="finalize interruption"):
        await _advance(session)
    root = json.loads(session.read_bytes())
    assert (Path(root["phases"]) / "finalize.intent.json").is_file()
    before = {str(path): path.read_bytes() for path in Path("digests").rglob("*") if path.is_file()}
    with pytest.raises((ValueError, OSError)):
        await _advance(session)
    assert {str(path): path.read_bytes() for path in Path("digests").rglob("*") if path.is_file()} == before
    assert all(acquired[name].await_count == 1 for name in ("original", "search", "external"))


@pytest.mark.parametrize("phase", ["research_plan", "ranking", "presentation"])
async def test_invalid_native_stage_semantics_cannot_trigger_retry_or_api_fallback(
    monkeypatch: pytest.MonkeyPatch, phase: str,
) -> None:
    _, _, session = _setup(monkeypatch, presentation=True)
    acquired = _acquisitions(monkeypatch)
    for _ in range(4):
        pending = await _advance(session)
        assert pending["status"] == "native_pending"
        result = _semantic_result(pending)
        if pending["phase"] == phase:
            if phase == "research_plan":
                result["queries"][0]["sources"] = ["invented_source"]
            elif phase == "ranking":
                result["rankings"][0]["evidence_quote_ids"] = ["invented_quote"]
            else:
                result["translations"].pop()
            response = _accept(pending, result)
            break
        _accept(pending, result)
    else:
        raise AssertionError("Requested synthetic invalid stage was never reached")
    before = response.read_bytes()
    counts = [acquired[name].await_count for name in ("original", "search", "external")]
    for _ in range(2):
        with pytest.raises(ValueError):
            await _advance(session)
    assert response.read_bytes() == before
    assert [acquired[name].await_count for name in ("original", "search", "external")] == counts


@pytest.mark.parametrize("phase", ["original_document", "research_evidence"])
async def test_acquisition_requires_explicit_persisted_intent_proof_before_source_io(
    monkeypatch: pytest.MonkeyPatch, phase: str,
) -> None:
    if phase == "research_evidence":
        _, _, session, pending, acquired = await _to_plan(monkeypatch)
    else:
        _, _, session = _setup(monkeypatch)
        acquired = _acquisitions(monkeypatch)
        pending = await native.advance(CONFIG, session)
    _accept(pending)
    requested = await native.advance(CONFIG, session)
    assert requested["status"] == "acquisition_pending" and requested["phase"] == phase
    counts = [acquired[name].await_count for name in ("original", "search", "external")]
    assert await native.advance(CONFIG, session) == requested
    assert [acquired[name].await_count for name in ("original", "search", "external")] == counts
    request = Path(requested["request"])
    intent = native.claim_acquisition(request, requested["request_sha256"])
    expected = native._sha(intent.read_bytes())
    started = request.with_name(f"{phase}.started.json")
    assert not started.exists()
    with pytest.raises(FileExistsError):
        native.claim_acquisition(request, requested["request_sha256"])
    for proof in (None, "f" * 64):
        with pytest.raises(ValueError, match="incomplete intent"):
            await native.advance(CONFIG, session, acquisition_intent_sha256=proof)
    assert not started.exists()
    assert [acquired[name].await_count for name in ("original", "search", "external")] == counts
    mock = acquired["original" if phase == "original_document" else "search"]
    callback = mock.side_effect

    async def check_started(*args: Any, **kwargs: Any) -> Any:
        assert json.loads(started.read_bytes()) == {
            "schema": "native-irritator-acquisition-start/v1", "intent_sha256": expected,
        }
        return await callback(*args, **kwargs)

    mock.side_effect = check_started
    next_phase = await native.advance(CONFIG, session, acquisition_intent_sha256=expected)
    assert next_phase["status"] == "native_pending"
    assert next_phase["phase"] == ("research_plan" if phase == "original_document" else "ranking")
    after = [acquired[name].await_count for name in ("original", "search", "external")]
    assert await native.advance(CONFIG, session, acquisition_intent_sha256=expected) == next_phase
    assert [acquired[name].await_count for name in ("original", "search", "external")] == after


async def test_native_and_acquisition_claim_types_cannot_be_interchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    _, _, session = _setup(monkeypatch)
    _acquisitions(monkeypatch)
    narrative = await native.advance(CONFIG, session)
    with pytest.raises(ValueError):
        native.claim_acquisition(Path(narrative["request"]), narrative["request_sha256"])
    _accept(narrative)
    acquisition = await native.advance(CONFIG, session)
    assert acquisition["status"] == "acquisition_pending"
    with pytest.raises(ValueError):
        native.claim_phase(Path(acquisition["request"]), acquisition["request_sha256"], "synthetic-controller")
    incoming = Path(acquisition["request"]).with_name("invalid-generation.json")
    incoming.write_text("{}")
    with pytest.raises(ValueError):
        native.accept_response(Path(acquisition["request"]), incoming, acquisition["request_sha256"])


async def test_native_abstention_archives_empty_result_without_acquisition_or_fragment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, session = _setup(monkeypatch)
    acquired = _acquisitions(monkeypatch)
    pending = await _advance(session)
    _accept(pending, {"narratives": [], "limitations": ["No delivered assertion warrants further investigation."]})
    complete = await _advance(session)
    assert complete["status"] == "empty" and complete["physical_starts"] == 0
    assert len(complete["artifacts"]) == 2
    record = json.loads(Path(json.loads(session.read_bytes())["attempt"]).read_bytes())
    assert record["supplement_status"] == "archive_only" and "fragment" not in record
    assert all(acquired[name].await_count == 0 for name in ("original", "search", "external"))
    assert await _advance(session) == complete


@pytest.mark.parametrize("elapsed", [-1, True, float("nan"), float("inf"), -float("inf"), 180.01])
async def test_replayed_acquisition_rejects_invalid_elapsed_seconds(
    monkeypatch: pytest.MonkeyPatch, elapsed: Any,
) -> None:
    _, _, session, _, acquired = await _to_plan(monkeypatch)
    directory = Path(json.loads(session.read_bytes())["phases"])
    pointer = directory / "original_document.acquired.json"
    reference = json.loads(pointer.read_bytes())
    content = Path(reference["result"]["path"])
    value = json.loads(content.read_bytes())
    value["acquisition_elapsed_seconds"] = elapsed
    # Deliberately emit non-finite JSON tokens as adversarial retained input.
    content.write_text(json.dumps(value, allow_nan=True), encoding="utf-8")
    reference["result"]["sha256"] = native._sha(content.read_bytes())
    pointer.write_bytes(canonical_json_bytes(reference))
    before = {str(path): path.read_bytes() for path in Path("digests").rglob("*") if path.is_file()}
    with pytest.raises(ValueError, match="cumulative request count"):
        await _advance(session)
    assert {str(path): path.read_bytes() for path in Path("digests").rglob("*") if path.is_file()} == before
    assert acquired["original"].await_count == 1
    assert acquired["search"].await_count == acquired["external"].await_count == 0


@pytest.mark.parametrize("research_seconds", [55.0, 55.5])
async def test_acquisition_deadline_is_cumulative_and_cached_requests_remain_stable(
    monkeypatch: pytest.MonkeyPatch, research_seconds: float,
) -> None:
    _, _, session = _setup(monkeypatch)
    acquired = _acquisitions(monkeypatch)
    seconds = [1000.0]
    timeouts: list[float] = []
    real_timeout = native.asyncio.timeout

    def timeout(delay: float) -> Any:
        timeouts.append(delay)
        return real_timeout(delay)

    # Replace only this module's clock, never asyncio's global monotonic clock.
    monkeypatch.setattr(native, "time", SimpleNamespace(monotonic=lambda: seconds[0]))
    monkeypatch.setattr(native, "asyncio", SimpleNamespace(timeout=timeout))
    for name, duration in (("original", 125.0), ("external", research_seconds)):
        callback = acquired[name].side_effect

        async def timed(*args: Any, _callback: Any = callback, _duration: float = duration, **kwargs: Any) -> Any:
            result = await _callback(*args, **kwargs)
            seconds[0] += _duration
            return result

        acquired[name].side_effect = timed
    _accept(await _advance(session))
    plan = await _advance(session)
    assert plan["phase"] == "research_plan" and timeouts == [180.0]
    directory = Path(json.loads(session.read_bytes())["phases"])
    original = json.loads((directory / "original_document.acquired.json").read_bytes())
    assert json.loads(Path(original["result"]["path"]).read_bytes())["acquisition_elapsed_seconds"] == 125.0
    plan_bytes = Path(plan["request"]).read_bytes()
    seconds[0] += 99999  # Controller wait time is not admitted source-I/O time.
    assert await _advance(session) == plan
    assert Path(plan["request"]).read_bytes() == plan_bytes
    _accept(plan)
    pending = await native.advance(CONFIG, session)
    assert pending["status"] == "acquisition_pending" and pending["phase"] == "research_evidence"
    request = Path(pending["request"])
    request_bytes = request.read_bytes()
    assert json.loads(request_bytes)["input"]["acquisition_seconds_before"] == 125.0
    assert await native.advance(CONFIG, session) == pending
    assert request.read_bytes() == request_bytes and native._sha(request_bytes) == pending["request_sha256"]
    intent = native.claim_acquisition(request, pending["request_sha256"])
    proof = native._sha(intent.read_bytes())
    if research_seconds > 55:
        with pytest.raises(ValueError, match="cumulative deadline exceeded"):
            await native.advance(CONFIG, session, acquisition_intent_sha256=proof)
        assert not (directory / "research_evidence.acquired.json").exists()
        before = {str(path): path.read_bytes() for path in directory.rglob("*") if path.is_file()}
        with pytest.raises(ValueError, match="incomplete intent"):
            await _advance(session)
        assert {str(path): path.read_bytes() for path in directory.rglob("*") if path.is_file()} == before
    else:
        ranking = await native.advance(CONFIG, session, acquisition_intent_sha256=proof)
        assert ranking["phase"] == "ranking"
        seconds[0] += 99999
        assert await _advance(session) == ranking
        assert request.read_bytes() == request_bytes
        research = json.loads((directory / "research_evidence.acquired.json").read_bytes())
        assert json.loads(Path(research["result"]["path"]).read_bytes())["acquisition_elapsed_seconds"] == 55.0
    assert timeouts == [180.0, 55.0]
    assert all(acquired[name].await_count == 1 for name in ("original", "search", "external"))


@pytest.mark.parametrize("phase", ["narrative", "research_plan", "ranking", "presentation"])
async def test_unknown_native_generation_intent_holds_without_repeated_pending_dispatch(
    monkeypatch: pytest.MonkeyPatch, phase: str,
) -> None:
    _, _, session = _setup(monkeypatch, presentation=True)
    acquired = _acquisitions(monkeypatch)
    for _ in range(4):
        pending = await _advance(session)
        if pending["phase"] == phase:
            break
        _accept(pending)
    else:
        raise AssertionError("Native phase was never reached")
    request = Path(pending["request"])
    native.claim_phase(request, pending["request_sha256"], "synthetic-controller/unknown-generation")
    before = {str(path): path.read_bytes() for path in Path("digests").rglob("*") if path.is_file()}
    counts = [acquired[name].await_count for name in ("original", "search", "external")]
    for _ in range(2):
        with pytest.raises(ValueError, match="incomplete intent.*no repeated generation"):
            await _advance(session)
    assert not request.with_name(f"{phase}.response.json").exists()
    assert {str(path): path.read_bytes() for path in Path("digests").rglob("*") if path.is_file()} == before
    assert [acquired[name].await_count for name in ("original", "search", "external")] == counts


async def test_oversized_native_translation_keeps_canonical_text_and_honest_localized_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, session = _setup(monkeypatch, presentation=True, translation_limit=256)
    acquired = _acquisitions(monkeypatch)
    target = ""
    for expected in ("narrative", "research_plan", "ranking"):
        pending = await _advance(session)
        assert pending["phase"] == expected
        if expected == "narrative":
            target = _messages(pending)["delivered_cards"][0]["summary"]
        _accept(pending)
    complete = await _advance(session)
    assert complete["status"] == "complete"
    root = json.loads(session.read_bytes())
    record = json.loads(Path(root["attempt"]).read_bytes())
    assert record["translation_status"] == "fallback"
    assert not list(Path(root["phases"]).glob("presentation.*"))
    canonical = json.loads(Path(record["native_result"]["path"]).read_bytes())
    fragment, reason = supplement.pending_fragment("digests", (NOW + timedelta(days=1)).date(), now=NOW)
    assert fragment is not None and reason == "eligible"
    notice = "Перевод недоступен или неполон; сохранён канонический текст."
    assert "Синтетическая карточка" in target
    assert canonical["narratives"][0]["claim"] == target
    assert canonical["narratives"][0]["delivered_quote"]["text"] == target
    markdown = Path(record["native_markdown"]["path"]).read_text(encoding="utf-8")
    for text in (fragment.fragment.text, markdown):
        assert notice in text
        assert target in text
        assert "исходный английский текст" not in text and "canonical English text retained" not in text
        assert canonical["ranked_signals"][0]["reasoning"] in text
        assert "The specification documents a client authentication condition." in text
        assert "Представление:" not in text and "Машинный перевод;" not in text
    # Native canonical text includes the exact Russian delivered target and
    # English research; neither is relabeled as wholly English or translated.
    assert await _advance(session) == complete
    assert all(acquired[name].await_count == 1 for name in ("original", "search", "external"))


async def test_fragment_provenance_parses_the_exact_result_bytes_whose_hash_was_verified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from digest.adapters.storage import post_delivery as post_storage

    session, _, _ = await _complete_native(monkeypatch)
    root = json.loads(session.read_bytes())
    marker = Path(root["attempt"])
    record = json.loads(marker.read_bytes())
    fragment = record["fragment"]
    result_path = Path(record["native_result"]["path"]).resolve()
    read_bytes = Path.read_bytes
    verified_bytes = read_bytes(result_path)
    result = json.loads(verified_bytes)
    acquisition = next(Path(ref["path"]) for ref in result["native_provenance"]["phases"]
                       if ref["path"].endswith("original_document.acquired.json"))
    acquisition.unlink()
    retained_marker = marker.read_bytes()
    calls = 0

    def changed_second_buffer(path: Path) -> bytes:
        nonlocal calls
        if path.resolve() == result_path:
            calls += 1
            # A second, unhashed parse must never downgrade native schema3 and
            # bypass the now-missing acquisition provenance (Astra NI-01).
            return verified_bytes if calls == 1 else canonical_json_bytes({"schema_version": 2})
        return read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", changed_second_buffer)
    with pytest.raises((ValueError, OSError)):
        post_storage.read_projection(fragment["projection"], fragment["projection_sha256"])
    assert calls == 1
    assert marker.read_bytes() == retained_marker


@pytest.mark.parametrize("output", ["markdown", "result_tmp", "attempt_tmp"])
async def test_native_finalization_output_symlinks_hold_without_overwriting_unrelated_records(
    monkeypatch: pytest.MonkeyPatch, output: str,
) -> None:
    cfg, archive, session = _setup(monkeypatch)
    acquired = _acquisitions(monkeypatch)
    for expected in ("narrative", "research_plan", "ranking"):
        pending = await _advance(session)
        assert pending["phase"] == expected
        _accept(pending)
    marker, result, markdown, _, directory = native._names(archive, cfg)
    target = {"markdown": markdown, "result_tmp": result, "attempt_tmp": marker}[output]
    link = target.with_suffix(target.suffix + ".tmp") if output.endswith("_tmp") else target
    unrelated = Path("unrelated-retained-record.txt").resolve()
    unrelated.write_bytes(b"Synthetic unrelated record; must remain byte-identical.\n")
    link.symlink_to(unrelated)
    before = {str(path): path.read_bytes() for path in Path("digests").rglob("*") if path.is_file()}
    primary = {str(path): path.read_bytes() for path in CACHE.rglob("*") if path.is_file()}
    expected_bytes = unrelated.read_bytes()
    for _ in range(2):
        with pytest.raises((ValueError, OSError)):
            await _advance(session)
    assert unrelated.read_bytes() == expected_bytes and link.is_symlink()
    assert {str(path): path.read_bytes() for path in Path("digests").rglob("*") if path.is_file()} == before
    assert {str(path): path.read_bytes() for path in CACHE.rglob("*") if path.is_file()} == primary
    assert not (directory / "finalize.intent.json").exists()
    assert all(acquired[name].await_count == 1 for name in ("original", "search", "external"))


def test_native_begin_rechecks_temporary_symlink_after_execution_claim_without_overwrite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create_claim = native.storage.create_native_execution_claim
    unrelated = Path("unrelated-retained-record.txt").resolve()
    unrelated.write_bytes(b"Synthetic unrelated record; must remain byte-identical.\n")
    expected_bytes = unrelated.read_bytes()
    retained: dict[Path, bytes] = {}

    def claim(path: Path, record: dict[str, Any]) -> bool:
        outcome = create_claim(path, record)
        assert outcome
        marker = Path(str(path).replace(".execute.json", ".json"))
        retained[marker] = marker.read_bytes()
        marker.with_suffix(marker.suffix + ".tmp").symlink_to(unrelated)
        return outcome

    monkeypatch.setattr(native.storage, "create_native_execution_claim", claim)
    with pytest.raises((ValueError, OSError)):
        _setup(monkeypatch)
    assert unrelated.read_bytes() == expected_bytes and len(retained) == 1
    for marker, content in retained.items():
        assert marker.read_bytes() == content
        assert marker.with_suffix(marker.suffix + ".tmp").is_symlink()
        assert not marker.with_suffix(".phases.json").exists()


async def test_diagnostic_acquisitions_preserve_legacy_replay_and_semantic_messages(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from dataclasses import asdict

    from digest.article_source import extract_html

    diagnostics = json.loads(canonical_json_bytes(
        asdict(extract_html("<article><p>" + BODY + "</p></article>").extraction_diagnostics),
    ))
    original_save = native.NativeResearch._save_acquisition
    messages_by_mode = {}
    requests_by_mode = {}
    for mode in ("legacy", "diagnostic"):
        directory = tmp_path / mode
        directory.mkdir()
        monkeypatch.chdir(directory)

        def save(self: Any, phase: str, value: dict[str, Any], budget: SourceRequestBudget,
                 started_at: float, *, mode: str = mode) -> None:
            snapshots = ([value["snapshot"]] if phase == "original_document"
                         else [item["snapshot"] for item in value["documents"]])
            for snapshot in snapshots:
                if mode == "legacy":
                    snapshot.pop("extraction_diagnostics")
                else:
                    snapshot["extraction_diagnostics"] = diagnostics
            original_save(self, phase, value, budget, started_at)

        monkeypatch.setattr(native.NativeResearch, "_save_acquisition", save)
        _, _, session = _setup(monkeypatch)
        acquired = _acquisitions(monkeypatch)
        messages, requests = {}, {}
        while True:
            pending = await _advance(session)
            if pending["status"] != "native_pending":
                break
            path = Path(pending["request"])
            raw = path.read_bytes()
            request = json.loads(raw)
            messages[pending["phase"]] = request["input"]["messages"]
            requests[pending["phase"]] = request
            counts = [acquired[name].await_count for name in ("original", "search", "external")]
            retained = {str(item): item.read_bytes() for item in Path("digests").rglob("*") if item.is_file()}
            assert await _advance(session) == pending
            assert path.read_bytes() == raw and native._sha(raw) == pending["request_sha256"]
            assert {str(item): item.read_bytes() for item in Path("digests").rglob("*") if item.is_file()} == retained
            assert [acquired[name].await_count for name in ("original", "search", "external")] == counts
            _accept(pending)
        assert pending["status"] == "complete"
        root = json.loads(session.read_bytes())
        for phase in native.ACQUISITIONS:
            ref = json.loads((Path(root["phases"]) / f"{phase}.acquired.json").read_bytes())["result"]
            raw = Path(ref["path"]).read_bytes()
            assert native._sha(raw) == ref["sha256"]
            value = json.loads(raw)
            snapshots = ([value["snapshot"]] if phase == "original_document"
                         else [item["snapshot"] for item in value["documents"]])
            assert snapshots
            for snapshot in snapshots:
                if mode == "legacy":
                    assert "extraction_diagnostics" not in snapshot
                else:
                    assert snapshot["extraction_diagnostics"] == diagnostics
            if mode == "diagnostic":
                # Alter only safe metadata, not source bytes or evidence text.
                snapshots[0]["extraction_diagnostics"]["saturated"] = True
                assert native._sha(canonical_json_bytes(value)) != ref["sha256"]
            assert any(ref == prior for request in requests.values() for prior in request["prior"])
        retained = {str(item): item.read_bytes() for item in Path("digests").rglob("*") if item.is_file()}
        assert await _advance(session) == pending
        assert {str(item): item.read_bytes() for item in Path("digests").rglob("*") if item.is_file()} == retained
        assert all(acquired[name].await_count == 1 for name in ("original", "search", "external"))
        messages_by_mode[mode], requests_by_mode[mode] = messages, requests
    # Model evidence/messages and their hashes are identical; native request
    # envelopes intentionally bind the different immutable acquisition bytes.
    assert messages_by_mode["legacy"] == messages_by_mode["diagnostic"]
    assert native._sha(canonical_json_bytes(messages_by_mode["legacy"])) == native._sha(
        canonical_json_bytes(messages_by_mode["diagnostic"]),
    )
    assert requests_by_mode["legacy"]["ranking"]["prior"] != requests_by_mode["diagnostic"]["ranking"]["prior"]


@pytest.mark.parametrize("phase", ["original_document", "research_evidence"])
@pytest.mark.parametrize("invalid", ["unknown_enum", "extra_field", "bool_count"])
async def test_native_acquisition_rejects_invalid_diagnostics_without_reacquisition(
    monkeypatch: pytest.MonkeyPatch, phase: str, invalid: str,
) -> None:
    from dataclasses import asdict

    from digest.article_source import extract_html

    _, _, session, plan, acquired = await _to_plan(monkeypatch)
    if phase == "research_evidence":
        _accept(plan)
        assert (await _advance(session))["phase"] == "ranking"
    root = json.loads(session.read_bytes())
    path = Path(root["phases"]) / f"{phase}.acquired.json"
    reference = json.loads(path.read_bytes())
    result_path = Path(reference["result"]["path"])
    value = json.loads(result_path.read_bytes())
    snapshot = value["snapshot"] if phase == "original_document" else value["documents"][0]["snapshot"]
    diagnostics = json.loads(canonical_json_bytes(
        asdict(extract_html("<article><p>" + BODY + "</p></article>").extraction_diagnostics),
    ))
    if invalid == "unknown_enum":
        diagnostics["phase"] = "secret untrusted instruction"
    elif invalid == "extra_field":
        diagnostics["raw_html"] = "secret content"
    else:
        diagnostics["omitted_regions"] = True
    snapshot["extraction_diagnostics"] = diagnostics
    result_path.write_bytes(canonical_json_bytes(value))
    reference["result"]["sha256"] = native._sha(result_path.read_bytes())
    path.write_bytes(canonical_json_bytes(reference))
    counts = [acquired[name].await_count for name in ("original", "search", "external")]
    retained = {str(item): item.read_bytes() for item in Path("digests").rglob("*") if item.is_file()}
    with pytest.raises(ValueError, match="checkpoint|diagnostics"):
        await _advance(session)
    assert [acquired[name].await_count for name in ("original", "search", "external")] == counts
    assert {str(item): item.read_bytes() for item in Path("digests").rglob("*") if item.is_file()} == retained
