"""Offline direct-object storage, proof binding and finite growth regression."""
from __future__ import annotations

import json
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import patch

import pytest

from digest.candidate_dispositions import CandidateDispositionCapture
from digest.candidate_review import CandidatePacket, CandidateProgress, merge_candidates, plan_packet
from digest.candidate_storage import (
    decode_active_candidate,
    digest,
    encode_active_candidate,
    freeze_packet,
    list_excluded,
    load_candidate,
    load_candidate_packets,
    put_article,
    read_article,
    read_candidate_header,
    read_packet,
    read_policy,
    read_report_record,
    save_candidate,
    write_policy,
)
from digest.review import run_primary_review
from tests.test_candidate_review import NOW, population, report_for


def completed_packet(count: int = 2) -> tuple[CandidateProgress, CandidatePacket]:
    config, articles = population(count)
    progress = merge_candidates(CandidateProgress(), articles, config, {}, now=NOW)
    packet = plan_packet(progress, config, NOW)
    assert packet is not None
    packet.report = report_for(packet, config)
    return progress, packet


def test_occurrences_stored_once_and_exact_report_is_frozen(tmp_path: Path) -> None:
    _, packet = completed_packet()
    article = packet.articles[0]
    sha = put_article(article, tmp_path)
    assert put_article(article, tmp_path) == sha
    assert read_article(sha, tmp_path) == article
    path = freeze_packet(packet, {"current_count": 2}, tmp_path)
    before = path.read_bytes()
    assert freeze_packet(replace(packet, handed_to_preparation=True), {"current_count": 999}, tmp_path) == path
    assert path.read_bytes() == before
    record = read_report_record(path.stem, tmp_path)
    assert set(record) == {"schema_version", "report_sha256", "packet_sha256", "report_bundle_id",
                           "summary", "packet", "sha256"}
    assert "articles" not in record["packet"]
    assert "candidate_accounting" not in record
    assert len(list((tmp_path / "candidate_sources").glob("*.json"))) == 2
    assert read_packet(path.stem, tmp_path) == packet


@pytest.mark.parametrize("kind", ["missing", "modified", "rehashed", "symlink"])
def test_report_direct_sources_fail_closed(tmp_path: Path, kind: str) -> None:
    _, packet = completed_packet()
    report_path = freeze_packet(packet, {}, tmp_path)
    article_path = tmp_path / "candidate_sources" / f"{digest(asdict(packet.articles[0]))}.json"
    if kind == "missing":
        article_path.unlink()
    elif kind == "symlink":
        original = article_path.with_suffix(".saved")
        article_path.rename(original)
        article_path.symlink_to(original)
    else:
        record = json.loads(article_path.read_text())
        record["article"]["description"] = "Changed metadata"
        if kind == "rehashed":
            record["sha256"] = digest({key: value for key, value in record.items() if key != "sha256"})
        article_path.write_text(json.dumps(record))
    with pytest.raises(ValueError):
        read_packet(report_path.stem, tmp_path)


def test_exclusion_index_is_reversible_and_unresolved_cache_is_not_receipt(tmp_path: Path) -> None:
    progress, _ = completed_packet()
    candidate = next(iter(progress.candidates.values()))
    candidate.eligible = False
    candidate.eligibility_reason = "current_blocklist"
    candidate.delivery_cache_observed_at = NOW.isoformat()
    save_candidate(candidate, (), tmp_path)
    assert list_excluded(tmp_path) == (candidate.identity,)
    assert load_candidate(candidate.identity, tmp_path) == candidate
    assert load_candidate_packets(candidate.identity, tmp_path) == ()
    candidate.eligible = True
    candidate.eligibility_reason = ""
    save_candidate(candidate, (), tmp_path)
    assert list_excluded(tmp_path) == ()
    assert len(list((tmp_path / "candidate_history").glob("*.json"))) == 2
    assert load_candidate(candidate.identity, tmp_path) == candidate


