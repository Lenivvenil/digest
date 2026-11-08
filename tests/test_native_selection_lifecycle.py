"""Offline full-pool persistence and interruption boundaries."""

from dataclasses import asdict
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from digest.adapters.storage import native as storage
from digest.adapters.storage.candidate_progress import load_candidate_progress
from digest.application.candidate_lifecycle import persist_candidates
from digest.application.native_selection_lifecycle import persist_selection_lifecycle
from digest.domain.catalog.articles import article_hash
from tests.test_candidate_review import NOW
from tests.test_candidate_storage import completed_packet


def inputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, dict]:
    monkeypatch.chdir(tmp_path)
    cache = Path(".cache")
    progress, packet = completed_packet(3)
    packet.report = None
    progress.packets.append(packet)
    persist_candidates(progress, cache)
    originals = {article_hash(a.title, a.link): asdict(a) for a in packet.articles}
    ids = list(originals)
    decisions = [
        {
            "id": i,
            "status": "card" if n == 0 else "capacity_deferred",
            "text": "Source-based card." if n == 0 else "",
            "reason": "Reviewed reason.",
            "duplicate_of": None,
        }
        for n, i in enumerate(ids)
    ]
    refs = {
        f"{name}_ref": asdict(storage.put_bytes(("{" + f'"kind":"{name}"' + "}").encode(), cache, name))
        for name in ("packet", "response", "review", "acceptance")
    }
    return cache, {**refs, "originals": originals, "decisions": decisions, "accepted_ids": {ids[0]}, "now": NOW}


