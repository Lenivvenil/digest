"""Freeze confirmed publication and original occurrence evidence before optional analysis."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, fields
from datetime import UTC, datetime
from pathlib import Path

from digest._serialization import canonical_json_bytes, restore_dataclass
from digest.adapters.storage import edition as storage
from digest.adapters.storage.candidate_objects import read_packet, read_report_record
from digest.domain.catalog.articles import article_hash
from digest.domain.catalog.occurrences import SourceOccurrence, occurrence_sha256
from digest.domain.editorial.reviews import BlindReviewReport, EvidenceBundle
from digest.domain.editorial.summaries import ArticleSummary
from digest.domain.investigation.delivered import DeliveredCard, DeliveredInvestigationInput, validate_delivered_input


def freeze_delivered_input(checkpoint: Path, content: bytes, bundle: EvidenceBundle) -> DeliveredInvestigationInput:
    """Require exact complete/applied origin receipts; never infer delivery from selection."""
    cache = Path(".cache")
    owner = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    edition, ready_sha = storage.load_edition(cache / storage.READY_FILE, owner, datetime.now(UTC))
    _, claim_sha = storage.load_claim(cache / storage.CLAIM_FILE, ready_sha, edition.owner_sha256)
    receipts_path = cache / storage.RECEIPTS_FILE
    receipts_sha = hashlib.sha256(receipts_path.read_bytes()).hexdigest()
    receipts = storage.load_receipts(
        cache, ready_sha, claim_sha, len(edition.payloads), edition.owner_sha256, expected=receipts_sha
    )
    if receipts is None or receipts.state != "confirmed" or not receipts.applied:
        raise ValueError("Delivered investigation requires a fully confirmed and applied origin edition.")
    relative = checkpoint.resolve().relative_to(Path.cwd().resolve()).as_posix()
    checkpoint_sha = hashlib.sha256(content).hexdigest()
    if edition.checkpoint_refs.get(relative) != checkpoint_sha:
        raise ValueError("Investigation checkpoint is not bound to the confirmed origin edition.")
    storage.verify_checkpoints(edition.checkpoint_refs, verify_bytes=True)
    raw = json.loads(content)
    report = restore_dataclass({field.name: raw[field.name] for field in fields(BlindReviewReport)}, BlindReviewReport)
    report_sha = hashlib.sha256(canonical_json_bytes(asdict(report))).hexdigest()
    packet = read_packet(report_sha, cache)
    if packet.report != report or packet.evidence != bundle:
        raise ValueError("Delivered investigation differs from the retained review/source packet.")
    accounting = relative.removesuffix(".review.json") + ".md.candidates.json"
    if accounting not in edition.checkpoint_refs or json.loads(Path(accounting).read_text()) != read_report_record(
        report_sha, cache
    ):
        raise ValueError("Origin edition lacks its exact archived source packet.")
    canonical = [restore_dataclass(item, ArticleSummary) for item in edition.canonical_metadata["cards"]]
    presentation = [restore_dataclass(item, ArticleSummary) for item in edition.presentation_metadata["cards"]]
    closing = edition.presentation_metadata.get("closing")
    if isinstance(closing, dict) and closing.get("card") is not None:
        canonical.append(restore_dataclass(edition.canonical_metadata["closing"]["card"], ArticleSummary))
    if presentation != [article.card for article in edition.articles] or len(canonical) != len(presentation):
        raise ValueError("Delivered investigation card order differs from the frozen publication.")
    occurrences = {article_hash(item.title, item.link): item for item in packet.articles}
    if any(
        f".cache/candidate_sources/{occurrence_sha256(item)}.json" not in edition.checkpoint_refs
        for item in packet.articles
    ):
        raise ValueError("Origin edition lacks its exact source occurrence references.")
    cards = tuple(
        DeliveredCard(
            article.full_hash,
            original,
            article.card,
            SourceOccurrence(**asdict(occurrences[article.full_hash])),
            occurrence_sha256(occurrences[article.full_hash]),
            tuple(article.covering_chunks),
        )
        for original, article in zip(canonical, edition.articles, strict=True)
    )
    origin = DeliveredInvestigationInput(
        edition.edition_id,
        ready_sha,
        claim_sha,
        receipts_sha,
        edition.owner_sha256,
        datetime.fromisoformat(edition.window_start).date().isoformat(),
        edition.canonical_sha256,
        edition.presentation_sha256,
        checkpoint_sha,
        report_sha,
        bundle.bundle_id,
        cards,
    )
    validate_delivered_input(origin, bundle)
    # Re-read exact dispatch identities after all provenance reads.
    storage.load_edition(cache / storage.READY_FILE, owner, datetime.now(UTC), ready_sha)
    storage.load_claim(cache / storage.CLAIM_FILE, ready_sha, edition.owner_sha256, claim_sha)
    if hashlib.sha256(receipts_path.read_bytes()).hexdigest() != origin.receipts_sha256:
        raise ValueError("Origin receipts changed while freezing the investigation.")
    return origin
