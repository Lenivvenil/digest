"""Canonical RSS analysis shared by prepared and legacy applications."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from digest.config import Config
    from digest.radar.collector import Article
    from digest.radar.summarizer import ArticleSummary, CategorySummary
    from digest.review import BlindReviewReport

async def analyze_articles(
    articles: dict[str, list[Article]], config: Config,
) -> tuple[list[CategorySummary], str | None, list[ArticleSummary], BlindReviewReport | None]:
    """Prioritize blind selection before optional category prose consumes quota."""
    from digest.radar import pick_top_articles, summarize_all

    if getattr(getattr(config, "review", None), "enabled", False):
        from digest.review import primary_cards, run_blind_review, run_primary_review

        report = await (run_primary_review(articles, config) if config.review.review_led_only
                        else run_blind_review(articles, config))
        cards = primary_cards(
            report, articles, config.radar.language, max_cards=config.review.max_selections,
            include_attribution=getattr(config.telegram, "delivery_mode", "cards") != "compact",
        )
        if config.review.review_led_only:
            logging.getLogger(__name__).info(
                "Review-led only: skipping legacy category summaries, trends and counter-signal analysis."
            )
            return [], None, cards, report
        summaries, trends = await summarize_all(articles, config)
        return summaries, trends, cards, report
    summaries, trends = await summarize_all(articles, config)
    cards = await pick_top_articles(articles, config, max_articles=7) if summaries else []
    return summaries, trends, cards, None
