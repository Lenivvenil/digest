"""Synthetic native optional composition: no model, credential or outbound execution."""

from __future__ import annotations

import json
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from digest.adapters.storage import edition as editions
from digest.adapters.storage import native as storage
from digest.application import native, prepared_delivery
from digest.config import ClosingConfig, ClosingSourceBinding, Config, SynthesisConfig
from digest.domain.catalog.articles import article_hash
from digest.domain.catalog.occurrences import occurrence_sha256
from digest.domain.editorial.native import encode, export_packet, read_packet, sha256
from digest.domain.editorial.native_optional import comparison, optional_verdict
from digest.presentation.source_attribution import SOURCE_CREDITS
from tests.test_native_editorial import CACHE, NOW, acceptance_for, config, freeze, occurrences, planned, review_for
from tests.test_native_editorial import isolated as isolated


def optional_config() -> Config:
    cfg = config()
    source = cfg.sources[-1]
    cfg.closing = ClosingConfig(True, (ClosingSourceBinding(source.name, source.url, source.category),))
    cfg.synthesis = SynthesisConfig(True, True)
    return cfg


def seed_optional(cfg: Config) -> storage.PendingNative:
    packet = planned()
    raw = export_packet(packet, publication_date=NOW.date(), language=cfg.radar.language, max_excerpt_chars=500,
                        owner_sha256=sha256(b"123"), editorial_context="Synthetic",
                        optional_policy=native._optional_policy(packet, cfg))
    pending = storage.PendingNative(1, sha256(b"123"), NOW.date().isoformat(), storage.put_bytes(raw, CACHE, "packet"))
    storage.save_pending(CACHE, pending)
    return pending


def output_for(pending: storage.PendingNative) -> dict[str, Any]:
    _, packet = read_packet(storage.read_reference(pending.packet))
    closer = occurrences()[-1]
    closing_id = article_hash(closer.title, closer.link)
    originals = {article_hash(a.title, a.link): a for a in packet.articles}
    main_ids = [item.evidence_id for item in packet.evidence.items if item.evidence_id != closing_id]
    return {
        "schema": "native-editorial-response/v2", "packet_sha256": pending.packet.sha256,
        "execution_ref": "synthetic-writer", "language": "ru",
        "items": [{"evidence_id": item.evidence_id, "status": "card" if item.evidence_id in main_ids else "withheld",
                   "text": "Неизменённый основной текст " + str(i) if item.evidence_id in main_ids else ""}
                  for i, item in enumerate(packet.evidence.items)],
        "closing": {"evidence_id": closing_id, "occurrence_sha256": occurrence_sha256(closer),
                    "text": "Неизменённый заключительный текст."},
        "comparison": {
            "supports": [{"evidence_id": identity, "occurrence_sha256": occurrence_sha256(originals[identity]),
                          "observation": f"Наблюдение {i}."} for i, identity in enumerate(main_ids)],
            "interpretation": "Источники описывают разные подходы.", "qualification": "Это не доказывает общий эффект.",
        },
    }


def source_review(pending: storage.PendingNative, response: dict[str, Any], rejected: int = 0) -> dict[str, Any]:
    result = json.loads(review_for(pending, encode(response), rejected=rejected))
    result["schema"] = "external-source-review/v3"
    result["optional_verdicts"] = [
        {"kind": kind, "value_sha256": sha256(encode(response[kind])), "verdict": "accept",
         "notes": "Synthetic source-specific observations, connection and qualification verdict."}
        for kind in ("closing", "comparison") if response.get(kind) is not None
    ]
    return result


def project_optional(cfg: Config, pending: storage.PendingNative, response: dict[str, Any],
                     review: dict[str, Any] | None = None) -> tuple[storage.PendingNative, dict[str, Any]]:
    retained = native.project_response(cfg, encode(response), encode(review or source_review(pending, response)),
                                      expected_packet_sha256=pending.packet.sha256, now=NOW)
    assert retained.projection
    return retained, json.loads(storage.read_reference(retained.projection))


