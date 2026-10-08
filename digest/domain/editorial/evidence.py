"""Deterministic, bounded RSS evidence construction without model execution."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from itertools import zip_longest
from urllib.parse import urlparse

from digest._sanitize import sanitize_article
from digest.domain.catalog.articles import Article, article_hash
from digest.domain.editorial.reviews import MAX_EVIDENCE_JSON_CHARS, SCHEMA_VERSION, EvidenceBundle, EvidenceItem


def ordered_unique_articles(articles_by_category: dict[str, list[Article]]) -> dict[str, Article]:
    groups = [sorted(articles_by_category[k], key=lambda a: (a.link, a.title, a.source, a.description))
              for k in sorted(articles_by_category)]
    unique: dict[str, Article] = {}
    for row in zip_longest(*groups):
        for article in row:
            if article is not None:
                unique.setdefault(article_hash(article.title, article.link), article)
    return unique


def build_evidence_bundle(
    articles_by_category: dict[str, list[Article]], *, max_evidence_articles: int, max_excerpt_chars: int,
) -> EvidenceBundle:
    """Trim once with deterministic round-robin category coverage for every slot."""
    unique = ordered_unique_articles(articles_by_category)
    items: list[EvidenceItem] = []
    evidence_chars = 0
    for identity, article in unique.items():
        if len(items) >= max_evidence_articles:
            break
        parsed_url = urlparse(article.link)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            continue
        title, excerpt, source = sanitize_article(article.title, article.description, article.source)
        excerpt = excerpt[:max_excerpt_chars]
        item = EvidenceItem(
            identity, title, article.link, source, article.category[:200],
            article.pub_date.isoformat() if article.pub_date else None,
            excerpt, excerpt != article.description,
        )
        size = len(json.dumps(asdict(item), ensure_ascii=False))
        if evidence_chars + size > MAX_EVIDENCE_JSON_CHARS:
            continue
        items.append(item)
        evidence_chars += size
    payload = {
        "schema_version": SCHEMA_VERSION, "evidence_kind": "sanitized_rss_excerpt",
        "omitted_articles": len(unique) - len(items), "items": [asdict(i) for i in items],
    }
    bundle_id = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return EvidenceBundle(SCHEMA_VERSION, bundle_id, "sanitized_rss_excerpt", len(unique) - len(items), tuple(items))