@pytest.mark.asyncio
async def test_terminal_index_requires_exact_capture_and_occurrence(tmp_path: Path) -> None:
    config, articles = population(2)
    progress = merge_candidates(CandidateProgress(), articles, config, {}, now=NOW)
    packet = plan_packet(progress, config, NOW)
    assert packet is not None
    capture = CandidateDispositionCapture()
    response = json.dumps({"selections": [], "limitations": ["No actionable metadata"], "dispositions": [
        {"evidence_id": item.evidence_id, "status": "not_selected", "reason": "No actionable detail."}
        for item in packet.evidence.items]})
    with patch("digest.review.complete", return_value=(response, {})):
        packet.report = await run_primary_review(articles, config, disposition_capture=capture)
    packet.disposition_attempts = tuple(capture.attempts)
    path = freeze_packet(packet, {}, tmp_path)
    attempt = capture.attempts[0]
    candidate = progress.candidates[attempt.dispositions[0].evidence_id]
    candidate.status = "not_selected"
    candidate.disposition = attempt.dispositions[0]
    candidate.decision_response_sha256 = attempt.response_sha256
    candidate.decision_prompt_hash = attempt.prompt_hash
    candidate.decision_occurrence_sha256 = digest(asdict(candidate.article))
    save_candidate(candidate, (path.stem,), tmp_path)
    assert load_candidate(candidate.identity, tmp_path) == candidate
    assert list_excluded(tmp_path) == ()
    with pytest.raises(ValueError, match="exact saved response"):
        save_candidate(candidate, (), tmp_path)
    candidate.decision_prompt_hash = "a" * 64
    with pytest.raises(ValueError, match="exact saved response"):
        save_candidate(candidate, (path.stem,), tmp_path)


def test_index_write_failure_leaves_old_state_readable(tmp_path: Path) -> None:
    progress, _ = completed_packet()
    candidate = next(iter(progress.candidates.values()))
    save_candidate(candidate, (), tmp_path)
    changed = replace(candidate, eligible=False, eligibility_reason="current_blocklist")
    from digest.candidate_storage import atomic_json_write

    def interrupt_index(path: Path, body: object) -> None:
        if path.parent.name == "candidate_index":
            raise OSError("Interrupted index replacement")
        atomic_json_write(path, body)

    with patch("digest.candidate_storage.atomic_json_write", side_effect=interrupt_index):
        with pytest.raises(OSError, match="Interrupted"):
            save_candidate(changed, (), tmp_path)
    assert load_candidate(candidate.identity, tmp_path) == candidate
    assert list_excluded(tmp_path) == ()


def test_policy_header_and_reference_paths_fail_closed(tmp_path: Path) -> None:
    assert read_policy(tmp_path) is None
    sha = digest({"blocklist": ["blocked"]})
    write_policy(sha, tmp_path)
    assert read_policy(tmp_path) == sha
    with pytest.raises(ValueError, match="reference"):
        read_article("../escape", tmp_path)
    with pytest.raises(ValueError, match="reference"):
        load_candidate("../escape", tmp_path)


def test_packet_archive_storage_grows_with_new_packets_not_all_prior_history(tmp_path: Path) -> None:
    sizes = []
    for window in range(20):
        config, articles = population(2)
        for article in articles["tech"]:
            article.title += f" window {window:02d}"
            article.link += f"/window-{window:02d}"
        progress = merge_candidates(CandidateProgress(), articles, config, {}, now=NOW)
        packet = plan_packet(progress, config, NOW)
        assert packet is not None
        packet.report = report_for(packet, config)
        path = freeze_packet(packet, {"current_count": 2}, tmp_path)
        sizes.append(path.stat().st_size)
    assert len(list((tmp_path / "candidate_reports").glob("*.json"))) == 20
    assert len(list((tmp_path / "candidate_sources").glob("*.json"))) == 40
    assert max(sizes) - min(sizes) < 100
    assert sum(sizes) < sizes[0] * 21


def test_planned_packet_roundtrip_preserves_unknown_work(tmp_path: Path) -> None:
    progress, packet = completed_packet()
    packet.report = None
    path = freeze_packet(packet, {}, tmp_path)
    assert read_packet(path.stem, tmp_path) == packet
    assert read_report_record(path.stem, tmp_path)["report_sha256"] is None
    candidate = next(iter(progress.candidates.values()))
    candidate.status = "technical_pending"
    candidate.eligible = False
    candidate.eligibility_reason = "source_disabled_or_changed"
    save_candidate(candidate, (path.stem,), tmp_path)
    assert load_candidate(candidate.identity, tmp_path) == candidate
    assert load_candidate_packets(candidate.identity, tmp_path) == (packet,)
    assert list_excluded(tmp_path) == (candidate.identity,)