def test_complete_native_optional_composition_freezes_exact_archive_and_sources() -> None:
    cfg = optional_config()
    cfg.obsidian.enabled, cfg.obsidian.output_dir = True, "digests"
    pending = seed_optional(cfg)
    response = output_for(pending)
    pending, projection = project_optional(cfg, pending, response)
    assert projection["schema"] == "native-composition/v2"
    canonical = projection["publication"]["canonical_metadata"]
    assert canonical["main_count"] == 2 and len(canonical["cards"]) == 3
    assert all(canonical["native_optional"][kind]["status"] == "included" for kind in ("closing", "comparison"))
    assert canonical["cards"][-1]["summary"] == response["closing"]["text"]
    _, packet = read_packet(storage.read_reference(pending.packet))
    evidence = {item.evidence_id: asdict(item) for item in packet.evidence.items}
    for binding in canonical["native_optional"]["comparison"]["bindings"]:
        assert binding["evidence"] == evidence[binding["evidence"]["evidence_id"]]
    path, digest = freeze(pending, cfg)
    ready, _ = editions.load_edition(path, "123", NOW, digest)
    assert storage.publication_fields(asdict(ready)) == projection["publication"]
    archive = next(Path(name).read_text() for name in ready.checkpoint_refs if name.endswith(".md"))
    placement = ready.presentation_metadata["synthesis"]
    assert placement["text"] in archive
    assert archive.index("## Главное") < archive.index(placement["text"]) < archive.index("## Напоследок")
    assert archive.count("## Напоследок") == 1
    for card in ready.articles:
        assert card.card.summary in archive.replace("\n> ", "\n")
    for credit in SOURCE_CREDITS.values():
        assert credit in archive
    assert ready.canonical_metadata["native_editorial"]["provider"] is None
    assert ready.presentation_metadata["translation"] is None


