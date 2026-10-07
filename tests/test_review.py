"""Blind-review contract safety tests; every model response is synthetic."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from digest.config import ReviewConfig, _load_review
from digest.delivery.markdown import write_digest
from digest.review import _parse_review, build_evidence_bundle, primary_cards, run_blind_review
from scripts.review_fixture import fixture_articles, fixture_config, fixture_response, run_fixture


def test_evidence_is_deterministic_and_changes_when_excerpt_changes() -> None:
    articles = fixture_articles()
    settings = ReviewConfig(max_evidence_articles=2)
    first = build_evidence_bundle(articles, settings)
    reordered = dict(reversed(list(articles.items())))
    assert build_evidence_bundle(reordered, settings) == first
    assert len({item.category for item in first.items}) == 2
    assert first.omitted_articles == 2
    changed = deepcopy(articles)
    for group in changed.values():
        for article in group:
            article.description += " New evidence."
    assert build_evidence_bundle(changed, settings).bundle_id != first.bundle_id


@pytest.mark.asyncio
async def test_every_slot_is_blind_and_gets_identical_evidence() -> None:
    requests = []

    async def adapter(role: object, messages: list[dict[str, str]], config: object, **kwargs: object) -> tuple:
        requests.append(deepcopy(messages))
        text, usage = await fixture_response(role, messages, config, **kwargs)
        if len(requests) == 1:
            data = json.loads(text)
            data["selections"][0]["reason"] = "PRIMARY_ONLY_SENTINEL"
            text = json.dumps(data)
        return text, usage

    with patch("digest.review.complete", side_effect=adapter):
        report = await run_blind_review(fixture_articles(), fixture_config())
    assert len(requests) == 3
    assert all(request == requests[0] for request in requests)
    assert "PRIMARY_ONLY_SENTINEL" not in json.dumps(requests)
    assert len({r.prompt_hash for r in report.reviews}) == 1
    assert report.status == "complete"
    assert report.selection_overlap == pytest.approx(1 / 3)
    assert len(report.disputed_ids) == 2


@pytest.mark.asyncio
async def test_agreement_does_not_call_third_model() -> None:
    response = None

    async def adapter(role: object, messages: list[dict[str, str]], config: object, **kwargs: object) -> tuple:
        nonlocal response
        if response is None:
            response = await fixture_response(role, messages, config, **kwargs)
        return response

    with patch("digest.review.complete", side_effect=adapter) as complete:
        report = await run_blind_review(fixture_articles(), fixture_config())
    assert complete.call_count == 2
    assert report.selection_overlap == 1
    assert report.disputed_ids == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError("quota"), "invalid JSON"])
async def test_peer_failure_is_incomplete_not_disagreement(failure: object) -> None:
    async def adapter(role: object, messages: list[dict[str, str]], config: object, **kwargs: object) -> tuple:
        if kwargs["provider_override"].name == "groq":
            if isinstance(failure, Exception):
                raise failure
            return str(failure), {}
        return await fixture_response(role, messages, config, **kwargs)

    with patch("digest.review.complete", side_effect=adapter) as complete:
        report = await run_blind_review(fixture_articles(), fixture_config())
    assert complete.call_count == 2
    assert report.status == "incomplete"
    assert report.selection_overlap is None
    assert report.reviews[0].status == "ok"
    assert report.disputed_ids == []
    assert len(primary_cards(report, fixture_articles(), "en")) == 2


def _valid_output() -> tuple:
    bundle = build_evidence_bundle(fixture_articles(), ReviewConfig())
    item = bundle.items[0]
    data = {"selections": [{"evidence_id": item.evidence_id, "reason": "Useful evidence.",
                            "quote": item.title, "confidence": "medium"}], "limitations": []}
    return bundle, data


@pytest.mark.parametrize("kind", ["unknown", "duplicate", "invented_quote", "wrong_type", "extra", "too_many"])
def test_invalid_entry_rejects_whole_review(kind: str) -> None:
    bundle, data = _valid_output()
    item = data["selections"][0]
    if kind == "unknown":
        item["evidence_id"] = "invented"
    elif kind == "duplicate":
        data["selections"].append(deepcopy(item))
    elif kind == "invented_quote":
        item["quote"] = "not in the supplied evidence"
    elif kind == "wrong_type":
        item["reason"] = ["text"]
    elif kind == "extra":
        item["url"] = "https://invented.example"
    else:
        data["selections"] *= 6
    with pytest.raises(ValueError):
        _parse_review(json.dumps(data), bundle)


def test_abstention_is_valid_only_with_explanation() -> None:
    bundle, _ = _valid_output()
    assert _parse_review('{"selections": [], "limitations": ["Insufficient evidence"]}', bundle)[0] == []
    with pytest.raises(ValueError):
        _parse_review('{"selections": [], "limitations": []}', bundle)


def test_duplicate_slot_identity_rejected() -> None:
    with pytest.raises(ValueError, match="distinct"):
        _load_review({"review": {"secondary": {"provider": "gemini", "model": "gemini-3.8-flash"}}})


@pytest.mark.asyncio
async def test_fixture_has_no_live_network_or_delivery_and_archives_contract(tmp_path: Path) -> None:
    with patch("httpx.AsyncClient", side_effect=AssertionError("Live HTTP is forbidden")):
        report = await run_fixture()
    config = fixture_config()
    config.obsidian.enabled = True
    config.obsidian.output_dir = str(tmp_path)
    path = write_digest("Fixture only", config, review_report=report)
    assert path is not None
    archive = json.loads(path.with_suffix(".review.json").read_text())
    assert archive == json.loads(json.dumps(asdict(report)))
    assert "Independent Blind Review" in path.read_text()


@pytest.mark.asyncio
async def test_completion_order_does_not_change_slot_attribution() -> None:
    async def adapter(role: object, messages: list[dict[str, str]], config: object, **kwargs: object) -> tuple:
        if kwargs["provider_override"].name == "gemini":
            await asyncio.sleep(0.001)
        return await fixture_response(role, messages, config, **kwargs)

    with patch("digest.review.complete", side_effect=adapter):
        report = await run_blind_review(fixture_articles(), fixture_config())
    assert [r.slot for r in report.reviews] == ["primary", "secondary", "third"]


@pytest.mark.asyncio
async def test_selection_survives_category_prose_failure() -> None:
    from digest.application.analysis import analyze_articles as _analyze_articles

    report = await run_fixture()
    with (
        patch("digest.review.run_blind_review", AsyncMock(return_value=report)),
        patch("digest.radar.summarize_all", AsyncMock(return_value=([], None))),
        patch("digest.radar.pick_top_articles", AsyncMock()) as legacy_picker,
    ):
        summaries, trends, cards, actual = await _analyze_articles(fixture_articles(), fixture_config())
    assert summaries == [] and trends is None
    assert len(cards) == 2 and actual is report
    legacy_picker.assert_not_called()


@pytest.mark.asyncio
async def test_duplicate_article_identity_uses_same_canonical_source_for_cards() -> None:
    from dataclasses import replace

    articles = fixture_articles()
    original = next(a for a in articles["Architecture"] if a.link.endswith("idempotency"))
    articles["zz_duplicate"] = [replace(original, source="DIFFERENT_SOURCE", category="zz_duplicate")]
    with patch("digest.review.complete", side_effect=fixture_response):
        report = await run_blind_review(articles, fixture_config())
    evidence = {i.url: i for i in report.evidence.items}[original.link]
    card = next(c for c in primary_cards(report, articles, "en") if c.link == original.link)
    assert evidence.source == card.source == original.source
    assert evidence.category == card.category == original.category


@pytest.mark.asyncio
async def test_disagreement_below_threshold_is_not_labeled_agreement() -> None:
    config = fixture_config()
    config.review.disagreement_threshold = 0.1
    with patch("digest.review.complete", side_effect=fixture_response) as complete:
        report = await run_blind_review(fixture_articles(), config)
    assert report.disputed_ids
    assert report.third_model_reason == "selection_disagreement_not_escalated"
    assert complete.call_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("abstained", [False, True])
async def test_empty_selection_review_diagnostics_survive_pipeline(
    abstained: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from digest.irritator import IrritatorStatus
    from digest.main import run

    monkeypatch.chdir(tmp_path)
    report = await run_fixture()
    for review in report.reviews:
        review.status = "abstained" if abstained else "unavailable"
        review.selections = []
        review.limitations = ["No adequate evidence"] if abstained else []
    report.status = "complete" if abstained else "incomplete"
    config = fixture_config()
    config.obsidian.enabled = True
    config.obsidian.output_dir = str(tmp_path / "digests")
    with (
        patch("httpx.AsyncClient", side_effect=AssertionError("HTTP forbidden")),
        patch("digest.config.load_config", return_value=config),
        patch("digest.radar.collect", AsyncMock(return_value=(fixture_articles(), {}))),
        patch("digest.application.analysis.analyze_articles", AsyncMock(return_value=([], None, [], report))),
        patch("digest.application.investigation.run_irritator",
              AsyncMock(return_value=([], [], IrritatorStatus("none", "empty")))),
        patch("digest.application.run_state.process_pending_approvals"),
    ):
        result = await run("fixture.yaml", False, False, False)
    assert result.markdown_saved
    assert result.review_status == report.status
    assert Path(result.markdown_path).with_suffix(".review.json").exists()


def test_oversized_or_non_web_evidence_is_omitted_once_for_every_model() -> None:
    from dataclasses import replace

    from digest.review import MAX_EVIDENCE_JSON_CHARS

    articles = fixture_articles()
    source = articles["AI"][0]
    articles["Oversized"] = [replace(source, link="https://example.com/" + "x" * 20000)]
    articles["Unsafe"] = [replace(source, link="javascript:alert(1)")]
    bundle = build_evidence_bundle(articles, ReviewConfig())
    assert bundle.omitted_articles == 2
    assert all(item.url.startswith("https://") for item in bundle.items)
    assert sum(len(json.dumps(asdict(item), ensure_ascii=False)) for item in bundle.items) <= MAX_EVIDENCE_JSON_CHARS


@pytest.mark.asyncio
async def test_telegram_status_exposes_incomplete_peer_review() -> None:
    from digest.application.legacy import _review_status_line

    report = await run_fixture()
    report.status = "incomplete"
    report.reviews[1].status = "unavailable"
    status = _review_status_line(report)
    assert "incomplete" in status and "no response" in status
    assert _review_status_line(None) == ""


@pytest.mark.asyncio
async def test_trial_isolates_cache_and_never_calls_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from digest.review_trial import run_trial

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    (tmp_path / ".cache").mkdir()
    state = tmp_path / ".cache/seen_articles.json"
    state.write_text('{"production": "untouched"}')
    config = fixture_config()
    config.telegram.enabled = True
    config.llm.max_retries = 3
    report = await run_fixture()

    async def collector(actual: object) -> tuple:
        assert Path.cwd() != tmp_path
        assert not Path(".cache/seen_articles.json").exists()
        assert not actual.telegram.enabled and not actual.adaptive.enabled
        assert actual.llm.max_retries == 0
        assert actual.review.max_evidence_articles <= 10
        return fixture_articles(), {"new": "not persisted"}

    with (
        patch("digest.review_trial.load_config", return_value=config),
        patch("digest.review_trial.collect", AsyncMock(side_effect=collector)),
        patch("digest.review_trial.run_blind_review", AsyncMock(return_value=report)),
        patch("digest.delivery.send_article_cards", side_effect=AssertionError("No Telegram")),
        patch("digest.radar.save_dedup_cache", side_effect=AssertionError("No state writes")),
        patch("digest.feedback.collect_feedback", side_effect=AssertionError("No feedback")),
    ):
        assert await run_trial(tmp_path / "fixture.yaml", tmp_path / "output") == 0
    assert Path.cwd() == tmp_path
    assert state.read_text() == '{"production": "untouched"}'
    assert (tmp_path / "output/review.json").exists()
    assert json.loads((tmp_path / "output/trial-metadata.json").read_text())["max_model_requests"] == 3


@pytest.mark.asyncio
async def test_invalid_review_preserves_reason_and_rejected_model_text() -> None:
    config = fixture_config()
    raw = ('{"selections":[{"evidence_id":"invented","reason":"Useful",'
           '"quote":"text","confidence":"high"}],"limitations":[]}')
    with patch("digest.review.complete", AsyncMock(return_value=(raw, {}))):
        report = await run_blind_review(fixture_articles(), config)
    assert report.reviews[0].error == "unknown evidence id"
    assert report.reviews[0].rejected_output == raw
    assert report.reviews[0].response_sha256
    assert report.third_model_reason == "incomplete_primary_comparison"


def test_rejected_response_diagnostics_are_bounded_and_redacted() -> None:
    from digest.review import _rejected_output_diagnostics

    text = "\x00sk-abcdefghijklmnopqrstuv Bearer abcdefghijklmnopqrstuv " + "x" * 33000
    reason, rejected, truncated = _rejected_output_diagnostics(text, ValueError("unexpected provider body"))
    assert reason == "invalid JSON or review contract"
    assert len(rejected) == 32000 and truncated
    assert "abcdefghijklmnopqrstuv" not in rejected
    assert "\x00" not in rejected


def test_review_status_pending_is_not_unavailable_in_russian():
    from digest.application.legacy import _review_status_line
    from digest.review import BlindReviewReport, ModelReview

    bundle = build_evidence_bundle(fixture_articles(), fixture_config().review)
    review = ModelReview("secondary", "groq", "openai/gpt-oss-120b", bundle.bundle_id, "hash", "unavailable",
                         error="pending_independent_review")
    report = BlindReviewReport(1, bundle, [review], "incomplete", None, [], "pending_independent_review")
    text = _review_status_line(report, "ru")
    assert "ожидает отдельного этапа" in text
    assert "secondary" not in text and "unavailable" not in text
    assert "ещё не завершено" in text
