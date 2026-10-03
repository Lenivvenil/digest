"""Immutable source and selection binding, atomic progress, and safe state paths."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from digest.article_source import FetchedArticle
from digest.reading_brief_state import (
    BriefState,
    Page,
    Route,
    Selection,
    checksum,
    load_source,
    load_state,
    make_spans,
    now,
    save_source,
    save_state,
    state_root,
)
from tests.factories import make_article


def state() -> BriefState:
    return BriefState(Selection.from_article(make_article()), Route("gemini", "gemini-3.8-flash", 1_048_576, 2048),
                      now(), now())


def test_offsets_reconstruct_every_character_without_a_source_window() -> None:
    text = "\n\nOpening.\n\n" + "Word " * 900 + "\n\nFINAL material exception.\n"
    spans = make_spans(text)
    assert "".join(text[span.start:span.end] for span in spans) == text
    assert spans[0].start == 0 and spans[-1].end == len(text)
    assert [span.id for span in spans] == list(range(1, len(spans) + 1))
    assert all(left.end == right.start for left, right in zip(spans, spans[1:], strict=False))


def test_source_snapshot_and_state_roundtrip_are_bound_to_selection(tmp_path: Path) -> None:
    item = state()
    fetched = FetchedArticle("Full public body.\n\nLate exception.", "https://example.com/final", now(), None,
                             "article", ("Text only; uninspected images.",))
    item.source_sha256, original = save_source(tmp_path, item.selection, fetched)
    item.pages = [Page(0, len(original.spans))]
    save_state(tmp_path, item)
    restored = load_state(tmp_path, item.selection.identity)
    assert restored == item and load_source(tmp_path, restored) == original
    assert original.coverage_notes == ["Text only; uninspected images."]
    assert not list(state_root(tmp_path).glob("*.tmp"))
    restored.selection = Selection.from_article(make_article(source="Different feed"))
    with pytest.raises(ValueError, match="selection_mismatch"):
        load_source(tmp_path, restored)


def test_checksum_damage_or_selection_rebinding_is_rejected(tmp_path: Path) -> None:
    item = state()
    save_state(tmp_path, item)
    path = state_root(tmp_path) / f"{item.selection.identity}.json"
    envelope = json.loads(path.read_text())
    envelope["payload"]["attempts"] = 99
    path.write_text(json.dumps(envelope))
    with pytest.raises(ValueError, match="checksum"):
        load_state(tmp_path, item.selection.identity)
    envelope["payload"]["selection"]["title"] = "A different article"
    envelope["sha256"] = checksum(envelope["payload"])
    path.write_text(json.dumps(envelope))
    with pytest.raises(ValueError, match="selection_mismatch"):
        load_state(tmp_path, item.selection.identity)


def test_symlinked_state_or_source_cannot_escape_cache(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (tmp_path / "reading_briefs").symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(ValueError, match="unsafe_state_path"):
        save_state(tmp_path, state())
    assert not list(elsewhere.iterdir())


def test_source_span_tamper_is_rejected_even_if_snapshot_name_is_rehashed(tmp_path: Path) -> None:
    item = state()
    item.source_sha256, _ = save_source(tmp_path, item.selection,
                                       FetchedArticle("Exact original body.", "https://example.com/final", now(),
                                                      None, "article"))
    root = state_root(tmp_path) / "sources"
    payload = json.loads((root / f"{item.source_sha256}.json").read_text())
    payload["spans"][0]["end"] -= 1
    item.source_sha256 = checksum(payload)
    (root / f"{item.source_sha256}.json").write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="span_manifest_mismatch"):
        load_source(tmp_path, item)