async def test_optional_delivery_counts_closing_once_and_comparison_not_as_article(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = optional_config()
    pending = seed_optional(cfg)
    pending, projection = project_optional(cfg, pending, output_for(pending))
    _, digest = freeze(pending, cfg)
    transport = AsyncMock(side_effect=[("confirmed", i + 1) for i in range(len(projection["publication"]["payloads"]))])
    monkeypatch.setattr(prepared_delivery, "send_prepared_chunk", transport)
    _, claim = prepared_delivery.claim_edition(digest, now=NOW)
    result = await prepared_delivery.send_prepared_edition(digest, claim, config=cfg, now=NOW)
    assert result.complete
    seen = json.loads((CACHE / "seen_articles.json").read_bytes())
    assert len(seen) == 3 and set(seen) == {article_hash(a.title, a.link) for a in occurrences()}
    assert not (CACHE / "candidate_progress.json").exists()


@pytest.mark.parametrize("setting", ["publish", "enabled", "closing"])
def test_disabled_optional_content_is_withheld_before_acceptance(setting: str) -> None:
    cfg = optional_config()
    if setting == "closing":
        cfg.closing = replace(cfg.closing, enabled=False)
    else:
        cfg.synthesis = replace(cfg.synthesis, **{setting: False})
    pending = seed_optional(cfg)
    response = output_for(pending)
    pending, projection = project_optional(cfg, pending, response)
    canonical = projection["publication"]["canonical_metadata"]
    kind = "closing" if setting == "closing" else "comparison"
    assert canonical["native_optional"][kind]["status"] in {"disabled_by_policy", "unpublished_by_policy"}
    path, _ = freeze(pending, cfg)
    ready = json.loads(path.read_bytes())
    withheld = response["closing"]["text"] if kind == "closing" else response["comparison"]["interpretation"]
    assert withheld not in str(ready["payloads"])


@pytest.mark.parametrize("kind", ["closing", "comparison"])
@pytest.mark.parametrize("mutation", ["missing", "duplicate", "reject", "hash", "unknown", "malformed", "null"])
def test_optional_review_failure_omits_only_affected_optional(kind: str, mutation: str) -> None:
    cfg = optional_config()
    pending = seed_optional(cfg)
    response = output_for(pending)
    review = source_review(pending, response)
    verdict = next(v for v in review["optional_verdicts"] if v["kind"] == kind)
    if mutation == "missing":
        review["optional_verdicts"].remove(verdict)
    elif mutation == "duplicate":
        review["optional_verdicts"].append(verdict.copy())
    elif mutation == "reject":
        verdict["verdict"] = "reject"
    elif mutation == "hash":
        verdict["value_sha256"] = "0" * 64
    elif mutation == "unknown":
        verdict["kind"] = "unknown"
    elif mutation == "malformed":
        verdict["kind"] = []
    else:
        review["optional_verdicts"] = None
    pending, projection = project_optional(cfg, pending, response, review)
    canonical = projection["publication"]["canonical_metadata"]
    assert canonical["main_count"] == 2 and canonical["native_optional"][kind]["status"] != "included"
    assert canonical["cards"][:2] == [
        asdict(card) for card in native.accepted_cards(storage.read_reference(pending.packet), encode(response),
            encode(review), read_packet(storage.read_reference(pending.packet))[1], "ru")[0]
    ]
    freeze(pending, cfg)


@pytest.mark.parametrize("kind", ["closing", "comparison"])
@pytest.mark.parametrize("mutation", ["hash", "unknown", "empty", "control", "extra", "long"])
def test_malformed_optional_source_or_text_never_discards_mains(kind: str, mutation: str) -> None:
    cfg = optional_config()
    pending = seed_optional(cfg)
    response = output_for(pending)
    value = response[kind] if kind == "closing" else response[kind]["supports"][0]
    text = "text" if kind == "closing" else "observation"
    if mutation == "hash":
        value["occurrence_sha256"] = "0" * 64
    elif mutation == "unknown":
        value["evidence_id"] = "0" * 32
    elif mutation in {"empty", "control", "long"}:
        value[text] = {"empty": "", "control": "a\nb", "long": "x" * 601}[mutation]
    else:
        value["extra"] = True
    pending, projection = project_optional(cfg, pending, response)
    canonical = projection["publication"]["canonical_metadata"]
    assert canonical["main_count"] == 2
    assert canonical["native_optional"][kind]["status"] == "withheld_invalid_source_or_layout"
    freeze(pending, cfg)


@pytest.mark.parametrize("rejected", [1, 2])
def test_comparison_rejected_dependency_and_empty_main_release(rejected: int) -> None:
    cfg = optional_config()
    pending = seed_optional(cfg)
    response = output_for(pending)
    pending, projection = project_optional(cfg, pending, response, source_review(pending, response, rejected))
    canonical = projection["publication"]["canonical_metadata"]
    assert canonical["main_count"] == 2 - rejected
    assert canonical["native_optional"]["comparison"]["status"] != "included"
    if rejected == 2:
        assert not projection["publication"]["articles"]
        assert canonical["native_optional"]["closing"]["status"] == "no_retained_main"
        with pytest.raises(ValueError, match="no main cards"):
            freeze(pending, cfg)
        path, _ = native.release_pending(cfg, expected_packet_sha256=pending.packet.sha256, now=NOW)
        assert json.loads(path.read_bytes())["reason"] == "no_retained_main"
        assert storage.load_pending(CACHE) is None
    else:
        assert canonical["native_optional"]["closing"]["status"] == "included"
        freeze(pending, cfg)


@pytest.mark.parametrize("change", ["closing", "approval", "publish", "enabled"])
def test_current_optional_policy_changes_block_exact_import(change: str) -> None:
    cfg = optional_config()
    pending = seed_optional(cfg)
    pending, _ = project_optional(cfg, pending, output_for(pending))
    if change == "closing":
        cfg.closing = replace(cfg.closing, enabled=False)
    elif change == "approval":
        cfg.closing = replace(cfg.closing, approved_sources=())
    else:
        cfg.synthesis = replace(cfg.synthesis, **{change: False})
    with pytest.raises(ValueError, match="composition/config changed"):
        freeze(pending, cfg)
    assert storage.load_pending(CACHE) == pending and not (CACHE / editions.READY_FILE).exists()


def test_closing_cannot_recycle_rejected_generated_main() -> None:
    cfg = optional_config()
    cfg.closing = ClosingConfig(True, tuple(ClosingSourceBinding(s.name, s.url, s.category) for s in cfg.sources))
    pending = seed_optional(cfg)
    response = output_for(pending)
    reviewed_main = response["comparison"]["supports"][1]
    response["closing"].update({k: reviewed_main[k] for k in ("evidence_id", "occurrence_sha256")})
    review = source_review(pending, response)
    review["card_verdicts"][1]["verdict"] = "reject"
    _, projection = project_optional(cfg, pending, response, review)
    closing = projection["publication"]["canonical_metadata"]["native_optional"]["closing"]
    assert closing["status"] == "withheld_invalid_source_or_layout"


@pytest.mark.parametrize("mutation", ["reversed", "duplicate", "one", "closing", "same_source", "empty_excerpt"])
def test_comparison_requires_two_ordered_complete_distinct_main_sources(mutation: str) -> None:
    cfg = optional_config()
    pending = seed_optional(cfg)
    response = output_for(pending)
    _, packet = read_packet(storage.read_reference(pending.packet))
    value = response["comparison"]
    ids = [item["evidence_id"] for item in response["items"] if item["status"] == "card"]
    if mutation == "reversed":
        value["supports"].reverse()
    elif mutation == "duplicate":
        value["supports"][1] = value["supports"][0]
    elif mutation == "one":
        value["supports"].pop()
    elif mutation == "closing":
        value["supports"][0].update({k: response["closing"][k] for k in ("evidence_id", "occurrence_sha256")})
    elif mutation == "same_source":
        originals = {article_hash(a.title, a.link): a for a in packet.articles}
        first = originals[ids[0]]
        changed = replace(originals[ids[1]], source=first.source, source_url=first.source_url)
        packet = replace(packet, articles=tuple(changed if article_hash(a.title, a.link) == ids[1] else a
                                                for a in packet.articles))
        value["supports"][1]["occurrence_sha256"] = occurrence_sha256(changed)
    else:
        packet = replace(packet, evidence=replace(packet.evidence, items=tuple(replace(item, excerpt="")
            if item.evidence_id == ids[0] else item for item in packet.evidence.items)))
    with pytest.raises(ValueError):
        comparison(value, packet, ids)


@pytest.mark.parametrize("comparison_enabled", [True, False])
def test_closing_archive_heading_does_not_need_fake_comparison(comparison_enabled: bool) -> None:
    cfg = optional_config()
    cfg.synthesis = replace(cfg.synthesis, enabled=comparison_enabled)
    cfg.obsidian.enabled, cfg.obsidian.output_dir = True, "digests"
    pending = seed_optional(cfg)
    pending, _ = project_optional(cfg, pending, output_for(pending))
    path, _ = freeze(pending, cfg)
    ready = json.loads(path.read_bytes())
    archive = next(Path(name).read_text() for name in ready["checkpoint_refs"] if name.endswith(".md"))
    assert archive.count("## Главное") == archive.count("## Напоследок") == 1
    assert ("Сопоставление выбранных материалов" in archive) == comparison_enabled


@pytest.mark.parametrize("target", ["closing", "comparison", "placement", "payload"])
async def test_optional_tampering_cannot_claim_or_send_rehashed_ready(target: str) -> None:
    cfg = optional_config()
    pending = seed_optional(cfg)
    pending, _ = project_optional(cfg, pending, output_for(pending))
    path, digest = freeze(pending, cfg)
    _, claim = prepared_delivery.claim_edition(digest, now=NOW)
    data = json.loads(path.read_bytes())
    if target in {"closing", "comparison"}:
        data["canonical_metadata"]["native_optional"][target]["status"] = "tampered"
    elif target == "placement":
        data["presentation_metadata"]["synthesis"]["before_article"] = 0
    else:
        data["payloads"][0]["text"] += " changed"
    data["canonical_sha256"] = sha256(editions.canonical_bytes(data["canonical_metadata"]))
    data["presentation_sha256"] = sha256(editions.canonical_bytes(data["presentation_metadata"]))
    data["content_sha256"] = sha256(editions.canonical_bytes({k: v for k, v in data.items() if k != "content_sha256"}))
    changed = editions.write_record(path, data)
    with pytest.raises(ValueError):
        await prepared_delivery.send_prepared_edition(changed, claim, config=cfg, now=NOW)
    (CACHE / editions.CLAIM_FILE).unlink()
    with pytest.raises(ValueError):
        prepared_delivery.claim_edition(changed, now=NOW)
    assert not (CACHE / editions.RECEIPTS_FILE).exists() and not (CACHE / editions.CLAIM_FILE).exists()


def test_absent_optionals_remain_absent_and_acceptance_still_required() -> None:
    cfg = optional_config()
    pending = seed_optional(cfg)
    response = output_for(pending)
    response.pop("closing")
    response["comparison"] = None
    pending, projection = project_optional(cfg, pending, response)
    canonical = projection["publication"]["canonical_metadata"]
    assert canonical["native_optional"]["closing"] == canonical["native_optional"]["comparison"] == {"status": "absent"}
    refusal = json.loads(acceptance_for(pending))
    refusal["verdict"] = "reject"
    assert pending.projection
    with pytest.raises(ValueError, match="acceptance"):
        native.import_accepted(cfg, encode(refusal), expected_projection_sha256=pending.projection.sha256, now=NOW)
    assert optional_verdict("closing", {}, {"optional_verdicts": []}) == "incomplete_optional_review"


def test_legacy_projected_v1_bytes_remain_identical_to_frozen_checkpoint() -> None:
    from tests.test_native_editorial import project

    pending = project()
    assert pending.projection
    # Independently generated by the frozen previous integration in a synthetic cwd.
    assert pending.projection.sha256 == "982f4fede49a4e83bd306ab258e76be08ca414abed98282d27a13939f4d03d94"
    freeze(pending)


def test_old_response_on_new_packet_does_not_activate_old_optional_objects() -> None:
    cfg = optional_config()
    pending = seed_optional(cfg)
    response = output_for(pending)
    response["schema"] = "native-editorial-response/v1"
    pending, projection = project_optional(cfg, pending, response, json.loads(review_for(pending, encode(response))))
    assert projection["schema"] == "native-composition/v1"
    assert "native_optional" not in projection["publication"]["canonical_metadata"]
    path, _ = freeze(pending, cfg)
    assert response["closing"]["text"] not in str(json.loads(path.read_bytes())["payloads"])


def test_new_response_cannot_upgrade_historical_v1_packet() -> None:
    from tests.test_native_editorial import seed

    cfg = optional_config()
    pending = seed()
    response = output_for(pending)
    before = set(CACHE.rglob("*"))
    with pytest.raises(ValueError, match="Independent acceptance"):
        project_optional(cfg, pending, response)
    assert set(CACHE.rglob("*")) == before


@pytest.mark.parametrize("kind", ["closing", "comparison"])
def test_captured_disabled_policy_cannot_be_enabled_by_later_config(kind: str) -> None:
    cfg = optional_config()
    if kind == "closing":
        cfg.closing = replace(cfg.closing, enabled=False)
    else:
        cfg.synthesis = replace(cfg.synthesis, publish=False)
    pending = seed_optional(cfg)
    cfg = optional_config()
    pending, projection = project_optional(cfg, pending, output_for(pending))
    assert projection["publication"]["canonical_metadata"]["native_optional"][kind]["status"] != "included"
    freeze(pending, cfg)


def test_comparison_layout_failure_withholds_only_comparison(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = optional_config()
    pending = seed_optional(cfg)
    monkeypatch.setattr(native, "_same_synthesis_layout", lambda *_: False)
    pending, projection = project_optional(cfg, pending, output_for(pending))
    optional = projection["publication"]["canonical_metadata"]["native_optional"]
    assert optional["comparison"]["status"] == "withheld_invalid_source_or_layout"
    assert optional["closing"]["status"] == "included"
    freeze(pending, cfg)


def test_closing_credit_split_omits_closing_without_changing_mains(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = optional_config()
    pending = seed_optional(cfg)
    render = native.render_compact_publication

    def split_closing(cards: Any, *args: Any, **kwargs: Any) -> Any:
        rendered = render(cards, *args, **kwargs)
        if len(cards) == 3:
            rendered.articles[-1].covering_chunks = (0, 1)
        return rendered

    monkeypatch.setattr(native, "render_compact_publication", split_closing)
    pending, projection = project_optional(cfg, pending, output_for(pending))
    canonical = projection["publication"]["canonical_metadata"]
    assert canonical["main_count"] == len(canonical["cards"]) == 2
    assert canonical["native_optional"]["closing"]["status"] == "withheld_invalid_source_or_layout"
    assert canonical["native_optional"]["comparison"]["status"] == "included"
    freeze(pending, cfg)


def test_source_approval_without_reviewed_credit_does_not_admit_closing() -> None:
    cfg = optional_config()
    generic = cfg.sources[0]
    cfg.closing = ClosingConfig(True, (ClosingSourceBinding(generic.name, generic.url, generic.category),))
    pending = seed_optional(cfg)
    envelope, _ = read_packet(storage.read_reference(pending.packet))
    assert envelope["optional_policy"]["closing_ids"] == []
    pending, projection = project_optional(cfg, pending, output_for(pending))
    assert projection["publication"]["canonical_metadata"]["native_optional"]["closing"]["status"] != "included"
    freeze(pending, cfg)


@pytest.mark.parametrize("mutation", ["duplicate", "unknown", "disabled", "type", "schema"])
def test_invalid_exported_optional_policy_rejected(mutation: str) -> None:
    cfg = optional_config()
    pending = seed_optional(cfg)
    envelope = json.loads(storage.read_reference(pending.packet))
    policy = envelope["optional_policy"]
    if mutation == "duplicate":
        policy["closing_ids"] *= 2
    elif mutation == "unknown":
        policy["closing_ids"] = ["0" * 32]
    elif mutation == "disabled":
        policy["closing_enabled"] = False
    elif mutation == "type":
        policy["comparison_publish"] = "true"
    else:
        envelope["schema"] = "native-source-packet/v1"
    with pytest.raises(ValueError):
        read_packet(encode(envelope))


@pytest.mark.parametrize("language", ["ru", "en"])
def test_native_optional_prose_keeps_one_authoritative_language(language: str) -> None:
    cfg = optional_config()
    cfg.radar.language = language
    cfg.obsidian.enabled, cfg.obsidian.output_dir = True, "digests"
    pending = seed_optional(cfg)
    response = output_for(pending)
    response["language"] = language
    if language == "en":
        response["closing"]["text"] = "Unchanged closing text."
        response["comparison"]["interpretation"] = "The sources describe different approaches."
        response["comparison"]["qualification"] = None
        for support in response["comparison"]["supports"]:
            support["observation"] = "Source-specific observation."
        for item in response["items"]:
            if item["status"] == "card":
                item["text"] = "Unchanged main text."
    pending, projection = project_optional(cfg, pending, response)
    path, _ = freeze(pending, cfg)
    ready = json.loads(path.read_bytes())
    archive = next(Path(name).read_text() for name in ready["checkpoint_refs"] if name.endswith(".md"))
    assert ("## Напоследок" if language == "ru" else "## Closing story") in archive
    assert response["closing"]["text"] in archive  # No translation or rewriting performed.
    assert projection["publication"]["presentation_metadata"]["translation"] is None


@pytest.mark.parametrize("position", [-1, 0, 3, 4])
def test_markdown_rejects_invalid_explicit_closing_boundary(position: int) -> None:
    from digest.delivery.markdown import write_digest
    from digest.domain.editorial.summaries import ArticleSummary

    cfg = optional_config()
    cfg.obsidian.enabled, cfg.obsidian.output_dir = True, "digests"
    cards = [ArticleSummary(a.title, a.link, a.source, a.category, "Synthetic") for a in occurrences()]
    with pytest.raises(ValueError, match="closing publication position"):
        write_digest("", cfg, top_articles=cards, closing_start=position)


def test_comparison_total_escaped_utf16_budget_omits_only_comparison() -> None:
    cfg = optional_config()
    pending = seed_optional(cfg)
    response = output_for(pending)
    for support in response["comparison"]["supports"]:
        support["observation"] = "😀" * 180
    response["comparison"]["interpretation"] = "i" * 240
    response["comparison"]["qualification"] = "q" * 160
    pending, projection = project_optional(cfg, pending, response)
    optional = projection["publication"]["canonical_metadata"]["native_optional"]
    assert optional["comparison"]["status"] == "withheld_invalid_source_or_layout"
    assert optional["closing"]["status"] == "included"
    freeze(pending, cfg)


def test_optional_archive_interruption_reuses_exact_frozen_composition(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = optional_config()
    cfg.obsidian.enabled, cfg.obsidian.output_dir = True, "digests"
    pending = seed_optional(cfg)
    response = output_for(pending)
    response["comparison"]["qualification"] = None
    pending, projection = project_optional(cfg, pending, response)
    prepare = prepared_delivery.prepare_edition

    def interrupted(*args: Any, **kwargs: Any) -> Any:
        raise OSError("Synthetic freeze interruption after native archive persistence")

    monkeypatch.setattr(prepared_delivery, "prepare_edition", interrupted)
    with pytest.raises(OSError, match="interruption"):
        freeze(pending, cfg)
    retained = storage.load_pending(CACHE)
    assert retained and retained.archive and retained.acceptance and retained.projection == pending.projection
    archive_raw = storage.read_reference(retained.archive)
    monkeypatch.setattr(prepared_delivery, "prepare_edition", prepare)
    path, digest = freeze(retained, cfg)
    ready, _ = editions.load_edition(path, "123", NOW, digest)
    assert storage.publication_fields(asdict(ready)) == projection["publication"]
    assert len(list(Path("digests").glob("*.md"))) == 1
    assert storage.read_reference(retained.archive) == archive_raw


@pytest.mark.parametrize("value", [[], 1, "unknown", {"kind": "closing"}])
def test_bad_optional_review_entry_is_safe(value: Any) -> None:
    assert optional_verdict("closing", {}, {"optional_verdicts": [value]}) == "invalid_optional_review"