def test_active_codec_writes_only_shared_sources(tmp_path: Path) -> None:
    progress, _ = completed_packet()
    candidate = next(iter(progress.candidates.values()))
    candidate.occurrences = (replace(candidate.article, description="Updated occurrence"),)
    raw = encode_active_candidate(candidate, tmp_path)
    assert set(raw) == {"candidate", "article_ref", "occurrence_refs"}
    assert "article" not in raw["candidate"]
    assert "occurrences" not in raw["candidate"]
    assert decode_active_candidate(raw, tmp_path) == candidate
    assert {path.name for path in tmp_path.iterdir()} == {"candidate_sources"}
    raw["candidate"]["identity"] = "a" * 32
    with pytest.raises(ValueError, match="identity"):
        decode_active_candidate(raw, tmp_path)


def test_latest_header_never_expands_sources_or_reports(tmp_path: Path) -> None:
    progress, _ = completed_packet()
    candidate = next(iter(progress.candidates.values()))
    save_candidate(candidate, (), tmp_path)
    with patch("digest.candidate_storage.read_article", side_effect=AssertionError("Unexpected source read")):
        header = read_candidate_header(candidate.identity, tmp_path)
    assert header is not None and header["status"] == "not_presented"
    assert header["identity"] == candidate.identity
    assert "article" not in header


def test_policy_headers_include_alternates_and_bind_full_source(tmp_path: Path) -> None:
    progress, _ = completed_packet()
    candidate = next(iter(progress.candidates.values()))
    alternate = replace(candidate.article, source="B", source_url="https://b.example/feed", published=None)
    candidate.occurrences = (alternate,)
    path = save_candidate(candidate, (), tmp_path)
    header = read_candidate_header(candidate.identity, tmp_path)
    assert header is not None
    assert header["policy_fields"] == [
        {"source": "A", "source_url": "https://a.example/feed", "category": "tech", "published": NOW.isoformat()},
        {"source": "B", "source_url": "https://b.example/feed", "category": "tech", "published": None},
    ]
    record = json.loads(path.read_text())
    record["policy_fields"][1]["source"] = "Invented source"
    record["sha256"] = digest({key: value for key, value in record.items() if key != "sha256"})
    path.write_text(json.dumps(record))
    with pytest.raises(ValueError, match="policy fields differ"):
        load_candidate(candidate.identity, tmp_path)


def test_policy_boolean_schema_is_not_version_one(tmp_path: Path) -> None:
    path = write_policy("a" * 64, tmp_path)
    record = json.loads(path.read_text())
    record["schema_version"] = True
    record["sha256"] = digest({key: value for key, value in record.items() if key != "sha256"})
    path.write_text(json.dumps(record))
    with pytest.raises(ValueError, match="policy header"):
        read_policy(tmp_path)


def test_active_checkpoint_switches_to_exact_packet_reference_after_freeze(tmp_path: Path) -> None:
    from digest.candidate_review import load_candidate_progress, save_candidate_progress

    progress, packet = completed_packet()
    progress.packets.append(packet)
    path = save_candidate_progress(progress, tmp_path)
    raw = json.loads(path.read_text())["candidate_accounting"]
    assert raw["kind"] == "candidate_working_set"
    assert "packet" in raw["packets"][0]
    assert load_candidate_progress(tmp_path) == progress
    freeze_packet(packet, {}, tmp_path)
    save_candidate_progress(progress, tmp_path)
    raw = json.loads(path.read_text())["candidate_accounting"]
    assert "packet_ref" in raw["packets"][0]
    assert load_candidate_progress(tmp_path) == progress


def test_report_sidecar_uses_immutable_sources_without_active_root(tmp_path: Path) -> None:
    from digest.candidate_review import archive_candidate_accounting, candidate_accounting_sources

    _, packet = completed_packet()
    assert packet.report is not None
    report = packet.report
    assert archive_candidate_accounting(report, tmp_path / "legacy.json", tmp_path) is None
    freeze_packet(packet, {}, tmp_path)
    sidecar = archive_candidate_accounting(report, tmp_path / "accepted.json", tmp_path)
    assert sidecar is not None
    before = sidecar.read_bytes()
    sources = candidate_accounting_sources(report, tmp_path)
    assert len(sources) == len(packet.articles)
    assert all(path.exists() for path in sources)
    assert archive_candidate_accounting(report, tmp_path / "accepted.json", tmp_path) == sidecar
    assert sidecar.read_bytes() == before
    sources[0].unlink()
    with pytest.raises(ValueError, match="Missing candidate evidence"):
        candidate_accounting_sources(report, tmp_path)


