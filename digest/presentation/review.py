"""Pure RSS review attribution, publication cards and Markdown rendering."""

from __future__ import annotations

from digest.domain.catalog.articles import Article
from digest.domain.editorial.attempts import ResolvedReview
from digest.domain.editorial.evidence import ordered_unique_articles
from digest.domain.editorial.reviews import BlindReviewReport
from digest.domain.editorial.summaries import ArticleSummary


def primary_notice(result: ResolvedReview, language: str) -> str:
    """Deterministic attribution, usable once for an entire compact issue."""
    primary = result.chosen.review
    label = "Мнение модели" if language == "ru" else "Model view"
    label += f" ({primary.provider}/{primary.model})"
    if result.report.status != "complete":
        label += "; независимое сравнение не завершено" if language == "ru" else "; independent comparison incomplete"
    return label


def primary_cards(
    result: ResolvedReview, articles_by_category: dict[str, list[Article]], language: str,
    *, include_attribution: bool = True, max_cards: int | None = None, exclude_ids: frozenset[str] = frozenset(),
) -> list[ArticleSummary]:
    """Apply publication capacity in review order without trimming the saved review."""
    if max_cards is not None and (type(max_cards) is not int or max_cards < 1):
        raise ValueError("Publication card limit must be a positive integer.")
    originals = ordered_unique_articles(articles_by_category)
    if result.outcome != "selected":
        return []
    label = primary_notice(result, language)
    cards = []
    selections = [selection for selection in result.selections if selection.evidence_id not in exclude_ids]
    for selection in selections[:max_cards]:
        article = originals[selection.evidence_id]
        summary = f"{label}: {selection.reason}" if include_attribution else selection.reason
        cards.append(ArticleSummary(article.title, article.link, article.source, article.category, summary))
    return cards


def render_review(report: BlindReviewReport) -> str:
    evidence = {item.evidence_id: item for item in report.evidence.items}
    lines = ["\n\n## Independent Blind Review", f"Status: {report.status}",
             "Same RSS excerpts; model opinions, not verified full-article conclusions.",
             f"Evidence: {report.evidence.bundle_id}; omitted articles: {report.evidence.omitted_articles}"]
    if report.selection_overlap is not None:
        lines.append(f"Selection overlap: {report.selection_overlap:.0%} (not factual agreement)")
    lines.append(f"Third-model decision: {report.third_model_reason}")
    for review in report.reviews:
        lines.append(f"\n### {review.slot}: {review.provider}/{review.model} — {review.status}")
        lines.append(f"Resolved model: {review.resolved_model or 'not reported by provider'}")
        provenance = "reused checkpoint" if review.reused_from_checkpoint else "new attempt"
        if review.error == "pending_independent_review" and review.attempted_at is None:
            provenance = "pending independent review (not attempted)"
        lines.append(f"Provenance: {provenance}; attempted: {review.attempted_at or 'not recorded'}; "
                     f"generated: {review.generated_at or 'not recorded'}")
        for selection in review.selections:
            item = evidence[selection.evidence_id]
            lines.append(f"- [{item.title}]({item.url}): {selection.reason} (confidence: {selection.confidence})")
            lines.append(f"  Evidence excerpt: {selection.quote}")
            if selection.typography_normalized:
                lines.append("  Quote provenance: hyphen/space typography aligned to exact supplied source text.")
        lines.extend(f"- Limitation: {limitation}" for limitation in review.limitations)
        for rejected in review.rejected_items:
            lines.append(f"- Rejected selection {rejected.index}: {rejected.reason}")
        if review.error:
            label = "Partial review validation" if review.status == "partial" else "Review unavailable"
            lines.append(f"- {label}: {review.error}")
    return "\n".join(lines)
