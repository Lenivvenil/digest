"""Model-free native export → exact projection → accepted immutable edition.

Managed runtime serializes these phases with ordinary preparation/delivery and
supplies independently reviewed bytes. This module neither schedules nor generates.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from digest.adapters.storage import edition as editions
from digest.adapters.storage import native as storage
from digest.adapters.storage.feedback import load_feedback
from digest.adapters.storage.sources import load_source_state, load_stats, save_source_category_map
from digest.application import native_supplement, prepared_delivery
from digest.application.source_scoring import prepare_collection_portfolio
from digest.config import Config
from digest.domain.catalog.articles import article_hash
from digest.domain.catalog.occurrences import occurrence_sha256
from digest.domain.delivery.edition import PreparedArticle, parse_instant, validate_checkpoint_reference
from digest.domain.delivery.supplement import PendingSupplement, PreparedSupplement
from digest.domain.editorial.candidate_policy import reconcile_eligibility
from digest.domain.editorial.candidates import Candidate, CandidatePacket, CandidateProgress
from digest.domain.editorial.native import accepted_cards, comparison_dependency, encode, export_packet, read_packet
from digest.domain.editorial.summaries import ArticleSummary
from digest.edition_runtime import _engine_provenance, _same_synthesis_layout, _strict_cache, publish_outputs
from digest.preparation import load_accepted_preparation
from digest.presentation import publication_language
from digest.presentation.source_attribution import attribute_source_card
from digest.presentation.supplement import reader_date
from digest.presentation.telegram import (
    CompactPublication,
    SupplementPlacement,
    SynthesisPlacement,
    comparison_text,
    render_compact_publication,
)


def _outputs(**values: str) -> None:
    publish_outputs(**values)
    logging.getLogger(__name__).info("Native handoff: %s", json.dumps(values, sort_keys=True))


def _portfolio(config: Config, cache: Path) -> tuple[Config, dict[str, int]]:
    return prepare_collection_portfolio(
        config,
        load_source_state(str(cache), strict=True),
        load_stats(str(cache), strict=True),
        load_feedback(str(cache), strict=True),
    )


def _guard(cache: Path, day: date, now: datetime) -> None:
    _, _, status = prepared_delivery.inspect_edition(cache_dir=cache, now=now)
    if status not in {"missing", "expired"}:
        raise ValueError("Existing edition/claim must be resolved before native handoff.")
    if load_accepted_preparation(cache_dir=cache, publication_date=day, now=now) is not None:
        raise ValueError("Accepted provider preparation is pending; native handoff blocked.")


def _identity(config: Config, day: date, now: datetime) -> str:
    if config.telegram.delivery_mode != "compact" or day < now.date():
        raise ValueError("Native handoff requires compact mode and a current/future UTC edition date.")
    return prepared_delivery._owner()[1]


def _pending(cache: Path, config: Config, now: datetime) -> storage.PendingNative:
    pending = storage.load_pending(cache)
    if pending is None:
        raise ValueError("No native handoff exists; export first.")
    if pending.owner_sha256 != _identity(config, date.fromisoformat(pending.publication_date), now):
        raise ValueError("Native handoff owner changed.")
    return pending


def _eligible(packet: CandidatePacket, config: Config, cache: Path, now: datetime) -> Config:
    effective, priorities = _portfolio(config, cache)
    delivered = _strict_cache(cache / "seen_articles.json")
    progress = CandidateProgress(
        candidates={
            article_hash(item.title, item.link): Candidate(
                article_hash(item.title, item.link),
                item,
                packet.planned_at,
                1,
            )
            for item in packet.articles
        }
    )
    from digest.radar.collector import _prune_cache

    reconcile_eligibility(
        progress,
        {source.name: source for source in effective.enabled_sources},
        config.filters.blocklist_keywords,
        _prune_cache(delivered),
        priorities,
        now,
        delivered,
    )
    if any(not item.eligible for item in progress.candidates.values()):
        raise ValueError("Native packet contains a no-longer-eligible exact source occurrence; no recomposition.")
    return effective


async def export_sources(
    config: Config,
    *,
    cache: Path = Path(".cache"),
    day: date | None = None,
    now: datetime | None = None,
) -> storage.PendingNative | None:
    instant = prepared_delivery._instant(now)
    intended = day or instant.date()
    owner = _identity(config, intended, instant)
    _guard(cache, intended, instant)
    pending = storage.load_pending(cache)
    if pending is not None:
        if pending.owner_sha256 != owner or pending.publication_date != intended.isoformat():
            raise ValueError("Conflicting native handoff is pending.")
        return pending  # No re-fetch or re-planning after the durable handoff.
    from digest.application.candidate_lifecycle import checkpoint_candidates
    from digest.application.preparation import (
        CandidateWork,
        _candidate_setup,
        _save_prepared_fetch_stats,
        plan_candidate_inputs,
    )
    from digest.radar import AllFeedsFailedError, collect

    effective, priorities = _portfolio(config, cache)
    pool = _candidate_setup(str(cache))
    metrics: dict[str, Any] = {}
    failed = False
    try:
        articles, _ = await collect(
            effective, effective_priorities=priorities, fetch_metrics=metrics, inventory=pool.inventory
        )
    except AllFeedsFailedError:
        failed, articles = True, {}
    source_state = load_source_state(str(cache), strict=True)
    _save_prepared_fetch_stats(load_stats(str(cache), strict=True), metrics, articles, config, str(cache), False)
    work, _, deferred = plan_candidate_inputs(
        pool,
        effective,
        config,
        priorities,
        str(cache),
        failed,
        trial_sources=frozenset(
            (source.name, source.url, source.category)
            for source in effective.enabled_sources
            if source.trial and not source_state.is_graduated(source.name) and not source_state.is_demoted(source.name)
        ),
    )
    save_source_category_map(effective.enabled_sources, str(cache))
    if not isinstance(work, CandidateWork):
        checkpoint_candidates(pool.progress, str(cache), skipped_empty_reports=deferred)
        return None
    if work.packet in work.progress.packets:
        raise ValueError("Existing dispatched/provider packet must finish through its original route.")
    effective = _eligible(work.packet, config, cache, instant)
    raw = export_packet(
        work.packet,
        publication_date=intended,
        language=publication_language(config),
        max_excerpt_chars=config.review.max_excerpt_chars,
        owner_sha256=owner,
        editorial_context=config.review.editorial_context,
        optional_policy=_optional_policy(work.packet, effective),
    )
    checkpoint_candidates(pool.progress, str(cache), skipped_empty_reports=deferred)
    pending = storage.PendingNative(1, owner, intended.isoformat(), storage.put_bytes(raw, cache, "packet"))
    storage.save_pending(cache, pending)
    return pending


def _present(
    cards: list[ArticleSummary], packet: CandidatePacket, day: date, language: str
) -> tuple[
    list[ArticleSummary],
    set[str],
]:
    originals = {article_hash(item.title, item.link): item for item in packet.articles}
    shown, credited = [], set()
    for card in cards:
        identity = article_hash(card.title, card.link)
        occurrence = originals[identity]
        published = (
            datetime.fromisoformat(occurrence.published).astimezone(timezone.utc) if occurrence.published else None
        )
        if published is not None and published.date() > day:
            stamp, target = reader_date(published.isoformat(), language), reader_date(day.isoformat(), language)
            notice = (
                f"Дата в ленте источника (UTC): {stamp}; позже даты выпуска ({target})."
                if language == "ru"
                else f"Source feed date (UTC): {stamp}; later than this edition’s date ({target})."
            )
            card = replace(card, summary=f"{notice} {card.summary}")
        card, added = attribute_source_card(card, occurrence, occurrence_sha256(occurrence))
        if added:
            credited.add(identity)
        shown.append(card)
    return shown, credited


def _optional_policy(packet: CandidatePacket, config: Config) -> dict[str, Any]:
    from digest.application.review_request import closing_source_bindings
    from digest.presentation.source_attribution import supports_source_attribution

    approved = closing_source_bindings(config.closing, config.enabled_sources)
    return {
        "closing_enabled": config.closing.enabled,
        "closing_ids": [
            article_hash(item.title, item.link)
            for item in packet.articles
            if config.closing.enabled
            and (item.source, item.source_url, item.category) in approved
            and supports_source_attribution(item.source_url)
        ],
        "comparison_enabled": config.synthesis.enabled,
        "comparison_publish": config.synthesis.publish,
    }


def _optional_publication(
    cards: list[ArticleSummary],
    shown: list[ArticleSummary],
    rendered: CompactPublication | None,
    packet: CandidatePacket,
    envelope: dict[str, Any],
    response: dict[str, Any],
    review: dict[str, Any],
    config: Config,
    day: date,
    language: str,
) -> tuple[list[ArticleSummary], list[ArticleSummary], CompactPublication | None, dict[str, Any]]:
    """Admit optional prose before final review; failures never remove accepted mains."""
    from digest.domain.editorial.native_optional import closing_card, comparison, comparison_metadata, optional_verdict

    captured, current = envelope["optional_policy"], _optional_policy(packet, config)
    main_ids = [article_hash(card.title, card.link) for card in cards]
    metadata: dict[str, Any] = {
        "main_count": len(cards), "closing_start": None, "synthesis": None,
        "closing": {"status": "absent"}, "comparison": {"status": "absent"},
    }
    for kind in ("closing", "comparison"):
        value = response.get(kind)
        if value is None:
            continue
        status = optional_verdict(kind, value, review)
        if not cards:
            status = "no_retained_main"
        elif not captured[kind + "_enabled"] or not current[kind + "_enabled"]:
            status = "disabled_by_policy"
        elif kind == "comparison" and (not captured["comparison_publish"] or not current["comparison_publish"]):
            status = "unpublished_by_policy"
        metadata[kind] = {"status": status}
        if status != "accepted":
            continue
        try:
            if kind == "closing":
                card = closing_card(value, packet,
                    {item["evidence_id"] for item in response["items"] if item["status"] == "card"},
                    set(captured["closing_ids"]) & set(current["closing_ids"]))
                extended, credited = _present([*cards, card], packet, day, language)
                candidate = render_compact_publication(extended, config, "")
                if any(item.full_hash in credited and len(item.covering_chunks) != 1 for item in candidate.articles):
                    raise ValueError("Optional closing cannot preserve required source credits.")
                metadata["closing"] = {"status": "included", "value": value, "card": asdict(card)}
                metadata["closing_start"] = len(cards)
                cards, shown, rendered = [*cards, card], extended, candidate
            else:
                result, bindings = comparison(value, packet, main_ids)
                text = comparison_text(language,
                    [(binding.source, binding.evidence.url, support.observation)
                     for binding, support in zip(bindings, result.supports, strict=True)],
                    result.interpretation, result.qualification)
                placement = SynthesisPlacement(text, len(main_ids))
                candidate = render_compact_publication(shown, config, "", synthesis=placement)
                if not _same_synthesis_layout(rendered, candidate):
                    raise ValueError("Optional comparison changes article/credit/vote layout.")
                metadata["comparison"] = {"status": "included", **comparison_metadata(result, bindings)}
                metadata["synthesis"] = asdict(placement)
                rendered = candidate
        except ValueError:
            metadata[kind] = {"status": "withheld_invalid_source_or_layout"}
    return cards, shown, rendered, metadata


def _projection(
    pending: storage.PendingNative,
    response_raw: bytes,
    review_raw: bytes,
    config: Config,
    supplement: PendingSupplement | None = None,
) -> dict[str, Any]:
    assert pending.response is not None and pending.review is not None
    packet_raw = storage.read_reference(pending.packet)
    envelope, packet = read_packet(packet_raw)
    language, day = envelope["language"], date.fromisoformat(pending.publication_date)
    if language != publication_language(config) or envelope["owner_sha256"] != pending.owner_sha256:
        raise ValueError("Native publication language or owner changed.")
    cards, response, review = accepted_cards(packet_raw, response_raw, review_raw, packet, language)
    shown, credited = _present(cards, packet, day, language)
    rendered = render_compact_publication(shown, config, "") if shown else None
    if rendered and any(item.full_hash in credited and len(item.covering_chunks) != 1 for item in rendered.articles):
        raise ValueError("Required source attribution cannot remain in one delivery chunk.")
    optional = None
    if response["schema"] == "native-editorial-response/v2":
        cards, shown, rendered, optional = _optional_publication(
            cards, shown, rendered, packet, envelope, response, review, config, day, language
        )
    inputs = {
        "packet_sha256": pending.packet.sha256,
        "response_sha256": editions.content_sha256(response_raw),
        "review_sha256": editions.content_sha256(review_raw),
    }
    provenance = {
        "kind": "external_native",
        "provider": None,
        "model": None,
        "usage": None,
        "writer_execution_ref": response["execution_ref"],
        "reviewer_execution_ref": review["reviewer_execution_ref"],
        "original_edition_verdict": review["verdict"],
        "inputs": inputs,
    }
    canonical = {
        "language": language,
        "cards": [asdict(card) for card in cards],
        "native_editorial": provenance,
        "contributing_sources": sorted({item.source for item in packet.articles}),
        "source_count": len({item.category for item in packet.articles}),
        "article_count": len(packet.articles),
        "counts_scope": "planned_packet_category_groups_and_articles",
        "distinct_packet_publishers": len({item.source for item in packet.articles}),
    }
    if config.obsidian.enabled:
        from digest.adapters.storage.issue_paths import safe_issue_path

        safe_issue_path(Path(config.obsidian.output_dir)).resolve().relative_to(Path.cwd().resolve())
    presentation = {
        "language": language,
        "cards": [asdict(card) for card in shown],
        "translation": None,
        "closing": "withheld_native_contract_not_implemented",
        "comparison": comparison_dependency(response, {article_hash(c.title, c.link) for c in cards}),
        "comparison_publication": "withheld_native_contract_not_implemented",
    }
    if optional is not None:
        canonical["main_count"] = optional["main_count"]
        canonical["native_optional"] = {key: optional[key] for key in ("closing", "comparison")}
        presentation.update({key: optional[key] for key in ("closing_start", "synthesis")})
        presentation["closing"] = optional["closing"]["status"]
        presentation["comparison"] = optional["comparison"]["status"]
        presentation["comparison_publication"] = optional["comparison"]["status"]
    frozen_supplement = None
    if supplement is not None:
        main_count = optional["main_count"] if optional is not None else len(cards)
        if not main_count:
            raise ValueError("A native supplement requires accepted main cards.")
        placement = SupplementPlacement(supplement.fragment.fragment_id, supplement.fragment.text, main_count)
        synthesis = SynthesisPlacement(**optional["synthesis"]) if optional and optional["synthesis"] else None
        try:
            rendered = render_compact_publication(shown, config, "", placement, synthesis=synthesis)
        except ValueError as exc:
            raise native_supplement.SupplementLayoutError("Native supplement cannot fit safely.") from exc
        _, all_credited = _present(cards, packet, day, language)
        if any(item.full_hash in all_credited and len(item.covering_chunks) != 1 for item in rendered.articles):
            raise native_supplement.SupplementLayoutError("Native supplement would split required source credit.")
        if synthesis is not None and (rendered.synthesis is None or len(rendered.synthesis) != 1):
            raise native_supplement.SupplementLayoutError("Native supplement would split the admitted comparison.")
        assert rendered.supplement is not None
        frozen_supplement = PreparedSupplement(supplement.fragment, supplement.projection,
            supplement.projection_sha256, supplement.attempt, rendered.supplement)
        presentation["supplement"] = {"status": "included", "before_article": main_count}
    owner, _ = prepared_delivery._owner()
    payloads = []
    if rendered:
        for chunk in rendered.chunks:
            payload: dict[str, Any] = {
                "chat_id": owner,
                "text": chunk.text,
                "parse_mode": "MarkdownV2",
                "disable_notification": False,
            }
            if chunk.reply_markup is not None:
                payload["reply_markup"] = chunk.reply_markup
            payloads.append(payload)
    return {
        "schema": "native-composition/v3" if supplement is not None else (
            "native-composition/v2" if optional is not None else "native-composition/v1"
        ),
        "inputs": inputs,
        "references": {
            "packet": asdict(pending.packet),
            "response": asdict(pending.response),
            "review": asdict(pending.review),
        },
        "archive_enabled": config.obsidian.enabled,
        "publication": {
            **({"supplement": json.loads(encode(asdict(frozen_supplement)))} if frozen_supplement is not None else {}),
            "owner_sha256": pending.owner_sha256,
            "bot_username": config.telegram.bot_username,
            "window_start": datetime.combine(day, datetime.min.time(), timezone.utc).isoformat(),
            "canonical_metadata": canonical,
            "presentation_metadata": presentation,
            "payloads": payloads,
            "articles": [
                asdict(
                    PreparedArticle(
                        item.full_hash,
                        item.source,
                        list(item.covering_chunks),
                        card,
                    )
                )
                for item, card in zip(rendered.articles, shown, strict=True)
            ]
            if rendered
            else [],
        },
    }


def project_response(
    config: Config,
    response_raw: bytes,
    review_raw: bytes,
    *,
    expected_packet_sha256: str,
    cache: Path = Path(".cache"),
    now: datetime | None = None,
) -> storage.PendingNative:
    instant = prepared_delivery._instant(now)
    pending = _pending(cache, config, instant)
    _guard(cache, date.fromisoformat(pending.publication_date), instant)
    if pending.packet.sha256 != expected_packet_sha256:
        raise ValueError("Native packet anchor mismatch.")
    _, packet = read_packet(storage.read_reference(pending.packet))
    effective = _eligible(packet, config, cache, instant)
    # Validate before writing any new sidecars. Existing accepted projection is immutable.
    response = storage.Reference("input.response.json", editions.content_sha256(response_raw))
    review = storage.Reference("input.review.json", editions.content_sha256(review_raw))
    staged = replace(pending, response=response, review=review)
    baseline = _projection(staged, response_raw, review_raw, effective)
    supplement = native_supplement.retained(pending)
    if pending.response is not None:
        if (
            pending.response.sha256 != response.sha256
            or pending.review is None
            or pending.review.sha256 != review.sha256
        ):
            raise ValueError("A native response/review is already retained; replacement blocked.")
        if supplement is not None:
            native_supplement.verify_available(supplement, day=date.fromisoformat(pending.publication_date),
                now=instant, owner_sha256=pending.owner_sha256)
        return pending
    supplement = (native_supplement.select_existing(effective, day=date.fromisoformat(pending.publication_date),
        now=instant, owner_sha256=pending.owner_sha256) if _main_count(baseline) else None)
    if supplement is not None:
        try:
            _projection(staged, response_raw, review_raw, effective, supplement)
        except native_supplement.SupplementLayoutError:
            from digest.application.supplement import archive_fragment

            archive_fragment(supplement, "supplement_rendering_failed")
            supplement = None
    staged = replace(
        pending,
        response=storage.put_bytes(response_raw, cache, "response"),
        review=storage.put_bytes(review_raw, cache, "source-review"),
    )
    projected = _projection(staged, response_raw, review_raw, effective, supplement)
    staged = replace(staged, projection=storage.put_bytes(encode(projected), cache, "composition"))
    storage.save_pending(cache, staged)
    return staged


def import_accepted(
    config: Config,
    acceptance_raw: bytes,
    *,
    expected_projection_sha256: str,
    cache: Path = Path(".cache"),
    now: datetime | None = None,
) -> tuple[Path, str]:
    instant = prepared_delivery._instant(now)
    pending = _pending(cache, config, instant)
    if pending.projection is None or pending.response is None or pending.review is None:
        raise ValueError("Native composition must be projected before final acceptance.")
    if pending.projection.sha256 != expected_projection_sha256:
        raise ValueError("Native projection anchor mismatch.")
    projected = storage.decode(storage.read_reference(pending.projection))
    storage.validate_acceptance(acceptance_raw, pending.projection, projected)
    if pending.acceptance is not None and pending.acceptance.sha256 != editions.content_sha256(acceptance_raw):
        raise ValueError("Final native acceptance replacement blocked.")
    # Ready publication is the existing recovery boundary, including after local interruption.
    ready, digest, status = prepared_delivery.inspect_edition(cache_dir=cache, now=instant)
    if ready is not None and status in {"ready", "pending_window", "confirmed"} and pending.acceptance is not None:
        if ready.get("native_acceptance") == asdict(pending.acceptance):
            native_supplement.finish_reservation(digest, cache=cache, now=instant,
                owner=prepared_delivery._owner()[0], confirmed=status == "confirmed")
            storage.clear_pending(cache)
            return cache / editions.READY_FILE, digest
    _guard(cache, date.fromisoformat(pending.publication_date), instant)
    _, packet = read_packet(storage.read_reference(pending.packet))
    effective = _eligible(packet, config, cache, instant)
    supplement = storage.projected_supplement(projected)
    if supplement is not None:
        native_supplement.verify_available(supplement, day=date.fromisoformat(pending.publication_date),
            now=instant, owner_sha256=pending.owner_sha256)
    current = _projection(
        pending,
        storage.read_reference(pending.response),
        storage.read_reference(pending.review),
        effective,
        supplement,
    )
    if current != projected or not _main_count(projected):
        raise ValueError("Accepted native composition/config changed or contains no main cards.")
    if pending.acceptance is None:
        pending = replace(pending, acceptance=storage.put_bytes(acceptance_raw, cache, "final-acceptance"))
        storage.save_pending(cache, pending)
    shown = [ArticleSummary(**item["card"]) for item in projected["publication"]["articles"]]
    presentation = projected["publication"]["presentation_metadata"]
    synthesis = SynthesisPlacement(**presentation["synthesis"]) if presentation.get("synthesis") else None
    if config.obsidian.enabled and pending.archive is None:
        from digest.delivery.markdown import write_digest

        if pending.archive_evidence is None:
            evidence = encode(
                {
                    "schema": "native-archive-evidence/v1",
                    "references": [asdict(item) for item in storage.composition_references(pending, projected)],
                }
            )
            pending = replace(pending, archive_evidence=storage.put_bytes(evidence, cache, "archive-evidence"))
            storage.save_pending(cache, pending)
        assert pending.archive_evidence is not None
        link = Path(os.path.relpath(pending.archive_evidence.path, config.obsidian.output_dir)).as_posix()
        canonical = projected["publication"]["canonical_metadata"]
        russian = canonical["language"] == "ru"
        label = "Материалы внешней редакторской проверки" if russian else "Native editorial evidence"
        scope_notice = (
            "Счётчики относятся к группам категорий и материалам исходного пакета, "
            "а не к опубликованным карточкам или охвату лент."
            if russian
            else (
                "Counts describe planned packet category groups and articles, "
                "not published cards or live feed coverage."
            )
        )
        archive = write_digest(
            f"[{label}]({link})\n\n{scope_notice}",
            config,
            top_articles=shown,
            date=datetime.fromisoformat(projected["publication"]["window_start"]),
            sources_count=canonical["source_count"],
            articles_count=canonical["article_count"],
            synthesis=synthesis,
            closing_start=presentation.get("closing_start"),
            supplement=(SupplementPlacement(supplement.fragment.fragment_id, supplement.fragment.text,
                presentation["supplement"]["before_article"]) if supplement is not None else None),
        )
        if archive is None:
            raise ValueError("Native archive failed; accepted handoff retained, no ready created.")
        pending = replace(pending, archive=storage.reference_file(archive))
        storage.save_pending(cache, pending)
    assert pending.acceptance is not None and pending.projection is not None
    path, digest = prepared_delivery.prepare_edition(
        shown,
        config,
        cache_dir=cache,
        now=instant,
        publication_date=date.fromisoformat(pending.publication_date),
        canonical_metadata=projected["publication"]["canonical_metadata"],
        presentation_metadata=projected["publication"]["presentation_metadata"],
        checkpoint_refs={item.path: item.sha256 for item in storage.composition_references(pending, projected)},
        producing_engine=_engine_provenance(),
        native_binding=(asdict(pending.projection), asdict(pending.acceptance)),
        synthesis=synthesis,
        supplement=supplement,
    )
    storage.clear_pending(cache)
    return path, digest


def _main_count(projected: dict[str, Any]) -> int:
    canonical = projected["publication"]["canonical_metadata"]
    return int(canonical.get("main_count", len(projected["publication"]["articles"])))


def _release_reason(pending: storage.PendingNative, instant: datetime) -> str:
    if date.fromisoformat(pending.publication_date) < instant.date():
        return "expired"
    if pending.projection is not None:
        projected = storage.decode(storage.read_reference(pending.projection))
        if not _main_count(projected):
            return "no_retained_main"
    return ""


def release_pending(
    config: Config,
    *,
    expected_packet_sha256: str,
    cache: Path = Path(".cache"),
    now: datetime | None = None,
) -> tuple[Path, str]:
    """Explicit no-generation recovery; retain evidence before releasing the active pointer."""
    from digest._serialization import restore_dataclass

    instant = prepared_delivery._instant(now)
    owner = _identity(config, instant.date(), instant)
    validate_checkpoint_reference("packet.json", expected_packet_sha256)
    _guard(cache, instant.date(), instant)
    pending = storage.load_pending(cache)
    path = cache / "native" / f"{expected_packet_sha256}.release.json"
    if path.exists():
        raw = storage.read_input(path)
        record = storage.decode(raw)
        retained: storage.PendingNative = restore_dataclass(record["pending"], storage.PendingNative)
        if (
            set(record) != {"schema", "pending", "reason", "released_at"}
            or record["schema"] != 1
            or record["reason"] not in {"expired", "no_retained_main"}
            or retained.packet.sha256 != expected_packet_sha256
            or retained.owner_sha256 != owner
            or pending is not None
            and pending != retained
        ):
            raise ValueError("Native release record does not match the exact handoff.")
        released_at = parse_instant(record["released_at"])
        storage.validate_pending(retained)
        if released_at > instant or _release_reason(retained, released_at) != record["reason"]:
            raise ValueError("Native release reason/time lacks exact retained evidence.")
        if storage.reference_file(path).sha256 != editions.content_sha256(raw):
            raise ValueError("Native release record changed during recovery.")
        if pending is not None:
            storage.clear_pending(cache)
        return path, editions.content_sha256(raw)
    if pending is None or pending.packet.sha256 != expected_packet_sha256 or pending.owner_sha256 != owner:
        raise ValueError("Native release requires the exact existing packet and owner.")
    reason = _release_reason(pending, instant)
    if not reason:
        raise ValueError("Current native work remains resumable; release is only for expired/no-main handoffs.")
    record = {"schema": 1, "pending": asdict(pending), "reason": reason, "released_at": instant.isoformat()}
    digest = editions.write_record(path, record)
    if storage.decode(storage.read_input(path)) != record:
        raise ValueError("Native release readback mismatch; handoff retained.")
    storage.clear_pending(cache)
    return path, digest


async def native_phase(args: Any) -> int:
    from digest.config import load_config

    config = load_config(args.config)
    if args.native_phase == "export":
        pending = await export_sources(config, day=date.fromisoformat(args.edition_date) if args.edition_date else None)
        _outputs(
            native_status="exported" if pending else "no_candidates",
            native_packet=pending.packet.path if pending else "",
            native_packet_sha256=pending.packet.sha256 if pending else "",
        )
    elif args.native_phase == "project":
        pending = project_response(
            config,
            storage.read_input(Path(args.native_response)),
            storage.read_input(Path(args.native_review)),
            expected_packet_sha256=args.native_packet_sha,
        )
        assert pending.projection is not None
        projected = json.loads(storage.read_reference(pending.projection))
        _outputs(
            native_status="projected" if _main_count(projected) else "no_ready",
            native_projection=pending.projection.path,
            native_projection_sha256=pending.projection.sha256,
        )
    elif args.native_phase == "import":
        path, digest = import_accepted(
            config,
            storage.read_input(Path(args.native_acceptance)),
            expected_projection_sha256=args.native_projection_sha,
        )
        _, _, status = prepared_delivery.inspect_edition()
        _outputs(edition_status=status, ready_path=str(path), ready_sha256=digest)
    else:
        path, digest = release_pending(config, expected_packet_sha256=args.native_packet_sha)
        _outputs(native_status="released", native_release=str(path), native_release_sha256=digest)
    return 0
