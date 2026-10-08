"""Finite pre-extraction wire and Python compatibility for source occurrences."""

from __future__ import annotations

import hashlib
import json
from dataclasses import FrozenInstanceError, asdict, fields, replace
from pathlib import Path

import pytest

from digest._serialization import canonical_json_bytes, restore_dataclass
from digest.adapters.storage.candidate_objects import read_article
from digest.application.source_attribution import main_attribution_occurrences
from digest.closing import ClosingOccurrence, load_closing, save_closing
from digest.closing import main_attribution_occurrences as legacy_attribution_occurrences
from digest.domain.catalog.occurrences import SourceOccurrence, occurrence_sha256
from digest.domain.editorial.candidates import CandidateArticle
from digest.preparation import PreparationSnapshot, load_preparation, save_preparation
from digest.radar.summarizer import ArticleSummary
from tests.test_closing import EA_FEED, NOW
from tests.test_edition_runtime import bound_snapshot

BASELINE = json.loads((Path(__file__).parent / "fixtures" / "source_occurrence_wire.json").read_text())


def test_named_compatibility_types_keep_old_python_contract_and_strict_decoding() -> None:
    contract = BASELINE["python_contract"]
    candidate = CandidateArticle(*contract["values"])
    closing = ClosingOccurrence(*contract["values"])
    occurrence = SourceOccurrence(*contract["values"])
    assert repr(candidate) == contract["candidate_repr"]
    assert repr(closing) == contract["closing_repr"]
    assert (candidate == closing) is contract["candidate_closing_equal"]
    assert isinstance(candidate, ClosingOccurrence) is contract["candidate_is_closing_instance"]
    assert candidate.article() == occurrence.article()
    for saved in (candidate, closing, occurrence):
        assert isinstance(saved, SourceOccurrence)
        assert [field.name for field in fields(saved)] == BASELINE["fields"]
        assert asdict(saved) == asdict(occurrence)
        assert occurrence_sha256(saved) == hashlib.sha256(canonical_json_bytes(asdict(saved))).hexdigest()
        assert restore_dataclass(asdict(saved), type(saved)) == saved
        with pytest.raises(ValueError, match="dataclass fields"):
            restore_dataclass({**asdict(saved), "extra": "unsupported"}, type(saved))
        with pytest.raises(FrozenInstanceError):
            saved.source = "changed"  # type: ignore[misc]
    # Construction retains old permissiveness; conversion performs date parsing.
    invalid = replace(candidate, published="invalid")
    with pytest.raises(ValueError):
        invalid.article()


def test_saved_source_packet_closing_and_preparation_match_pre_extraction_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    # This historical wire fixture binds its original request, not today's editorial instructions.
    monkeypatch.setattr(
        "digest.application.candidate_review.review_prompt_hash",
        lambda _: "f79d60aa5b8de2bad6766db4e41474362d742b6523e0336ce4a182be5e826399",
    )
    main = ArticleSummary("Source title — исходный", "https://example.com/article?tag=a%20b", "Source", "Tech",
                          'Canonical claim with "quoted text"\nand café.')
    closing = ArticleSummary("Closing title", "https://example.com/closing", "Community", "Society",
                             "Neighbours restored public access.")
    snapshot = bound_snapshot(
        PreparationSnapshot([main], [], "Canonical notice", None, 2, 2, ["Source", "Community"]),
        closing, main_feed=EA_FEED,
    )
    assert snapshot.review_report is not None and snapshot.closing is not None
    assert snapshot.closing.provenance is not None
    save_closing(snapshot.closing, snapshot.review_report, ".cache")
    save_preparation(snapshot, ".cache", NOW)
    actual = {path.relative_to(".cache").as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
              for path in Path(".cache").rglob("*.json")}
    assert actual == BASELINE["files"]
    assert hashlib.sha256(canonical_json_bytes(asdict(snapshot))).hexdigest() == BASELINE["snapshot_asdict_sha256"]
    assert (hashlib.sha256(canonical_json_bytes(asdict(snapshot.closing.provenance))).hexdigest()
            == BASELINE["provenance_asdict_sha256"])
    assert load_preparation(".cache", NOW) == snapshot
    assert load_closing(snapshot.review_report, ".cache") == snapshot.closing
    for path in Path(".cache/candidate_sources").glob("*.json"):
        saved = read_article(path.stem, ".cache")
        assert type(saved) is CandidateArticle
        assert occurrence_sha256(saved) == path.stem
    resolved = main_attribution_occurrences(snapshot.review_report, [main], [], closing_snapshot=True)
    legacy = legacy_attribution_occurrences(snapshot.review_report, [main], [], closing_snapshot=True)
    assert {key: asdict(value) for key, value in resolved.items()} == {
        key: asdict(value) for key, value in legacy.items()
    }
    assert all(type(value) is ClosingOccurrence for value in legacy.values())
