"""LLM-powered ranking of counter-signals."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from digest._serialization import extract_json as _extract_json
from digest.adapters.models.execution import ModelExecution
from digest.config import Config
from digest.domain.investigation.signals import Signal
from digest.irritator.narrative_extractor import Narrative
from digest.llm import LLMRole, complete

logger = logging.getLogger(__name__)

RANK_RELATIONS = ("contradicts", "complicates", "supports", "context", "insufficient")
RANK_RELATION_CONTRACT = (
    "Classify each source against the exact source-attributed assertion or announced decision: "
    "contradicts = evidence against what the assertion states; "
    "complicates = a sourced material condition, implementation cost, tradeoff or limitation "
    "relevant to that decision. "
    "A useful decision tradeoff need not refute the announcement or show that its author ignored it. "
    "supports = evidence consistent with the assertion; context = related background or an alternative without "
    "a material decision implication; insufficient = supplied evidence cannot establish a relation. "
    "In reasoning distinguish what the external source actually finds from your editorial explanation of why "
    "it matters to this specific assertion or decision. Preserve favourable results and limitations together. "
    "Do not add an unstated simplicity, primary-solution, necessity, sufficiency or exclusivity premise. "
    "Literal quotation alone does not establish the relation. "
    "Only contradicts and complicates qualify as counter-signals. "
)


@dataclass
class RankedSignal:
    """A signal scored and annotated by LLM."""

    signal: Signal
    score: int
    reasoning: str
    narrative_claim: str


# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------

_SYSTEM_PROMPTS: dict[str, str] = {
    "ru": (
        "Ты — аналитик контр-сигналов. Оцени каждый сигнал по одному критерию: "
        "насколько он ПРОТИВОРЕЧИТ или УСЛОЖНЯЕТ нарратив."
    ),
    "en": (
        "You are a counter-signal analyst. Score each signal on a single criterion: "
        "how strongly it CONTRADICTS or COMPLICATES the narrative."
    ),
}

_USER_PROMPTS: dict[str, str] = {
    "ru": (
        "Нарратив: {claim}\n\n"
        "Сигналы для оценки:\n{signals_text}\n\n"
        "Оцени каждый сигнал по шкале 1-10, где:\n"
        "9-10 = прямые доказательства того, что нарратив неверен или преувеличен\n"
        "7-8 = существенное осложнение или важная оговорка к утверждению или решению\n"
        "5-6 = ограниченная, но подтверждённая оговорка к данному утверждению\n"
        "1-4 = слабая релевантность\n\n"
        "Для каждого сигнала верни JSON-объект с полями:\n"
        '- "index" — порядковый номер сигнала (начиная с 0)\n'
        '- "score" — оценка от 1 до 10\n'
        '- "relation" — contradicts, complicates, supports, context или insufficient\n'
        '- "reasoning" — обоснование оценки (1-2 предложения)\n\n'
        "Верни JSON-массив объектов. Ничего больше не добавляй."
    ),
    "en": (
        "Narrative: {claim}\n\n"
        "Signals to evaluate:\n{signals_text}\n\n"
        "Score each signal on a 1-10 scale where:\n"
        "9-10 = direct evidence the narrative is wrong or overstated\n"
        "7-8 = significant complication or important caveat relevant to the assertion or decision\n"
        "5-6 = limited but supported qualification of this claim\n"
        "1-4 = weak relevance\n\n"
        "For each signal return a JSON object with fields:\n"
        '- "index" — signal index (starting from 0)\n'
        '- "score" — score from 1 to 10\n'
        '- "relation" — contradicts, complicates, supports, context or insufficient\n'
        '- "reasoning" — justification for the score (1-2 sentences)\n\n'
        "Return a JSON array of objects. Return nothing else."
    ),
}


MAX_RANKING_JSON_CHARS = 8000


def _ranking_signal_packet(signals: list[Signal]) -> tuple[str, set[int]]:
    """Admit whole evidence records within one JSON budget, keeping source indices."""
    records: list[str] = []
    admitted: set[int] = set()
    size = 2  # JSON array brackets.
    for index, signal in enumerate(signals):
        record = json.dumps({
            "index": index,
            "source_name": signal.source_name,
            "title": signal.title,
            "url": signal.url,
            "snippet": signal.snippet,
        }, ensure_ascii=False)
        extra = len(record) + (2 if records else 0)
        if size + extra > MAX_RANKING_JSON_CHARS:
            continue
        records.append(record)
        admitted.add(index)
        size += extra
    return "[" + ", ".join(records) + "]", admitted


def _build_prompt(
    narrative: Narrative,
    signals: list[Signal],
    language: str,
    *,
    signals_text: str | None = None,
) -> list[dict[str, str]]:
    """Build LLM messages for signal ranking."""
    if signals_text is None:
        signals_text, _admitted = _ranking_signal_packet(signals)
    system = _SYSTEM_PROMPTS.get(language, _SYSTEM_PROMPTS["ru"])
    user_tmpl = _USER_PROMPTS.get(language, _USER_PROMPTS["ru"])
    user = user_tmpl.format(claim=narrative.claim, signals_text=signals_text)
    return [
        {"role": "system", "content": system + " " + RANK_RELATION_CONTRACT},
        {"role": "user", "content": user},
    ]


def _parse_rankings(
    raw: Any,
    signals: list[Signal],
    narrative_claim: str,
    min_score: int,
    *,
    admitted_indices: set[int] | None = None,
) -> list[RankedSignal]:
    """Validate every entry, then keep counter-relations at or above min_score."""
    if not isinstance(raw, list):
        raise ValueError(f"Expected JSON array, got {type(raw).__name__}")
    if admitted_indices is None:
        _packet, admitted_indices = _ranking_signal_packet(signals)

    validated: list[tuple[str, RankedSignal]] = []
    seen: set[int] = set()
    for item in raw:
        if not isinstance(item, dict) or set(item) != {"index", "score", "reasoning", "relation"}:
            raise ValueError("Invalid ranking fields.")
        idx, score, relation, reasoning = (item[key] for key in ("index", "score", "relation", "reasoning"))
        if type(idx) is not int or idx not in admitted_indices or idx in seen:
            raise ValueError("Unknown or duplicate ranking index.")
        if type(score) is not int or not 1 <= score <= 10:
            raise ValueError("Ranking score must be an integer from 1 through 10.")
        if not isinstance(relation, str) or relation not in RANK_RELATIONS:
            raise ValueError("Invalid ranking relation.")
        if not isinstance(reasoning, str) or not reasoning.strip():
            raise ValueError("Ranking reasoning must be nonempty text.")
        seen.add(idx)
        validated.append((relation, RankedSignal(signals[idx], score, reasoning.strip(), narrative_claim)))

    ranked = [signal for relation, signal in validated
              if relation in ("contradicts", "complicates") and signal.score >= min_score]
    ranked.sort(key=lambda r: r.score, reverse=True)
    return ranked


async def rank_signals(
    narrative: Narrative,
    signals: list[Signal],
    config: Config,
    *, execution: ModelExecution,
) -> list[RankedSignal]:
    """Rank signals against a narrative via LLM.

    Returns ranked signals above min_signal_score, sorted by score descending.
    """
    if not signals:
        return []

    signals_text, admitted_indices = _ranking_signal_packet(signals)
    omitted_count = len(signals) - len(admitted_indices)
    if omitted_count:
        logger.warning("Ranking omitted %d whole signals due to the evidence budget", omitted_count)
    if not admitted_indices:
        return []

    messages = _build_prompt(narrative, signals, config.radar.language, signals_text=signals_text)
    text, _usage = await complete(
        LLMRole.RANK_SIGNALS, messages, config, temperature=0.3, execution=execution,
    )

    raw = _extract_json(text)
    ranked = _parse_rankings(
        raw, signals, narrative.claim, config.irritator.min_signal_score,
        admitted_indices=admitted_indices,
    )

    # Limit to top_signals
    ranked = ranked[: config.irritator.top_signals]

    logger.info(
        "Ranked %d signals above threshold %d for narrative '%s'",
        len(ranked),
        config.irritator.min_signal_score,
        narrative.claim[:60],
    )
    return ranked
