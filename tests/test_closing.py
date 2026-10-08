"""Optional same-response closing contract; all model calls are offline doubles."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from digest.adapters.models.execution import ModelExecution
from digest.adapters.storage.candidate_progress import load_candidate_progress
from digest.application.candidate_review import begin_packet, merge_candidates, plan_packet
from digest.application.preparation import CandidateWork, _preparation_closing, _review_candidates
from digest.application.review import run_primary_review
from digest.application.review_request import build_evidence_bundle, build_review_messages
from digest.closing import (
    ClosingDecision,
    attribute_closing_card,
    decide_closing,
    eligible_ids,
    load_closing,
    save_closing,
)
from digest.config import ClosingConfig, ClosingSourceBinding, Config, SourceConfig, load_config
from digest.domain.editorial.attempts import restore_review
from digest.domain.editorial.candidate_policy import pending_completed_report
from digest.domain.editorial.candidates import CandidateProgress
from digest.preparation import PreparationSnapshot, _canonical, load_preparation, save_preparation
from digest.presentation.review import primary_cards
from digest.radar.collector import Article
from scripts.review_fixture import fixture_config
from tests.test_config import MINIMAL_CONFIG, _write_config

NOW = datetime(2026, 10, 6, 12, tzinfo=UTC)

NHS_FEED = "https://www.england.nhs.uk/feed/"
NHS_CREDIT = (
    "NHS England RSS feeds. Open Government Licence v3.0: "
    "https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/"
)
EA_FEED = "https://www.gov.uk/search/news-and-communications.atom?organisations%5B%5D=environment-agency"
EA_CREDIT = (
    "Contains public sector information licensed under the Open Government Licence v3.0. "
    "https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/"
)


def population() -> tuple[Config, dict[str, list[Article]]]:
    config = fixture_config()
    config.review.review_led_only = True
    config.review.max_selections = 2
    config.telegram.delivery_mode = "compact"
    config.sources = [SourceConfig("Community", "https://example.com/feed", "Society", True)]
    config.closing = ClosingConfig(True, (ClosingSourceBinding("Community", "https://example.com/feed", "Society"),))
    articles = {"Society": [
        Article(f"Item {index}", f"https://example.com/{index}", "Neighbours restored public access",
                "Community", "Society", NOW) for index in range(4)
    ]}
    return config, articles


def response(messages: list[dict[str, str]], closing: object = "first") -> str:
    items = json.loads(messages[1]["content"])["evidence"]["items"]
    raw: dict[str, Any] = {
        "selections": [{"evidence_id": item["evidence_id"], "reason": "Neighbours restored public access.",
                        "quote": item["excerpt"], "confidence": "high"} for item in items],
        "limitations": ["Synthetic evidence only"],
        "dispositions": [{"evidence_id": item["evidence_id"], "status": "selected"} for item in items],
    }
    if closing == "first":
        raw["closing"] = {"schema_version": 1, "evidence_id": items[0]["evidence_id"]}
    elif closing != "missing":
        raw["closing"] = closing
    return json.dumps(raw)


@pytest.mark.asyncio
@pytest.mark.parametrize("feed,credit", [
    (NHS_FEED, NHS_CREDIT), (EA_FEED, EA_CREDIT),
    ("https://www.england.nhs.uk/feed", None),
    ("https://www.england.nhs.uk/another-feed/", None),
    (EA_FEED.replace("%5B%5D", "[]"), None),
])
async def test_credit_uses_exact_frozen_feed_and_preserves_canonical_evidence_and_identity(
    monkeypatch: pytest.MonkeyPatch, feed: str, credit: str | None,
) -> None:
    execution = ModelExecution()
    config, articles = population()
    config.sources[0].url = feed
    config.closing = replace(config.closing, approved_sources=(ClosingSourceBinding("Community", feed, "Society"),))
    # A known article hostname cannot substitute for the immutable feed binding.
    articles["Society"] = [replace(item, link=f"https://www.england.nhs.uk/news/{index}")
                           for index, item in enumerate(articles["Society"])]
    packet = plan_packet(merge_candidates(CandidateProgress(), articles, config, {}, now=NOW), config, NOW)
    assert packet is not None
    completion = AsyncMock(side_effect=lambda role, messages, *args, **kwargs: (response(messages), {}))
    monkeypatch.setattr("digest.application.review.complete", completion)
    result = await run_primary_review(articles, config, execution=execution)
    report = result.report
    decision = decide_closing(result, packet, config.closing, config.sources)
    assert decision.card is not None and decision.provenance is not None
    canonical = _canonical((asdict(decision), asdict(report), asdict(packet)))
    config.sources[0].url = "https://changed.example/feed"
    for summary in (decision.card.summary, "Соседи восстановили доступ."):
        presented = replace(decision.card, summary=summary)
        attributed = attribute_closing_card(decision, presented)
        if credit is None:
            assert attributed is None
        else:
            assert attributed == replace(presented, summary=f"{summary} {credit}")
            assert attributed is not presented and "\n" not in attributed.summary
            for field in ("title", "link", "source", "category"):
                assert attribute_closing_card(decision, replace(presented, **{field: "changed"})) is None
            tampered = replace(decision.provenance, occurrence_sha256="0" * 64)
            assert attribute_closing_card(replace(decision, provenance=tampered), presented) is None
            occurrence = replace(decision.provenance.occurrence, source="Different source")
            tampered = replace(decision.provenance, occurrence=occurrence,
                               occurrence_sha256=hashlib.sha256(_canonical(asdict(occurrence))).hexdigest())
            assert attribute_closing_card(replace(decision, provenance=tampered), presented) is None
    assert _canonical((asdict(decision), asdict(report), asdict(packet))) == canonical
    completion.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("designation", [[], 1, {"schema_version": 1, "evidence_id": "unknown"},
                                          {"schema_version": True, "evidence_id": None}, "missing"])
async def test_bad_optional_designation_preserves_all_main_selections(
    monkeypatch: pytest.MonkeyPatch, designation: object,
) -> None:
    execution = ModelExecution()
    config, articles = population()
    complete = AsyncMock(side_effect=lambda role, messages, *args, **kwargs: (response(messages, designation), {}))
    monkeypatch.setattr("digest.application.review.complete", complete)
    result = await run_primary_review(articles, config, execution=execution)
    report = result.report
    assert complete.await_count == 1
    assert report.reviews[0].status == "ok" and len(report.reviews[0].selections) == 4
    assert result.attempts[0].closing.status == "incomplete"
    assert len(primary_cards(result, articles, "en", max_cards=2)) == 2


@pytest.mark.asyncio
async def test_capture_uses_exact_fallback_and_filters_before_unchanged_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution = ModelExecution()
    config, articles = population()
    progress = merge_candidates(CandidateProgress(), articles, config, {}, now=NOW)
    packet = plan_packet(progress, config, NOW)
    assert packet is not None
    begin_packet(progress, packet, tmp_path)
    count = 0

    async def complete(role: Any, messages: list[dict[str, str]], *args: Any, **kwargs: Any) -> tuple[str, dict]:
        nonlocal count
        count += 1
        if count == 1:
            # Even a syntactically valid optional ID in failed primary output cannot be borrowed.
            raw = json.loads(response(messages))
            raw["selections"] = "invalid"
            return json.dumps(raw), {}
        raw = json.loads(response(messages))
        raw["closing"]["evidence_id"] = raw["selections"][1]["evidence_id"]
        return json.dumps(raw), {}

    monkeypatch.setattr("digest.application.review.complete", complete)
    reviewed = await _review_candidates(CandidateWork(progress, packet), articles, config, str(tmp_path),
        execution=execution)
    cards, report = reviewed.cards, reviewed.report
    assert report is not None and count == 2
    main, decision = _preparation_closing(cards, restore_review(report), articles, config, str(tmp_path))
    assert decision is not None and decision.status == "selected" and decision.provenance is not None
    assert decision.provenance.slot == "secondary"
    assert decision.provenance.response_sha256 == report.reviews[1].response_sha256
    assert decision.provenance.prompt_hash == packet.prompt_hash
    assert decision.provenance.occurrence.source_url == config.sources[0].url
    assert len(main) == config.review.max_selections == 2
    assert decision.card not in main
    assert [card.link for card in main] == ["https://example.com/0", "https://example.com/2"]
    restored = load_candidate_progress(tmp_path)
    assert pending_completed_report(restored) == report
    report_bytes = _canonical(asdict(report))
    replayed = await _review_candidates(
        CandidateWork(restored, restored.packets[0]), articles, config, str(tmp_path),
        execution=execution,
    )
    recovered, reused = replayed.cards, replayed.report
    assert count == 2 and reused == report
    assert _preparation_closing(recovered, restore_review(report), articles, config, str(tmp_path)) == (main, decision)
    assert _canonical(asdict(report)) == report_bytes
    assert "closing" not in asdict(packet) and "closing" not in asdict(report.reviews[1])


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing", "corrupt", "write"])
async def test_optional_capture_failure_keeps_completed_main_without_reselection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    execution = ModelExecution()
    config, articles = population()
    progress = merge_candidates(CandidateProgress(), articles, config, {}, now=NOW)
    packet = plan_packet(progress, config, NOW)
    assert packet is not None
    begin_packet(progress, packet, tmp_path)
    complete = AsyncMock(side_effect=lambda role, messages, *args, **kwargs: (response(messages), {}))
    monkeypatch.setattr("digest.application.review.complete", complete)
    if failure == "write":
        monkeypatch.setattr("digest.closing.save_closing", lambda *args: (_ for _ in ()).throw(OSError("disk")))
    reviewed = await _review_candidates(CandidateWork(progress, packet), articles, config, str(tmp_path),
        execution=execution)
    cards, report = reviewed.cards, reviewed.report
    assert report is not None
    for path in (tmp_path / "closing_decisions").glob("*.json"):
        if failure == "missing":
            path.unlink()
        elif failure == "corrupt":
            path.write_text("{bad")
    main, decision = _preparation_closing(cards, restore_review(report), articles, config, str(tmp_path))
    assert main == cards and decision is not None and decision.status == "incomplete"
    restored = load_candidate_progress(tmp_path)
    assert pending_completed_report(restored) == report
    await _review_candidates(CandidateWork(restored, restored.packets[0]), articles, config, str(tmp_path),
        execution=execution)
    assert complete.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("source_change", ["disabled", "missing", "changed_url", "ambiguous"])
async def test_unavailable_feed_binding_cannot_designate_or_remove_main(
    monkeypatch: pytest.MonkeyPatch, source_change: str,
) -> None:
    execution = ModelExecution()
    config, articles = population()
    packet = plan_packet(merge_candidates(CandidateProgress(), articles, config, {}, now=NOW), config, NOW)
    assert packet is not None
    if source_change == "disabled":
        config.sources[0].enabled = False
    elif source_change == "missing":
        config.sources = []
    elif source_change == "changed_url":
        config.sources[0].url += "changed"
    else:
        config.sources.append(replace(config.sources[0], url="https://other.example/feed"))
    monkeypatch.setattr("digest.application.review.complete", AsyncMock(
        side_effect=lambda role, messages, *args, **kwargs: (response(messages), {})))
    result = await run_primary_review(articles, config, execution=execution)
    report = result.report
    assert eligible_ids(report.evidence, config.closing, config.sources) == []
    decision = decide_closing(result, packet, config.closing, config.sources)
    assert decision.status == "unavailable" and len(primary_cards(result, articles, "en", max_cards=2)) == 2


@pytest.mark.asyncio
async def test_terminal_v2_selected_and_omitted_roundtrip_and_strict_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution = ModelExecution()
    config, articles = population()
    packet = plan_packet(merge_candidates(CandidateProgress(), articles, config, {}, now=NOW), config, NOW)
    assert packet is not None
    monkeypatch.setattr("digest.application.review.complete", AsyncMock(
        side_effect=lambda role, messages, *args, **kwargs: (response(messages), {})))
    result = await run_primary_review(articles, config, execution=execution)
    report = result.report
    decision = decide_closing(result, packet, config.closing, config.sources)
    save_closing(decision, report, tmp_path)
    cards, _ = _preparation_closing(primary_cards(result, articles, "en"), result, articles, config, str(tmp_path))
    snapshot = PreparationSnapshot(cards, [], "Notice", report, 1, 4, ["Community"], decision)
    path = save_preparation(snapshot, tmp_path, NOW)
    assert json.loads(path.read_text())["schema_version"] == 2
    assert load_preparation(tmp_path, NOW) == snapshot
    record = json.loads(path.read_text())
    record["snapshot"]["closing"]["provenance"]["response_sha256"] = "0" * 64
    body = {key: value for key, value in record.items() if key != "sha256"}
    record["sha256"] = hashlib.sha256(_canonical(body)).hexdigest()
    path.write_text(json.dumps(record))
    with pytest.raises(ValueError, match="Invalid preparation"):
        load_preparation(tmp_path, NOW)
    path.unlink()
    omitted = replace(snapshot, closing=ClosingDecision("unavailable", "no_suitable_item_in_packet"))
    save_preparation(omitted, tmp_path, NOW)
    assert load_preparation(tmp_path, NOW) == omitted


def test_legacy_disabled_wire_shape_and_prompt_are_unchanged(tmp_path: Path) -> None:
    config, articles = population()
    bundle = build_evidence_bundle(articles, config.review)
    assert build_review_messages(bundle, config.review, "en", sources=config.sources) == build_review_messages(
        bundle, config.review, "en", sources=config.sources, closing=ClosingConfig())
    snapshot = PreparationSnapshot([], [], "exact legacy", None, 0, 0, [])
    path = save_preparation(snapshot, tmp_path, NOW)
    record = json.loads(path.read_text())
    payload = asdict(snapshot)
    payload.pop("closing")
    body = {"schema_version": 1, "utc_date": NOW.date().isoformat(), "created_at": NOW.isoformat(),
            "publication_date": NOW.date().isoformat(), "snapshot": payload}
    expected = {**body, "sha256": hashlib.sha256(_canonical(body)).hexdigest()}
    assert record == expected
    original = path.read_bytes()
    assert load_preparation(tmp_path, NOW) == snapshot
    assert path.read_bytes() == original
    assert save_preparation(snapshot, tmp_path, NOW).read_bytes() == original


def test_closing_default_disabled_and_stale_binding_does_not_block_config(tmp_path: Path) -> None:
    path = _write_config(tmp_path, MINIMAL_CONFIG)
    assert not load_config(path).closing.enabled
    text = Path(path).read_text() + '''
review:
  enabled: true
  review_led_only: true
  max_detailed_selections: 6
telegram:
  delivery_mode: compact
closing:
  enabled: true
  approved_sources:
    - name: Absent Feed
      url: https://missing.example/feed
      category: Community
'''
    Path(path).write_text(text)
    config = load_config(path)
    assert config.closing.enabled and len(config.sources) == 1 and config.sources[0].name == "Test Feed"
    assert eligible_ids(build_evidence_bundle({}, config.review), config.closing, config.sources) == []


@pytest.mark.asyncio
async def test_explicit_abstention_is_persisted_without_inventing_a_story(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution = ModelExecution()
    config, articles = population()
    monkeypatch.setattr(
        "digest.application.review.complete", AsyncMock(side_effect=lambda role, messages, *args, **kwargs: (
        response(messages, {"schema_version": 1, "evidence_id": None}), {})))
    result = await run_primary_review(articles, config, execution=execution)
    report = result.report
    packet = plan_packet(merge_candidates(CandidateProgress(), articles, config, {}, now=NOW), config, NOW)
    assert packet is not None
    decision = decide_closing(result, packet, config.closing, config.sources)
    assert decision == ClosingDecision("unavailable", "no_suitable_item_in_packet")
    save_closing(decision, report, tmp_path)
    assert load_closing(report, tmp_path) == decision


@pytest.mark.asyncio
async def test_fresh_handoff_rechecks_allowlist_but_accepted_snapshot_does_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution = ModelExecution()
    config, articles = population()
    progress = merge_candidates(CandidateProgress(), articles, config, {}, now=NOW)
    packet = plan_packet(progress, config, NOW)
    assert packet is not None
    begin_packet(progress, packet, tmp_path)
    complete = AsyncMock(side_effect=lambda role, messages, *args, **kwargs: (response(messages), {}))
    monkeypatch.setattr("digest.application.review.complete", complete)
    reviewed = await _review_candidates(CandidateWork(progress, packet), articles, config, str(tmp_path),
        execution=execution)
    cards, report = reviewed.cards, reviewed.report
    assert report is not None
    main, selected = _preparation_closing(cards, restore_review(report), articles, config, str(tmp_path))
    assert selected is not None and selected.status == "selected"
    snapshot = PreparationSnapshot(main, [], "Notice", report, 1, 4, ["Community"], selected)
    save_preparation(snapshot, tmp_path, NOW)
    config.closing = ClosingConfig(True, (ClosingSourceBinding("Other", "https://other.example/feed", "Other"),))
    unchanged, omitted = _preparation_closing(cards, restore_review(report), articles, config, str(tmp_path))
    assert unchanged == cards
    assert omitted == ClosingDecision("unavailable", "source_no_longer_eligible_for_closing")
    assert load_preparation(tmp_path, NOW) == snapshot
    assert complete.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("conflict", ["closing_field", "designation_id", "selected_id"])
async def test_conflicting_optional_or_selected_identity_omits_closing_only(
    monkeypatch: pytest.MonkeyPatch, conflict: str,
) -> None:
    execution = ModelExecution()
    config, articles = population()

    async def complete(role: Any, messages: list[dict[str, str]], *args: Any, **kwargs: Any) -> tuple[str, dict]:
        raw = json.loads(response(messages))
        if conflict == "selected_id":
            raw["selections"].pop()
            raw["selections"].append(raw["selections"][0])
            return json.dumps(raw), {}
        text = json.dumps(raw)
        if conflict == "closing_field":
            return text[:-1] + ', "closing": {"schema_version": 1, "evidence_id": null}}', {}
        designation = json.dumps(raw["closing"])
        return text.replace(designation, designation[:-1] + ', "evidence_id": null}'), {}

    monkeypatch.setattr("digest.application.review.complete", complete)
    result = await run_primary_review(articles, config, execution=execution)
    report = result.report
    assert report.reviews[0].status in {"ok", "partial"}
    assert len(primary_cards(result, articles, "en", max_cards=2)) == 2
    assert result.attempts[0].closing.status == "incomplete"


@pytest.mark.asyncio
async def test_sole_selected_story_remains_main_and_freezes_without_reselection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution = ModelExecution()
    from digest.delivery.edition import READY_FILE
    from digest.edition_runtime import accept_preparation, preparation_stats, present_preparation
    from digest.preparation import AcceptedPreparation

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    config, articles = population()
    articles["Society"] = articles["Society"][:1]
    progress = merge_candidates(CandidateProgress(), articles, config, {}, now=NOW)
    packet = plan_packet(progress, config, NOW)
    assert packet is not None
    begin_packet(progress, packet, ".cache")
    complete = AsyncMock(side_effect=lambda role, messages, *args, **kwargs: (response(messages), {}))
    monkeypatch.setattr("digest.application.review.complete", complete)
    reviewed = await _review_candidates(CandidateWork(progress, packet), articles, config, ".cache",
        execution=execution)
    cards, report = reviewed.cards, reviewed.report
    assert report is not None
    main, decision = _preparation_closing(cards, restore_review(report), articles, config, ".cache")
    assert main == cards and len(main) == 1
    assert decision == ClosingDecision("unavailable", "closing_would_empty_main_selection")
    snapshot = PreparationSnapshot(main, [], "Notice", report, 1, 1, ["Community"], decision)
    accepted = accept_preparation(snapshot, reviewed.result, cache_dir=".cache")
    assert isinstance(accepted, AcceptedPreparation) and accepted.snapshot == snapshot
    stats = preparation_stats(await present_preparation(accepted, config, execution=execution))
    assert stats.edition_status == "ready"
    manifest = json.loads(Path(".cache", READY_FILE).read_text())
    assert len(manifest["presentation_metadata"]["cards"]) == 1
    assert manifest["canonical_metadata"]["closing"]["status"] == "unavailable"
    assert complete.await_count == 1
