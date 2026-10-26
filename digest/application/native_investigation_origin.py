"""Read-only adapter from a confirmed native archive to delivered investigation.

No provider review is synthesized. Source excerpts remain RSS evidence and all
publication prose comes from the accepted canonical/presentation projection.
"""

from __future__ import annotations

import os
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from digest._serialization import restore_dataclass
from digest._util import utc_instant
from digest.adapters.storage import edition as editions
from digest.adapters.storage import native as native_storage
from digest.adapters.storage.issue_paths import safe_issue_path
from digest.domain.catalog.articles import article_hash
from digest.domain.catalog.occurrences import SourceOccurrence, occurrence_sha256
from digest.domain.delivery.edition import NativeEdition, NativeSupplementEdition
from digest.domain.editorial.native import accepted_cards, read_packet
from digest.domain.editorial.native_optional import closing_card, optional_verdict
from digest.domain.editorial.reviews import EvidenceBundle
from digest.domain.editorial.summaries import ArticleSummary
from digest.domain.investigation.delivered import (
    DeliveredCard,
    NativeDeliveredInvestigationInput,
    NativeEvidenceReference,
    NativeInvestigationReferences,
    validate_delivered_input,
)


def _reference(value: Any, edition: NativeEdition) -> native_storage.Reference:
    reference: native_storage.Reference = restore_dataclass(value, native_storage.Reference)
    if edition.checkpoint_refs.get(reference.path) != reference.sha256:
        raise ValueError("Native investigation reference is not in the delivered edition.")
    native_storage.read_reference(reference)
    return reference


def _native_cards(
    edition: NativeEdition, projected: dict[str, Any], packet_raw: bytes, response_raw: bytes, review_raw: bytes,
) -> tuple[tuple[DeliveredCard, ...], EvidenceBundle]:
    envelope, packet = read_packet(packet_raw)
    canonical = edition.canonical_metadata
    day = datetime.fromisoformat(edition.window_start).date().isoformat()
    if (envelope["owner_sha256"] != edition.owner_sha256 or envelope["publication_date"] != day
            or envelope["language"] != canonical["language"]):
        raise ValueError("Native investigation packet owner, day or language changed.")
    accepted, response, review = accepted_cards(packet_raw, response_raw, review_raw, packet, envelope["language"])
    main_count = canonical.get("main_count", len(accepted))
    if type(main_count) is not int or main_count != len(accepted) or not accepted:
        raise ValueError("Native investigation main-card accounting changed.")
    originals = {article_hash(item.title, item.link): item for item in packet.articles}
    if "native_optional" in canonical:
        closing = canonical["native_optional"]["closing"]
        if closing["status"] == "included":
            value = response.get("closing")
            if (optional_verdict("closing", value, review) != "accepted"
                    or closing["value"] != value or not envelope["optional_policy"]["closing_enabled"]):
                raise ValueError("Native investigation closing lacks its original acceptance.")
            card = closing_card(value, packet,
                {item["evidence_id"] for item in response["items"] if item["status"] == "card"},
                set(envelope["optional_policy"]["closing_ids"]))
            if closing["card"] != asdict(card):
                raise ValueError("Native investigation closing differs from its original source.")
            accepted.append(card)
    canonical_cards = [restore_dataclass(value, ArticleSummary) for value in canonical["cards"]]
    presented = [restore_dataclass(value, ArticleSummary) for value in edition.presentation_metadata["cards"]]
    if (canonical_cards != accepted or presented != [article.card for article in edition.articles]
            or len(accepted) != len(edition.articles)):
        raise ValueError("Native delivered card body or order differs from its accepted source review.")
    cards = tuple(
        DeliveredCard(article.full_hash, card, article.card,
            SourceOccurrence(**asdict(originals[article.full_hash])),
            occurrence_sha256(originals[article.full_hash]), tuple(article.covering_chunks))
        for card, article in zip(accepted, edition.articles, strict=True)
    )
    return cards, packet.evidence