def test_active_root_rejects_undeployed_prototype_schema(tmp_path: Path) -> None:
    from digest.candidate_review import CANDIDATE_FILE, load_candidate_progress

    old_body = {"schema_version": 1, "candidates": {}, "packets": [], "latest_collection_json": "{}"}
    (tmp_path / CANDIDATE_FILE).write_text(json.dumps({"candidate_accounting": old_body, "sha256": digest(old_body)}))
    with pytest.raises(ValueError, match="Unsupported candidate prototype"):
        load_candidate_progress(tmp_path)


def test_preflight_charges_current_refs_not_retained_collection_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from copy import deepcopy

    from digest import candidate_review as review
    from digest.radar.collector import CollectionInventory

    config, articles = population(1)
    progress = merge_candidates(CandidateProgress(), articles, config, {}, now=NOW)
    old = plan_packet(progress, config, NOW)
    assert old is not None
    identity = old.evidence.items[0].evidence_id
    observation = {
        "identity": identity,
        "article_reference": {"identity": identity, "occurrence_sha256": digest(asdict(old.articles[0]))},
        "exclusion_reasons": [], "effective_priority": 3, "repeated_of": 0, "source_names": ["A"],
    }
    # A bounded synthetic old collection isolates the admission calculation;
    # this is not an estimate of ordinary feed arrivals.
    progress.latest_collection_json = json.dumps(
        {"observations": [observation] * 5000, "sources": []}, sort_keys=True)
    old.collection_json = progress.latest_collection_json
    review.begin_packet(progress, old, tmp_path)
    report = report_for(old, config, "invalid")
    review.reconcile_packet(progress, old, report, config, tmp_path)
    progress = review.load_candidate_progress(tmp_path)

    _, fresh = population(1)
    fresh["tech"][0].title = "Fresh"
    fresh["tech"][0].link = "https://a.example/new"
    config.review.max_evidence_articles = 1
    merge_candidates(progress, fresh, config, {}, now=NOW,
                     inventory=CollectionInventory(), cache_dir=tmp_path)
    new = plan_packet(progress, config, NOW)
    assert new is not None and new.evidence.items[0].title == "Fresh"
    preview = deepcopy(progress)
    preview.packets.append(new)
    preview.candidates[new.evidence.items[0].evidence_id].status = "technical_pending"
    current_bytes = review.progress_size(preview, tmp_path)
    limit = current_bytes + review.RESPONSE_STORAGE_RESERVE + 512
    assert review.progress_size(preview) + review.RESPONSE_STORAGE_RESERVE > limit

    monkeypatch.setattr(review, "MAX_BYTES", limit)
    review.begin_packet(progress, new, tmp_path)
    restored = review.load_candidate_progress(tmp_path)
    assert len(restored.packets) == 2
    assert restored.packets[0].report == report
    assert restored.candidates[identity].status == "technical_pending"
    assert review.progress_size(restored, tmp_path) + review.RESPONSE_STORAGE_RESERVE <= limit


