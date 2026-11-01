"""Original extraction exposure is necessary, not semantic acceptance."""
from __future__ import annotations

import json
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import pytest

from digest._serialization import canonical_json_bytes
from digest.adapters.storage import native as storage
from digest.application import native_supplement
from digest.domain.delivery.supplement import PendingSupplement, fragment_identity
from digest.domain.editorial.native import sha256


def bind_context(pending: PendingSupplement, *, text: str = "Synthetic original extraction.",
                 supplied: str | None = "Synthetic original extraction.", status: str = "complete",
                 unknown: bool = False) -> PendingSupplement:
    """Create synthetic retained phase evidence, never rewrite a real record."""
    result = json.loads(Path(pending.fragment.result).read_bytes())
    phase_dir = Path(pending.attempt).with_suffix(".phases")
    phase_dir.mkdir(parents=True, exist_ok=True)
    session_path = Path(pending.attempt).with_suffix(".phases.json")
    session_path.write_bytes(canonical_json_bytes({
        "schema": "native-irritator-session/v1", "checkpoint": pending.fragment.checkpoint,
        "delivered_sha256": sha256(canonical_json_bytes(asdict(pending.fragment.origin))),
        "config_sha256": "a" * 64, "attempt": pending.attempt, "attempt_sha256": "b" * 64,
        "claim": "digests/synthetic-claim.json", "claim_sha256": "c" * 64, "phases": phase_dir.as_posix(),
    }))
    session = storage.Reference(session_path.as_posix(), sha256(session_path.read_bytes()))
    messages = [{"role": "system", "content": "Synthetic ranking instruction."},
                {"role": "user", "content": json.dumps({"original_document_context": supplied})}]
    request_path = phase_dir / "ranking.request.json"
    request_path.write_bytes(canonical_json_bytes({
        "schema": "native-irritator-request/v1", "phase": "ranking", "session_sha256": session.sha256,
        "prior": [], "input": {"messages": messages},
    }))
    request = storage.Reference(request_path.as_posix(), sha256(request_path.read_bytes()))
    result.update(schema_version=3, native_provenance={
        "schema": "native-irritator-provenance/v1", "session": asdict(session), "phases": [asdict(request)]},
        documents=[{"role": "origin", "snapshot": {"status": status, "text": text}}],
        diagnostics=[{"stage": "ranking", "prompt_sha256": sha256(canonical_json_bytes(messages))}])
    if unknown:
        result.pop("native_provenance")
        result["schema_version"] = 1
    raw = canonical_json_bytes(result)
    Path(pending.fragment.result).write_bytes(raw)
    fragment = replace(pending.fragment, result_sha256=sha256(raw), fragment_id="")
    fragment = replace(fragment, fragment_id=fragment_identity(fragment))
    projection = canonical_json_bytes(asdict(fragment))
    Path(pending.projection).write_bytes(projection)
    pending = replace(pending, fragment=fragment, projection_sha256=sha256(projection))
    record = json.loads(Path(pending.attempt).read_bytes())
    record["fragment"].update(projection_sha256=pending.projection_sha256, fragment_id=fragment.fragment_id)
    Path(pending.attempt).write_bytes(canonical_json_bytes(record))
    return pending


@pytest.mark.parametrize(("text", "supplied", "status", "unknown", "expected"), [
    ("Original", "Original", "complete", False, True),
    ("", "", "complete", False, False),
    ("   ", "   ", "complete", False, False),
    ("x" * 9501, None, "complete", False, False),
    ("Original", None, "complete", False, False),
    ("", None, "failed", False, False),
    ("Original", "Original", "complete", True, False),
    ("Original", "Partial", "complete", False, False),
    ("Original", "Original", "partial", False, False),
    ("x" * 9501, "x" * 9501, "complete", False, True),
])
@pytest.mark.asyncio
async def test_retained_context_exposure(monkeypatch: pytest.MonkeyPatch, isolated: Any,
                                         text: str, supplied: str | None, status: str,
                                         unknown: bool, expected: bool) -> None:
    from tests.test_native_fragment_destination import NEXT, _origin_fragment, _project_next

    pending = bind_context(await _origin_fragment(monkeypatch), text=text, supplied=supplied,
                           status=status, unknown=unknown)
    preserved = {p: p.read_bytes() for p in Path(".").rglob("*") if p.is_file()}
    assert native_supplement.ranking_had_original_context(pending) is expected
    assert all(path.read_bytes() == raw for path, raw in preserved.items())
    _, _, projection = _project_next(monkeypatch, day=NEXT, optional=False)
    assert ("supplement" in projection["publication"]) is expected
    for path in (Path(pending.attempt), Path(pending.projection), Path(pending.fragment.result)):
        assert path.read_bytes() == preserved[path]
    if not expected:
        # Same projection as no optional fragment at all; main cards are untouched.
        Path(pending.attempt).unlink()
        _, _, baseline = _project_next(monkeypatch, day=NEXT, optional=False)
        assert projection == baseline


