"""Offline resume tests: archived evidence and successful reviews are immutable inputs."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import FrozenInstanceError, asdict, replace
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from digest.config import Config, ReviewModelConfig
from digest.review import BlindReviewReport, EvidenceBundle, ModelReview, primary_cards, run_blind_review
from digest.review_checkpoint import FullSourceEvidence
from scripts.review_fixture import fixture_articles, fixture_config, fixture_response


@pytest.fixture(autouse=True)
def no_live_http() -> Any:
    with patch("httpx.AsyncClient", side_effect=AssertionError("Live HTTP is forbidden in resume tests")):
        yield


def _trial_config() -> Config:
    config = fixture_config()
    config.review.max_evidence_articles = 10
    config.review.max_excerpt_chars = 400
    config.review.max_selections = 3
    return config


async def _report(config: Config) -> BlindReviewReport:
    with patch("digest.review.complete", side_effect=fixture_response):
        return await run_blind_review(fixture_articles(), config)


def _archive(path: Path, report: BlindReviewReport) -> dict[str, Any]:
    payload = asdict(report)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return payload


def _review_content(review: ModelReview) -> dict[str, Any]:
    payload = asdict(review)
    payload.pop("generated_at")
    payload.pop("attempted_at")
    payload.pop("reused_from_checkpoint")
    return payload


def _report_content(report: BlindReviewReport) -> dict[str, Any]:
    payload = asdict(report)
    payload["reviews"] = [_review_content(review) for review in report.reviews]
    return payload


def _rehash_evidence(evidence: dict[str, Any]) -> None:
    payload = {key: value for key, value in evidence.items() if key != "bundle_id"}
    evidence["bundle_id"] = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


@pytest.mark.asyncio
async def test_checkpoint_roundtrip_preserves_evidence_and_valid_reviews(tmp_path: Path) -> None:
    from digest.review_checkpoint import load_review_checkpoint

    config = _trial_config()
    original = await _report(config)
    path = tmp_path / "review.json"
    _archive(path, original)
    before = path.read_bytes()

    bundle, cached = load_review_checkpoint(path, config)

    assert bundle == original.evidence
    assert isinstance(bundle.items, tuple)
    assert [asdict(review) for review in cached] == [asdict(review) for review in original.reviews]
    assert path.read_bytes() == before
    with pytest.raises(FrozenInstanceError):
        bundle.bundle_id = "changed"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        bundle.items[0].excerpt = "changed"  # type: ignore[misc]


@pytest.mark.asyncio
async def test_resume_reuses_valid_results_and_calls_only_missing_slots() -> None:
    from digest.review import run_evidence_review

    config = _trial_config()
    original = await _report(config)
    cached = [deepcopy(original.reviews[0])]
    cached[0].usage = {"prompt_tokens": 17, "completion_tokens": 9}
    cached[0].resolved_model = "fixture-resolved-model"
    before_bundle, before_reviews = asdict(original.evidence), [asdict(review) for review in cached]

    with patch("digest.review.complete", side_effect=fixture_response) as complete:
        resumed = await run_evidence_review(original.evidence, config, cached_reviews=cached)

    assert [call.kwargs["provider_override"].model for call in complete.call_args_list] == [
        config.review.secondary.model, config.review.tie_breaker.model,
    ]
    assert resumed.status == "complete"
    assert resumed.selection_overlap == original.selection_overlap
    assert resumed.disputed_ids == original.disputed_ids
    assert asdict(resumed.evidence) == before_bundle
    assert asdict(resumed.reviews[0]) == {**before_reviews[0], "reused_from_checkpoint": True}
    assert asdict(original.evidence) == before_bundle
    assert [asdict(review) for review in cached] == before_reviews
    for call in complete.call_args_list:
        assert json.loads(call.args[1][1]["content"])["evidence"] == json.loads(json.dumps(before_bundle))


@pytest.mark.asyncio
async def test_complete_checkpoint_makes_no_model_calls() -> None:
    from digest.review import run_evidence_review

    config = _trial_config()
    original = await _report(config)
    with patch("digest.review.complete", AsyncMock(side_effect=AssertionError("No review is missing"))) as complete:
        resumed = await run_evidence_review(original.evidence, config, original.reviews)
    complete.assert_not_called()
    assert _report_content(resumed) == _report_content(original)


@pytest.mark.asyncio
async def test_abstained_cached_reviews_are_reused_without_third_model() -> None:
    from digest.review import run_evidence_review

    config = _trial_config()
    original = await _report(config)
    cached = deepcopy(original.reviews[:2])
    for review in cached:
        review.status = "abstained"
        review.selections = []
        review.limitations = ["Supplied excerpts are insufficient to select useful evidence."]
    with patch("digest.review.complete", AsyncMock(side_effect=AssertionError("Valid abstentions are reusable"))):
        resumed = await run_evidence_review(original.evidence, config, cached)
    assert resumed.status == "complete"
    assert resumed.selection_overlap == 1.0
    assert len(resumed.reviews) == 2
    assert all(review.status == "abstained" for review in resumed.reviews)


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["model", "provider", "prompt_hash", "bundle_id", "slot"])
async def test_cached_review_identity_must_match_current_slot(field: str) -> None:
    from digest.review import run_evidence_review

    config = _trial_config()
    original = await _report(config)
    stale = deepcopy(original.reviews[0])
    setattr(stale, field, "secondary" if field == "slot" else "stale-value")
    with patch("digest.review.complete", side_effect=fixture_response) as complete:
        resumed = await run_evidence_review(original.evidence, config, [stale])
    assert complete.call_count == 3
    assert _report_content(resumed) == _report_content(original)


@pytest.mark.asyncio
async def test_changed_prompt_retries_all_slots_on_exact_original_evidence() -> None:
    from digest.review import run_evidence_review

    config = _trial_config()
    original = await _report(config)
    config.radar.language = "ru"
    with patch("digest.review.complete", side_effect=fixture_response) as complete:
        resumed = await run_evidence_review(original.evidence, config, original.reviews)
    assert complete.call_count == 3
    assert asdict(resumed.evidence) == asdict(original.evidence)
    assert all(review.prompt_hash != original.reviews[0].prompt_hash for review in resumed.reviews)


@pytest.mark.asyncio
async def test_changed_configured_model_retries_only_that_slot() -> None:
    from digest.review import run_evidence_review

    config = _trial_config()
    original = await _report(config)
    config.review.primary = ReviewModelConfig("gemini", "replacement-model")

    async def replacement(role: object, messages: list[dict[str, str]], actual: object, **kwargs: Any) -> tuple:
        kwargs["provider_override"] = replace(kwargs["provider_override"], model="gemini-3.8-flash")
        return await fixture_response(role, messages, actual, **kwargs)

    with patch("digest.review.complete", side_effect=replacement) as complete:
        resumed = await run_evidence_review(original.evidence, config, original.reviews)
    assert complete.call_count == 1
    assert complete.call_args.kwargs["provider_override"].model == "replacement-model"
    assert resumed.reviews[0].model == "replacement-model"
    assert asdict(resumed.reviews[1]) == {**asdict(original.reviews[1]), "reused_from_checkpoint": True}
    assert asdict(resumed.reviews[2]) == {**asdict(original.reviews[2]), "reused_from_checkpoint": True}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [
    "unknown_id", "duplicate_id", "invented_quote", "empty_reason", "long_reason",
    "bad_confidence", "too_many_selections", "bad_limitations", "ok_without_selection",
    "abstained_with_selection", "unexplained_abstention",
])
async def test_cached_reviews_are_revalidated_before_reuse(kind: str) -> None:
    from digest.review import run_evidence_review

    config = _trial_config()
    original = await _report(config)
    cached = deepcopy(original.reviews)
    review = cached[0]
    selection = review.selections[0]
    if kind == "unknown_id":
        review.selections[0] = replace(selection, evidence_id="invented")
    elif kind == "duplicate_id":
        review.selections.append(selection)
    elif kind == "invented_quote":
        review.selections[0] = replace(selection, quote="This quote is absent from every excerpt.")
    elif kind == "empty_reason":
        review.selections[0] = replace(selection, reason="  ")
    elif kind == "long_reason":
        review.selections[0] = replace(selection, reason="x" * 601)
    elif kind == "bad_confidence":
        review.selections[0] = replace(selection, confidence="certain")
    elif kind == "too_many_selections":
        review.selections *= 3
    elif kind == "bad_limitations":
        review.limitations = ["  "]
    elif kind == "ok_without_selection":
        review.selections = []
    elif kind == "abstained_with_selection":
        review.status = "abstained"
    elif kind == "unexplained_abstention":
        review.status = "abstained"
        review.selections = []
        review.limitations = []

    with patch("digest.review.complete", AsyncMock()) as complete:
        with pytest.raises(ValueError):
            await run_evidence_review(original.evidence, config, cached)
    complete.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["invalid", "unavailable"])
async def test_failed_cached_slots_are_retried(status: str) -> None:
    from digest.review import run_evidence_review

    config = _trial_config()
    original = await _report(config)
    cached = deepcopy(original.reviews)
    cached[0].status = status
    with patch("digest.review.complete", side_effect=fixture_response) as complete:
        resumed = await run_evidence_review(original.evidence, config, cached)
    assert complete.call_count == 1
    assert complete.call_args.kwargs["provider_override"].model == config.review.primary.model
    assert _report_content(resumed) == _report_content(original)


@pytest.mark.asyncio
async def test_corrupt_in_memory_bundle_is_rejected_before_model_calls() -> None:
    from digest.review import run_evidence_review

    config = _trial_config()
    original = await _report(config)
    forged = replace(original.evidence, items=(replace(original.evidence.items[0], excerpt="tampered"),))
    with patch("digest.review.complete", AsyncMock()) as complete:
        with pytest.raises(ValueError):
            await run_evidence_review(forged, config, original.reviews)
    complete.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["hash", "schema", "duplicate_evidence", "wrong_type", "non_web_url"])
async def test_checkpoint_rejects_corrupt_evidence_even_with_a_recomputed_hash(kind: str, tmp_path: Path) -> None:
    from digest.review_checkpoint import load_review_checkpoint

    config = _trial_config()
    path = tmp_path / "review.json"
    payload = _archive(path, await _report(config))
    evidence = payload["evidence"]
    if kind == "hash":
        evidence["items"][0]["excerpt"] = "tampered without updating the hash"
    elif kind == "schema":
        evidence["schema_version"] = 999
    elif kind == "duplicate_evidence":
        evidence["items"] = [*evidence["items"], deepcopy(evidence["items"][0])]
    elif kind == "wrong_type":
        evidence["omitted_articles"] = True
    else:
        evidence["items"][0]["url"] = "file:///etc/passwd"
    if kind != "hash":
        _rehash_evidence(evidence)
    path.write_text(json.dumps(payload), encoding="utf-8")
    with patch("digest.review.complete", AsyncMock()) as complete:
        with pytest.raises(ValueError):
            load_review_checkpoint(path, config)
    complete.assert_not_called()


@pytest.mark.asyncio
async def test_duplicate_checkpoint_slots_are_rejected(tmp_path: Path) -> None:
    from digest.review_checkpoint import load_review_checkpoint

    config = _trial_config()
    path = tmp_path / "review.json"
    payload = _archive(path, await _report(config))
    payload["reviews"] = payload["reviews"][:2] + [deepcopy(payload["reviews"][0])]
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="(?i)duplicate"):
        load_review_checkpoint(path, config)


@pytest.mark.asyncio
async def test_duplicate_in_memory_slots_are_rejected_before_model_calls() -> None:
    from digest.review import run_evidence_review

    config = _trial_config()
    original = await _report(config)
    cached = [original.reviews[0], deepcopy(original.reviews[0])]
    with patch("digest.review.complete", AsyncMock()) as complete:
        with pytest.raises(ValueError, match="(?i)duplicate"):
            await run_evidence_review(original.evidence, config, cached)
    complete.assert_not_called()


@pytest.mark.asyncio
async def test_trial_resume_skips_collection_preserves_input_and_writes_separate_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from digest.review_trial import run_trial

    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    config = _trial_config()
    original = await _report(config)
    original.reviews = original.reviews[:2]
    original.reviews[1].status = "unavailable"
    original.reviews[1].selections = []
    original.reviews[1].limitations = []
    original.status = "incomplete"
    source = tmp_path / "original-review.json"
    _archive(source, original)
    before = source.read_bytes()
    output = tmp_path / "resumed"
    with (
        patch("digest.review_trial.load_config", return_value=config),
        patch("digest.review_trial.collect", AsyncMock(side_effect=AssertionError("Do not collect"))) as collect,
        patch("digest.review.complete", side_effect=fixture_response) as complete,
        patch("digest.delivery.send_article_cards", side_effect=AssertionError("No Telegram")),
        patch("digest.radar.save_dedup_cache", side_effect=AssertionError("No state writes")),
        patch("digest.feedback.collect_feedback", side_effect=AssertionError("No feedback")),
    ):
        assert await run_trial(tmp_path / "fixture.yaml", output, resume_path=source) == 0
    collect.assert_not_called()
    assert complete.call_count == 2
    assert source.read_bytes() == before
    resumed = json.loads((output / "review.json").read_text())
    assert resumed["evidence"] == json.loads(before)["evidence"]
    assert resumed["reviews"][0] == {**json.loads(before)["reviews"][0], "reused_from_checkpoint": True}
    assert resumed["status"] == "complete"
    assert (output / "review.md").exists()
    metadata = json.loads((output / "trial-metadata.json").read_text())
    assert metadata["trial_only"] and not metadata["production_state_written"] and not metadata["telegram_used"]
    assert metadata["resumed"]
    assert metadata["reused_slots"] == ["primary"]
    assert metadata["new_attempt_slots"] == ["secondary", "third"]


@pytest.mark.asyncio
@pytest.mark.parametrize("alias", [False, True])
async def test_trial_rejects_overwriting_resume_input_before_calls(
    alias: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from digest.review_trial import run_trial

    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    config = _trial_config()
    output = tmp_path / "output"
    output.mkdir()
    source = output / "review.json"
    _archive(source, await _report(config))
    before = source.read_bytes()
    resume = source
    if alias:
        resume = tmp_path / "input-alias.json"
        resume.symlink_to(source)
    with (
        patch("digest.review_trial.load_config", return_value=config),
        patch("digest.review_trial.collect", AsyncMock()) as collect,
        patch("digest.review.complete", AsyncMock()) as complete,
    ):
        with pytest.raises(ValueError):
            await run_trial(tmp_path / "fixture.yaml", output, resume_path=resume)
    collect.assert_not_called()
    complete.assert_not_called()
    assert source.read_bytes() == before


@pytest.mark.asyncio
async def test_legacy_checkpoint_without_timestamps_reuses_unknown_provenance(tmp_path: Path) -> None:
    from digest.review import run_evidence_review
    from digest.review_checkpoint import load_review_checkpoint

    config = _trial_config()
    path = tmp_path / "legacy.json"
    payload = _archive(path, await _report(config))
    for review in payload["reviews"]:
        review.pop("generated_at")
        review.pop("attempted_at")
        review.pop("reused_from_checkpoint")
    path.write_text(json.dumps(payload), encoding="utf-8")
    bundle, cached = load_review_checkpoint(path, config)
    with patch("digest.review.complete", AsyncMock(side_effect=AssertionError("Legacy success is reusable"))):
        resumed = await run_evidence_review(bundle, config, cached)
    assert all(review.generated_at is None for review in resumed.reviews)
    assert all(review.reused_from_checkpoint for review in resumed.reviews)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["invalid", "unavailable"])
@pytest.mark.parametrize("language", ["en", "ru"])
async def test_cards_fall_back_to_successful_secondary_with_explicit_attribution(status: str, language: str) -> None:
    config = _trial_config()
    report = await _report(config)
    report.reviews[0].status = status
    report.reviews[0].selections = []
    report.status = "incomplete"
    cards = primary_cards(report, fixture_articles(), language)
    secondary = report.reviews[1]
    expected_urls = {item.url for item in report.evidence.items
                     if item.evidence_id in {selection.evidence_id for selection in secondary.selections}}
    assert {card.link for card in cards} == expected_urls
    assert all(f"{secondary.provider}/{secondary.model}" in card.summary for card in cards)
    label = "independent comparison incomplete" if language == "en" else "независимое сравнение не завершено"
    assert all(label in card.summary for card in cards)


@pytest.mark.asyncio
async def test_primary_abstention_does_not_silently_fall_back_to_secondary() -> None:
    report = await _report(_trial_config())
    report.reviews[0].status = "abstained"
    report.reviews[0].selections = []
    report.reviews[0].limitations = ["Evidence is insufficient."]
    assert primary_cards(report, fixture_articles(), "en") == []


@pytest.mark.asyncio
async def test_trial_rejects_corrupt_checkpoint_without_collection_or_model_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from digest.review_trial import run_trial

    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    config = _trial_config()
    source = tmp_path / "corrupt.json"
    payload = _archive(source, await _report(config))
    payload["evidence"]["items"][0]["title"] = "Changed title, same asserted evidence hash"
    source.write_text(json.dumps(payload), encoding="utf-8")
    with (
        patch("digest.review_trial.load_config", return_value=config),
        patch("digest.review_trial.collect", AsyncMock()) as collect,
        patch("digest.review.complete", AsyncMock()) as complete,
    ):
        with pytest.raises(ValueError):
            await run_trial(tmp_path / "fixture.yaml", tmp_path / "out", resume_path=source)
    collect.assert_not_called()
    complete.assert_not_called()
    assert not (tmp_path / "out").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("location", ["report", "evidence"])
async def test_boolean_schema_versions_are_not_accepted_as_version_one(location: str, tmp_path: Path) -> None:
    from digest.review_checkpoint import load_review_checkpoint

    config = _trial_config()
    source = tmp_path / "invalid-schema.json"
    payload = _archive(source, await _report(config))
    target = payload if location == "report" else payload["evidence"]
    target["schema_version"] = True
    if location == "evidence":
        _rehash_evidence(payload["evidence"])
    source.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError):
        load_review_checkpoint(source, config)


@pytest.mark.asyncio
async def test_mutable_evidence_item_container_is_rejected_before_model_calls() -> None:
    from digest.review import run_evidence_review

    config = _trial_config()
    original = await _report(config)
    bundle = replace(original.evidence, items=list(original.evidence.items))
    with patch("digest.review.complete", AsyncMock()) as complete:
        with pytest.raises(ValueError):
            await run_evidence_review(bundle, config)
    complete.assert_not_called()


def _full_source_evidence(tmp_path: Path, bundle: EvidenceBundle) -> FullSourceEvidence:
    from digest.article_source import FetchedArticle
    from digest.reading_brief_state import BriefState, Page, PageResult, Route, Selection, save_source
    from digest.review_checkpoint import build_full_source_evidence

    # This stored backlog article intentionally is not a member of today's RSS bundle.
    selection = Selection("Provider rollout announcement", "https://provider.example/rollout",
                          "Provider engineering", "technology", None)
    body = ("Opening context without the selected evidence. " * 20 + "\n\n"
            "The provider reports API-powered deployments improved reliability.\n\n"
            "The reported result applies only to the trial deployment, not every customer.")
    snapshot, source = save_source(tmp_path, selection, FetchedArticle(
        body, selection.link, "2026-10-02T12:00:00+00:00", None, "article",
    ))
    page = Page(0, len(source.spans), "a" * 64, PageResult(
        [span.id for span in source.spans], [2], [3], "A model-only reading angle", [2], False,
    ))
    state = BriefState(selection, Route("gemini", "exact-source-reader", 10000, 2000),
                       "2026-10-02T12:00:00+00:00", "2026-10-02T12:00:00+00:00",
                       status="ready", source_sha256=snapshot, pages=[page])
    # Reading-brief validation is tested by its owner; this adapter receives only checked states.
    with patch("digest.reading_brief.ready_brief_evidence", return_value=(state, source)):
        result = build_full_source_evidence(bundle, tmp_path, [selection.identity])
    assert source.text == body
    return result


@pytest.mark.asyncio
async def test_full_source_checkpoint_roundtrip_preserves_rss_and_selected_literal_provenance(tmp_path: Path) -> None:
    from digest.review_checkpoint import load_full_source_evidence, load_review_checkpoint

    config = _trial_config()
    original = await _report(config)
    source_evidence = _full_source_evidence(tmp_path, original.evidence)
    path = tmp_path / "review.json"
    payload = _archive(path, original)
    payload["full_source_required"] = True
    payload["full_source_evidence"] = asdict(source_evidence)
    path.write_text(json.dumps(payload), encoding="utf-8")
    before = path.read_bytes()

    rss_bundle, cached = load_review_checkpoint(path, config)
    loaded = load_full_source_evidence(path, rss_bundle, config)

    assert rss_bundle == original.evidence and cached == original.reviews
    assert loaded == source_evidence
    assert loaded.rss_bundle_id == original.evidence.bundle_id
    assert len(loaded.items) == 2
    assert loaded.items[0].start > 500
    assert loaded.items[0].roles == ("angle_support", "selected")
    assert loaded.items[1].roles == ("qualification",)
    assert all(item.article_id not in {item.evidence_id for item in rss_bundle.items} for item in loaded.items)
    serialized = json.dumps(asdict(loaded))
    assert "Opening context without" not in serialized
    assert "A model-only reading angle" not in serialized
    assert "trial deployment, not every customer" in serialized
    assert path.read_bytes() == before
    with pytest.raises(FrozenInstanceError):
        loaded.items[0].excerpt = "changed"  # type: ignore[misc]


@pytest.mark.asyncio
async def test_legacy_checkpoint_has_no_full_source_authority(tmp_path: Path) -> None:
    from digest.review_checkpoint import load_full_source_evidence

    config = _trial_config()
    original = await _report(config)
    path = tmp_path / "review.json"
    _archive(path, original)
    assert load_full_source_evidence(path, original.evidence, config) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", [
    "excerpt", "offset", "span_boolean", "body_hash", "article_identity", "source_url", "rss_link", "schema",
    "duplicate",
])
async def test_full_source_checkpoint_rejects_tampered_provenance(mutation: str, tmp_path: Path) -> None:
    from digest.review_checkpoint import load_full_source_evidence

    config = _trial_config()
    original = await _report(config)
    source_evidence = _full_source_evidence(tmp_path, original.evidence)
    path = tmp_path / "review.json"
    payload = _archive(path, original)
    evidence = json.loads(json.dumps(asdict(source_evidence)))
    item = evidence["items"][0]
    if mutation == "excerpt":
        item["excerpt"] = "Fabricated evidence"
    elif mutation == "offset":
        item["end"] += 1
    elif mutation == "span_boolean":
        item["span_id"] = True
    elif mutation == "body_hash":
        item["body_sha256"] = "not-a-source-hash"
    elif mutation == "article_identity":
        item["article_id"] = "not-the-original-article"
    elif mutation == "source_url":
        item["final_url"] = "file:///etc/passwd"
    elif mutation == "rss_link":
        evidence["rss_bundle_id"] = "an-unrelated-checkpoint"
    elif mutation == "schema":
        evidence["schema_version"] = True
    else:
        evidence["items"].append(deepcopy(item))
    # Rehashing the outer container must not hide corrupt span identity/provenance.
    _rehash_evidence(evidence)
    payload["full_source_evidence"] = evidence
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError):
        load_full_source_evidence(path, original.evidence, config)


def test_full_source_transport_budget_rejects_without_truncating(tmp_path: Path) -> None:
    from digest.review import build_evidence_bundle
    from digest.review_checkpoint import validate_full_source_evidence

    config = _trial_config()
    rss_bundle = build_evidence_bundle(fixture_articles(), config.review)
    evidence = _full_source_evidence(tmp_path, rss_bundle)
    before = asdict(evidence)
    with patch("digest.review_checkpoint.MAX_FULL_SOURCE_BYTES", 100):
        with pytest.raises(ValueError, match="exceeds checkpoint budget"):
            validate_full_source_evidence(evidence, rss_bundle)
    assert asdict(evidence) == before
