"""Saved selection admission and internal reports only; all network and models are forbidden."""

from __future__ import annotations

import json
import textwrap
from dataclasses import asdict
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from digest import editorial_enrichment as enrichment
from digest.config import Config
from digest.editorial_state import EditorialState, admit_articles, load_state, store_state
from digest.editorial_worker import WorkerResult, summarize_state
from digest.radar.collector import Article, article_hash
from digest.review import BlindReviewReport, EvidenceSelection, ModelReview, build_evidence_bundle
from scripts.review_fixture import fixture_config
from tests.test_config import MINIMAL_CONFIG


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("Live HTTP or model calls are forbidden in enrichment tests")

    monkeypatch.setattr("httpx.AsyncClient", forbidden)
    monkeypatch.setattr("digest.review.complete", forbidden)
    monkeypatch.setattr("digest.editorial_worker.complete", forbidden)


@pytest.fixture
def checkpoint(tmp_path: Path) -> tuple[Path, Config, dict[str, Any]]:
    config = fixture_config()
    config.review.max_evidence_articles = 2
    originals = [Article(f"Item  {index}", f"https://example.com/{index}", "Exact synthetic excerpt.",
                         "Fixture", "Architecture", None) for index in range(3)]
    bundle = build_evidence_bundle({"Architecture": originals}, config.review)
    selections = [EvidenceSelection(bundle.items[0].evidence_id, "Selection opinion only.", "Exact synthetic", "high")]
    primary = ModelReview("primary", "groq", "fixture-primary", bundle.bundle_id, "a" * 64, "ok", selections)
    secondary = ModelReview("secondary", "gemini", "fixture-secondary", bundle.bundle_id, "b" * 64, "ok", [
        EvidenceSelection(bundle.items[1].evidence_id, "Second selection.", "synthetic excerpt", "medium"),
    ])
    report = BlindReviewReport(1, bundle, [primary, secondary], "complete", 0, [], "not_needed")
    raw = asdict(report)
    path = tmp_path / "review.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return path, config, raw


def rewrite(path: Path, raw: dict[str, Any]) -> None:
    path.write_text(json.dumps(raw), encoding="utf-8")


def test_selection_maps_sanitized_identity_and_distinguishes_unreviewed(checkpoint: tuple) -> None:
    path, config, raw = checkpoint
    before = path.read_bytes()
    manifest, articles = enrichment.load_selection(path, config)
    identity = raw["evidence"]["items"][0]["evidence_id"]
    assert manifest.in_bundle_selected == (identity,)
    assert len(manifest.in_bundle_unselected) == 1 and manifest.omitted_unreviewed == 1
    assert articles[0].description == "Exact synthetic excerpt."
    assert articles[0].title == "Item 0"
    worker_id = article_hash(articles[0].title, articles[0].link)
    assert identity != worker_id
    assert manifest.article_ids_by_evidence_id == {identity: worker_id}
    assert "Selection opinion" not in articles[0].description
    assert path.read_bytes() == before


@pytest.mark.parametrize("status,expected_slot,count", [
    ("ok", "primary", 1), ("invalid", "secondary", 1), ("unavailable", "secondary", 1),
    ("abstained", "primary", 0),
])
def test_primary_fallback_and_abstention_semantics(checkpoint: tuple, status: str,
                                                 expected_slot: str, count: int) -> None:
    path, config, raw = checkpoint
    primary = raw["reviews"][0]
    primary["status"] = status
    if status != "ok":
        primary["selections"] = []
        primary["limitations"] = ["No selection in this bounded bundle."]
    raw["reviews"].reverse()  # Slot identity, not arbitrary saved list ordering.
    rewrite(path, raw)
    manifest, articles = enrichment.load_selection(path, config)
    assert manifest.selection_slot == expected_slot and len(articles) == count


@pytest.mark.parametrize("mutation", ["quote", "unknown_id", "duplicate", "status", "bundle", "type", "hash"])
def test_untrusted_checkpoint_cannot_admit_work(checkpoint: tuple, mutation: str, tmp_path: Path) -> None:
    path, config, raw = checkpoint
    primary = raw["reviews"][0]
    if mutation == "quote":
        primary["selections"][0]["quote"] = "Invented source quote."
    elif mutation == "unknown_id":
        primary["selections"][0]["evidence_id"] = "unknown"
    elif mutation == "duplicate":
        primary["selections"].append(primary["selections"][0].copy())
    elif mutation == "status":
        primary["status"] = "abstained"
    elif mutation == "bundle":
        primary["bundle_id"] = "c" * 64
    elif mutation == "type":
        primary["selections"][0]["quote"] = 123
    else:
        raw["evidence"]["items"][0]["excerpt"] = "Tampered source"
    rewrite(path, raw)
    state_dir = tmp_path / "state"
    with pytest.raises(ValueError):
        enrichment.prepare_selected_state(config, state_dir, path)
    assert not (state_dir / "state.json").exists()
    assert not (state_dir / "selections").exists()


def test_resume_is_idempotent_and_uses_saved_checkpoint(checkpoint: tuple, tmp_path: Path) -> None:
    path, config, _ = checkpoint
    directory = tmp_path / "state"
    first = enrichment.prepare_selected_state(config, directory, path)
    state_before = (directory / "state.json").read_bytes()
    assert enrichment.prepare_selected_state(config, directory, path) == first
    assert (directory / "state.json").read_bytes() == state_before
    path.unlink()
    assert enrichment.prepare_selected_state(config, directory) == first
    assert len(load_state(directory).articles) == 1
    assert len(list((directory / "selections").glob("*.review.json"))) == 1


