"""Synthetic native delivered-origin proofs; receipts are fixtures, never real delivery."""

from __future__ import annotations

import json
from dataclasses import asdict, fields, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from digest._serialization import canonical_json_bytes, restore_dataclass
from digest.adapters.models.execution import ModelExecution
from digest.adapters.storage import edition as editions
from digest.adapters.storage import post_delivery as post_storage
from digest.application import prepared_delivery
from digest.config import Config
from digest.domain.delivery.edition import ChunkReceipt, Receipts
from digest.domain.delivery.supplement import SupplementFragment
from digest.domain.investigation.delivered import DeliveredInvestigationInput
from tests.test_native_composition import optional_config, output_for, project_optional, seed_optional
from tests.test_native_editorial import CACHE, NOW, config, freeze, project
from tests.test_native_editorial import isolated as isolated
from tests.test_post_delivery import _client_context, accepted_result


def _snapshot() -> dict[str, bytes]:
    return {str(path): path.read_bytes() for path in Path(".cache").rglob("*") if path.is_file()}


def _confirmed(*, optional: bool = False, archive: bool = True) -> tuple[Config, Path]:
    cfg = optional_config() if optional else config()
    cfg.obsidian.enabled, cfg.obsidian.output_dir = archive, "digests"
    if optional:
        pending = seed_optional(cfg)
        pending, _ = project_optional(cfg, pending, output_for(pending))
    else:
        pending = project(cfg=cfg)
    ready_path, ready_sha = freeze(pending, cfg)
    _, claim_sha = prepared_delivery.claim_edition(ready_sha, now=NOW)
    ready = json.loads(ready_path.read_bytes())
    # Positive synthetic receipt fixture; no transport has been invoked.
    receipts = Receipts(
        1, ready_sha, claim_sha, "confirmed", len(ready["payloads"]),
        [ChunkReceipt(i, i + 1, ready["owner_sha256"]) for i in range(len(ready["payloads"]))], True,
    )
    editions.write_record(CACHE / editions.RECEIPTS_FILE, asdict(receipts))
    if archive:
        return cfg, next(Path(name) for name in ready["checkpoint_refs"] if name.endswith(".md"))
    Path("digests").mkdir()
    unrelated = Path("digests/unrelated.md")
    unrelated.write_text("Not a delivered native archive.\n")
    return cfg, unrelated


def _freeze(archive: Path) -> tuple[Any, Any]:
    from digest.application.native_investigation_origin import freeze_native_delivered_input

    return freeze_native_delivered_input(archive, archive.read_bytes(), now=NOW)


def _rewrite(path: Path, value: Any) -> None:
    path.write_bytes(canonical_json_bytes(value))


def _rewrite_ready(value: dict[str, Any]) -> None:
    """Rehash the mutable test manifest and bind the synthetic dispatch to it."""
    value["canonical_sha256"] = editions.content_sha256(canonical_json_bytes(value["canonical_metadata"]))
    value["presentation_sha256"] = editions.content_sha256(canonical_json_bytes(value["presentation_metadata"]))
    body = {key: item for key, item in value.items() if key != "content_sha256"}
    value["content_sha256"] = editions.content_sha256(canonical_json_bytes(body))
    ready_sha = editions.write_record(CACHE / editions.READY_FILE, value)
    claim = json.loads((CACHE / editions.CLAIM_FILE).read_bytes())
    claim["ready_sha256"] = ready_sha
    claim_sha = editions.write_record(CACHE / editions.CLAIM_FILE, claim)
    receipts = json.loads((CACHE / editions.RECEIPTS_FILE).read_bytes())
    receipts.update(ready_sha256=ready_sha, claim_sha256=claim_sha)
    editions.write_record(CACHE / editions.RECEIPTS_FILE, receipts)


