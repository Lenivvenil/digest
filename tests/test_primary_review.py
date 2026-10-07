"""Primary delivery is bounded and independent review remains visibly pending."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from digest.cli.reporting import publish_review_checkpoint as _publish_review_checkpoint
from digest.config import ProviderConfig
from digest.delivery import ArticleDeliveryResult
from digest.main import RunStats, main
from digest.review import primary_cards, render_review, run_evidence_review, run_primary_review
from scripts.review_fixture import fixture_articles, fixture_config, fixture_response


@pytest.mark.asyncio
@pytest.mark.parametrize("abstain", [False, True])
async def test_valid_primary_stops_without_peer_or_third(abstain: bool) -> None:
    config = fixture_config()
    config.llm.max_retries = 3
    original = deepcopy(config)

    async def adapter(role: Any, messages: list[dict[str, str]], used: Any, **kwargs: Any) -> tuple:
        assert used is not config and used.llm is not config.llm
        assert used.llm.max_retries == 0
        assert kwargs["provider_override"].model == config.review.primary.model
        if abstain:
            return json.dumps({"selections": [], "limitations": ["Insufficient useful evidence"]}), {}
        return await fixture_response(role, messages, used, **kwargs)

    with patch("digest.review.complete", side_effect=adapter) as complete:
        report = await run_primary_review(fixture_articles(), config)
    complete.assert_awaited_once()
    assert config == original
    assert report.status == "incomplete"
    assert report.selection_overlap is None and report.disputed_ids == []
    assert report.third_model_reason == "pending_independent_review"
    assert [review.slot for review in report.reviews] == ["primary", "secondary"]
    primary, secondary = report.reviews
    assert primary.status == ("abstained" if abstain else "ok")
    assert primary.attempted_at and primary.generated_at
    assert secondary.status == "unavailable" and secondary.error == "pending_independent_review"
    assert secondary.provider == config.review.secondary.provider
    assert secondary.model == config.review.secondary.model
    assert secondary.attempted_at is secondary.generated_at is None
    assert secondary.response_sha256 is None and secondary.usage == {}
    assert primary.prompt_hash == secondary.prompt_hash
    assert primary.bundle_id == secondary.bundle_id == report.evidence.bundle_id
    assert "pending independent review (not attempted)" in render_review(report)
    assert bool(primary_cards(report, fixture_articles(), "en")) is not abstain


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["unavailable", "invalid"])
@pytest.mark.parametrize("fallback", ["ok", "abstained", "unavailable", "invalid"])
async def test_primary_failure_attempts_only_secondary_once(failure: str, fallback: str) -> None:
    config = fixture_config()
    config.llm.max_retries = 3
    calls = []

    async def adapter(role: Any, messages: list[dict[str, str]], used: Any, **kwargs: Any) -> tuple:
        model = kwargs["provider_override"].model
        calls.append((model, deepcopy(messages)))
        assert used.llm.max_retries == 0
        status = failure if model == config.review.primary.model else fallback
        if status == "unavailable":
            raise RuntimeError("provider unavailable")
        if status == "invalid":
            return "not valid review JSON", {}
        if status == "abstained":
            return '{"selections": [], "limitations": ["No useful evidence"]}', {}
        return await fixture_response(role, messages, used, **kwargs)

    with patch("digest.review.complete", side_effect=adapter):
        report = await run_primary_review(fixture_articles(), config)
    assert [model for model, _ in calls] == [config.review.primary.model, config.review.secondary.model]
    assert calls[0][1] == calls[1][1]
    assert [review.status for review in report.reviews] == [failure, fallback]
    assert all(review.attempted_at for review in report.reviews)
    assert report.status == "incomplete" and report.selection_overlap is None
    assert report.third_model_reason == "pending_independent_review"
    assert bool(primary_cards(report, fixture_articles(), "en")) is (fallback == "ok")
    assert config.llm.max_retries == 3


@pytest.mark.asyncio
async def test_primary_and_later_reviews_share_exact_prompt_and_bundle() -> None:
    config = fixture_config()
    prompts = []

    async def adapter(role: Any, messages: list[dict[str, str]], used: Any, **kwargs: Any) -> tuple:
        prompts.append(deepcopy(messages))
        return await fixture_response(role, messages, used, **kwargs)

    with patch("digest.review.complete", side_effect=adapter):
        first = await run_primary_review(fixture_articles(), config)
        final = await run_evidence_review(first.evidence, config, first.reviews)
    assert len(prompts) == 3
    assert all(prompt == prompts[0] for prompt in prompts)
    assert final.evidence == first.evidence and final.status == "complete"
    assert len({review.prompt_hash for review in first.reviews + final.reviews}) == 1
    assert final.reviews[0].reused_from_checkpoint
    assert final.reviews[0].attempted_at == first.reviews[0].attempted_at
    assert all(review.attempted_at for review in final.reviews)


@pytest.mark.asyncio
async def test_delivery_retries_disabled_in_real_completion_wrapper() -> None:
    import httpx

    config = fixture_config()
    config.llm.max_retries = 3
    client = AsyncMock()
    client.__aenter__.return_value = client
    with (
        patch("httpx.AsyncClient", return_value=client),
        patch("digest.llm._call_provider", AsyncMock(side_effect=httpx.ReadTimeout("offline"))) as provider,
    ):
        report = await run_primary_review(fixture_articles(), config)
    assert provider.await_count == 2
    assert [review.status for review in report.reviews] == ["unavailable", "unavailable"]
    assert config.llm.max_retries == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "cards", "state"])
async def test_checkpoint_output_only_after_confirmed_cards_archive_and_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str | None,
) -> None:
    from digest.source_scorer import save_source_category_map

    monkeypatch.chdir(tmp_path)
    output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-local-test-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "fake-local-test-chat")
    config = fixture_config()
    config.review.review_led_only = True
    config.telegram.enabled = config.telegram.required = config.obsidian.enabled = True
    config.obsidian.output_dir = "digests"
    config.llm.providers = [ProviderConfig("gemini", config.review.primary.model, ["summarize"])]
    events = []

    async def cards(*args: Any, **kwargs: Any) -> ArticleDeliveryResult:
        assert not output.exists()
        assert len(list((tmp_path / "digests").glob("*.review.json"))) == 1
        events.append("cards")
        return ArticleDeliveryResult(attempted=2, sent=0 if failure == "cards" else 2,
                                     failed=2 if failure == "cards" else 0)

    def save_state(*args: Any, **kwargs: Any) -> None:
        assert not output.exists()
        assert events == ["cards"]
        events.append("state")
        if failure == "state":
            raise OSError("State save failed")
        save_source_category_map(*args, **kwargs)

    client = AsyncMock()
    client.__aenter__.return_value = client
    client.get.side_effect = client.post.side_effect = AssertionError("No live HTTP")
    with (
        patch("httpx.AsyncClient", return_value=client),
        patch("digest.config.load_config", return_value=config),
        patch("digest.radar.collect", AsyncMock(return_value=(fixture_articles(), {}))),
        patch("digest.review.complete", side_effect=fixture_response) as complete,
        patch("digest.radar.summarize_all", AsyncMock(side_effect=AssertionError("No summaries"))) as summaries,
        patch("digest.radar.pick_top_articles", AsyncMock(side_effect=AssertionError("No picker"))) as picker,
        patch("digest.application.investigation.run_irritator",
              AsyncMock(side_effect=AssertionError("No Irritator"))) as irritator,
        patch("digest.delivery.send_article_cards", side_effect=cards),
        patch("digest.delivery.send_counter_signals", AsyncMock()) as counter_signals,
        patch("digest.delivery.telegram._send_chunk", AsyncMock()) as footer,
        patch("digest.delivery.telegram.escape_markdownv2", side_effect=lambda text: text),
        patch("digest.source_scorer.save_source_category_map", side_effect=save_state),
    ):
        exit_code = await main(["--config", "fixture.yaml"])
    assert exit_code == (1 if failure else 0)
    assert events == ["cards", "state"]
    complete.assert_awaited_once()
    summaries.assert_not_called()
    picker.assert_not_called()
    irritator.assert_not_called()
    counter_signals.assert_not_called()
    client.get.assert_not_called()
    client.post.assert_not_called()
    footer.assert_awaited_once()
    assert "Independent comparison and counter-signal stage postponed" in footer.call_args.args[3]
    archive = next((tmp_path / "digests").glob("*.review.json"))
    payload = json.loads(archive.read_text())
    assert payload["status"] == "incomplete"
    assert payload["reviews"][1]["attempted_at"] is None
    markdown = next((tmp_path / "digests").glob("*.md")).read_text()
    assert "Independent comparison and counter-signal stage postponed" in markdown
    assert "0 narratives" not in markdown
    if failure:
        assert not output.exists()
    else:
        assert output.read_text() == f"review_checkpoint={archive.relative_to(tmp_path).as_posix()}\n"
        assert (tmp_path / ".cache" / "feedback.json").exists()


@pytest.mark.parametrize("kind", ["outside", "unsafe", "not_generated", "missing", "mismatch"])
def test_checkpoint_output_rejects_untrusted_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str,
) -> None:
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    output = work / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    directory = tmp_path if kind == "outside" else work / ("unsafe\nname" if kind == "unsafe" else "digests")
    directory.mkdir(exist_ok=True)
    markdown = directory / ("arbitrary.md" if kind == "not_generated" else "2026-09-30.md")
    markdown.write_text("test")
    checkpoint = markdown.with_suffix(".review.json")
    if kind != "missing":
        checkpoint.write_text("{}")
    stats = RunStats(1, 1, 1, True, False, True, str(markdown), review_status="incomplete",
                     review_checkpoint=str(checkpoint))
    if kind == "mismatch":
        stats.markdown_path = str(directory / "2026-09-29.md")
        Path(stats.markdown_path).write_text("other")
    _publish_review_checkpoint(stats)
    assert not output.exists()


@pytest.mark.parametrize("args", [["--dry-run"], ["--radar-only"]])
@pytest.mark.asyncio
async def test_non_delivery_modes_never_publish_checkpoint(args: list[str]) -> None:
    stats = RunStats(1, 1, 1, False, False, False, "")
    with (
        patch("digest.main.run", AsyncMock(return_value=stats)),
        patch("digest.cli.reporting.publish_review_checkpoint") as publish,
    ):
        assert await main(args) == 0
    publish.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("finish", [None, "MAX_TOKENS"])
async def test_reading_primary_incomplete_completion_never_implies_editorial_rejection(finish: str | None) -> None:
    from digest.candidate_dispositions import CandidateDispositionCapture
    from digest.config import ReadingBriefConfig

    config = fixture_config()
    config.reading_brief = ReadingBriefConfig(True, "gemini", "gemini-3.8-flash")

    async def select(*args: Any, **kwargs: Any) -> Any:
        text, usage = await fixture_response(*args, **kwargs)
        return text, usage | {"finish_reason": finish}

    capture = CandidateDispositionCapture()
    with patch("digest.review.complete", side_effect=select):
        report = await run_primary_review(fixture_articles(), config, disposition_capture=capture)
    assert bool(report.reviews[0].selections) is (finish is None)
    if finish is not None:
        assert report.reviews[0].error == "provider reported unfinished response"
    assert capture.attempts[0].status == "incomplete"
    assert not any(item.status in {"not_selected", "duplicate"} for item in capture.attempts[0].dispositions)
