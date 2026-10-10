"""Category admission keeps historical policy while verifying its persisted handoff."""

from __future__ import annotations

import json
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from digest.adapters.models.execution import ModelExecution
from digest.adapters.storage.edition import READY_FILE
from digest.application import preparation as application
from digest.config import SourceConfig
from digest.domain.catalog.sources import SourceStateStore
from digest.domain.editorial.reviews import BlindReviewReport
from digest.edition_runtime import FrozenPreparation
from digest.feedback import FeedbackStore
from digest.irritator import IrritatorStatus
from digest.preparation import AcceptedPreparation, load_accepted_preparation, load_preparation
from digest.radar.collector import Article
from digest.radar.summarizer import ArticleSummary, CategorySummary
from scripts.review_fixture import fixture_config
from tests.test_preparation import _snapshot


@pytest.fixture
def category_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[application.CategoryAnalysis, application.CollectedArticles, application.PreparationRun]:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    config = fixture_config()
    config.review.enabled = False
    config.review.review_led_only = False
    config.telegram.delivery_mode = "compact"
    config.obsidian.enabled = True
    config.obsidian.output_dir = "digests"
    config.radar.perspectives = True
    cards = [
        ArticleSummary("Systems", "https://example.com/systems", "Source A", "Tech", "Systems claim"),
        ArticleSummary("Community", "https://example.com/community", "Source B", "World", "Community claim"),
    ]
    summaries = [
        CategorySummary(
            "Tech", "Systems prose. 🟢 Optimist: capacity. 🔴 Skeptic: cost. ⚖️ Realist: trial.", 3, cards[:1]
        ),
        CategorySummary("World", "Distinct community prose.", 2, cards[1:]),
    ]
    articles = {
        card.category: [Article(card.title, card.link, "Source evidence", card.source, card.category, None)]
        for card in cards
    }
    config.sources = [
        SourceConfig(card.source, f"https://example.com/{index}/rss", card.category, True)
        for index, card in enumerate(cards)
    ]
    collected = application.CollectedArticles(config, articles, {}, False, 2)
    run = application.PreparationRun(config, ModelExecution(), SourceStateStore(), {}, FeedbackStore(), True, 7)
    monkeypatch.setattr(
        "digest.application.investigation.run_irritator",
        AsyncMock(return_value=([], [], IrritatorStatus("Offline fixture", "empty"))),
    )
    return (
        application.CategoryAnalysis(summaries, "Cross-category trend: shared infrastructure.", cards, None),
        collected,
        run,
    )


def category_report(*reviews: tuple[str, str]) -> BlindReviewReport:
    report = _snapshot().review_report
    assert report is not None
    original = report.reviews[0]
    report.reviews = [
        replace(
            original,
            slot=slot,
            status=status,
            selections=original.selections if status in {"ok", "partial"} else [],
            limitations=["No suitable edition from this evidence."] if status == "abstained" else [],
        )
        for slot, status in reviews
    ]
    return report


