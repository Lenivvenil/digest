"""Presentation-stage orchestration shared by digest and edition workflows."""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from digest.adapters.models.execution import ModelExecution
    from digest.config import Config
    from digest.irritator.ranker import RankedSignal
    from digest.radar.summarizer import ArticleSummary, CategorySummary
    from digest.review import BlindReviewReport


def clean_summary(text: str) -> str:
    """Remove LLM artifacts: greetings, redundant URLs, separators."""
    text = re.sub(
        r"(?m)^\s*(Link|URL|Source|Read more|Ссылка|Источник|Читать далее)\s*:\s*https?://\S+\s*$",
        "",
        text,
    )
    text = re.sub(
        r"(?m)^(Добрый день|Привет|Здравствуйте|Hello|Hi)!?\s*.*?(дайджест|digest).*$",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"(?m)^(Ежедневный дайджест|Daily digest|Today'?s digest|Вот ваш ежедневный).*$",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"(?m)^\s*---\s*$", "", text)
    text = re.sub(
        r"(?m)^([\U0001f300-\U0001faff\u2600-\u27bf]?\s*)Категория\s*[«\"](.*?)[»\"]\s*(?:содержит.*)?$",
        r"## \1\2",
        text,
    )
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def deferred_review_status(language: str) -> str:
    if language == "ru":
        return "Независимое сравнение и этап контрсигналов отложены до завершения основной доставки."
    return "Independent comparison and counter-signal stage postponed until after primary delivery."


def combined_summary(
    summaries: list[CategorySummary], trends: str | None, review_led_only: bool, language: str,
) -> str:
    if review_led_only:
        return deferred_review_status(language)
    return clean_summary(
        "\n\n".join(summary.summary_text for summary in summaries) + (f"\n\n{trends}" if trends else "")
    )


def publication_intro(combined: str, report: BlindReviewReport | None, config: Config) -> str:
    if getattr(config.telegram, "delivery_mode", "cards") == "compact" and report is not None:
        from digest.review import primary_notice

        return primary_notice(report, config.radar.language) + "\n\n" + combined
    return combined


async def primary_presentation(
    combined: str, cards: list[ArticleSummary], config: Config, cache: Path, dry_run: bool,
    *, execution: ModelExecution,
) -> tuple[str, list[ArticleSummary]]:
    """Optional rendering only; source evidence and supplementary work stay canonical."""
    presented, translated_cards, _ = await publication_presentation(
        combined, cards, [], config, cache, dry_run, execution=execution,
    )
    return presented, translated_cards


async def publication_presentation(
    combined: str, cards: list[ArticleSummary], ranked: list[RankedSignal],
    config: Config, cache: Path, dry_run: bool,
    *, execution: ModelExecution,
) -> tuple[str, list[ArticleSummary], list[RankedSignal]]:
    if not getattr(getattr(config, "translation", None), "enabled", False):
        return combined, cards, ranked
    from digest.translation import translate_publication_presentation

    if dry_run:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory(prefix="digest-translation-preview-") as temporary:
            return await translate_publication_presentation(
                combined, cards, ranked, config, Path(temporary), execution=execution,
            )
    return await translate_publication_presentation(combined, cards, ranked, config, cache, execution=execution)
