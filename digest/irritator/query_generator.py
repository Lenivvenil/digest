"""Generate adversarial search queries for counter-signal discovery from narratives."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from digest._serialization import extract_json as _extract_json
from digest.adapters.models.execution import ModelExecution
from digest.config import Config
from digest.domain.investigation.queries import SearchQuery as SearchQuery
from digest.irritator.narrative_extractor import Narrative
from digest.irritator.query_contract import QUERY_CONTRACT, lexical_atoms
from digest.llm import LLMRole, complete

logger = logging.getLogger(__name__)


@dataclass
class QueryDiagnostics:
    successful: int = 0
    failed: int = 0


@dataclass(frozen=True)
class QueryBatch:
    """Generated queries and outcomes for every narrative attempt."""

    queries_by_narrative: dict[str, list[SearchQuery]]
    diagnostics: QueryDiagnostics


# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------

_SYSTEM_PROMPTS: dict[str, str] = {
    "ru": (
        "Ты — поисковый аналитик-критик. Твоя задача — составить поисковые запросы, "
        "для поиска материалов, позволяющих проверить доминирующий нарратив."
    ),
    "en": (
        "You are a critical search analyst. Your task is to craft search queries "
        "for material that can test a dominant narrative."
    ),
}

_USER_PROMPTS: dict[str, str] = {
    "ru": (
        "Нарратив: {claim}\n"
        "Категория: {category}\n\n"
        "Составь {queries_per_narrative} кратких поисковых запросов по теме или сущности.\n"
        "Не закладывай желаемое опровержение в поисковые слова. Используй разные ракурсы темы.\n"
        "Контргипотезу и причину проверки укажи в intent; отношение найденного материала к нарративу "
        "оценивается после поиска.\n\n"
        "Для каждого запроса верни JSON-объект с полями:\n"
        '- "query" — поисковый запрос (на английском)\n'
        '- "intent" — что именно ищем (1 предложение)\n\n'
        "Верни JSON-массив объектов. Ничего больше не добавляй."
    ),
    "en": (
        "Narrative: {claim}\n"
        "Category: {category}\n\n"
        "Craft {queries_per_narrative} concise topic/entity search queries from different angles.\n"
        "Do not require the desired counterclaim in search keywords. Put the counter-hypothesis "
        "and reason to investigate in intent; assess the retrieved material's relation after search.\n\n"
        "For each query return a JSON object with fields:\n"
        '- "query" — the search query\n'
        '- "intent" — what we are looking for (1 sentence)\n\n'
        "Return a JSON array of objects. Return nothing else."
    ),
}

_REQUIRED_FIELDS = {"query", "intent"}


def _build_prompt(
    narrative: Narrative,
    language: str,
    queries_per_narrative: int,
) -> list[dict[str, str]]:
    """Build LLM messages for adversarial query generation."""
    system = _SYSTEM_PROMPTS.get(language, _SYSTEM_PROMPTS["ru"])
    user_tmpl = _USER_PROMPTS.get(language, _USER_PROMPTS["ru"])
    user = user_tmpl.format(
        claim=narrative.claim,
        category=narrative.category,
        queries_per_narrative=queries_per_narrative,
    )
    return [
        {"role": "system", "content": system + " " + QUERY_CONTRACT},
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
        lexical_atoms(item["query"])
        queries.append(
            SearchQuery(
                query=item["query"],
                intent=str(item["intent"]),
            )
        )
    return queries


async def _generate_for_narrative(
    narrative: Narrative,
    config: Config,
    *, execution: ModelExecution,
) -> list[SearchQuery]:
    """Generate adversarial queries for a single narrative."""
    messages = _build_prompt(
        narrative,
        config.radar.language,
        config.irritator.queries_per_narrative,
    )
    text, _usage = await complete(
        LLMRole.GENERATE_QUERIES, messages, config, temperature=0.5, execution=execution,
    )
    raw = _extract_json(text)
    return _parse_queries(raw)


async def generate_queries(
    narratives: list[Narrative],
    config: Config,
    *, execution: ModelExecution,
) -> QueryBatch:
    """Generate adversarial search queries for all narratives in parallel.

    Return a claim-to-queries mapping with per-attempt diagnostic counts.
    Ordinary child failures are logged and counted; cancellation propagates.
    """
    diagnostics = QueryDiagnostics()
    if not narratives:
        return QueryBatch({}, diagnostics)

    tasks = [_generate_for_narrative(n, config, execution=execution) for n in narratives]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    queries_by_narrative: dict[str, list[SearchQuery]] = {}
    for narrative, result in zip(narratives, results, strict=True):
        if isinstance(result, asyncio.CancelledError):
            raise result
        if isinstance(result, Exception):
            diagnostics.failed += 1
            logger.warning(
                "Query generation failed (%s)",
                type(result).__name__,
            )
            continue
        diagnostics.successful += 1
        queries_by_narrative[narrative.claim] = result  # type: ignore[assignment]

    total = sum(len(qs) for qs in queries_by_narrative.values())
    logger.info(
        "Generated %d queries for %d/%d narratives",
        total,
        len(queries_by_narrative),
        len(narratives),
    )
    return QueryBatch(queries_by_narrative, diagnostics)