def measured_candidate_window(
    cache_dir: Path, window: int, count: int = 20, *, same_identity: bool = False,
) -> dict[str, int]:
    """Exercise the actual offline lifecycle and measure all persisted objects."""
    from datetime import timedelta

    from digest.candidate_dispositions import capture_review_dispositions
    from digest.candidate_review import (
        CANDIDATE_FILE,
        begin_packet,
        load_candidate_progress,
        mark_prepared,
        reconcile_packet,
    )

    config, articles = population(count)
    for article in articles["tech"]:
        if not same_identity:
            article.title += f" window {window:02d}"
            article.link += f"/window-{window:02d}"
        excerpt = f"Evidence revision {window:02d} " if same_identity else "Evidence details "
        article.description = excerpt.ljust(540, "x")
    now = NOW + timedelta(hours=window)
    progress = load_candidate_progress(cache_dir)
    merge_candidates(progress, articles, config, {}, now=now, cache_dir=cache_dir)
    packet = plan_packet(progress, config, now)
    assert packet is not None and len(packet.evidence.items) == count
    begin_packet(progress, packet, cache_dir)
    report = report_for(packet, config, "abstained")
    raw = json.dumps({"selections": [], "limitations": ["RSS evidence only"], "dispositions": [
        {"evidence_id": item.evidence_id, "status": "not_selected", "reason": "No actionable technical detail."}
        for item in packet.evidence.items]})
    import hashlib

    report.reviews[0].response_sha256 = hashlib.sha256(raw.encode()).hexdigest()
    capture = CandidateDispositionCapture([capture_review_dispositions(packet.evidence, report.reviews[0], raw)])
    reconcile_packet(progress, packet, report, config, cache_dir, disposition_capture=capture)
    report_key = digest(asdict(report))
    frozen = read_packet(report_key, cache_dir)
    assert frozen.report == report and frozen.articles == packet.articles
    identities = tuple(progress.candidates)
    mark_prepared(progress, packet.evidence.bundle_id, cache_dir)
    restored = load_candidate_progress(cache_dir)
    assert restored.candidates == {} and restored.packets == []
    for identity in identities:
        candidate = load_candidate(identity, cache_dir)
        assert candidate is not None and candidate.status == "not_selected"
        assert load_candidate_packets(identity, cache_dir) == (replace(frozen, handed_to_preparation=True),)
    files = list(cache_dir.rglob("*.json"))
    row = {"window": window + 1, "active_candidates": len(restored.candidates),
           "active_packets": len(restored.packets),
           "active_checkpoint_bytes": (cache_dir / CANDIDATE_FILE).stat().st_size,
           "latest_report_bytes": (cache_dir / "candidate_reports" / f"{report_key}.json").stat().st_size,
           "all_persisted_bytes": sum(path.stat().st_size for path in files)}
    for directory in ("candidate_reports", "candidate_sources", "candidate_index", "candidate_history"):
        objects = list((cache_dir / directory).glob("*.json"))
        row[f"{directory}_count"] = len(objects)
        row[f"{directory}_bytes"] = sum(path.stat().st_size for path in objects)
    return row


def test_integrated_twenty_windows_retire_active_work_without_recursive_history(tmp_path: Path) -> None:
    rows = [measured_candidate_window(tmp_path, window) for window in range(20)]
    assert {row["active_checkpoint_bytes"] for row in rows} == {rows[0]["active_checkpoint_bytes"]}
    assert max(row["latest_report_bytes"] for row in rows) - min(row["latest_report_bytes"] for row in rows) < 100
    assert rows[-1]["candidate_reports_count"] == 20
    assert rows[-1]["candidate_sources_count"] == rows[-1]["candidate_index_count"] == 400
    assert rows[-1]["candidate_history_count"] == 400
    # This finite fixed-width population has constant per-window storage cost.
    # The assertion includes sources, historical states and latest-state indexes.
    increments = [later["all_persisted_bytes"] - earlier["all_persisted_bytes"]
                  for earlier, later in zip(rows, rows[1:], strict=False)]
    assert max(increments) - min(increments) < 100
    assert rows[-1]["all_persisted_bytes"] <= rows[0]["all_persisted_bytes"] * 20


def test_same_identity_revisions_keep_latest_index_proof_bounded(tmp_path: Path) -> None:
    rows = []
    index_sizes = []
    report_keys = []
    for window in range(8):
        rows.append(measured_candidate_window(tmp_path, window, 1, same_identity=True))
        index = next((tmp_path / "candidate_index").glob("*.json"))
        state = json.loads(index.read_text())
        assert len(state["report_refs"]) == 1 and state["occurrence_refs"] == []
        index_sizes.append(index.stat().st_size)
        report_keys.append(state["report_refs"][0])
        candidate = load_candidate(index.stem, tmp_path)
        assert candidate is not None and candidate.article.description.startswith(f"Evidence revision {window:02d}")
        assert candidate.first_observed_at == NOW.isoformat()
    assert max(index_sizes) - min(index_sizes) < 100
    assert rows[-1]["candidate_index_count"] == 1
    assert rows[-1]["candidate_reports_count"] == rows[-1]["candidate_sources_count"] == 8
    assert rows[-1]["candidate_history_count"] == 8
    assert len(set(report_keys)) == 8
    for window, key in enumerate(report_keys):
        packet = read_packet(key, tmp_path)
        assert packet.articles[0].description.startswith(f"Evidence revision {window:02d}")
        assert packet.disposition_attempts[0].dispositions[0].status == "not_selected"
    for path in (tmp_path / "candidate_history").glob("*.json"):
        state = json.loads(path.read_text())
        assert len(state["report_refs"]) == 1 and state["occurrence_refs"] == []


