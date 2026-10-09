"""Lossless canonical text for supplementary counter-signals."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from digest.domain.investigation.delivered import DeliveredInvestigationInput
    from digest.irritator.evidence_stage import DeliveredNarrative, EvidenceIrritatorResult


_FRAGMENT_LABELS = {
    "en": {
        "heading": "Irritator: supplementary evidence",
        "origin": "Origin edition",
        "investigation": "Investigation",
        "complete": "complete",
        "incomplete": "incomplete",
        "assertion": "Assertion checked",
        "title": "Canonical title",
        "summary": "Canonical summary",
        "source": "Original source",
        "published": "published",
        "unknown": "unavailable",
        "evidence": "Original evidence",
        "external": "External evidence",
        "external_date": "External source date",
        "limits": "Coverage and limitations (canonical wording)",
    },
    "ru": {
        "heading": "Ирритатор: дополнительные свидетельства",
        "origin": "Исходный выпуск",
        "investigation": "Проверка",
        "complete": "завершена",
        "incomplete": "неполная",
        "assertion": "Проверяемое утверждение",
        "title": "Канонический заголовок",
        "summary": "Каноническое резюме",
        "source": "Исходный источник",
        "published": "дата публикации",
        "unknown": "неизвестна",
        "evidence": "Исходное свидетельство",
        "external": "Внешнее свидетельство",
        "external_date": "Дата внешнего источника",
        "limits": "Охват и ограничения (исходная формулировка)",
    },
}


def fragment_text(
    result: EvidenceIrritatorResult,
    presented: EvidenceIrritatorResult,
    origin: DeliveredInvestigationInput,
    *,
    language: str,
    notice: str,
    investigated_at: str,
) -> str:
    """Present accepted values losslessly; source quotations and qualifications stay canonical."""
    labels = _FRAGMENT_LABELS.get(language, _FRAGMENT_LABELS["en"])
    narrative = cast("DeliveredNarrative", result.narratives[0])
    target = next(card for card in origin.cards if card.card_id == narrative.target_card_id)
    lines = [
        labels["heading"],
        f"{labels['origin']}: {origin.publication_day}",
        f"{labels['investigation']}: {investigated_at}; {labels[result.status]}",
        f"{labels['assertion']}: {presented.narratives[0].claim}",
        f"{labels[narrative.delivered_quote.field]}: {narrative.delivered_quote.text}",
        f"{labels['source']}: {target.canonical.link}; "
        f"{labels['published']}: {target.occurrence.published or labels['unknown']}",
        *(f"{labels['evidence']}: {quote}" for quote in narrative.quotes.values()),
    ]
    for shown, canonical in zip(presented.ranked_signals, result.ranked_signals, strict=True):
        lines.extend(
            [
                signal_text(shown, language),
                f"{labels['external']}: {canonical.quote}",
                f"{labels['external_date']}: {canonical.signal.published or labels['unknown']}",
            ]
        )
    lines.extend([labels["limits"], result.coverage, *result.limitations])
    if notice:
        lines.append(notice)
    return "\n\n".join(lines)


def signal_text(ranked: Any, language: str = "en") -> str:
    """Keep source identity, assessment and its complete qualification together."""
    label = "Оспаривает или уточняет" if language == "ru" else "Challenges or complicates"
    score = "Оценка" if language == "ru" else "Score"
    return (
        f"{ranked.signal.title}\n{ranked.signal.url}\n"
        f"{label}: {ranked.narrative_claim}\n"
        f"{score}: {ranked.score}/10\n{ranked.reasoning}"
    )


def split_supplement(
    text: str,
    escape: Callable[[str], str],
    max_units: int = 3800,
) -> list[str]:
    """Escape independently valid chunks without losing text or splitting URLs.

    The bound conservatively counts UTF-16 code units after Markdown escaping.
    All chunks are prepared before delivery, so an impossible URL fails before send.
    Concatenating decoded chunks recovers the original text exactly.
    """
    if max_units < 4:
        raise ValueError("Supplement chunk budget is too small.")

    def units(value: str) -> int:
        return len(value.encode("utf-16-le")) // 2

    chunks: list[str] = []
    current = ""
    used = 0
    for token in re.split(r"(https?://\S+|\s+)", text):
        if not token:
            continue
        encoded = escape(token)
        size = units(encoded)
        if token.startswith(("https://", "http://")) and size > max_units:
            raise ValueError("Supplement URL exceeds the Telegram message budget.")
        # Prefer intact words and URLs; split only oversized non-URL tokens.
        pieces = [token] if size <= max_units else list(token)
        for piece in pieces:
            encoded = escape(piece)
            size = units(encoded)
            if used + size > max_units:
                chunks.append(current)
                current, used = "", 0
            current += encoded
            used += size
    if current:
        chunks.append(current)
    return chunks