@pytest.fixture
def native_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    from digest import post_delivery
    from digest.application import native_investigation_origin, supplement

    class Clock(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:
            return NOW if tz else NOW.replace(tzinfo=None)

    for module in (post_delivery, native_investigation_origin, supplement):
        monkeypatch.setattr(module, "datetime", Clock)


@pytest.mark.parametrize("optional", [False, True])
def test_exact_native_archive_freezes_original_cards_and_no_fake_review(optional: bool) -> None:
    _, archive = _confirmed(optional=optional)
    before = _snapshot()
    origin, bundle = _freeze(archive)
    ready = json.loads((CACHE / editions.READY_FILE).read_bytes())
    assert type(origin).__name__ == "NativeDeliveredInvestigationInput"
    assert "report_sha256" not in asdict(origin)
    assert "reviews" not in asdict(origin) and "provider" not in asdict(origin)
    assert origin.checkpoint_sha256 == editions.content_sha256(archive.read_bytes())
    assert origin.ready_sha256 == editions.content_sha256((CACHE / editions.READY_FILE).read_bytes())
    assert origin.claim_sha256 == editions.content_sha256((CACHE / editions.CLAIM_FILE).read_bytes())
    assert origin.receipts_sha256 == editions.content_sha256((CACHE / editions.RECEIPTS_FILE).read_bytes())
    assert origin.owner_sha256 == ready["owner_sha256"]
    assert origin.publication_day == NOW.date().isoformat()
    assert origin.bundle_id == bundle.bundle_id
    assert [asdict(card.canonical) for card in origin.cards] == ready["canonical_metadata"]["cards"]
    assert [asdict(card.presentation) for card in origin.cards] == ready["presentation_metadata"]["cards"]
    assert [list(card.covering_chunks) for card in origin.cards] == [a["covering_chunks"] for a in ready["articles"]]
    assert len(origin.cards) == (3 if optional else 2)
    assert all(card.occurrence.source_url for card in origin.cards)
    assert origin.native.schema == "native-investigation-references/v1"
    for name in ("packet", "response", "source_review", "projection", "final_acceptance", "archive_evidence"):
        reference = getattr(origin.native, name)
        assert ready["checkpoint_refs"][reference.path] == reference.sha256
        assert editions.content_sha256(Path(reference.path).read_bytes()) == reference.sha256
    assert _snapshot() == before
    assert not list(Path("digests").glob("*.review.json"))


@pytest.mark.parametrize(
    "label", ["packet", "response", "source-review", "composition", "final-acceptance", "archive-evidence"],
)
def test_changed_native_checkpoint_bytes_are_rejected(label: str) -> None:
    _, archive = _confirmed()
    path = next(CACHE.glob(f"native/*.{label}.json"))
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError):
        _freeze(archive)


@pytest.mark.parametrize(
    "mutation", ["canonical", "presentation", "order", "coverage", "payload", "native_ref", "schema"],
)
def test_rehashed_ready_cannot_change_the_accepted_native_publication(mutation: str) -> None:
    _, archive = _confirmed()
    ready = json.loads((CACHE / editions.READY_FILE).read_bytes())
    if mutation == "canonical":
        ready["canonical_metadata"]["cards"][0]["summary"] += " Changed canonical claim."
    elif mutation == "presentation":
        ready["presentation_metadata"]["cards"][0]["summary"] += " Changed reader claim."
    elif mutation == "order":
        ready["articles"].reverse()
    elif mutation == "coverage":
        ready["articles"][0]["covering_chunks"] = []
    elif mutation == "payload":
        ready["payloads"][0]["text"] += " Changed payload."
    elif mutation == "native_ref":
        ready["native_projection"]["sha256"] = "f" * 64
    else:
        ready["schema"] = 2
    _rewrite_ready(ready)
    with pytest.raises(ValueError):
        _freeze(archive)


@pytest.mark.parametrize(
    "mutation", ["unknown", "partial", "failed", "sending", "unapplied", "coverage", "owner", "claim"],
)
def test_only_full_confirmed_applied_exact_receipts_admit_an_origin(mutation: str) -> None:
    _, archive = _confirmed()
    path = CACHE / editions.RECEIPTS_FILE
    receipts = json.loads(path.read_bytes())
    if mutation in {"unknown", "partial", "failed", "sending"}:
        receipts["state"] = mutation
    elif mutation == "unapplied":
        receipts["applied"] = False
    elif mutation == "coverage":
        receipts["confirmed"].pop()
    elif mutation == "owner":
        receipts["confirmed"][0]["owner_sha256"] = "f" * 64
    else:
        receipts["claim_sha256"] = "f" * 64
    _rewrite(path, receipts)
    before = _snapshot()
    with pytest.raises(ValueError):
        _freeze(archive)
    assert _snapshot() == before


