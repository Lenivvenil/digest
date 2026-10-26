"""Offline native fragment destinations; transport receipts remain synthetic fixtures."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, fields, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest

from digest._serialization import canonical_json_bytes, restore_dataclass
from digest.adapters.models.execution import ModelExecution
from digest.adapters.storage import edition as editions
from digest.adapters.storage import native as storage
from digest.application import candidate_review, native, prepared_delivery, supplement
from digest.config import Config
from digest.domain.catalog.articles import article_hash
from digest.domain.catalog.occurrences import occurrence_sha256
from digest.domain.delivery.supplement import PendingSupplement
from digest.domain.editorial.candidates import Candidate, CandidateProgress
from digest.domain.editorial.native import encode, export_packet, sha256
from digest.post_delivery import execute_native_post_delivery, prepare_native_post_delivery
from digest.presentation.telegram import escape_markdownv2
from tests.test_native_composition import optional_config, source_review
from tests.test_native_editorial import CACHE, NOW, acceptance_for, occurrences
from tests.test_native_editorial import isolated as isolated
from tests.test_native_investigation_origin import _confirmed
from tests.test_post_delivery import _client_context, accepted_result, pending_supplement

NEXT = NOW + timedelta(days=1)


def _clock(monkeypatch: pytest.MonkeyPatch, instant: datetime) -> None:
    class Clock(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:
            return instant if tz else instant.replace(tzinfo=None)

    for target in (
        "digest._util.datetime", "digest.application.native.datetime", "digest.application.prepared_delivery.datetime",
        "digest.application.candidate_review.datetime", "digest.application.supplement.datetime",
        "digest.application.native_investigation_origin.datetime", "digest.post_delivery.datetime",
        "digest.radar.collector.datetime", "digest.edition_runtime.datetime",
    ):
        monkeypatch.setattr(target, Clock)


async def _origin_fragment(
    monkeypatch: pytest.MonkeyPatch, *, legacy: bool = False, long: bool = False,
) -> PendingSupplement:
    _clock(monkeypatch, NOW)
    if legacy:
        return await pending_supplement(monkeypatch, long=long)
    cfg, archive = _confirmed()

    async def stage(bundle: Any, config: Any, client: Any, *, delivered: Any, **kwargs: Any) -> Any:
        return accepted_result(bundle, delivered, long=long)

    with (
        patch("digest.post_delivery._config", return_value=cfg),
        patch("httpx.AsyncClient", return_value=_client_context()),
        patch("digest.irritator.evidence_stage.run_evidence_irritator", side_effect=stage),
    ):
        marker = prepare_native_post_delivery(Path("config.yaml"), archive)
        assert marker is not None
        assert await execute_native_post_delivery(Path("config.yaml"), archive, execution=ModelExecution()) == 2
    pending, reason = supplement.pending_fragment("digests", NEXT.date(), now=NOW)
    assert pending is not None and reason == "eligible"
    return pending


def _project_next(
    monkeypatch: pytest.MonkeyPatch, *, day: datetime = NEXT, optional: bool = True,
) -> tuple[Config, storage.PendingNative, dict[str, Any]]:
    _clock(monkeypatch, day)
    cfg = optional_config()
    cfg.obsidian.enabled, cfg.obsidian.output_dir = True, "digests"
    if not optional:
        cfg.closing = replace(cfg.closing, enabled=False)
        cfg.synthesis = replace(cfg.synthesis, enabled=False)
    originals = [
        replace(article, title=f"Destination {day.date()} headline {index}",
                link=f"https://example.invalid/destination/{day.date()}/{index}",
                description=f"Destination evidence {index}.", published=day.isoformat())
        for index, article in enumerate(occurrences())
    ]
    progress = CandidateProgress(candidates={
        article_hash(article.title, article.link): Candidate(article_hash(article.title, article.link), article,
                                                            day.isoformat(), 3)
        for article in originals
    })
    packet = candidate_review.plan_packet(progress, cfg, now=day)
    assert packet is not None
    owner = sha256(os.environ["TELEGRAM_CHAT_ID"].encode())
    raw = export_packet(packet, publication_date=day.date(), language="ru", max_excerpt_chars=500,
                        owner_sha256=owner, editorial_context="Synthetic destination priorities",
                        optional_policy=native._optional_policy(packet, cfg))
    pending = storage.PendingNative(1, owner, day.date().isoformat(), storage.put_bytes(raw, CACHE, "packet"))
    storage.save_pending(CACHE, pending)
    closing_id = article_hash(originals[-1].title, originals[-1].link)
    by_id = {article_hash(article.title, article.link): article for article in originals}
    main_ids = [item.evidence_id for item in packet.evidence.items if item.evidence_id != closing_id]
    response: dict[str, Any] = {
        "schema": "native-editorial-response/v2", "packet_sha256": pending.packet.sha256,
        "execution_ref": "synthetic-destination-writer", "language": "ru",
        "items": [{"evidence_id": item.evidence_id, "status": "card" if item.evidence_id in main_ids else "withheld",
                   "text": f"Основной текст назначения {index}." if item.evidence_id in main_ids else ""}
                  for index, item in enumerate(packet.evidence.items)],
    }
    if optional:
        response.update(
            closing={"evidence_id": closing_id, "occurrence_sha256": occurrence_sha256(originals[-1]),
                     "text": "Заключительный текст назначения."},
            comparison={
                "supports": [{"evidence_id": identity, "occurrence_sha256": occurrence_sha256(by_id[identity]),
                              "observation": f"Наблюдение назначения {index}."}
                             for index, identity in enumerate(main_ids)],
                "interpretation": "Сравнение назначения.", "qualification": "Ограничения назначения.",
            },
        )
    review = source_review(pending, response)
    review["writer_execution_ref"] = response["execution_ref"]
    projected = native.project_response(cfg, encode(response), encode(review),
                                       expected_packet_sha256=pending.packet.sha256, now=day)
    assert projected.projection is not None
    return cfg, projected, json.loads(storage.read_reference(projected.projection))


def _import(cfg: Config, pending: storage.PendingNative, *, now: datetime = NEXT) -> tuple[dict[str, Any], str]:
    assert pending.projection is not None
    path, digest = native.import_accepted(cfg, acceptance_for(pending),
                                         expected_projection_sha256=pending.projection.sha256, now=now)
    return json.loads(path.read_bytes()), digest


def _marker(pending: PendingSupplement) -> dict[str, Any]:
    return json.loads(Path(pending.attempt).read_bytes())


@pytest.mark.parametrize("legacy", [False, True])
async def test_native_destination_carries_exact_fragment_before_closing_and_preserves_comparison(
    monkeypatch: pytest.MonkeyPatch, legacy: bool,
) -> None:
    fragment = await _origin_fragment(monkeypatch, legacy=legacy)
    before = Path(fragment.attempt).read_bytes()
    cfg, pending, projection = _project_next(monkeypatch)
    assert projection["schema"] == "native-composition/v3"
    frozen = projection["publication"]["supplement"]
    assert frozen["fragment"] == json.loads(canonical_json_bytes(asdict(fragment.fragment)))
    assert frozen["attempt"] == fragment.attempt
    assert Path(fragment.attempt).read_bytes() == before  # Projection is not a reservation.
    manifest, digest = _import(cfg, pending)
    assert manifest["schema"] == 5 and "current_review_checkpoint" not in manifest
    assert storage.publication_fields(manifest) == projection["publication"]
    ready, _ = editions.load_edition(CACHE / editions.READY_FILE, os.environ["TELEGRAM_CHAT_ID"], NEXT, digest)
    assert type(ready).__name__ == "NativeSupplementEdition"
    assert manifest["supplement"] == frozen
    text = "".join(payload["text"] for payload in manifest["payloads"])
    escaped = escape_markdownv2(fragment.fragment.text)
    assert text.count(escaped) == 1
    assert text.index("Основной текст") < text.index("Сравнение назначения") < text.index(escaped)
    assert text.index(escaped) < text.index("Заключительный текст назначения")
    start, end, offset = text.index(escaped), text.index(escaped) + len(escaped), 0
    coverage = []
    for index, payload in enumerate(manifest["payloads"]):
        stop = offset + len(payload["text"])
        if offset < end and start < stop:
            coverage.append(index)
        offset = stop
    assert frozen["coverage"]["covering_chunks"] == coverage
    assert fragment.attempt not in manifest["checkpoint_refs"]
    assert len(manifest["articles"]) == 3  # Fragment/comparison are not articles or votes.
    archive = next(Path(name).read_text() for name in manifest["checkpoint_refs"]
                   if name.endswith(".md") and name != fragment.fragment.checkpoint)
    assert archive.count(fragment.fragment.text) == 1
    assert archive.index(fragment.fragment.text) < archive.index("Заключительный текст назначения")
    record = _marker(fragment)
    assert record["supplement_status"] == "included_ready" and record["fragment"]["included_ready"] == digest


@pytest.mark.parametrize("reason", ["missing", "expired", "conflicting", "reserved", "missing_projection"])
async def test_unavailable_fragment_omits_only_optional_block(monkeypatch: pytest.MonkeyPatch, reason: str) -> None:
    fragment = None if reason == "missing" else await _origin_fragment(monkeypatch)
    day = NEXT
    if fragment is not None:
        path = Path(fragment.attempt)
        if reason == "expired":
            day = NOW + timedelta(days=4)
        elif reason == "conflicting":
            path.with_name("conflicting.native.post-attempt.json").write_bytes(path.read_bytes())
        elif reason == "reserved":
            record = _marker(fragment)
            record["supplement_status"] = "included_ready"
            record["fragment"]["included_ready"] = "f" * 64
            path.write_bytes(canonical_json_bytes(record))
        elif reason == "missing_projection":
            Path(fragment.projection).unlink()
    cfg, pending, projection = _project_next(monkeypatch, day=day)
    assert projection["schema"] == "native-composition/v2" and "supplement" not in projection["publication"]
    assert len(projection["publication"]["articles"]) == 3
    manifest, _ = _import(cfg, pending, now=day)
    assert manifest["schema"] == 4 and "supplement" not in manifest
    if reason == "expired":
        assert fragment is not None and _marker(fragment)["supplement_status"] == "expired"


@pytest.mark.parametrize("mutation", ["verdict", "projection", "text", "coverage", "order"])
async def test_final_acceptance_rejects_changed_fragment_composition_without_reservation(
    monkeypatch: pytest.MonkeyPatch, mutation: str,
) -> None:
    fragment = await _origin_fragment(monkeypatch)
    cfg, pending, projection = _project_next(monkeypatch)
    assert pending.projection is not None
    raw = json.loads(acceptance_for(pending))
    if mutation == "verdict":
        raw["verdict"] = "reject"
    elif mutation == "projection":
        raw["projection_sha256"] = "f" * 64
    else:
        if mutation == "text":
            projection["publication"]["supplement"]["fragment"]["text"] += " Changed fragment."
        elif mutation == "coverage":
            projection["publication"]["supplement"]["coverage"]["covering_chunks"] = []
        else:
            projection["publication"]["articles"].reverse()
        Path(pending.projection.path).write_bytes(encode(projection))
    before = Path(fragment.attempt).read_bytes()
    with pytest.raises(ValueError):
        native.import_accepted(cfg, encode(raw), expected_projection_sha256=pending.projection.sha256, now=NEXT)
    assert Path(fragment.attempt).read_bytes() == before
    assert _marker(fragment)["supplement_status"] == "pending"


async def test_ready5_sender_consumes_full_fragment_once_without_rerender(monkeypatch: pytest.MonkeyPatch) -> None:
    fragment = await _origin_fragment(monkeypatch, long=True)
    cfg, pending, _ = _project_next(monkeypatch)
    manifest, ready = _import(cfg, pending)
    _, claim = prepared_delivery.claim_edition(ready, now=NEXT)
    send = AsyncMock(side_effect=[("confirmed", index + 1) for index in range(len(manifest["payloads"]))])
    monkeypatch.setattr(prepared_delivery, "send_prepared_chunk", send)
    monkeypatch.setattr(
        prepared_delivery, "render_compact_publication", Mock(side_effect=AssertionError("No rerender")),
    )
    result = await prepared_delivery.send_prepared_edition(ready, claim, config=cfg, now=NEXT)
    assert result.complete and len(result.delivered_hashes) == 3
    assert [call.args[2] for call in send.await_args_list] == manifest["payloads"]
    assert _marker(fragment)["supplement_status"] == "consumed"
    retained = Path(fragment.attempt).read_bytes()
    again = await prepared_delivery.send_prepared_edition(ready, claim, config=cfg, now=NEXT)
    assert again.complete and send.await_count == len(manifest["payloads"])
    assert Path(fragment.attempt).read_bytes() == retained
    assert json.loads((CACHE / editions.RECEIPTS_FILE).read_bytes())["applied"] is True


@pytest.mark.parametrize("boundary", ["before_fragment", "within_fragment"])
@pytest.mark.parametrize("outcome", ["unknown", "failed"])
async def test_incomplete_fragment_coverage_keeps_native_ready_unapplied_and_reserved(
    monkeypatch: pytest.MonkeyPatch, boundary: str, outcome: str,
) -> None:
    fragment = await _origin_fragment(monkeypatch, long=True)
    cfg, pending, _ = _project_next(monkeypatch)
    manifest, ready = _import(cfg, pending)
    _, claim = prepared_delivery.claim_edition(ready, now=NEXT)
    coverage = manifest["supplement"]["coverage"]["covering_chunks"]
    cutoff = coverage[0] if boundary == "before_fragment" else coverage[-1]
    send = AsyncMock(side_effect=[("confirmed", index + 1) for index in range(cutoff)] + [(outcome, None)])
    monkeypatch.setattr(prepared_delivery, "send_prepared_chunk", send)
    with pytest.raises(ValueError, match="not completely confirmed"):
        await prepared_delivery.send_prepared_edition(ready, claim, config=cfg, now=NEXT)
    receipts = json.loads((CACHE / editions.RECEIPTS_FILE).read_bytes())
    assert not receipts["applied"] and _marker(fragment)["supplement_status"] == "included_ready"
    assert receipts["state"] == ("unknown" if outcome == "unknown" else "partial" if cutoff else "failed")
    assert prepared_delivery.inspect_edition(now=NEXT)[2] == "held"
    with pytest.raises(ValueError, match="replay"):
        await prepared_delivery.send_prepared_edition(ready, claim, config=cfg, now=NEXT)
    assert send.await_count == cutoff + 1


async def test_expired_unclaimed_ready5_releases_and_rebinds_fragment_to_later_native_edition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fragment = await _origin_fragment(monkeypatch)
    cfg, pending, _ = _project_next(monkeypatch)
    _, previous = _import(cfg, pending)
    later = NEXT + timedelta(days=1)
    _clock(monkeypatch, later)
    recovered, reason = supplement.pending_fragment("digests", later.date(), now=later)
    assert recovered == fragment and reason == "eligible"
    record = _marker(fragment)
    assert record["supplement_status"] == "pending"
    assert record["fragment"]["released_binding"] == {"ready_sha256": previous, "reason": "expired_unclaimed_ready"}
    cfg, pending, projection = _project_next(monkeypatch, day=later)
    assert projection["publication"]["supplement"]["fragment"]["fragment_id"] == fragment.fragment.fragment_id
    _, ready = _import(cfg, pending, now=later)
    assert ready != previous and _marker(fragment)["fragment"]["included_ready"] == ready


async def test_claimed_native_fragment_is_not_released_on_expiry(monkeypatch: pytest.MonkeyPatch) -> None:
    fragment = await _origin_fragment(monkeypatch)
    cfg, pending, _ = _project_next(monkeypatch)
    _, ready = _import(cfg, pending)
    prepared_delivery.claim_edition(ready, now=NEXT)
    before = Path(fragment.attempt).read_bytes()
    later = NEXT + timedelta(days=3)
    assert supplement.pending_fragment("digests", later.date(), now=later) == (None, "unverified_pending_fragment")
    assert Path(fragment.attempt).read_bytes() == before


def test_retained_native_pending_fields_and_schema4_bytes_stay_exact(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg, pending, projection = _project_next(monkeypatch)
    names = {"schema", "owner_sha256", "publication_date", "packet", "response", "review", "projection",
             "acceptance", "archive_evidence", "archive"}
    assert {field.name for field in fields(storage.PendingNative)} == names
    raw = canonical_json_bytes(asdict(pending))
    assert canonical_json_bytes(asdict(restore_dataclass(json.loads(raw), storage.PendingNative))) == raw
    assert projection["schema"] == "native-composition/v2"
    manifest, ready_sha = _import(cfg, pending)
    path = CACHE / editions.READY_FILE
    raw_ready = path.read_bytes()
    assert manifest["schema"] == 4
    restored, digest = editions.load_edition(path, os.environ["TELEGRAM_CHAT_ID"], NEXT)
    assert type(restored).__name__ == "NativeEdition" and digest == ready_sha
    assert canonical_json_bytes(asdict(restored)) == raw_ready
    assert path.read_bytes() == raw_ready


@pytest.mark.parametrize("boundary", ["reservation", "pending_cleanup"])
async def test_native_import_recovers_exact_ready_prefix_without_reprojection(
    monkeypatch: pytest.MonkeyPatch, boundary: str,
) -> None:
    fragment = await _origin_fragment(monkeypatch)
    # Isolate a prefix with no unrelated active dispatch files. The immutable
    # fragment already retains its verified origin; these are synthetic fixtures.
    for name in (editions.READY_FILE, editions.CLAIM_FILE, editions.RECEIPTS_FILE):
        (CACHE / name).unlink()
    cfg, pending, _ = _project_next(monkeypatch)
    target = {
        "reservation": "digest.application.supplement.storage.save_attempt",
        "pending_cleanup": "digest.application.native.storage.clear_pending",
    }[boundary]
    with patch(target, side_effect=OSError("Synthetic import interruption")):
        with pytest.raises(OSError, match="Synthetic import interruption"):
            _import(cfg, pending)
    ready_path = CACHE / editions.READY_FILE
    retained_ready = ready_path.read_bytes()
    retained_sha = sha256(retained_ready)
    assert json.loads(retained_ready)["schema"] == 5
    assert (CACHE / storage.PENDING_FILE).exists()
    expected = "pending" if boundary == "reservation" else "included_ready"
    assert _marker(fragment)["supplement_status"] == expected
    if boundary != "pending_cleanup":
        with pytest.raises(ValueError):
            prepared_delivery.claim_edition(retained_sha, now=NEXT)
    with patch("digest.application.native._projection", side_effect=AssertionError("Recovery must not reproject")):
        manifest, digest = _import(cfg, pending)
    assert manifest["schema"] == 5 and digest == retained_sha and ready_path.read_bytes() == retained_ready
    assert not (CACHE / storage.PENDING_FILE).exists()
    assert not (CACHE / editions.CLAIM_FILE).exists() and not (CACHE / editions.RECEIPTS_FILE).exists()
    assert _marker(fragment)["supplement_status"] == "included_ready"
    assert _marker(fragment)["fragment"]["included_ready"] == digest
    prepared_delivery.claim_edition(digest, now=NEXT)


@pytest.mark.parametrize("consume_failure", [False, True])
async def test_native_fragment_consumption_depends_on_its_coverage_not_later_unknown_chunks(
    monkeypatch: pytest.MonkeyPatch, consume_failure: bool,
) -> None:
    fragment = await _origin_fragment(monkeypatch, long=True)
    # An unrelated footer guarantees chunks after the complete fragment. It is
    # part of the exact final accepted projection, not inserted after acceptance.
    from digest.presentation import telegram

    labels = dict(telegram._LABELS["ru"])
    labels["feedback"] = "Unrelated footer text " * 400
    with patch("digest.presentation.telegram._labels", return_value=labels):
        cfg, pending, _ = _project_next(monkeypatch)
        manifest, ready = _import(cfg, pending)
    _, claim = prepared_delivery.claim_edition(ready, now=NEXT)
    prefix = manifest["supplement"]["coverage"]["covering_chunks"][-1] + 1
    assert prefix < len(manifest["payloads"])
    send = AsyncMock(side_effect=[("confirmed", index + 1) for index in range(prefix)] + [("unknown", None)])
    monkeypatch.setattr(prepared_delivery, "send_prepared_chunk", send)
    if consume_failure:
        with patch(
            "digest.application.supplement.storage.save_attempt", side_effect=OSError("Synthetic consume failure"),
        ):
            with pytest.raises(OSError, match="Synthetic consume failure"):
                await prepared_delivery.send_prepared_edition(ready, claim, config=cfg, now=NEXT)
    else:
        result = await prepared_delivery.send_prepared_edition(ready, claim, config=cfg, now=NEXT)
        assert not result.complete
    receipts = json.loads((CACHE / editions.RECEIPTS_FILE).read_bytes())
    assert receipts["state"] == "unknown" and receipts["applied"] is not consume_failure
    assert _marker(fragment)["supplement_status"] == ("included_ready" if consume_failure else "consumed")
    assert send.await_count == prefix + 1
    with pytest.raises(ValueError, match="replay"):
        await prepared_delivery.send_prepared_edition(ready, claim, config=cfg, now=NEXT)
    assert send.await_count == prefix + 1


async def test_confirmed_native5_remains_a_next_investigation_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    from digest.application.native_investigation_origin import freeze_native_delivered_input

    previous = await _origin_fragment(monkeypatch)
    cfg, pending, _ = _project_next(monkeypatch)
    manifest, ready = _import(cfg, pending)
    _, claim = prepared_delivery.claim_edition(ready, now=NEXT)
    monkeypatch.setattr(prepared_delivery, "send_prepared_chunk", AsyncMock(
        side_effect=[("confirmed", index + 1) for index in range(len(manifest["payloads"]))],
    ))
    assert (await prepared_delivery.send_prepared_edition(ready, claim, config=cfg, now=NEXT)).complete
    archive = next(Path(name) for name in manifest["checkpoint_refs"]
                   if name.endswith(".md") and name != previous.fragment.checkpoint)
    origin, bundle = freeze_native_delivered_input(archive, archive.read_bytes(), now=NEXT)
    assert len(origin.cards) == 3 and all(card.canonical.title.startswith("Destination") for card in origin.cards)
    assert origin.ready_sha256 == ready and origin.bundle_id == bundle.bundle_id
    assert origin.publication_day == NEXT.date().isoformat()
    assert previous.fragment.fragment_id not in {card.card_id for card in origin.cards}
    with patch("digest.post_delivery._config", return_value=cfg):
        marker = prepare_native_post_delivery(Path("config.yaml"), archive)
    assert marker is not None and json.loads(marker.read_bytes())["delivered"]["ready_sha256"] == ready


@pytest.mark.parametrize("boundary", ["reservation", "dispatch_cleanup"])
async def test_native_import_preserves_hold_for_mismatched_old_dispatch_prefix(
    monkeypatch: pytest.MonkeyPatch, boundary: str,
) -> None:
    fragment = await _origin_fragment(monkeypatch)
    cfg, pending, _ = _project_next(monkeypatch)
    target = ("digest.application.supplement.storage.save_attempt" if boundary == "reservation"
              else "digest.application.prepared_delivery.storage.remove_previous_dispatch")
    with patch(target, side_effect=OSError("Synthetic old-dispatch interruption")):
        with pytest.raises(OSError, match="Synthetic old-dispatch interruption"):
            _import(cfg, pending)
    paths = [CACHE / name for name in (editions.READY_FILE, editions.CLAIM_FILE,
                                      editions.RECEIPTS_FILE, storage.PENDING_FILE)] + [Path(fragment.attempt)]
    before = {str(path): path.read_bytes() for path in paths}
    ready_sha = sha256((CACHE / editions.READY_FILE).read_bytes())
    with pytest.raises(ValueError):
        _import(cfg, pending)
    with pytest.raises(ValueError):
        prepared_delivery.claim_edition(ready_sha, now=NEXT)
    assert {str(path): path.read_bytes() for path in paths} == before


@pytest.mark.parametrize("mutation", ["disposition", "frozen_text", "missing_projection"])
async def test_selected_fragment_change_after_projection_holds_import_without_recomposition(
    monkeypatch: pytest.MonkeyPatch, mutation: str,
) -> None:
    fragment = await _origin_fragment(monkeypatch)
    cfg, pending, _ = _project_next(monkeypatch)
    assert pending.projection is not None
    if mutation == "disposition":
        record = _marker(fragment)
        record["supplement_status"] = "archive_only"
        Path(fragment.attempt).write_bytes(canonical_json_bytes(record))
    elif mutation == "frozen_text":
        frozen = json.loads(Path(fragment.projection).read_bytes())
        frozen["text"] += " Changed after composition."
        Path(fragment.projection).write_bytes(canonical_json_bytes(frozen))
    else:
        Path(fragment.projection).unlink()
    previous_ready = (CACHE / editions.READY_FILE).read_bytes()
    projected = Path(pending.projection.path).read_bytes()
    attempt = Path(fragment.attempt).read_bytes()
    with pytest.raises((ValueError, OSError)):
        _import(cfg, pending)
    assert (CACHE / editions.READY_FILE).read_bytes() == previous_ready
    assert Path(pending.projection.path).read_bytes() == projected
    assert Path(fragment.attempt).read_bytes() == attempt


async def test_late_fragment_does_not_change_frozen_no_fragment_composition(monkeypatch: pytest.MonkeyPatch) -> None:
    fragment = await _origin_fragment(monkeypatch)
    attempt = Path(fragment.attempt)
    saved = attempt.read_bytes()
    attempt.unlink()  # Synthetic delayed arrival of the already verified optional record.
    cfg, pending, projection = _project_next(monkeypatch)
    assert projection["schema"] == "native-composition/v2" and "supplement" not in projection["publication"]
    attempt.write_bytes(saved)
    manifest, _ = _import(cfg, pending)
    assert manifest["schema"] == 4 and storage.publication_fields(manifest) == projection["publication"]
    assert attempt.read_bytes() == saved and _marker(fragment)["supplement_status"] == "pending"


async def test_native_destination_omits_fragment_owned_by_different_recipient(monkeypatch: pytest.MonkeyPatch) -> None:
    fragment = await _origin_fragment(monkeypatch)
    before = Path(fragment.attempt).read_bytes()
    # Distinct synthetic destination owner with no active prior dispatch.
    for name in (editions.READY_FILE, editions.CLAIM_FILE, editions.RECEIPTS_FILE):
        (CACHE / name).unlink()
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "456")
    cfg, pending, projection = _project_next(monkeypatch)
    assert "supplement" not in projection["publication"]
    manifest, _ = _import(cfg, pending)
    assert manifest["schema"] == 4 and manifest["owner_sha256"] == sha256(b"456")
    assert Path(fragment.attempt).read_bytes() == before


@pytest.mark.parametrize("name", [editions.CLAIM_FILE, editions.RECEIPTS_FILE])
async def test_expired_native5_dangling_dispatch_symlink_never_releases_fragment(
    monkeypatch: pytest.MonkeyPatch, name: str,
) -> None:
    fragment = await _origin_fragment(monkeypatch)
    cfg, pending, _ = _project_next(monkeypatch)
    _, _ = _import(cfg, pending)
    dispatch = CACHE / name
    dispatch.symlink_to("nonexistent-synthetic-dispatch.json")
    assert dispatch.is_symlink() and not dispatch.exists()
    before = {str(path): path.read_bytes() for path in (Path(fragment.attempt), CACHE / editions.READY_FILE)}
    later = NEXT + timedelta(days=1)
    assert supplement.pending_fragment("digests", later.date(), now=later) == (None, "unverified_pending_fragment")
    assert {str(path): path.read_bytes() for path in (Path(fragment.attempt), CACHE / editions.READY_FILE)} == before
    assert dispatch.is_symlink() and dispatch.readlink() == Path("nonexistent-synthetic-dispatch.json")


async def _delivered_native5(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[PendingSupplement, dict[str, Any], str, str, Path]:
    fragment = await _origin_fragment(monkeypatch)
    cfg, pending, _ = _project_next(monkeypatch)
    manifest, ready = _import(cfg, pending)
    _, claim = prepared_delivery.claim_edition(ready, now=NEXT)
    monkeypatch.setattr(prepared_delivery, "send_prepared_chunk", AsyncMock(
        side_effect=[("confirmed", index + 1) for index in range(len(manifest["payloads"]))],
    ))
    assert (await prepared_delivery.send_prepared_edition(ready, claim, config=cfg, now=NEXT)).complete
    archive = next(Path(name) for name in manifest["checkpoint_refs"]
                   if name.endswith(".md") and name != fragment.fragment.checkpoint)
    return fragment, manifest, ready, claim, archive


async def test_consumed_native_fragment_verifier_is_read_only_and_accepts_exact_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from digest.application.native_investigation_origin import freeze_native_delivered_input

    fragment, manifest, ready, claim, archive = await _delivered_native5(monkeypatch)
    edition, _ = editions.load_edition(CACHE / editions.READY_FILE, os.environ["TELEGRAM_CHAT_ID"], NEXT, ready)
    receipts = editions.load_receipts(CACHE, ready, claim, len(manifest["payloads"]), manifest["owner_sha256"])
    assert receipts is not None
    paths = [CACHE / name for name in (editions.READY_FILE, editions.CLAIM_FILE, editions.RECEIPTS_FILE)]
    paths += [Path(fragment.attempt), archive]
    before = {str(path): path.read_bytes() for path in paths}
    for _ in range(2):
        supplement.verify_consumed_fragment(edition, ready, receipts)
        origin, _ = freeze_native_delivered_input(archive, archive.read_bytes(), now=NEXT)
        assert origin.ready_sha256 == ready
    assert {str(path): path.read_bytes() for path in paths} == before


@pytest.mark.parametrize("mutation", ["status", "message", "owner", "coverage", "ready_binding", "receipt"])
async def test_tampered_consumption_proof_holds_native5_origin_without_mutation(
    monkeypatch: pytest.MonkeyPatch, mutation: str,
) -> None:
    from digest.application.native_investigation_origin import freeze_native_delivered_input

    fragment, manifest, ready, claim, archive = await _delivered_native5(monkeypatch)
    record = _marker(fragment)
    if mutation == "status":
        record["supplement_status"] = "included_ready"
    elif mutation == "message":
        record["fragment"]["consumed_chunks"][0]["message_id"] += 1
    elif mutation == "owner":
        record["fragment"]["consumed_chunks"][0]["owner_sha256"] = "f" * 64
    elif mutation == "coverage":
        record["fragment"]["consumed_chunks"].pop()
    elif mutation == "ready_binding":
        record["fragment"]["included_ready"] = "f" * 64
    else:
        path = CACHE / editions.RECEIPTS_FILE
        receipt_record = json.loads(path.read_bytes())
        chunk = manifest["supplement"]["coverage"]["covering_chunks"][0]
        receipt_record["confirmed"][chunk]["message_id"] += 1
        path.write_bytes(canonical_json_bytes(receipt_record))
    Path(fragment.attempt).write_bytes(canonical_json_bytes(record))
    edition, _ = editions.load_edition(CACHE / editions.READY_FILE, os.environ["TELEGRAM_CHAT_ID"], NEXT, ready)
    receipts = editions.load_receipts(CACHE, ready, claim, len(manifest["payloads"]), manifest["owner_sha256"])
    assert receipts is not None
    paths = [CACHE / name for name in (editions.READY_FILE, editions.CLAIM_FILE, editions.RECEIPTS_FILE)]
    paths += [Path(fragment.attempt), archive]
    before = {str(path): path.read_bytes() for path in paths}
    with pytest.raises(ValueError):
        supplement.verify_consumed_fragment(edition, ready, receipts)
    with pytest.raises(ValueError):
        freeze_native_delivered_input(archive, archive.read_bytes(), now=NEXT)
    assert {str(path): path.read_bytes() for path in paths} == before


async def test_consumed_fragment_verifier_rejects_receipts_for_another_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    fragment, manifest, ready, claim, _ = await _delivered_native5(monkeypatch)
    edition, _ = editions.load_edition(CACHE / editions.READY_FILE, os.environ["TELEGRAM_CHAT_ID"], NEXT, ready)
    receipts = editions.load_receipts(CACHE, ready, claim, len(manifest["payloads"]), manifest["owner_sha256"])
    assert receipts is not None
    before = Path(fragment.attempt).read_bytes()
    with pytest.raises(ValueError):
        supplement.verify_consumed_fragment(edition, ready, replace(receipts, ready_sha256="f" * 64))
    assert Path(fragment.attempt).read_bytes() == before


async def test_native5_origin_rereads_consumption_proof_before_return(monkeypatch: pytest.MonkeyPatch) -> None:
    from digest.application import native_investigation_origin

    fragment, _, _, _, archive = await _delivered_native5(monkeypatch)
    validate = native_investigation_origin.validate_delivered_input
    injected: bytes | None = None

    def change_after_validation(*args: Any, **kwargs: Any) -> Any:
        nonlocal injected
        result = validate(*args, **kwargs)
        record = _marker(fragment)
        record["fragment"]["consumed_chunks"][0]["message_id"] += 1
        injected = canonical_json_bytes(record)
        Path(fragment.attempt).write_bytes(injected)
        return result

    monkeypatch.setattr(native_investigation_origin, "validate_delivered_input", change_after_validation)
    primary = {str(path): path.read_bytes() for path in CACHE.rglob("*") if path.is_file()}
    with pytest.raises(ValueError):
        native_investigation_origin.freeze_native_delivered_input(archive, archive.read_bytes(), now=NEXT)
    assert Path(fragment.attempt).read_bytes() == injected
    assert {str(path): path.read_bytes() for path in CACHE.rglob("*") if path.is_file()} == primary


async def test_rebound_native_ready_missing_reservation_releases_its_own_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fragment = await _origin_fragment(monkeypatch)
    cfg, pending, _ = _project_next(monkeypatch)
    _, first_ready = _import(cfg, pending)
    second_day = NEXT + timedelta(days=1)
    cfg, second, _ = _project_next(monkeypatch, day=second_day)
    assert _marker(fragment)["fragment"]["released_binding"]["ready_sha256"] == first_ready
    with patch(
        "digest.application.supplement.storage.save_attempt", side_effect=OSError("Synthetic reservation failure"),
    ):
        with pytest.raises(OSError, match="reservation failure"):
            _import(cfg, second, now=second_day)
    second_ready = sha256((CACHE / editions.READY_FILE).read_bytes())
    assert second_ready != first_ready and _marker(fragment)["supplement_status"] == "pending"
    assert not (CACHE / editions.CLAIM_FILE).exists() and not (CACHE / editions.RECEIPTS_FILE).exists()
    third_day = second_day + timedelta(days=1)
    _clock(monkeypatch, third_day)
    native.release_pending(cfg, expected_packet_sha256=second.packet.sha256, now=third_day)
    cfg, third, _ = _project_next(monkeypatch, day=third_day)
    _, third_ready = _import(cfg, third, now=third_day)
    record = _marker(fragment)
    assert third_ready not in (first_ready, second_ready)
    assert record["fragment"]["included_ready"] == third_ready
    assert record["fragment"]["released_binding"] == {
        "ready_sha256": second_ready, "reason": "expired_unclaimed_ready",
    }


async def _last_day_missing_reservation(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[PendingSupplement, Config, storage.PendingNative, datetime, str]:
    fragment = await _origin_fragment(monkeypatch)
    # Retire only synthetic prior dispatch fixtures to isolate the proven safe
    # current-ready/no-dispatch prefix, not to model a recovery of uncertain state.
    for name in (editions.READY_FILE, editions.CLAIM_FILE, editions.RECEIPTS_FILE):
        (CACHE / name).unlink()
    last_day = NEXT + timedelta(days=2)
    cfg, pending, _ = _project_next(monkeypatch, day=last_day)
    with patch(
        "digest.application.supplement.storage.save_attempt", side_effect=OSError("Synthetic reservation failure"),
    ):
        with pytest.raises(OSError, match="reservation failure"):
            _import(cfg, pending, now=last_day)
    assert _marker(fragment)["supplement_status"] == "pending"
    return fragment, cfg, pending, last_day, sha256((CACHE / editions.READY_FILE).read_bytes())


async def test_native_missing_reservation_expiry_does_not_resurrect_fragment_or_block_primary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fragment, cfg, pending, last_day, previous_ready = await _last_day_missing_reservation(monkeypatch)
    expired_day = last_day + timedelta(days=1)
    _clock(monkeypatch, expired_day)
    native.release_pending(cfg, expected_packet_sha256=pending.packet.sha256, now=expired_day)
    cfg, following, projection = _project_next(monkeypatch, day=expired_day)
    assert _marker(fragment)["supplement_status"] == "expired" and "supplement" not in projection["publication"]
    manifest, ready = _import(cfg, following, now=expired_day)
    record = _marker(fragment)
    assert manifest["schema"] == 4 and ready != previous_ready
    assert record["supplement_status"] == "expired" and record["fragment"]["included_ready"] is None
    assert record["supplement_reason"] == "origin_day_plus_four_utc"
    assert record["fragment"]["released_binding"] == {
        "ready_sha256": previous_ready, "reason": "expired_unclaimed_ready",
    }


@pytest.mark.parametrize("mutation", ["wrong_reason", "too_early"])
async def test_native_missing_reservation_cannot_release_unproven_expired_disposition(
    monkeypatch: pytest.MonkeyPatch, mutation: str,
) -> None:
    fragment, _, _, last_day, ready = await _last_day_missing_reservation(monkeypatch)
    now = last_day if mutation == "too_early" else last_day + timedelta(days=1)
    record = _marker(fragment)
    record.update(supplement_status="expired", supplement_reason=(
        "unproven_expiry" if mutation == "wrong_reason" else "origin_day_plus_four_utc"
    ))
    Path(fragment.attempt).write_bytes(canonical_json_bytes(record))
    data, _ = editions.load_edition(CACHE / editions.READY_FILE, os.environ["TELEGRAM_CHAT_ID"], now, ready)
    paths = [CACHE / editions.READY_FILE, CACHE / storage.PENDING_FILE, Path(fragment.attempt)]
    before = {str(path): path.read_bytes() for path in paths}
    with pytest.raises(ValueError):
        supplement.release_replaced_fragment(data, ready, now, CACHE)
    assert {str(path): path.read_bytes() for path in paths} == before
    assert not (CACHE / editions.CLAIM_FILE).exists() and not (CACHE / editions.RECEIPTS_FILE).exists()