def test_retains_full_pool_old_packets_and_never_marks_seen(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cache, kwargs = inputs(tmp_path, monkeypatch)
    before = load_candidate_progress(cache)
    seen = cache / "seen_articles.json"
    seen.write_text('{"untouched":"receipt"}')
    reference = persist_selection_lifecycle(cache, **kwargs)
    assert load_candidate_progress(cache) == before
    assert seen.read_text() == '{"untouched":"receipt"}'
    body = storage.decode(storage.read_reference(reference))
    assert len(body["candidates"]) == len(kwargs["originals"])
    assert sum(c["source_accepted"] for c in body["candidates"]) == 1
    assert persist_selection_lifecycle(cache, **{**kwargs, "now": NOW + timedelta(hours=1)}) == reference


def test_missing_candidates_survive_as_unfinished(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cache, kwargs = inputs(tmp_path, monkeypatch)
    progress = load_candidate_progress(cache)
    progress.candidates.clear()
    persist_candidates(progress, cache)
    persist_selection_lifecycle(cache, **kwargs)
    restored = load_candidate_progress(cache)
    assert set(restored.candidates) == set(kwargs["originals"])
    assert all(c.status == "not_presented" and c.disposition is None for c in restored.candidates.values())
    assert restored.packets == progress.packets


@pytest.mark.parametrize("mutation", ["occurrence", "coverage", "accepted", "proof"])
def test_invalid_inputs_fail_before_working_set_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    cache, kwargs = inputs(tmp_path, monkeypatch)
    before = (cache / "candidate_progress.json").read_bytes()
    if mutation == "occurrence":
        next(iter(kwargs["originals"].values()))["description"] = "Changed occurrence"
    elif mutation == "coverage":
        kwargs["decisions"].pop()
    elif mutation == "accepted":
        kwargs["accepted_ids"] = {kwargs["decisions"][1]["id"]}
    else:
        kwargs["response_ref"]["sha256"] = "0" * 64
    with pytest.raises(ValueError):
        persist_selection_lifecycle(cache, **kwargs)
    assert (cache / "candidate_progress.json").read_bytes() == before


def test_sidecar_failure_keeps_unfinished_work_and_retry_recovers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache, kwargs = inputs(tmp_path, monkeypatch)
    before = load_candidate_progress(cache)
    with patch("digest.application.native_selection_lifecycle.storage.put_bytes", side_effect=OSError("interrupted")):
        with pytest.raises(OSError, match="interrupted"):
            persist_selection_lifecycle(cache, **kwargs)
    assert load_candidate_progress(cache) == before
    reference = persist_selection_lifecycle(cache, **kwargs)
    assert storage.read_reference(reference)


def test_working_set_failure_does_not_publish_sidecar(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cache, kwargs = inputs(tmp_path, monkeypatch)
    with patch("digest.application.native_selection_lifecycle.persist_candidates", side_effect=OSError("interrupted")):
        with pytest.raises(OSError):
            persist_selection_lifecycle(cache, **kwargs)
    assert not list((cache / "native").glob("*.selection-lifecycle.json"))


def test_pure_builder_matches_frozen_bytes_without_state_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from digest.application.native_selection_lifecycle import build_lifecycle_record
    from digest.domain.editorial.native import encode

    cache, kwargs = inputs(tmp_path, monkeypatch)
    build_args = {key: value for key, value in kwargs.items() if key != "now"}
    with patch("digest.application.native_selection_lifecycle.storage.read_reference", side_effect=AssertionError):
        record = build_lifecycle_record(**build_args)
    reference = persist_selection_lifecycle(cache, **kwargs)
    assert storage.read_reference(reference) == encode(record)


def test_preserves_completed_report_proof_and_unrelated_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from digest.adapters.storage.candidate_objects import freeze_packet
    from digest.domain.editorial.candidates import Candidate
    from tests.test_candidate_review import population, report_for

    cache, kwargs = inputs(tmp_path, monkeypatch)
    progress = load_candidate_progress(cache)
    config, _ = population(3)
    packet = progress.packets[0]
    packet.report = report_for(packet, config)
    frozen = freeze_packet(packet, {}, cache)
    proof_before = frozen.read_bytes()
    article = packet.articles[0]
    from dataclasses import replace

    unrelated = replace(article, title="Unrelated previously pending source")
    identity = article_hash(unrelated.title, unrelated.link)
    progress.candidates[identity] = Candidate(identity, unrelated, NOW.isoformat(), 2, status="technical_pending")
    persist_candidates(progress, cache)
    before = load_candidate_progress(cache)
    persist_selection_lifecycle(cache, **kwargs)
    assert load_candidate_progress(cache) == before
    assert frozen.read_bytes() == proof_before


def test_export_only_pool_retention_preserves_existing_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from digest.application.native_selection_lifecycle import retain_selection_pool

    cache, kwargs = inputs(tmp_path, monkeypatch)
    progress = load_candidate_progress(cache)
    first = next(iter(progress.candidates))
    prior = progress.candidates.pop(first)
    persist_candidates(progress, cache)
    before = load_candidate_progress(cache)
    native_before = set((cache / "native").iterdir())
    retain_selection_pool(cache, kwargs["originals"], now=NOW)
    restored = load_candidate_progress(cache)
    assert restored.candidates[first].article == prior.article
    assert restored.candidates[first].status == "not_presented"
    assert restored.candidates[first].disposition is None
    assert {key: value for key, value in restored.candidates.items() if key != first} == before.candidates
    assert restored.packets == before.packets
    assert not (cache / "seen_articles.json").exists()
    assert set((cache / "native").iterdir()) == native_before
    retain_selection_pool(cache, kwargs["originals"], now=NOW + timedelta(hours=1))
    assert load_candidate_progress(cache) == restored


@pytest.mark.parametrize("mutation", ["identity", "occurrence", "naive", "empty"])
def test_export_pool_invalid_sources_fail_before_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    from digest.application.native_selection_lifecycle import retain_selection_pool

    cache, kwargs = inputs(tmp_path, monkeypatch)
    originals = kwargs["originals"]
    now = NOW
    if mutation == "identity":
        originals["invalid-identity"] = originals.pop(next(iter(originals)))
    elif mutation == "occurrence":
        next(iter(originals.values()))["description"] = "Different source occurrence"
    elif mutation == "naive":
        now = NOW.replace(tzinfo=None)
    else:
        originals = {}
    before = (cache / "candidate_progress.json").read_bytes()
    with pytest.raises(ValueError):
        retain_selection_pool(cache, originals, now=now)
    assert (cache / "candidate_progress.json").read_bytes() == before


def test_export_pool_rejects_existing_owned_work_without_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from digest.application.native_selection_lifecycle import retain_selection_pool

    cache, kwargs = inputs(tmp_path, monkeypatch)
    progress = load_candidate_progress(cache)
    next(iter(progress.candidates.values())).status = "technical_pending"
    persist_candidates(progress, cache)
    before = load_candidate_progress(cache)
    with pytest.raises(ValueError, match="cannot be resurrected"):
        retain_selection_pool(cache, kwargs["originals"], now=NOW)
    assert load_candidate_progress(cache) == before