@pytest.mark.parametrize("target", [editions.CLAIM_FILE, editions.RECEIPTS_FILE])
def test_missing_dispatch_proof_is_never_inferred(target: str) -> None:
    _, archive = _confirmed()
    (CACHE / target).unlink()
    with pytest.raises((OSError, ValueError)):
        _freeze(archive)


def test_wrong_current_owner_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _, archive = _confirmed()
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "456")
    with pytest.raises(ValueError):
        _freeze(archive)


@pytest.mark.parametrize("mutation", ["body", "unbound", "no_archive", "symlink", "parent_symlink", "outside"])
def test_native_archive_requires_exact_bound_safe_file(mutation: str, tmp_path: Path) -> None:
    _, archive = _confirmed(archive=mutation != "no_archive")
    if mutation == "body":
        archive.write_bytes(archive.read_bytes() + b"Changed archive body.\n")
    elif mutation == "unbound":
        different = archive.with_name("different.md")
        different.write_bytes(archive.read_bytes())
        archive = different
    elif mutation == "symlink":
        link = archive.with_name("linked.md")
        link.symlink_to(archive.name)
        archive = link
    elif mutation == "parent_symlink":
        Path("linked-digests").symlink_to(Path("digests"), target_is_directory=True)
        archive = Path("linked-digests") / archive.name
    elif mutation == "outside":
        outside = tmp_path.parent / (tmp_path.name + "-outside.md")
        outside.write_bytes(archive.read_bytes())
        archive = outside
    with pytest.raises(ValueError):
        _freeze(archive)


