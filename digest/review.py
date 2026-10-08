"""Compatibility exports for RSS evidence, review execution and presentation.

Review slots receive identical RSS excerpts, never another model's opinions.
This measures selection overlap, not factual consensus or full-article accuracy.
"""

from digest._sanitize import sanitize_article as sanitize_article
from digest.adapters.models.execution import ModelExecution as ModelExecution
from digest.adapters.models.review import groq_review_response_format
from digest.application.review import _review_slot as _review_slot
from digest.application.review import _review_usage as _review_usage
from digest.application.review import run_blind_review as run_blind_review
from digest.application.review import run_evidence_review as run_evidence_review
from digest.application.review import run_primary_review as run_primary_review
from digest.application.review_request import _configured_category_interests as _configured_category_interests
from digest.application.review_request import build_evidence_bundle as build_evidence_bundle
from digest.application.review_request import build_review_messages as build_review_messages
from digest.application.review_request import eligible_ids as eligible_ids
from digest.closing import ClosingCapture as ClosingCapture
from digest.closing import capture_closing as capture_closing
from digest.config import ClosingConfig as ClosingConfig
from digest.config import Config as Config
from digest.config import ProviderConfig as ProviderConfig
from digest.config import ReviewConfig as ReviewConfig
from digest.config import ReviewModelConfig as ReviewModelConfig
from digest.domain.catalog.articles import Article as Article
from digest.domain.catalog.articles import article_hash as article_hash
from digest.domain.catalog.sources import SourceConfig as SourceConfig
from digest.domain.editorial.dispositions import CandidateDispositionCapture as CandidateDispositionCapture
from digest.domain.editorial.dispositions import capture_review_dispositions as capture_review_dispositions
from digest.domain.editorial.evidence import ordered_unique_articles
from digest.domain.editorial.reviews import MAX_EVIDENCE_JSON_CHARS as MAX_EVIDENCE_JSON_CHARS
from digest.domain.editorial.reviews import SCHEMA_VERSION as SCHEMA_VERSION
from digest.domain.editorial.reviews import BlindReviewReport as BlindReviewReport
from digest.domain.editorial.reviews import EvidenceBundle as EvidenceBundle
from digest.domain.editorial.reviews import EvidenceItem as EvidenceItem
from digest.domain.editorial.reviews import EvidenceSelection as EvidenceSelection
from digest.domain.editorial.reviews import ModelReview as ModelReview
from digest.domain.editorial.reviews import RejectedSelection as RejectedSelection
from digest.domain.editorial.reviews import ReviewReuseIdentity as ReviewReuseIdentity
from digest.domain.editorial.reviews import _parse_live_review as _parse_live_review
from digest.domain.editorial.reviews import _parse_live_selection as _parse_live_selection
from digest.domain.editorial.reviews import _parse_review as _parse_review
from digest.domain.editorial.reviews import _parse_review_envelope as _parse_review_envelope
from digest.domain.editorial.reviews import _rejected_output_diagnostics as _rejected_output_diagnostics
from digest.domain.editorial.reviews import canonical_evidence_quote as canonical_evidence_quote
from digest.domain.editorial.reviews import delivery_review as delivery_review
from digest.domain.editorial.reviews import reusable_model_review as reusable_model_review
from digest.domain.editorial.reviews import review_prompt_hash as review_prompt_hash
from digest.domain.editorial.reviews import validate_request_evidence_bundle as validate_request_evidence_bundle
from digest.domain.editorial.reviews import validated_cached_selections as validated_cached_selections
from digest.domain.editorial.summaries import ArticleSummary as ArticleSummary
from digest.llm import LLMRole as LLMRole
from digest.llm import complete as complete
from digest.presentation.review import primary_cards as primary_cards
from digest.presentation.review import primary_notice as primary_notice
from digest.presentation.review import render_review as render_review

_ordered_unique_articles = ordered_unique_articles
_groq_review_format = groq_review_response_format
_delivery_review = delivery_review
_validated_cached_selections = validated_cached_selections
