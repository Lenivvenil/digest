"""Compatible candidate exports; production consumers use the domain/application owners."""

from __future__ import annotations

from pathlib import Path

from digest.adapters.storage.candidate_progress import CANDIDATE_FILE as CANDIDATE_FILE
from digest.adapters.storage.candidate_progress import MAX_BYTES as MAX_BYTES
from digest.adapters.storage.candidate_progress import archive_candidate_accounting as archive_candidate_accounting
from digest.adapters.storage.candidate_progress import candidate_accounting_sources as candidate_accounting_sources
from digest.adapters.storage.candidate_progress import load_candidate_progress as load_candidate_progress
from digest.adapters.storage.candidate_progress import progress_size as progress_size
from digest.application.candidate_lifecycle import checkpoint_candidates as checkpoint_candidates
from digest.application.candidate_lifecycle import ensure_report_accounting as ensure_report_accounting
from digest.application.candidate_lifecycle import index_candidate as index_candidate
from digest.application.candidate_lifecycle import persist_candidates as persist_candidates
from digest.application.candidate_review import RESPONSE_STORAGE_RESERVE as RESPONSE_STORAGE_RESERVE
from digest.application.candidate_review import begin_packet as begin_packet
from digest.application.candidate_review import mark_prepared as mark_prepared
from digest.application.candidate_review import merge_candidates as merge_candidates
from digest.application.candidate_review import plan_packet as plan_packet
from digest.application.candidate_review import reconcile_packet as reconcile_packet
from digest.application.review_request import build_evidence_bundle as build_evidence_bundle
from digest.application.review_request import build_review_messages as build_review_messages
from digest.config import Config as Config
from digest.domain.catalog.articles import Article as Article
from digest.domain.catalog.articles import article_hash as article_hash
from digest.domain.editorial.candidate_policy import _digest as _digest
from digest.domain.editorial.candidate_policy import candidate_accounting as candidate_accounting
from digest.domain.editorial.candidate_policy import packet_articles as packet_articles
from digest.domain.editorial.candidate_policy import pending_completed_report as pending_completed_report
from digest.domain.editorial.candidates import Candidate as Candidate
from digest.domain.editorial.candidates import CandidateArticle as CandidateArticle
from digest.domain.editorial.candidates import CandidatePacket as CandidatePacket
from digest.domain.editorial.candidates import CandidateProgress as CandidateProgress
from digest.domain.editorial.candidates import CandidateStatus as CandidateStatus
from digest.domain.editorial.dispositions import CandidateDispositionCapture as CandidateDispositionCapture
from digest.domain.editorial.dispositions import validate_disposition_attempt as validate_disposition_attempt
from digest.domain.editorial.reviews import BlindReviewReport as BlindReviewReport
from digest.domain.editorial.reviews import delivery_review as delivery_review
from digest.domain.editorial.reviews import review_prompt_hash as review_prompt_hash
from digest.domain.editorial.reviews import validate_request_evidence_bundle as validate_request_evidence_bundle
from digest.domain.editorial.reviews import validated_cached_selections as validated_cached_selections
from digest.filters import is_blocked as is_blocked
from digest.radar.collector import CollectionInventory as CollectionInventory


def save_candidate_progress(
    progress: CandidateProgress, cache_dir: str | Path = ".cache", *, retire: bool = True,
    skipped_empty_reports: set[str] | None = None,
) -> Path:
    """Legacy API: retain its retirement semantics while new callers name the effect."""
    if retire:
        return checkpoint_candidates(progress, cache_dir, skipped_empty_reports=skipped_empty_reports)
    return persist_candidates(progress, cache_dir)