@pytest.mark.parametrize("target", [editions.READY_FILE, editions.CLAIM_FILE, editions.RECEIPTS_FILE])
def test_dispatch_reread_rejects_changes_during_origin_freeze(target: str, monkeypatch: pytest.MonkeyPatch) -> None:
    _, archive = _confirmed()
    from digest.application import native_investigation_origin

    original = native_investigation_origin.validate_delivered_input

    def mutate_then_validate(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        path = CACHE / target
        path.write_bytes(path.read_bytes() + b" ")
        return result

    monkeypatch.setattr(native_investigation_origin, "validate_delivered_input", mutate_then_validate)
    with pytest.raises(ValueError):
        _freeze(archive)


def test_legacy_origin_and_fragment_exact_fieldsets_and_bytes_remain_readable() -> None:
    _, archive = _confirmed()
    native, bundle = _freeze(archive)
    names = {
        "edition_id", "ready_sha256", "claim_sha256", "receipts_sha256", "owner_sha256", "publication_day",
        "canonical_sha256", "presentation_sha256", "checkpoint_sha256", "report_sha256", "bundle_id", "cards",
    }
    assert {field.name for field in fields(DeliveredInvestigationInput)} == names
    legacy_data = {key: value for key, value in asdict(native).items() if key in names}
    legacy_data["report_sha256"] = "a" * 64
    legacy_bytes = canonical_json_bytes(legacy_data)
    legacy = restore_dataclass(json.loads(legacy_bytes), DeliveredInvestigationInput)
    assert canonical_json_bytes(asdict(legacy)) == legacy_bytes
    from digest.post_delivery import _delivered_input

    marker = {
        "schema_version": 3, "input_contract": "delivered-card-v1", "delivered": json.loads(legacy_bytes),
        "delivered_sha256": editions.content_sha256(legacy_bytes), "checkpoint_sha256": legacy.checkpoint_sha256,
    }
    assert _delivered_input(marker, bundle) == legacy
    fragment = SupplementFragment("f" * 64, legacy, str(archive), "digests/old.irritator.json", "a" * 64,
                                  NOW.isoformat(), "Retained historical fragment.")
    fragment_bytes = canonical_json_bytes(asdict(fragment))
    restored = restore_dataclass(json.loads(fragment_bytes), SupplementFragment)
    assert canonical_json_bytes(asdict(restored)) == fragment_bytes
    assert type(restored.origin) is DeliveredInvestigationInput


def test_native_prepare_is_one_attempt_and_keeps_primary_state_untouched(native_clock: None) -> None:
    from digest.post_delivery import prepare_native_post_delivery

    cfg, archive = _confirmed()
    before = _snapshot()
    with patch("digest.post_delivery._config", return_value=cfg):
        marker = prepare_native_post_delivery(Path("config.yaml"), archive)
        assert marker == archive.resolve().with_suffix(".native.post-attempt.json")
        assert marker is not None
        first = marker.read_bytes()
        assert prepare_native_post_delivery(Path("config.yaml"), archive) is None
    record = json.loads(first)
    assert record["schema_version"] == 4 and record["input_contract"] == "native-delivered-card-v1"
    assert "report_sha256" not in record["delivered"]
    assert marker.read_bytes() == first and first.endswith(b"\n")
    assert _snapshot() == before
    assert not list(Path("digests").glob("*.review.json"))


async def test_native_optional_execution_reuses_existing_compact_fragment_without_transport(native_clock: None) -> None:
    from digest.application.supplement import pending_fragment
    from digest.post_delivery import execute_native_post_delivery, prepare_native_post_delivery

    cfg, archive = _confirmed(optional=True)
    primary = _snapshot()

    async def stage(bundle: Any, config: Any, client: Any, *, delivered: Any, **kwargs: Any) -> Any:
        assert len(delivered.cards) == 3 and type(delivered).__name__ == "NativeDeliveredInvestigationInput"
        assert not kwargs.get("require_full_source", False)
        return accepted_result(bundle, delivered)

    with (
        patch("digest.post_delivery._config", return_value=cfg),
        patch("httpx.AsyncClient", return_value=_client_context()),
        patch("digest.irritator.evidence_stage.run_evidence_irritator", side_effect=stage) as run,
        patch("digest.post_delivery._send_supplement", AsyncMock(side_effect=AssertionError("No transport"))) as send,
    ):
        marker = prepare_native_post_delivery(Path("config.yaml"), archive)
        assert marker is not None
        assert await execute_native_post_delivery(Path("config.yaml"), archive, execution=ModelExecution()) == 2
        with pytest.raises(ValueError):
            await execute_native_post_delivery(Path("config.yaml"), archive, execution=ModelExecution())
    run.assert_awaited_once()
    send.assert_not_called()
    record = json.loads(marker.read_bytes())
    assert record["supplement_status"] == "pending"
    pending, status = pending_fragment("digests", NOW.date() + timedelta(days=1), now=NOW)
    assert pending is not None and status == "eligible"
    assert type(pending.fragment.origin).__name__ == "NativeDeliveredInvestigationInput"
    assert "LATE QUALIFIER" in pending.fragment.text
    assert post_storage.read_projection(pending.projection, pending.projection_sha256) == pending.fragment
    assert _snapshot() == primary


async def test_native_execute_rechecks_origin_before_analysis(native_clock: None) -> None:
    from digest.post_delivery import execute_native_post_delivery, prepare_native_post_delivery

    cfg, archive = _confirmed()
    with patch("digest.post_delivery._config", return_value=cfg):
        marker = prepare_native_post_delivery(Path("config.yaml"), archive)
        assert marker is not None
        before = marker.read_bytes()
        path = next(CACHE.glob("native/*.response.json"))
        path.write_bytes(path.read_bytes() + b" ")
        with patch("digest.irritator.evidence_stage.run_evidence_irritator", AsyncMock()) as run:
            with pytest.raises(ValueError):
                await execute_native_post_delivery(Path("config.yaml"), archive, execution=ModelExecution())
    run.assert_not_called()
    assert marker.read_bytes() == before


def test_native_prepare_rejects_changed_archive_directory(native_clock: None) -> None:
    from digest.post_delivery import prepare_native_post_delivery

    cfg, archive = _confirmed()
    cfg.obsidian.output_dir = "other-digests"
    with patch("digest.post_delivery._config", return_value=cfg), pytest.raises(ValueError):
        prepare_native_post_delivery(Path("config.yaml"), archive)
    assert not list(Path("digests").glob("*.native.post-attempt.json"))


@pytest.mark.parametrize("offset", [-1, 4, 30])
def test_native_origin_refuses_future_or_expired_publication(offset: int) -> None:
    from digest.application.native_investigation_origin import freeze_native_delivered_input

    _, archive = _confirmed()
    with pytest.raises(ValueError, match="future or expired"):
        freeze_native_delivered_input(archive, archive.read_bytes(), now=NOW + timedelta(days=offset))


@pytest.mark.parametrize("label", [
    "packet", "response", "source-review", "composition", "final-acceptance", "archive-evidence", "archive",
])
def test_native_reference_reread_rejects_changes_during_freeze(label: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from digest.application import native_investigation_origin

    _, archive = _confirmed()
    original = native_investigation_origin.validate_delivered_input

    def mutate_then_validate(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        path = archive if label == "archive" else next(CACHE.glob(f"native/*.{label}.json"))
        path.write_bytes(path.read_bytes() + b" ")
        return result

    monkeypatch.setattr(native_investigation_origin, "validate_delivered_input", mutate_then_validate)
    with pytest.raises(ValueError):
        _freeze(archive)


async def test_native_three_stage_pipeline_with_stubbed_models_and_documents_creates_fragment(
    native_clock: None,
) -> None:
    """Exercise actual source/quote/ranking/fragment logic, not a synthetic stage return."""
    import hashlib

    from digest.irritator.documents import SourceRequestBudget
    from digest.irritator.research import document_signals
    from digest.post_delivery import execute_native_post_delivery, prepare_native_post_delivery
    from tests.test_irritator_research import BODY, LINK, _entry, _plan, _section, _snapshot

    cfg, archive = _confirmed()
    cfg.irritator.sources = []
    origin, bundle = _freeze(archive)
    card = origin.cards[0]
    item = next(item for item in bundle.items if item.evidence_id == card.card_id)
    narrative = {
        "narratives": [{
            "claim": "A proposed conditional interpretation.", "category": item.category,
            "implicit_assumptions": [], "why_worth_challenging": "Check documented conditions.",
            "evidence_ids": [item.evidence_id], "quotes": {item.evidence_id: item.excerpt},
            "target_card_id": card.card_id,
            "delivered_quote": {"field": "summary", "text": card.canonical.summary},
        }],
        "limitations": [],
    }
    external = _snapshot(sections=(_section("client", BODY),))
    source = _snapshot(url=card.canonical.link, text="Original synthetic source context.")
    link_id = "link-" + hashlib.sha256(
        f"{source.final_url}\0{source.content_sha256}\0{LINK.url}\0{LINK.title}".encode(),
    ).hexdigest()[:24]
    source = replace(source, links=(replace(LINK, link_id=link_id),))
    signal = document_signals(external, "client authentication", "Specification")[0]
    ranking = _entry(signal)
    ranking["target_quote"] = card.canonical.summary
    model = AsyncMock(side_effect=[
        (json.dumps(narrative), {}), (json.dumps(_plan(document_ids=[link_id])), {}),
        (json.dumps({"rankings": [ranking], "limitations": []}), {}),
    ])
    reads: list[str] = []

    async def acquire(url: str, *, budget: SourceRequestBudget) -> Any:
        budget.consume()
        reads.append(url)
        return source if len(reads) == 1 else external

    primary = {str(path): path.read_bytes() for path in CACHE.rglob("*") if path.is_file()}
    with (
        patch("digest.post_delivery._config", return_value=cfg),
        patch("httpx.AsyncClient", return_value=_client_context()),
        patch("digest.irritator.evidence_stage.complete", model),
        patch("digest.irritator.evidence_stage.acquire_document", side_effect=acquire),
        patch("digest.irritator.evidence_stage.search_hackernews", AsyncMock()) as hn,
        patch("digest.irritator.evidence_stage.search_arxiv", AsyncMock()) as arxiv,
        patch("digest.irritator.evidence_stage.search_devto", AsyncMock()) as devto,
        patch("digest.post_delivery._send_supplement", AsyncMock(side_effect=AssertionError("No transport"))) as send,
    ):
        marker = prepare_native_post_delivery(Path("config.yaml"), archive)
        assert marker is not None
        assert await execute_native_post_delivery(Path("config.yaml"), archive, execution=ModelExecution()) == 0
    assert model.await_count == 3 and reads == [card.canonical.link, LINK.url]
    for search in (hn, arxiv, devto):
        search.assert_not_awaited()
    send.assert_not_called()
    record = json.loads(marker.read_bytes())
    assert record["stage_status"] == "complete" and record["supplement_status"] == "pending"
    fragment = post_storage.read_projection(record["fragment"]["projection"], record["fragment"]["projection_sha256"])
    assert type(fragment.origin).__name__ == "NativeDeliveredInvestigationInput"
    assert fragment.origin == origin
    result = json.loads(Path(fragment.result).read_bytes())
    assert result["source_request_starts"] == 2 and len(result["documents"]) == 2
    assert result["narratives"][0]["claim"] == card.canonical.summary
    assert {str(path): path.read_bytes() for path in CACHE.rglob("*") if path.is_file()} == primary


async def test_native_execution_claim_survives_interruption_before_marker_update(native_clock: None) -> None:
    from digest.post_delivery import execute_native_post_delivery, prepare_native_post_delivery

    cfg, archive = _confirmed()
    with patch("digest.post_delivery._config", return_value=cfg):
        marker = prepare_native_post_delivery(Path("config.yaml"), archive)
        assert marker is not None
        before = marker.read_bytes()
        with (
            patch("digest.post_delivery.storage.save_attempt", side_effect=OSError("Synthetic write interruption")),
            patch("digest.irritator.evidence_stage.run_evidence_irritator", AsyncMock()) as run,
        ):
            with pytest.raises(OSError, match="Synthetic write interruption"):
                await execute_native_post_delivery(Path("config.yaml"), archive, execution=ModelExecution())
        run.assert_not_called()
        claim = marker.with_suffix(".execute.json")
        assert claim.is_file() and marker.read_bytes() == before
        claim_before = claim.read_bytes()
        with patch("digest.irritator.evidence_stage.run_evidence_irritator", AsyncMock()) as rerun:
            with pytest.raises(ValueError, match="already claimed"):
                await execute_native_post_delivery(Path("config.yaml"), archive, execution=ModelExecution())
        rerun.assert_not_called()
        assert prepare_native_post_delivery(Path("config.yaml"), archive) is None
    assert marker.read_bytes() == before and claim.read_bytes() == claim_before


@pytest.mark.parametrize("mutation", ["contract", "legacy_schema", "provenance_schema", "extra_field", "missing_field"])
def test_native_marker_decoder_rejects_cross_contract_or_inexact_values(mutation: str, native_clock: None) -> None:
    from digest.post_delivery import _delivered_input, prepare_native_post_delivery

    cfg, archive = _confirmed()
    with patch("digest.post_delivery._config", return_value=cfg):
        marker = prepare_native_post_delivery(Path("config.yaml"), archive)
    assert marker is not None
    record = json.loads(marker.read_bytes())
    _, bundle = _freeze(archive)
    if mutation == "contract":
        record["input_contract"] = "delivered-card-v1"
    elif mutation == "legacy_schema":
        record["schema_version"] = 3
    elif mutation == "provenance_schema":
        record["delivered"]["native"]["schema"] = "unknown/v1"
    elif mutation == "extra_field":
        record["delivered"]["report_sha256"] = "a" * 64
    else:
        del record["delivered"]["native"]
    record["delivered_sha256"] = editions.content_sha256(canonical_json_bytes(record["delivered"]))
    with pytest.raises(ValueError):
        _delivered_input(record, bundle)


@pytest.mark.parametrize("clock", ["naive", "expired_negative_offset", "eligible_positive_offset"])
def test_native_origin_eligibility_uses_aware_utc_day(clock: str) -> None:
    from digest.application.native_investigation_origin import freeze_native_delivered_input

    _, archive = _confirmed()
    if clock == "naive":
        instant = NOW.replace(tzinfo=None)
    elif clock == "expired_negative_offset":
        instant = (NOW.replace(hour=0) + timedelta(days=4)).astimezone(timezone(timedelta(hours=-12)))
    else:
        instant = (NOW + timedelta(days=3)).astimezone(timezone(timedelta(hours=14)))
    if clock == "eligible_positive_offset":
        origin, _ = freeze_native_delivered_input(archive, archive.read_bytes(), now=instant)
        assert origin.publication_day == NOW.date().isoformat()
    else:
        with pytest.raises(ValueError):
            freeze_native_delivered_input(archive, archive.read_bytes(), now=instant)