def freeze_native_delivered_input(
    archive: Path, content: bytes, *, now: datetime | None = None,
) -> tuple[NativeDeliveredInvestigationInput, EvidenceBundle]:
    """Verify native provenance and exact complete/applied dispatch, then reread it.

    The caller must serialize runtime writers. Exact rereads detect intervening
    changes; they are not a filesystem-wide transaction or transport attestation.
    """
    archive = safe_issue_path(archive)
    relative = archive.relative_to(Path.cwd().resolve()).as_posix()
    if archive.suffix != ".md" or native_storage.read_input(archive) != content:
        raise ValueError("Native investigation requires the unchanged delivered Markdown archive.")
    instant = utc_instant(now or datetime.now(UTC))
    cache = Path(".cache")
    owner = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    edition, ready_sha = editions.load_edition(cache / editions.READY_FILE, owner, instant)
    if not isinstance(edition, NativeEdition) or edition.schema not in (4, 5):
        raise ValueError("Native investigation requires a native schema-4/5 origin.")
    publication_day = datetime.fromisoformat(edition.window_start).date()
    if not publication_day <= instant.date() < publication_day + timedelta(days=4):
        raise ValueError("Native investigation origin is future or expired.")
    checkpoint_sha = editions.content_sha256(content)
    if edition.checkpoint_refs.get(relative) != checkpoint_sha:
        raise ValueError("Native Markdown archive is not bound to the delivered edition.")
    _, claim_sha = editions.load_claim(cache / editions.CLAIM_FILE, ready_sha, edition.owner_sha256)
    receipts_path = cache / editions.RECEIPTS_FILE
    receipts_sha = editions.content_sha256(safe_issue_path(receipts_path).read_bytes())
    receipts = editions.load_receipts(cache, ready_sha, claim_sha, len(edition.payloads),
                                     edition.owner_sha256, expected=receipts_sha)
    if (receipts is None or receipts.state != "confirmed" or not receipts.applied
            or receipts.attempted != len(edition.payloads)
            or any(type(item.chunk) is not int for item in receipts.confirmed)
            or {item.chunk for item in receipts.confirmed} != set(range(len(edition.payloads)))):
        raise ValueError("Native investigation requires fully confirmed and applied origin receipts.")
    if isinstance(edition, NativeSupplementEdition):
        from digest.application.supplement import verify_consumed_fragment

        verify_consumed_fragment(edition, ready_sha, receipts)
    editions.verify_checkpoints(edition.checkpoint_refs, verify_bytes=True)
    projection = _reference(edition.native_projection, edition)
    acceptance = _reference(edition.native_acceptance, edition)
    projected = native_storage.decode(native_storage.read_reference(projection))
    native_storage.validate_acceptance(native_storage.read_reference(acceptance), projection, projected)
    if projected.get("archive_enabled") is not True:
        raise ValueError("Native investigation requires an archive-enabled accepted projection.")
    packet, response, review = (
        _reference(projected["references"][key], edition) for key in ("packet", "response", "review")
    )
    expected_refs = [asdict(item) for item in (packet, response, review, projection, acceptance)]
    if isinstance(edition, NativeSupplementEdition):
        expected_refs.extend(asdict(item) for item in native_storage.supplement_references(projected))
    archive_refs = [native_storage.Reference(path, digest) for path, digest in edition.checkpoint_refs.items()
                    if path.endswith(".archive-evidence.json")]
    if len(archive_refs) != 1:
        raise ValueError("Native investigation requires one exact archive-evidence record.")
    archive_ref = archive_refs[0]
    if native_storage.decode(native_storage.read_reference(archive_ref)) != {
        "schema": "native-archive-evidence/v1", "references": expected_refs,
    }:
        raise ValueError("Native archive evidence differs from the delivered provenance.")
    cards, bundle = _native_cards(edition, projected, native_storage.read_reference(packet),
        native_storage.read_reference(response), native_storage.read_reference(review))
    confirmed = {item.chunk for item in receipts.confirmed}
    if any(not set(card.covering_chunks) <= confirmed for card in cards):
        raise ValueError("Native delivered card lacks full confirmed coverage.")
    refs = NativeInvestigationReferences("native-investigation-references/v1", *(
        NativeEvidenceReference(item.path, item.sha256)
        for item in (packet, response, review, projection, acceptance, archive_ref)
    ))
    origin = NativeDeliveredInvestigationInput(edition.edition_id, ready_sha, claim_sha, receipts_sha,
        edition.owner_sha256, publication_day.isoformat(), edition.canonical_sha256,
        edition.presentation_sha256, checkpoint_sha, bundle.bundle_id, cards, refs)
    validate_delivered_input(origin, bundle)
    # Re-read every anchor after provenance construction and before the caller can
    # reserve optional work. No primary state or archive is mutated here.
    editions.load_edition(cache / editions.READY_FILE, owner, instant, ready_sha)
    editions.load_claim(cache / editions.CLAIM_FILE, ready_sha, edition.owner_sha256, claim_sha)
    editions.load_receipts(cache, ready_sha, claim_sha, len(edition.payloads),
                           edition.owner_sha256, expected=receipts_sha)
    editions.verify_checkpoints(edition.checkpoint_refs, verify_bytes=True)
    if isinstance(edition, NativeSupplementEdition):
        verify_consumed_fragment(edition, ready_sha, receipts)
    if native_storage.read_input(archive) != content:
        raise ValueError("Native archive changed while freezing the investigation.")
    return origin, bundle
