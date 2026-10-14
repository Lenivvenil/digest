"""Blind-review contract safety tests; every model response is synthetic."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from digest.adapters.models.execution import ModelExecution
from digest.application.review import run_blind_review, run_evidence_review
from digest.application.review_request import build_evidence_bundle
from digest.config import ReviewConfig, _load_review
from digest.delivery.markdown import write_digest
from digest.domain.editorial.attempts import restore_review
from digest.domain.editorial.reviews import BlindReviewReport, ModelReview, RejectedSelection, _parse_review
from digest.domain.editorial.summaries import ArticleSummary
from digest.presentation.review import future_feed_date_cards, literal_article_title, primary_cards, render_review
from scripts.review_fixture import fixture_articles, fixture_config, fixture_response, run_fixture


def test_title_label_preserves_literal_punctuation_and_folds_only_display_whitespace() -> None:
    assert literal_article_title("Plain title") == "Plain title"
    assert literal_article_title("Bank & Fintech [update]") == r"Bank \& Fintech \[update\]"
    assert literal_article_title("line one\n\nline two") == "line one line two"


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
    execution = ModelExecution()
    requests = []
    completed_providers = []

    async def adapter(role: object, messages: list[dict[str, str]], config: object, **kwargs: object) -> tuple:
        provider = kwargs["provider_override"].name
        is_primary = provider == "gemini"
        requests.append(deepcopy(messages))
        text, usage = await fixture_response(role, messages, config, **kwargs)
        if is_primary:
            data = json.loads(text)
            data["selections"][0]["reason"] = "PRIMARY_ONLY_SENTINEL"
            text = json.dumps(data)
            await asyncio.sleep(0.001)
        completed_providers.append(provider)
        return text, usage

    with patch("digest.application.review.complete", side_effect=adapter):
        report = await run_blind_review(fixture_articles(), fixture_config(), execution=execution)
    assert len(requests) == 3
    assert completed_providers == ["groq", "gemini", "groq"]
    assert [review.slot for review in report.reviews] == ["primary", "secondary", "third"]
    assert all(request == requests[0] for request in requests)
    assert "PRIMARY_ONLY_SENTINEL" not in json.dumps(requests)
    assert len({r.prompt_hash for r in report.reviews}) == 1
    assert report.status == "complete"
    assert report.selection_overlap == pytest.approx(1 / 3)
    assert len(report.disputed_ids) == 2


@pytest.mark.asyncio
async def test_agreement_does_not_call_third_model() -> None:
    execution = ModelExecution()
    response = None

    async def adapter(role: object, messages: list[dict[str, str]], config: object, **kwargs: object) -> tuple:
        nonlocal response
        if response is None:
            response = await fixture_response(role, messages, config, **kwargs)
        return response

    with patch("digest.application.review.complete", side_effect=adapter) as complete:
        report = await run_blind_review(fixture_articles(), fixture_config(), execution=execution)
    assert complete.call_count == 2
    assert report.selection_overlap == 1
    assert report.disputed_ids == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError("quota"), "invalid JSON"])
async def test_peer_failure_is_incomplete_not_disagreement(failure: object) -> None:
    execution = ModelExecution()

    async def adapter(role: object, messages: list[dict[str, str]], config: object, **kwargs: object) -> tuple:
        if kwargs["provider_override"].name == "groq":
            if isinstance(failure, Exception):
                raise failure
            return str(failure), {}
        return await fixture_response(role, messages, config, **kwargs)

    with patch("digest.application.review.complete", side_effect=adapter) as complete:
        report = await run_blind_review(fixture_articles(), fixture_config(), execution=execution)
    assert complete.call_count == 2
    assert report.status == "incomplete"
    assert report.selection_overlap is None
    assert report.reviews[0].status == "ok"
    assert report.disputed_ids == []
    assert len(primary_cards(restore_review(report), fixture_articles(), "en")) == 2


def _valid_output() -> tuple:
    bundle = build_evidence_bundle(fixture_articles(), ReviewConfig())
    item = bundle.items[0]
    data = {
        "selections": [
            {"evidence_id": item.evidence_id, "reason": "Useful evidence.", "quote": item.title, "confidence": "medium"}
        ],
        "limitations": [],
    }
    return bundle, data


@pytest.mark.parametrize(
    "published,language,normalized",
    [
        ("2026-11-25T15:00:00+00:00", "en", "25 Nov 2026"),
        ("2026-10-11T23:30:00-02:00", "ru", "12.10.2026"),
        ("2026-10-12T00:00:00.123456Z", "de", "12 Oct 2026"),
        ("2026-10-11T23:59:59+00:00", "en", None),
        ("2026-10-12T01:00:00+02:00", "en", None),
        ("2026-10-10T23:59:59+00:00", "en", None),
        (None, "en", None),
        ("not a feed date", "en", None),
        ("2026-11-25T15:00:00", "en", None),
        ("0001-01-01T00:00:00+01:00", "en", None),
        ("9999-12-31T23:59:59-01:00", "en", None),
    ],
)
def test_future_feed_date_cards_use_only_known_aware_utc_days(
    published: str | None, language: str, normalized: str | None,
) -> None:
    bundle, _ = _valid_output()
    item = replace(bundle.items[0], published=published)
    report = BlindReviewReport(1, replace(bundle, items=(item,)), [], "incomplete", None, [], "Fixture")
    original = deepcopy(asdict(report))
    card = ArticleSummary(item.title, item.url, item.source, item.category, "Canonical reason.")
    presented = future_feed_date_cards([card], report, date(2026, 10, 11), language)
    if normalized is None:
        expected = card.summary
    elif language == "ru":
        expected = (
            f"Дата в ленте источника (UTC): {normalized}; "
            "позже даты выпуска (11.10.2026). Canonical reason."
        )
    else:
        expected = (
            f"Source feed date (UTC): {normalized}; "
            "later than this edition’s date (11 Oct 2026). Canonical reason."
        )
    assert presented == [replace(card, summary=expected)]
    assert presented[0] is not card and card.summary == "Canonical reason."
    assert asdict(report) == original


def test_future_feed_date_cards_need_exact_article_identity_and_a_report() -> None:
    bundle, _ = _valid_output()
    item = replace(bundle.items[0], published="2026-11-25T15:00:00+00:00")
    report = BlindReviewReport(1, replace(bundle, items=(item,)), [], "incomplete", None, [], "Fixture")
    card = ArticleSummary(item.title, item.url, item.source, item.category, "Canonical reason.")
    cards = [replace(card, title="Another title"), replace(card, link="https://example.com/another")]
    unchanged = future_feed_date_cards(cards, report, date(2026, 10, 11), "en")
    assert unchanged == cards and all(actual is not original for actual, original in zip(unchanged, cards, strict=True))
    assert future_feed_date_cards([card], None, date(2026, 10, 11), "en") == [card]


@pytest.mark.parametrize(
    ("kind", "error"),
    [
        ("duplicate", "duplicated evidence id"),
        ("invented_quote", "quote is not in supplied evidence"),
        ("extra", "invalid selection schema"),
    ],
)
def test_invalid_entry_rejects_whole_review(kind: str, error: str) -> None:
    bundle, data = _valid_output()
    item = data["selections"][0]
    if kind == "duplicate":
        data["selections"].append(deepcopy(item))
    elif kind == "invented_quote":
        item["quote"] = "not in the supplied evidence"
    else:
        item["url"] = "https://invented.example"
    # Strict parsing raises even after an accepted prefix; it never salvages it.
    with pytest.raises(ValueError, match=f"^{error}$"):
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
    execution = ModelExecution()
    articles = fixture_articles()
    for group in articles.values():
        for article in group:
            article.title = "![badge](https://example.invalid/pixel)"
    with (
        patch("httpx.AsyncClient", side_effect=AssertionError("Live HTTP is forbidden")),
        patch("scripts.review_fixture.fixture_articles", return_value=articles),
    ):
        report = await run_fixture(execution=execution)
    original = deepcopy(asdict(report))
    config = fixture_config()
    config.obsidian.enabled = True
    config.obsidian.output_dir = str(tmp_path)
    path = write_digest("Fixture only", config, review_report=report)
    assert path is not None
    archive = json.loads(path.with_suffix(".review.json").read_text())
    assert archive == json.loads(json.dumps(asdict(report)))
    assert "Independent Blind Review" not in path.read_text()
    assert f"[Detailed review report]({path.with_suffix('.review.json').name})" in path.read_text()
    evidence = {item.evidence_id: item for item in report.evidence.items}
    preview = render_review(report)
    assert any(review.selections for review in report.reviews)
    for review in report.reviews:
        for selected in review.selections:
            item = evidence[selected.evidence_id]
            label = r"\!\[badge\]\(https\:\/\/example\.invalid\/pixel\)"
            assert f"[{label}]({item.url}): {selected.reason}" in preview
    assert preview not in path.read_text()
    assert asdict(report) == original


@pytest.mark.asyncio
async def test_selection_survives_category_prose_failure() -> None:
    execution = ModelExecution()
    from digest.application.analysis import analyze_articles as _analyze_articles

    report = await run_fixture(execution=execution)
    with (
        patch("digest.application.review.run_blind_review", AsyncMock(return_value=report)),
        patch("digest.radar.summarize_all", AsyncMock(return_value=([], None))),
        patch("digest.radar.pick_top_articles", AsyncMock()) as legacy_picker,
    ):
        summaries, trends, cards, actual = await _analyze_articles(
            fixture_articles(), fixture_config(), execution=execution
        )
    assert summaries == [] and trends is None
    assert len(cards) == 2 and actual is report
    legacy_picker.assert_not_called()


@pytest.mark.asyncio
async def test_duplicate_article_identity_uses_same_canonical_source_for_cards() -> None:
    execution = ModelExecution()
    from dataclasses import replace

    articles = fixture_articles()
    original = next(a for a in articles["Architecture"] if a.link.endswith("idempotency"))
    articles["zz_duplicate"] = [replace(original, source="DIFFERENT_SOURCE", category="zz_duplicate")]
    with patch("digest.application.review.complete", side_effect=fixture_response):
        report = await run_blind_review(articles, fixture_config(), execution=execution)
    evidence = {i.url: i for i in report.evidence.items}[original.link]
    card = next(c for c in primary_cards(restore_review(report), articles, "en") if c.link == original.link)
    assert evidence.source == card.source == original.source
    assert evidence.category == card.category == original.category


@pytest.mark.asyncio
async def test_disagreement_below_threshold_is_not_labeled_agreement() -> None:
    execution = ModelExecution()
    config = fixture_config()
    config.review.disagreement_threshold = 0.1
    with patch("digest.application.review.complete", side_effect=fixture_response) as complete:
        report = await run_blind_review(fixture_articles(), config, execution=execution)
    assert report.disputed_ids
    assert report.third_model_reason == "selection_disagreement_not_escalated"
    assert complete.call_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("abstained", [False, True])
async def test_empty_selection_review_diagnostics_survive_pipeline(
    abstained: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution = ModelExecution()
    from digest.irritator import IrritatorStatus
    from digest.main import run

    monkeypatch.chdir(tmp_path)
    report = await run_fixture(execution=execution)
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
        patch(
            "digest.application.investigation.run_irritator",
            AsyncMock(return_value=([], [], IrritatorStatus("none", "empty"))),
        ),
        patch("digest.application.run_state.process_pending_approvals"),
    ):
        result = await run("fixture.yaml", False, False, False)
    assert result.markdown_saved
    assert result.review_status == report.status
    assert Path(result.markdown_path).with_suffix(".review.json").exists()


def test_oversized_or_non_web_evidence_is_omitted_once_for_every_model() -> None:
    from dataclasses import replace

    from digest.domain.editorial.reviews import MAX_EVIDENCE_JSON_CHARS

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
    execution = ModelExecution()
    from digest.application.legacy import _review_status_line

    report = await run_fixture(execution=execution)
    report.status = "incomplete"
    report.reviews[1].status = "unavailable"
    status = _review_status_line(report)
    assert "incomplete" in status and "no response" in status
    assert _review_status_line(None) == ""


@pytest.mark.asyncio
async def test_trial_isolates_cache_and_never_calls_delivery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution = ModelExecution()
    from digest.review_trial import run_trial

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    (tmp_path / ".cache").mkdir()
    state = tmp_path / ".cache/seen_articles.json"
    state.write_text('{"production": "untouched"}')
    config = fixture_config()
    config.telegram.enabled = True
    config.llm.max_retries = 3
    report = await run_fixture(execution=execution)

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
        patch("digest.application.feedback.collect_feedback", side_effect=AssertionError("No feedback")),
    ):
        assert await run_trial(tmp_path / "fixture.yaml", tmp_path / "output", execution=execution) == 0
    assert Path.cwd() == tmp_path
    assert state.read_text() == '{"production": "untouched"}'
    assert (tmp_path / "output/review.json").exists()
    assert json.loads((tmp_path / "output/trial-metadata.json").read_text())["max_model_requests"] == 3


@pytest.mark.asyncio
async def test_invalid_review_preserves_reason_and_rejected_model_text() -> None:
    execution = ModelExecution()
    config = fixture_config()
    raw = (
        '{"selections":[{"evidence_id":"invented","reason":"Useful",'
        '"quote":"text","confidence":"high"}],"limitations":[]}'
    )
    with patch("digest.application.review.complete", AsyncMock(return_value=(raw, {}))):
        report = await run_blind_review(fixture_articles(), config, execution=execution)
    assert report.reviews[0].error == "unknown evidence id"
    assert report.reviews[0].rejected_output == raw
    assert report.reviews[0].response_sha256
    assert report.third_model_reason == "incomplete_primary_comparison"


def test_rejected_response_diagnostics_are_bounded_and_redacted() -> None:
    from digest.domain.editorial.reviews import _rejected_output_diagnostics

    text = "\x00sk-abcdefghijklmnopqrstuv Bearer abcdefghijklmnopqrstuv " + "x" * 33000
    reason, rejected, truncated = _rejected_output_diagnostics(text, ValueError("unexpected provider body"))
    assert reason == "invalid JSON or review contract"
    assert len(rejected) == 32000 and truncated
    assert "abcdefghijklmnopqrstuv" not in rejected
    assert "\x00" not in rejected


def test_review_status_pending_is_not_unavailable_in_russian():
    from digest.application.legacy import _review_status_line
    from digest.domain.editorial.reviews import BlindReviewReport, ModelReview

    bundle = build_evidence_bundle(fixture_articles(), fixture_config().review)
    review = ModelReview(
        "secondary",
        "groq",
        "openai/gpt-oss-120b",
        bundle.bundle_id,
        "hash",
        "unavailable",
        error="pending_independent_review",
    )
    report = BlindReviewReport(1, bundle, [review], "incomplete", None, [], "pending_independent_review")
    text = _review_status_line(report, "ru")
    assert "ожидает отдельного этапа" in text
    assert "secondary" not in text and "unavailable" not in text
    assert "ещё не завершено" in text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cached_slots", "limit", "expected_calls", "held_slots"),
    [
        ((), 0, [], ["primary", "secondary"]),
        ((), 1, ["primary"], ["secondary"]),
        (("primary",), 1, ["secondary"], ["third"]),
        (("secondary",), 1, ["primary"], ["third"]),
        (("primary", "secondary"), 1, ["third"], []),
        (("primary", "secondary", "third"), 0, [], []),
        (("primary", "third"), 1, ["secondary"], []),
        ((), None, ["primary", "secondary", "third"], []),
    ],
)
async def test_new_slot_admission_is_ordered_and_reusable_reviews_are_free(
    cached_slots: tuple[str, ...], limit: int | None, expected_calls: list[str], held_slots: list[str],
) -> None:
    original = await run_fixture(execution=ModelExecution())
    config = fixture_config()
    models = {
        "primary": config.review.primary, "secondary": config.review.secondary, "third": config.review.tie_breaker,
    }
    cached = [review for review in reversed(original.reviews) if review.slot in cached_slots]
    before = deepcopy([asdict(review) for review in cached])
    with patch("digest.application.review.complete", side_effect=fixture_response) as complete:
        report = await run_evidence_review(
            original.evidence, config, cached, execution=ModelExecution(), new_slot_limit=limit,
        )
    assert [call.kwargs["provider_override"].model for call in complete.call_args_list] == [
        models[name].model for name in expected_calls
    ]
    assert [asdict(review) for review in cached] == before
    assert [review.slot for review in report.reviews if review.error == "new_slot_allowance_exhausted"] == held_slots
    for review in report.reviews:
        if review.slot in cached_slots:
            previous = next(item for item in before if item["slot"] == review.slot)
            assert asdict(review) == {**previous, "reused_from_checkpoint": True}
        if review.slot in held_slots:
            previous = next(item for item in original.reviews if item.slot == review.slot)
            assert review == ModelReview(
                previous.slot, previous.provider, previous.model, previous.bundle_id, previous.prompt_hash,
                "unavailable", error="new_slot_allowance_exhausted",
            )
    assert report.status == ("incomplete" if held_slots else "complete")
    if held_slots == ["third"]:
        assert report.third_model_reason == "new_slot_allowance_exhausted"
    elif held_slots:
        assert report.third_model_reason == "incomplete_primary_comparison"


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [-1, True, 1.5, "1"])
async def test_new_slot_limit_rejects_non_integer_or_negative_values_before_requests(limit: object) -> None:
    config = fixture_config()
    bundle = build_evidence_bundle(fixture_articles(), config.review)
    with patch("digest.application.review.complete", AsyncMock()) as complete:
        with pytest.raises(ValueError, match="nonnegative integer"):
            await run_evidence_review(bundle, config, execution=ModelExecution(), new_slot_limit=limit)
    complete.assert_not_called()


@pytest.mark.asyncio
async def test_new_slot_limit_preserves_partial_reuse_and_incomplete_comparison() -> None:
    original = await run_fixture(execution=ModelExecution())
    original.reviews[0].status = "partial"
    original.reviews[0].rejected_items = [RejectedSelection(2, "unknown evidence id")]
    with patch("digest.application.review.complete", AsyncMock()) as complete:
        report = await run_evidence_review(
            original.evidence, fixture_config(), original.reviews, execution=ModelExecution(), new_slot_limit=0,
        )
    complete.assert_not_called()
    assert report.reviews[0].status == "partial" and report.reviews[0].reused_from_checkpoint
    assert len(report.reviews) == 2 and report.status == "incomplete"
    assert report.selection_overlap is None and report.third_model_reason == "incomplete_primary_comparison"


@pytest.mark.asyncio
async def test_new_slot_limit_still_validates_cache_and_denies_wrong_request_binding() -> None:
    original = await run_fixture(execution=ModelExecution())
    config = fixture_config()
    cached = deepcopy(original.reviews)
    cached[0].prompt_hash = "another-request"
    with patch("digest.application.review.complete", AsyncMock()) as complete:
        held = await run_evidence_review(
            original.evidence, config, cached, execution=ModelExecution(), new_slot_limit=0,
        )
    complete.assert_not_called()
    assert held.reviews[0].error == "new_slot_allowance_exhausted"
    assert not held.reviews[0].reused_from_checkpoint
    cached = deepcopy(original.reviews)
    cached[0].selections[0] = replace(cached[0].selections[0], quote="Not supplied.")
    with patch("digest.application.review.complete", AsyncMock()) as complete:
        with pytest.raises(ValueError):
            await run_evidence_review(
                original.evidence, config, cached, execution=ModelExecution(), new_slot_limit=0,
            )
    complete.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("disagreement", [False, True])
async def test_admitted_third_is_not_called_without_disagreement_or_configured_model(disagreement: bool) -> None:
    original = await run_fixture(execution=ModelExecution())
    config = fixture_config()
    if disagreement:
        config.review.tie_breaker = None
    else:
        original.reviews[1].selections = deepcopy(original.reviews[0].selections)
    with patch("digest.application.review.complete", AsyncMock()) as complete:
        report = await run_evidence_review(
            original.evidence, config, original.reviews[:2], execution=ModelExecution(), new_slot_limit=1,
        )
    complete.assert_not_called()
    assert report.status == "complete" and len(report.reviews) == 2
    assert report.third_model_reason == ("third_model_not_configured" if disagreement else "no_selection_disagreement")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "attempted_at", "expected"),
    [
        ("new_slot_allowance_exhausted", None, "request allowance exhausted (not attempted)"),
        ("new_slot_allowance_exhausted", "2026-10-12T01:00:00+00:00", "new attempt"),
        ("RuntimeError", None, "new attempt"),
    ],
)
async def test_render_marks_only_closed_unattempted_exhaustion(
    error: str, attempted_at: str | None, expected: str,
) -> None:
    original = await run_fixture(execution=ModelExecution())
    original.reviews = [replace(
        original.reviews[0], status="unavailable", selections=[], error=error, attempted_at=attempted_at,
    )]
    markdown = render_review(original)
    assert f"Provenance: {expected};" in markdown
    if expected == "request allowance exhausted (not attempted)":
        assert "Resolved model: not requested" in markdown
        assert "Review not attempted: new_slot_allowance_exhausted" in markdown
        assert "not reported by provider" not in markdown and "new attempt" not in markdown
