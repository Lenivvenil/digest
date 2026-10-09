"""Primary/fallback and independent RSS review execution over one exact request."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from digest.adapters.models.execution import ModelExecution
from digest.adapters.models.review import groq_review_response_format
from digest.application.review_request import build_evidence_bundle, build_review_messages, eligible_ids
from digest.config import Config, ProviderConfig, ReviewModelConfig
from digest.domain.catalog.articles import Article
from digest.domain.editorial.attempts import ResolvedReview, ReviewAttempt, capture_closing_selection, resolve_review
from digest.domain.editorial.dispositions import capture_review_dispositions
from digest.domain.editorial.reviews import (
    SCHEMA_VERSION,
    BlindReviewReport,
    EvidenceBundle,
    ModelReview,
    ReviewReuseIdentity,
    _parse_live_review,
    _rejected_output_diagnostics,
    reusable_model_review,
    review_prompt_hash,
    validate_request_evidence_bundle,
)
from digest.llm import LLMRole, complete


def _review_usage(usage: dict[str, Any]) -> dict[str, int]:
    """Retain allowlisted numeric diagnostics, never reasoning text or arbitrary headers."""
    keys = {
        "prompt_tokens",
        "completion_tokens",
        "rate_limit_limit_requests",
        "rate_limit_remaining_requests",
        "rate_limit_limit_tokens",
        "rate_limit_remaining_tokens",
    }
    result = {key: value for key, value in usage.items() if key in keys and type(value) is int and value >= 0}
    details = usage.get("completion_tokens_details")
    reasoning = details.get("reasoning_tokens") if isinstance(details, dict) else None
    if type(reasoning) is int and reasoning >= 0:
        result["reasoning_tokens"] = reasoning
    return result


async def _review_slot(
    slot: str,
    model: ReviewModelConfig,
    bundle: EvidenceBundle,
    messages: list[dict[str, str]],
    config: Config,
    *,
    execution: ModelExecution,
) -> ReviewAttempt:
    prompt_hash = review_prompt_hash(messages)
    result = ModelReview(
        slot,
        model.provider,
        model.model,
        bundle.bundle_id,
        prompt_hash,
        "unavailable",
        attempted_at=datetime.now(UTC).isoformat(),
    )
    text: str | None = None
    finish_reason: str | None = None

    closing_enabled = getattr(getattr(config, "closing", None), "enabled", False)

    def attempt() -> ReviewAttempt:
        closing = None
        if closing_enabled:
            closing, selection = capture_closing_selection(
                result,
                text,
                finish_reason,
                bundle,
                eligible=set(eligible_ids(bundle, config.closing, config.sources)),
                max_detailed_selections=config.review.max_detailed_selections,
            )
            if selection is not None:
                result.selections.append(selection)
                if result.status == "abstained":
                    result.status = "ok"
        return ReviewAttempt(
            result,
            capture_review_dispositions(
                bundle,
                result,
                text,
                finish_reason=finish_reason,
                derive_selected=closing_enabled,
                rejected_optional_ids=closing.rejected_evidence_ids if closing is not None else (),
            ),
            closing,
        )

    options: dict[str, Any] = {}
    if (model.provider, model.model) == ("groq", "openai/gpt-oss-120b"):
        options = {
            "reasoning_effort": "low",
            "response_format": groq_review_response_format(allow_closing=closing_enabled),
        }
    try:
        text, usage = await complete(
            LLMRole.REVIEW_EVIDENCE,
            messages,
            config,
            execution=execution,
            temperature=0.2,
            provider_override=ProviderConfig(model.provider, model.model, ["review_evidence"]),
            max_output_tokens=config.review.max_output_tokens,
            **options,
        )
    except Exception as exc:
        result.error = type(exc).__name__  # Never retain response bodies or credentials.
        return attempt()
    reported_finish = usage.get("finish_reason")
    finish_reason = reported_finish if isinstance(reported_finish, str) else None
    result.generated_at = datetime.now(UTC).isoformat()
    result.response_sha256 = hashlib.sha256(text.encode()).hexdigest()
    resolved_model = usage.get("resolved_model")
    result.resolved_model = resolved_model if isinstance(resolved_model, str) else None
    result.usage = _review_usage(usage)
    try:
        if finish_reason is not None and finish_reason not in {"stop", "STOP", "end_turn"}:
            raise ValueError("provider reported unfinished response")
        result.selections, result.limitations, result.rejected_items = _parse_live_review(
            text,
            bundle,
            max_detailed_selections=config.review.max_detailed_selections,
            allow_closing=closing_enabled,
        )
    except (ValueError, TypeError, KeyError) as exc:
        result.status = "invalid"
        result.error, result.rejected_output, result.rejected_output_truncated = _rejected_output_diagnostics(text, exc)
        return attempt()
    if result.rejected_items:
        result.status = "partial" if result.selections else "invalid"
        result.error, result.rejected_output, result.rejected_output_truncated = _rejected_output_diagnostics(
            text,
            ValueError(result.rejected_items[0].reason),
        )
        return attempt()
    result.status = "ok" if result.selections else "abstained"
    return attempt()


async def run_blind_review(
    articles_by_category: dict[str, list[Article]],
    config: Config,
    *,
    execution: ModelExecution,
) -> BlindReviewReport:
    settings = config.review
    bundle = build_evidence_bundle(articles_by_category, settings)
    return await run_evidence_review(bundle, config, execution=execution)


async def run_primary_review(
    articles_by_category: dict[str, list[Article]],
    config: Config,
    *,
    execution: ModelExecution,
) -> ResolvedReview:
    """Select delivery cards with one primary attempt and at most one fallback.

    Independent comparison is deliberately pending, including when both slots
    were attempted for delivery. The checkpoint keeps the identical evidence
    and prompt contract used by the later blind review stage.
    """
    settings = config.review
    bundle = build_evidence_bundle(articles_by_category, settings)
    validate_request_evidence_bundle(
        bundle, max_evidence_articles=settings.max_evidence_articles, max_excerpt_chars=settings.max_excerpt_chars
    )
    messages = build_review_messages(
        bundle, settings, config.radar.language, sources=config.sources, closing=getattr(config, "closing", None)
    )
    prompt_hash = review_prompt_hash(messages)
    # Delivery starts a fresh execution unless reading shares the existing request budget.
    delivery_config = replace(config, llm=replace(config.llm, max_retries=0))
    if getattr(getattr(config, "reading_brief", None), "enabled", False):
        delivery_execution = execution.share_initialized(config.llm)
    else:
        delivery_execution = ModelExecution()
    primary = await _review_slot(
        "primary", settings.primary, bundle, messages, delivery_config, execution=delivery_execution
    )
    secondary = ModelReview(
        "secondary",
        settings.secondary.provider,
        settings.secondary.model,
        bundle.bundle_id,
        prompt_hash,
        "unavailable",
        error="pending_independent_review",
    )
    attempts = [primary]
    if primary.review.status in {"invalid", "unavailable"}:
        fallback = await _review_slot(
            "secondary",
            settings.secondary,
            bundle,
            messages,
            delivery_config,
            execution=delivery_execution,
        )
        attempts.append(fallback)
        secondary = fallback.review
    report = BlindReviewReport(
        SCHEMA_VERSION,
        bundle,
        [primary.review, secondary],
        "incomplete",
        None,
        [],
        "pending_independent_review",
    )
    return resolve_review(report, tuple(attempts))


async def run_evidence_review(
    bundle: EvidenceBundle,
    config: Config,
    cached_reviews: list[ModelReview] | None = None,
    *,
    execution: ModelExecution,
) -> BlindReviewReport:
    """Resume only independently validated successes for the identical evidence and prompt."""
    settings = config.review
    validate_request_evidence_bundle(
        bundle, max_evidence_articles=settings.max_evidence_articles, max_excerpt_chars=settings.max_excerpt_chars
    )
    messages = build_review_messages(
        bundle, settings, config.radar.language, sources=config.sources, closing=getattr(config, "closing", None)
    )
    prompt_hash = review_prompt_hash(messages)
    cached = {review.slot: review for review in cached_reviews or []}
    if len(cached) != len(cached_reviews or []):
        raise ValueError("Checkpoint contains duplicate review slots.")

    reusable: dict[str, ModelReview] = {}
    models = {"primary": settings.primary, "secondary": settings.secondary, "third": settings.tie_breaker}
    for name, previous in cached.items():
        model = models.get(name)
        identity = (
            ReviewReuseIdentity(name, model.provider, model.model, bundle.bundle_id, prompt_hash)
            if model is not None
            else None
        )
        reused = reusable_model_review(previous, bundle, identity)
        if reused is not None:
            reusable[name] = reused

    async def slot(name: str, model: ReviewModelConfig, *, execution: ModelExecution) -> ModelReview:
        if name in reusable:
            return reusable[name]
        return (await _review_slot(name, model, bundle, messages, config, execution=execution)).review

    reviews = list(
        await asyncio.gather(
            slot("primary", settings.primary, execution=execution),
            slot("secondary", settings.secondary, execution=execution),
        )
    )
    valid = all(r.status in {"ok", "abstained"} for r in reviews)
    first, second = ({s.evidence_id for s in r.selections} for r in reviews)
    union = first | second
    overlap = (len(first & second) / len(union) if union else 1.0) if valid else None
    disputed = sorted(first ^ second) if valid else []
    reason = "incomplete_primary_comparison"
    if valid:
        reason = "selection_disagreement_not_escalated" if disputed else "no_selection_disagreement"
    if valid and overlap is not None and overlap < settings.disagreement_threshold:
        reason = "third_model_not_configured"
        if settings.tie_breaker:
            reviews.append(await slot("third", settings.tie_breaker, execution=execution))
            reason = "selection_overlap_below_threshold"
    return BlindReviewReport(
        SCHEMA_VERSION,
        bundle,
        reviews,
        "complete" if all(r.status in {"ok", "abstained"} for r in reviews) else "incomplete",
        overlap,
        disputed,
        reason,
    )
