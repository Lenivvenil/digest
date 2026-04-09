"""Extract dominant narratives from radar category summaries."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from src.config import Config
from src.llm import LLMRole, _extract_json, complete
from src.radar.summarizer import CategorySummary

logger = logging.getLogger(__name__)


@dataclass
class Narrative:
    """A dominant narrative extracted from category summaries."""

    claim: str
    category: str
    implicit_assumptions: list[str]
    why_worth_challenging: str


# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------

_SYSTEM_PROMPTS: dict[str, str] = {
    "ru": (
        "Ты — критический аналитик. Твоя задача — выявить доминирующие нарративы "
        "(главные предположения), которые пронизывают новостное освещение, "
        "но редко оспариваются."
    ),
    "en": (
        "You are a critical analyst. Your task is to identify dominant narratives "
        "(key assumptions) that permeate news coverage but are rarely questioned."
    ),
}

_USER_PROMPTS: dict[str, str] = {
    "ru": (
        "Проанализируй следующие категорийные саммари новостей и выдели до "
        "{max_narratives} доминирующих нарративов — неявных предположений, "
        "которые пронизывают освещение, но редко оспариваются.\n\n"
        "{summaries_text}\n\n"
        "Для каждого нарратива верни JSON-объект с полями:\n"
        '- "claim" — суть нарратива в 1-2 предложениях\n'
        '- "category" — категория новостей, из которой возник нарратив\n'
        '- "implicit_assumptions" — список из 2-3 ключевых неявных предположений\n'
        '- "why_worth_challenging" — абзац (3-4 предложения) о том, '
        "почему этот нарратив стоит оспорить\n\n"
        "Верни JSON-массив объектов. Ничего больше не добавляй."
    ),
    "en": (
        "Analyse the following category summaries and identify up to "
        "{max_narratives} dominant narratives — implicit assumptions "
        "that permeate coverage but are rarely questioned.\n\n"
        "{summaries_text}\n\n"
        "For each narrative return a JSON object with fields:\n"
        '- "claim" — the essence of the narrative in 1-2 sentences\n'
        '- "category" — the news category the narrative emerged from\n'
        '- "implicit_assumptions" — list of 2-3 key unstated assumptions\n'
        '- "why_worth_challenging" — a paragraph (3-4 sentences) explaining '
        "why this narrative deserves scrutiny\n\n"
        "Return a JSON array of objects. Return nothing else."
    ),
}

_REQUIRED_FIELDS = {"claim", "category", "implicit_assumptions", "why_worth_challenging"}


def _build_prompt(
    summaries: list[CategorySummary],
    language: str,
    max_narratives: int,
) -> list[dict[str, str]]:
    """Build LLM messages for narrative extraction."""
    summaries_text = "\n\n".join(
        f"### {s.category} ({s.article_count} articles)\n{s.summary_text}"
        for s in summaries
    )
    system = _SYSTEM_PROMPTS.get(language, _SYSTEM_PROMPTS["ru"])
    user_tmpl = _USER_PROMPTS.get(language, _USER_PROMPTS["ru"])
    user = user_tmpl.format(
        max_narratives=max_narratives,
        summaries_text=summaries_text,
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def _parse_narratives(raw: Any, max_narratives: int) -> list[Narrative]:
    """Validate parsed JSON and construct Narrative dataclasses."""
    if not isinstance(raw, list):
        raise ValueError(f"Expected JSON array, got {type(raw).__name__}")
    narratives: list[Narrative] = []
    for i, item in enumerate(raw[:max_narratives]):
        if not isinstance(item, dict):
            raise ValueError(f"Narrative #{i} is not a JSON object")
        missing = _REQUIRED_FIELDS - item.keys()
        if missing:
            raise ValueError(
                f"Narrative #{i} missing field(s): {', '.join(sorted(missing))}"
            )
        assumptions = item["implicit_assumptions"]
        if not isinstance(assumptions, list):
            raise ValueError(
                f"Narrative #{i} 'implicit_assumptions' must be a list, "
                f"got {type(assumptions).__name__}"
            )
        narratives.append(
            Narrative(
                claim=str(item["claim"]),
                category=str(item["category"]),
                implicit_assumptions=[str(a) for a in assumptions],
                why_worth_challenging=str(item["why_worth_challenging"]),
            )
        )
    return narratives


async def extract_narratives(
    summaries: list[CategorySummary],
    config: Config,
) -> list[Narrative]:
    """Extract dominant narratives from category summaries via LLM.

    Returns list of Narrative ordered by dominance.
    Raises ValueError on invalid LLM response, RuntimeError if all providers fail.
    """
    if not summaries:
        logger.debug("No summaries provided, skipping narrative extraction")
        return []

    max_narratives = config.irritator.max_narratives
    messages = _build_prompt(summaries, config.radar.language, max_narratives)

    text, _usage = await complete(
        LLMRole.EXTRACT_NARRATIVES, messages, config, temperature=0.5
    )

    raw = _extract_json(text)
    narratives = _parse_narratives(raw, max_narratives)

    logger.info("Extracted %d narratives from %d summaries", len(narratives), len(summaries))
    return narratives
