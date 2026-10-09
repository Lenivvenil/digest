"""Deterministic, bounded RSS evidence construction without model execution."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from itertools import zip_longest
from urllib.parse import urlparse

from digest._sanitize import sanitize_article
from digest.domain.catalog.articles import Article, article_hash
from digest.domain.editorial.reviews import MAX_EVIDENCE_JSON_CHARS as MAX_EVIDENCE_JSON_CHARS
from digest.domain.editorial.reviews import SCHEMA_VERSION, EvidenceBundle, EvidenceItem


def ordered_unique_articles(articles_by_category: dict[str, list[Article]]) -> dict[str, Article]:
    groups = [
        sorted(articles_by_category[k], key=lambda a: (a.link, a.title, a.source, a.description))
        for k in sorted(articles_by_category)
    ]
    unique: dict[str, Article] = {}
    for row in zip_longest(*groups):
        for article in row:
            if article is not None:
                unique.setdefault(article_hash(article.title, article.link), article)
    return unique


def prepare_evidence_item(article: Article, *, max_excerpt_chars: int) -> tuple[EvidenceItem, int] | None:
    """Canonicalize one occurrence and measure its existing per-item JSON budget."""
    parsed_url = urlparse(article.link)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        return None
    title, excerpt, source = sanitize_article(article.title, article.description, article.source)
    excerpt = excerpt[:max_excerpt_chars]
    item = EvidenceItem(
        article_hash(article.title, article.link),
        title,
        article.link,
        source,
        article.category[:200],
        article.pub_date.isoformat() if article.pub_date else None,
        excerpt,
        excerpt != article.description,
    )
    return item, len(json.dumps(asdict(item), ensure_ascii=False))


def evidence_bundle_from_items(items: tuple[EvidenceItem, ...], *, omitted_articles: int) -> EvidenceBundle:
    """Freeze already admitted items with the canonical evidence payload encoding."""
    payload = {
        "schema_version": SCHEMA_VERSION,
        "evidence_kind": "sanitized_rss_excerpt",
        "omitted_articles": omitted_articles,
        "items": [asdict(item) for item in items],
    }
    bundle_id = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return EvidenceBundle(SCHEMA_VERSION, bundle_id, "sanitized_rss_excerpt", omitted_articles, items)


def build_evidence_bundle(
    articles_by_category: dict[str, list[Article]],
    *,
    max_evidence_articles: int,
    max_excerpt_chars: int,
) -> EvidenceBundle:
    """Trim once with deterministic round-robin category coverage for every slot."""
    unique = ordered_unique_articles(articles_by_category)
    items: list[EvidenceItem] = []
    evidence_chars = 0
    for article in unique.values():
        if len(items) >= max_evidence_articles:
            break
        prepared = prepare_evidence_item(article, max_excerpt_chars=max_excerpt_chars)
        if prepared is None:
            continue
        item, size = prepared
        if evidence_chars + size > MAX_EVIDENCE_JSON_CHARS:
            continue
        items.append(item)
        evidence_chars += size
    return evidence_bundle_from_items(tuple(items), omitted_articles=len(unique) - len(items))
