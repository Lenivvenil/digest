"""Generate search queries for counter-signal sources from narratives."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from src.config import Config
from src.irritator.narrative_extractor import Narrative
from src.llm import LLMRole, _extract_json, complete

logger = logging.getLogger(__name__)


@dataclass
class SearchQuery:
    """A search query targeting a specific counter-signal source."""

    query: str
    target_source: str
    intent: str


# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------

_SYSTEM_PROMPTS: dict[str, str] = {
    "ru": (
        "Ты — поисковый аналитик. Твоя задача — составить поисковые запросы, "
        "которые помогут найти контр-сигналы к доминирующему нарративу."
    ),
    "en": (
        "You are a search analyst. Your task is to craft search queries "
        "that will help find counter-signals to a dominant narrative."
    ),
}

_USER_PROMPTS: dict[str, str] = {
    "ru": (
        "Нарратив: {claim}\n"
        "Категория: {category}\n"
        "Неявные предположения:\n{assumptions}\n"
        "Почему стоит оспорить: {why_worth_challenging}\n\n"
        "Составь {queries_per_narrative} поисковых запросов для поиска "
        "контр-сигналов к этому нарративу. Запросы будут использованы "
        "в следующих источниках: {sources}.\n\n"
        "Для каждого запроса верни JSON-объект с полями:\n"
        '- "query" — поисковый запрос (на английском, т.к. источники англоязычные)\n'
        '- "target_source" — один из: {sources}\n'
        '- "intent" — что именно ищем (1 предложение)\n\n'
        "Верни JSON-массив объектов. Ничего больше не добавляй."
    ),
    "en": (
        "Narrative: {claim}\n"
        "Category: {category}\n"
        "Implicit assumptions:\n{assumptions}\n"
        "Why worth challenging: {why_worth_challenging}\n\n"
        "Craft {queries_per_narrative} search queries to find counter-signals "
        "to this narrative. Queries will be used in: {sources}.\n\n"
        "For each query return a JSON object with fields:\n"
        '- "query" — the search query\n'
        '- "target_source" — one of: {sources}\n'
        '- "intent" — what we are looking for (1 sentence)\n\n'
        "Return a JSON array of objects. Return nothing else."
    ),
}

_REQUIRED_FIELDS = {"query", "target_source", "intent"}


def _build_prompt(
    narrative: Narrative,
    language: str,
    queries_per_narrative: int,
    sources: list[str],
) -> list[dict[str, str]]:
    """Build LLM messages for query generation."""
    assumptions = "\n".join(f"- {a}" for a in narrative.implicit_assumptions)
    sources_str = ", ".join(sources)
    system = _SYSTEM_PROMPTS.get(language, _SYSTEM_PROMPTS["ru"])
    user_tmpl = _USER_PROMPTS.get(language, _USER_PROMPTS["ru"])
    user = user_tmpl.format(
        claim=narrative.claim,
        category=narrative.category,
        assumptions=assumptions,
        why_worth_challenging=narrative.why_worth_challenging,
        queries_per_narrative=queries_per_narrative,
        sources=sources_str,
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def _parse_queries(raw: Any) -> list[SearchQuery]:
    """Validate parsed JSON and construct SearchQuery dataclasses."""
    if not isinstance(raw, list):
        raise ValueError(f"Expected JSON array, got {type(raw).__name__}")
    queries: list[SearchQuery] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"Query #{i} is not a JSON object")
        missing = _REQUIRED_FIELDS - item.keys()
        if missing:
            raise ValueError(
                f"Query #{i} missing field(s): {', '.join(sorted(missing))}"
            )
        queries.append(
            SearchQuery(
                query=str(item["query"]),
                target_source=str(item["target_source"]),
                intent=str(item["intent"]),
            )
        )
    return queries


async def _generate_for_narrative(
    narrative: Narrative,
    config: Config,
) -> list[SearchQuery]:
    """Generate queries for a single narrative."""
    messages = _build_prompt(
        narrative,
        config.radar.language,
        config.irritator.queries_per_narrative,
        config.irritator.sources,
    )
    text, _usage = await complete(
        LLMRole.GENERATE_QUERIES, messages, config, temperature=0.5
    )
    raw = _extract_json(text)
    return _parse_queries(raw)


async def generate_queries(
    narratives: list[Narrative],
    config: Config,
) -> dict[str, list[SearchQuery]]:
    """Generate search queries for all narratives in parallel.

    Returns dict mapping narrative claim to its search queries.
    Raises ValueError on invalid LLM response, RuntimeError if all providers fail.
    """
    if not narratives:
        return {}

    tasks = [_generate_for_narrative(n, config) for n in narratives]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    queries_by_narrative: dict[str, list[SearchQuery]] = {}
    for narrative, result in zip(narratives, results, strict=True):
        if isinstance(result, Exception):
            logger.warning(
                "Query generation failed for narrative '%s': %s",
                narrative.claim[:80],
                result,
            )
            continue
        queries_by_narrative[narrative.claim] = result

    total = sum(len(qs) for qs in queries_by_narrative.values())
    logger.info(
        "Generated %d queries for %d/%d narratives",
        total,
        len(queries_by_narrative),
        len(narratives),
    )
    return queries_by_narrative