def test_packet_collection_retains_excluded_source_references_after_active_retirement(tmp_path: Path) -> None:
    import hashlib

    from digest.candidate_dispositions import capture_review_dispositions
    from digest.candidate_review import (
        archive_candidate_accounting,
        begin_packet,
        candidate_accounting_sources,
        load_candidate_progress,
        mark_prepared,
        reconcile_packet,
        save_candidate_progress,
    )
    from digest.radar.collector import CandidateObservation, CollectionInventory, SourceCollectionOutcome, article_hash

    config, articles = population(2)
    eligible, excluded = articles["tech"]
    inventory = CollectionInventory(
        observations=[CandidateObservation(eligible, article_hash(eligible.title, eligible.link), 3, ()),
                      CandidateObservation(excluded, article_hash(excluded.title, excluded.link), 3, ("blocklist",))],
        sources=[SourceCollectionOutcome("A", config.sources[0].url, "tech", 3, True, 2)],
    )
    progress = merge_candidates(CandidateProgress(), {"tech": [eligible]}, config, {}, now=NOW, inventory=inventory)
    packet = plan_packet(progress, config, NOW)
    assert packet is not None and len(packet.evidence.items) == 1
    begin_packet(progress, packet, tmp_path)
    report = report_for(packet, config, "abstained")
    raw = json.dumps({"selections": [], "dispositions": [{"evidence_id": packet.evidence.items[0].evidence_id,
                       "status": "not_selected", "reason": "No actionable metadata."}]})
    report.reviews[0].response_sha256 = hashlib.sha256(raw.encode()).hexdigest()
    capture = CandidateDispositionCapture([capture_review_dispositions(packet.evidence, report.reviews[0], raw)])
    reconcile_packet(progress, packet, report, config, tmp_path, disposition_capture=capture)
    record = read_report_record(digest(asdict(report)), tmp_path)
    collection = json.loads(record["packet"]["collection_json"])
    assert record["summary"]["current_collection"] == collection
    assert len(collection["observations"]) == 2
    saved_excluded = collection["observations"][1]
    assert saved_excluded["exclusion_reasons"] == ["blocklist"] and "article" not in saved_excluded
    excluded_ref = saved_excluded["article_reference"]["occurrence_sha256"]
    assert read_article(excluded_ref, tmp_path).article() == excluded
    assert excluded_ref not in record["packet"]["article_refs"]
    mark_prepared(progress, report.evidence.bundle_id, tmp_path)
    assert load_candidate_progress(tmp_path).candidates == {}
    progress.latest_collection_json = "{}"
    save_candidate_progress(progress, tmp_path)
    assert read_report_record(digest(asdict(report)), tmp_path) == record
    sources = candidate_accounting_sources(report, tmp_path)
    assert len(sources) == 2 and any(path.stem == excluded_ref for path in sources)
    sidecar = archive_candidate_accounting(report, tmp_path / "accepted.json", tmp_path)
    assert sidecar is not None and json.loads(sidecar.read_text()) == record
    (tmp_path / "candidate_sources" / f"{excluded_ref}.json").unlink()
    with pytest.raises(ValueError, match="Missing candidate evidence"):
        candidate_accounting_sources(report, tmp_path)


def test_legacy_index_without_handoff_provenance_does_not_invent_acceptance(tmp_path: Path) -> None:
    progress, packet = completed_packet()
    assert packet.report is not None
    freeze_packet(packet, {}, tmp_path)
    candidate = next(iter(progress.candidates.values()))
    key = digest(asdict(packet.report))
    save_candidate(candidate, (key,), tmp_path, report_handoffs={key: True})
    path = tmp_path / "candidate_index" / f"{candidate.identity}.json"
    raw = json.loads(path.read_text())
    raw.pop("report_handoffs")
    raw["sha256"] = digest({key: value for key, value in raw.items() if key != "sha256"})
    path.write_text(json.dumps(raw))
    assert load_candidate_packets(candidate.identity, tmp_path)[0].handed_to_preparation is False