def test_multiple_saved_shortlists_resume_only_their_selected_union(checkpoint: tuple, tmp_path: Path) -> None:
    path, config, raw = checkpoint
    directory = tmp_path / "state"
    enrichment.prepare_selected_state(config, directory, path)
    raw["reviews"][0]["status"] = "unavailable"
    raw["reviews"][0]["selections"] = []
    rewrite(path, raw)
    manifests = enrichment.prepare_selected_state(config, directory, path)
    assert len(manifests) == 2 and len(load_state(directory).articles) == 2
    assert enrichment.prepare_selected_state(config, directory) == sorted(
        manifests, key=lambda manifest: manifest.checkpoint_sha256,
    )


def test_partial_primary_keeps_only_valid_saved_selections(checkpoint: tuple) -> None:
    path, config, raw = checkpoint
    raw["reviews"][0]["status"] = "partial"
    raw["reviews"][0]["rejected_items"] = [{"index": 1, "reason": "unknown evidence id", "evidence_id": None}]
    rewrite(path, raw)
    manifest, articles = enrichment.load_selection(path, config)
    assert manifest.selection_slot == "primary" and manifest.selection_status == "partial"
    assert len(articles) == 1


@pytest.mark.parametrize("target", ["checkpoint", "manifest", "article"])
def test_resume_detects_changed_saved_provenance(checkpoint: tuple, tmp_path: Path, target: str) -> None:
    path, config, _ = checkpoint
    directory = tmp_path / "state"
    enrichment.prepare_selected_state(config, directory, path)
    if target == "article":
        state = load_state(directory)
        next(iter(state.articles.values())).description = "Changed stored input"
        store_state(state, directory)
    else:
        suffix = "review" if target == "checkpoint" else "manifest"
        saved = next((directory / "selections").glob(f"*.{suffix}.json"))
        raw = json.loads(saved.read_text())
        if target == "checkpoint":
            raw["selection_overlap"] = 0.7
        else:
            raw["omitted_unreviewed"] = 0
        rewrite(saved, raw)
    with pytest.raises(ValueError):
        enrichment.prepare_selected_state(config, directory)


def test_all_admit_state_is_refused_without_mutation(checkpoint: tuple, tmp_path: Path) -> None:
    path, config, _ = checkpoint
    directory = tmp_path / "existing"
    state = EditorialState()
    admit_articles(state, [Article("Unrelated", "https://example.com/unrelated", "Other input", "X", "Y", None)])
    store_state(state, directory)
    before = (directory / "state.json").read_bytes()
    with pytest.raises(ValueError, match="unrelated articles"):
        enrichment.prepare_selected_state(config, directory, path)
    assert (directory / "state.json").read_bytes() == before
    assert not (directory / "selections").exists()


@pytest.mark.asyncio
async def test_default_report_and_resume_never_invoke_worker(checkpoint: tuple, tmp_path: Path,
                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    path, config, _ = checkpoint
    monkeypatch.setattr(enrichment, "enrichment_config", lambda _: config)
    worker = AsyncMock(side_effect=AssertionError("Report-only must not execute"))
    monkeypatch.setattr(enrichment, "run_editorial_pass", worker)
    output, directory = tmp_path / "report", tmp_path / "state"
    args = ["--state", str(directory), "--output", str(output)]
    assert await enrichment.main(args + ["--checkpoint", str(path)]) == 0
    assert await enrichment.main(args) == 0
    worker.assert_not_called()
    report = json.loads((output / "enrichment-report.json").read_text())
    assert report["status"] == "internal_drafts_not_fact_verified"
    assert report["summary"]["admitted"] == 1 and report["summary"]["acquired"] == 0
    assert report["summary"]["rejected"] == 0 and report["drafts"] == []
    markdown = (output / "enrichment-report.md").read_text()
    assert "Not fact-verified" in markdown and "omitted_unreviewed: 1" in markdown


@pytest.mark.asyncio
async def test_execute_passes_only_selected_articles_and_budget(checkpoint: tuple, tmp_path: Path,
                                                               monkeypatch: pytest.MonkeyPatch) -> None:
    path, config, raw = checkpoint
    monkeypatch.setattr(enrichment, "enrichment_config", lambda _: config)

    async def run(config: Config, state_dir: Path, articles: list[Article], **kwargs: Any) -> WorkerResult:
        assert [article.link for article in articles] == [raw["evidence"]["items"][0]["url"]]
        assert kwargs == {"deadline_seconds": 30.0, "max_calls": 1}
        state = load_state(state_dir)
        return WorkerResult(state, summarize_state(state, config))

    worker = AsyncMock(side_effect=run)
    monkeypatch.setattr(enrichment, "run_editorial_pass", worker)
    assert await enrichment.main(["--checkpoint", str(path), "--state", str(tmp_path / "state"),
                                  "--output", str(tmp_path / "report"), "--execute",
                                  "--deadline-seconds", "30", "--max-calls", "1"]) == 0
    worker.assert_awaited_once()


@pytest.mark.parametrize("language,expected", [(None, "en"), ("null", "en"), ("ru", "ru"), ("en", "en")])
def test_entry_language_default_preserves_explicit_config(tmp_path: Path, language: str | None, expected: str) -> None:
    path = tmp_path / "config.yaml"
    text = textwrap.dedent(MINIMAL_CONFIG)
    if language == "null":
        text += "\nradar: null\n"
    elif language is not None:
        text += f"\nradar:\n  language: {language}\n"
    path.write_text(text)
    before = path.read_bytes()
    config = enrichment.enrichment_config(path)
    assert config.radar.language == expected
    assert config.llm.max_retries == 0
    assert path.read_bytes() == before