async def test_selected_category_presents_independently_restored_acceptance(
    category_run: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from digest import preparation as storage

    work, collected, run = category_run
    expected = application._snapshot(work, collected, run.config)
    order: list[str] = []
    save, load = storage.save_preparation, storage.load_accepted_preparation

    def saved(*args: Any, **kwargs: Any) -> Path:
        path = save(*args, **kwargs)
        order.append("save")
        return path

    def loaded(*args: Any, **kwargs: Any) -> AcceptedPreparation | None:
        accepted = load(*args, **kwargs)
        if accepted is not None:
            order.append("readback")
        return accepted

    async def presented(accepted: AcceptedPreparation, *args: Any, **kwargs: Any) -> FrozenPreparation:
        assert order == ["save", "readback", "statistics", "map"]
        assert accepted == load_accepted_preparation()
        assert accepted.snapshot == expected
        assert accepted.snapshot.summaries is not work.summaries
        assert accepted.snapshot.top_articles is not work.cards
        order.append("presentation")
        return FrozenPreparation("ready", "fixture-ready-hash", "not_requested", len(expected.combined), None, "")

    monkeypatch.setattr(storage, "save_preparation", saved)
    monkeypatch.setattr(storage, "load_accepted_preparation", loaded)
    monkeypatch.setattr(application, "_save_prepared_fetch_stats", lambda *args: order.append("statistics"))
    monkeypatch.setattr("digest.adapters.storage.sources.save_source_category_map", lambda *args: order.append("map"))
    monkeypatch.setattr("digest.edition_runtime.present_preparation", presented)
    stats = await application._prepare_category_edition(work, collected, run, verbose=False, publication_date=None)
    assert stats.edition_status == "ready" and stats.feedback_collected == 7
    assert order == ["save", "readback", "statistics", "map", "presentation"]


@pytest.mark.parametrize(
    "reviews,status,persisted",
    [
        ((), "no_ready", False),
        ((("primary", "unavailable"), ("secondary", "abstained")), "selection_incomplete", False),
        ((("secondary", "abstained"), ("primary", "ok")), "no_ready", False),
        ((("secondary", "ok"), ("primary", "abstained")), "selection_incomplete", False),
        ((("secondary", "abstained"), ("primary", "abstained")), "no_ready", True),
    ],
)
async def test_empty_category_retains_report_order_and_save_policy(
    category_run: Any,
    monkeypatch: pytest.MonkeyPatch,
    reviews: tuple[tuple[str, str], ...],
    status: str,
    persisted: bool,
) -> None:
    from digest import preparation as storage

    work, collected, run = category_run
    work.cards = []
    work.report = category_report(*reviews) if reviews else None
    expected = application._snapshot(work, collected, run.config)
    assert expected.summaries and expected.combined
    effects: list[str] = []
    monkeypatch.setattr(application, "_save_prepared_fetch_stats", lambda *args: effects.append("statistics"))
    monkeypatch.setattr("digest.adapters.storage.sources.save_source_category_map", lambda *args: effects.append("map"))
    if not persisted:
        monkeypatch.setattr(
            storage, "load_accepted_preparation", lambda *args, **kwargs: pytest.fail("No admission read")
        )
        monkeypatch.setattr(storage, "save_preparation", lambda *args, **kwargs: pytest.fail("No admission write"))
    stats = await application._prepare_category_edition(work, collected, run, verbose=False, publication_date=None)
    assert stats.edition_status == status
    assert stats.review_status == (work.report.status if work.report is not None else "not_requested")
    assert stats.feedback_collected == 7 and effects == ["statistics", "map"]
    assert Path(".cache/pending_preparation.json").exists() is persisted
    assert not Path(".cache", READY_FILE).exists() and not Path("digests").exists()
    if persisted:
        assert load_accepted_preparation().snapshot == expected


@pytest.mark.parametrize("failure", ["save", "readback"])
async def test_category_save_verification_failure_blocks_operational_effects(
    category_run: Any,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    from digest import preparation as storage

    work, collected, run = category_run
    save = storage.save_preparation
    retained: list[bytes] = []
    effects: list[str] = []

    def saved(*args: Any, **kwargs: Any) -> Path:
        if failure == "save":
            raise OSError("synthetic save failure")
        path = save(*args, **kwargs)
        path.write_text("corrupt checkpoint after save")
        retained.append(path.read_bytes())
        return path

    monkeypatch.setattr(storage, "save_preparation", saved)
    monkeypatch.setattr(application, "_save_prepared_fetch_stats", lambda *args: effects.append("statistics"))
    monkeypatch.setattr("digest.adapters.storage.sources.save_source_category_map", lambda *args: effects.append("map"))
    presentation = AsyncMock(side_effect=AssertionError("No presentation after failed readback"))
    monkeypatch.setattr("digest.edition_runtime.present_preparation", presentation)
    with pytest.raises((ValueError, OSError)):
        await application._prepare_category_edition(work, collected, run, verbose=False, publication_date=None)
    assert effects == []
    presentation.assert_not_awaited()
    path = Path(".cache/pending_preparation.json")
    assert path.exists() is (failure != "save")
    if retained:
        assert path.read_bytes() == retained[0]


@pytest.mark.parametrize("failure", ["statistics", "map", "entry_readback"])
async def test_category_failure_retains_exact_completed_operational_prefix(
    category_run: Any,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    work, collected, run = category_run
    expected = application._snapshot(work, collected, run.config)
    order: list[str] = []
    saved: list[bytes] = []

    def effect(name: str) -> None:
        assert load_preparation() == expected
        path = Path(".cache/pending_preparation.json")
        if not saved:
            saved.append(path.read_bytes())
        order.append(name)
        if failure == name:
            raise OSError(f"synthetic {name} failure")
        Path(f".cache/{name}-effect").write_text(name)
        if name == "map" and failure == "entry_readback":
            path.write_text("Corrupted after both operational writes")
            saved[0] = path.read_bytes()

    monkeypatch.setattr(application, "_save_prepared_fetch_stats", lambda *args: effect("statistics"))
    monkeypatch.setattr("digest.adapters.storage.sources.save_source_category_map", lambda *args: effect("map"))
    assembly = AsyncMock(side_effect=AssertionError("No publication effects"))
    monkeypatch.setattr("digest.application.publication.assemble_publication", assembly)
    with pytest.raises((ValueError, OSError)):
        await application._prepare_category_edition(work, collected, run, verbose=False, publication_date=None)
    assert order == (["statistics"] if failure == "statistics" else ["statistics", "map"])
    assert Path(".cache/statistics-effect").exists() is (failure != "statistics")
    assert Path(".cache/map-effect").exists() is (failure == "entry_readback")
    assert Path(".cache/pending_preparation.json").read_bytes() == saved[0]
    assembly.assert_not_awaited()


async def test_category_content_survives_freeze_failure_and_same_day_recovery(
    category_run: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from digest.edition_runtime import recover_preparation
    from digest.main import run as main_run
    from digest.presentation.telegram import escape_markdownv2

    work, collected, run = category_run
    expected = application._snapshot(work, collected, run.config)

    def fail_freeze(*args: Any, **kwargs: Any) -> None:
        raise OSError("synthetic freeze failure")

    with monkeypatch.context() as failing:
        failing.setattr("digest.application.prepared_delivery.prepare_edition", fail_freeze)
        with pytest.raises(OSError, match="synthetic freeze failure"):
            await application._prepare_category_edition(work, collected, run, verbose=False, publication_date=None)
    accepted = recover_preparation()
    assert isinstance(accepted, AcceptedPreparation) and accepted.snapshot == expected
    assert accepted.snapshot.summaries is not work.summaries
    monkeypatch.setattr(application, "_collect_and_review", AsyncMock(side_effect=AssertionError("No reselection")))
    monkeypatch.setattr("digest.config.load_config", lambda _: run.config)
    stats = await main_run("config.yaml", False, False, False, feedback_precollected=True, prepare_only=True)
    assert stats.edition_status == "ready" and stats.markdown_saved
    manifest = json.loads(Path(".cache", READY_FILE).read_text())
    metadata = manifest["canonical_metadata"]
    assert metadata["combined"] == expected.combined
    assert metadata["cards"] == [asdict(card) for card in work.cards]
    assert metadata["contributing_sources"] == ["Source A", "Source B"]
    assert metadata["source_count"] == metadata["article_count"] == 2
    assert [(summary.category, summary.article_count) for summary in accepted.snapshot.summaries] == [
        ("Tech", 3),
        ("World", 2),
    ]
    archive = Path(stats.markdown_path).read_text()
    payload = "\n".join(item["text"] for item in manifest["payloads"])
    for text in [*(summary.summary_text for summary in work.summaries), work.trends]:
        assert text in archive and escape_markdownv2(text) in payload
    assert archive.index(work.summaries[0].summary_text) < archive.index(work.summaries[1].summary_text)
    assert load_preparation() is None


async def test_accepted_empty_category_recovers_without_advancing_collection(
    category_run: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from digest.main import run as main_run

    work, collected, run = category_run
    work.cards = []
    work.report = category_report(("primary", "abstained"))
    first = await application._prepare_category_edition(work, collected, run, verbose=False, publication_date=None)
    assert first.edition_status == "no_ready"
    path = Path(".cache/pending_preparation.json")
    retained = path.read_bytes()
    collection = AsyncMock(side_effect=AssertionError("Accepted empty work must not advance collection"))
    monkeypatch.setattr(application, "_collect_and_review", collection)
    monkeypatch.setattr("digest.config.load_config", lambda _: run.config)
    resumed = await main_run("config.yaml", False, False, False, feedback_precollected=True, prepare_only=True)
    assert resumed.edition_status == "no_ready" and resumed.review_status == work.report.status
    assert path.read_bytes() == retained and not Path(".cache", READY_FILE).exists()
    collection.assert_not_awaited()


async def test_clear_failure_keeps_ready_precedence_over_verified_category_checkpoint(
    category_run: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from digest.edition_runtime import ExistingEdition, recover_preparation

    work, collected, run = category_run
    expected = application._snapshot(work, collected, run.config)

    def fail_clear() -> None:
        assert Path(".cache", READY_FILE).exists()
        assert load_preparation() == expected
        raise OSError("synthetic clear failure after readiness")

    monkeypatch.setattr("digest.preparation.clear_preparation", fail_clear)
    with pytest.raises(OSError, match="clear failure after readiness"):
        await application._prepare_category_edition(work, collected, run, verbose=False, publication_date=None)
    retained = {path: path.read_bytes() for path in Path(".cache").glob("*.json")}
    monkeypatch.setattr(
        "digest.preparation.load_accepted_preparation",
        lambda *args, **kwargs: pytest.fail("Ready precedence must avoid pending read"),
    )
    recovered = recover_preparation()
    assert isinstance(recovered, ExistingEdition) and recovered.status == "ready"
    assert {path: path.read_bytes() for path in retained} == retained