from tests.test_native_editorial import isolated as isolated  # noqa: E402


@pytest.mark.parametrize("mutation", ["unknown_session", "extra_request", "bad_prior", "bad_input",
                                      "bad_messages", "unknown_request", "wrong_session", "prompt_mismatch",
                                      "duplicate_request", "changed_request", "deep_payload", "nonfinite_payload",
                                      "nonstr_content", "future_ranking"])
@pytest.mark.asyncio
async def test_unverified_proof_is_omitted(monkeypatch: pytest.MonkeyPatch, isolated: Any, mutation: str) -> None:
    from tests.test_native_fragment_destination import _origin_fragment

    pending = await _origin_fragment(monkeypatch)
    result = json.loads(Path(pending.fragment.result).read_bytes())
    provenance = result["native_provenance"]
    session_ref, request_ref = provenance["session"], provenance["phases"][0]
    request = json.loads(Path(request_ref["path"]).read_bytes())
    if mutation in {"deep_payload", "nonfinite_payload", "nonstr_content"}:
        messages = request["input"]["messages"]
        if mutation == "deep_payload":
            messages[1]["content"] = ('{"original_document_context":"Synthetic original extraction.","nested":'
                                      + '[' * 20000 + '0' + ']' * 20000 + '}')
        elif mutation == "nonfinite_payload":
            messages[1]["content"] = '{"original_document_context":"Synthetic original extraction.","x":NaN}'
        else:
            messages[0]["content"] = ["not a string"]
        result["diagnostics"][0]["prompt_sha256"] = sha256(canonical_json_bytes(messages))
    elif mutation == "future_ranking":
        extra = storage.put_bytes(canonical_json_bytes({"phase": "ranking", "schema": "future-ranking/v2"}),
                                  Path(".cache"), "unknown")
        provenance["phases"].append(asdict(extra))
    elif mutation == "unknown_session":
        Path(session_ref["path"]).write_bytes(canonical_json_bytes({"fixture": "unknown session"}))
        session_ref["sha256"] = sha256(Path(session_ref["path"]).read_bytes())
        request["session_sha256"] = session_ref["sha256"]
    elif mutation == "extra_request":
        request["future_semantics"] = True
    elif mutation == "bad_prior":
        request["prior"] = "invalid"
    elif mutation == "bad_input":
        request["input"]["extra"] = True
    elif mutation == "bad_messages":
        request["input"]["messages"][0]["extra"] = True
    elif mutation == "unknown_request":
        request["schema"] = "native-irritator-request/v2"
    elif mutation == "wrong_session":
        request["session_sha256"] = "e" * 64
    elif mutation == "prompt_mismatch":
        result["diagnostics"][0]["prompt_sha256"] = "e" * 64
    elif mutation == "duplicate_request":
        provenance["phases"].append(request_ref.copy())
    Path(request_ref["path"]).write_bytes(canonical_json_bytes(request))
    if mutation != "changed_request":
        request_ref["sha256"] = sha256(Path(request_ref["path"]).read_bytes())
    else:
        Path(request_ref["path"]).write_bytes(b"{}")
    raw = canonical_json_bytes(result)
    Path(pending.fragment.result).write_bytes(raw)
    pending = replace(pending, fragment=replace(pending.fragment, result_sha256=sha256(raw)))
    assert not native_supplement.ranking_had_original_context(pending)
