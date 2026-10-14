"""Lossless canonical text for supplementary counter-signals."""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime
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
        "limits": "Caveats",
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
        "limits": "Важные оговорки",
    },
}


def reader_date(value: str, language: str) -> str:
    """Human-readable source/check date; retained records keep the exact timestamp."""
    try:
        instant = datetime.fromisoformat(value)
        if instant.tzinfo is not None:
            instant = instant.astimezone(UTC)
        return instant.strftime("%d.%m.%Y" if language == "ru" else "%d %b %Y")
    except (ValueError, OverflowError):
        return value


def _reader_coverage(result: EvidenceIrritatorResult, language: str) -> str:
    from digest.domain.investigation.coverage import COVERAGE, FULL_SOURCE_COVERAGE

    if result.coverage not in {COVERAGE, FULL_SOURCE_COVERAGE}:
        return result.coverage  # An unfamiliar qualification cannot be discarded.
    if result.coverage == FULL_SOURCE_COVERAGE:
        return (
            "Ограниченная проверка одного утверждения по выбранным моделью отрывкам и найденным фрагментам. "
            "Поиск и контекст ограничены, источники могут быть пристрастны. "
            "Это не полные статьи и не независимое подтверждение; отсутствие опровержения не подтверждает утверждение."
            if language == "ru" else
            "Limited check of one claim using model-selected passages and search excerpts. "
            "Search and context are limited; sources may be biased. "
            "This is neither full-article coverage nor independent corroboration; "
            "absence of counter-evidence is not confirmation."
        )
    if language == "ru":
        evidence = "RSS-выдержкам"
        return (
            f"Ограниченная проверка одного утверждения по {evidence} и найденным фрагментам. "
            "Поиск и контекст ограничены, источники могут быть пристрастны. "
            "Полные статьи не проверены; отсутствие опровержения не подтверждает утверждение."
        )
    evidence = "RSS excerpts"
    return (
        f"Limited check of one claim using {evidence} and search excerpts. "
        "Search and context are limited; sources may be biased. "
        "Full articles were not verified; no counter-signal does not confirm the claim."
    )


# A closed vocabulary of generic process caveats. Unknown wording always stays visible.
# The full result/report retains these strings; the reader coverage sentence owns
# excerpt-only scope and the limitations of a bounded search.
_REPEATED_SCOPE = frozenset({
    "Evidence is a sanitized RSS excerpt, not the full article.",
    "Evidence consists of short excerpts; full articles were not reviewed.",
    "Excerpt may omit additional qualifiers or context.",
    "Only publicly indexed sources are searchable.",
    "Vendor-specific blogs may be biased.",
    "Non-English coverage might be missed.",
    "Technical nuances may not be evident in headlines.",
    "The ranking relies on limited snippet content, which may omit relevant details.",
})
_CAVEATS_RU = {
    "Potential for schedule changes not captured in excerpt.": "Изменения расписания могли не попасть в отрывок.",
    "Information may be outdated before the rollout.": "Сведения могут устареть до запуска.",
}
_READINESS_RU = {
    "DNS resolver readiness": "Готовность DNS-резолверов независимо не проверена.",
    "deployment readiness": "Готовность к внедрению независимо не проверена.",
    "production readiness": "Готовность к промышленной эксплуатации независимо не проверена.",
}
_SCOPE_RU = {"DNS infrastructure": "DNS-инфраструктурой", "network infrastructure": "сетевой инфраструктурой"}
_AUTHORITY_RU = {"DNS authority": "органов управления DNS", "regulatory": "регуляторов"}


def _localized_caveat(limitation: str, language: str) -> str:
    if language != "ru":
        return limitation
    if limitation in _CAVEATS_RU:
        return _CAVEATS_RU[limitation]
    readiness = re.fullmatch(r"No independent verification of (.+)\.", limitation)
    if readiness and readiness[1] in _READINESS_RU:
        return _READINESS_RU[readiness[1]]
    scope = re.fullmatch(r"Scope limited to (.+), not direct banking impact\.", limitation)
    if scope and scope[1] in _SCOPE_RU:
        return f"Охват ограничен {_SCOPE_RU[scope[1]]}; прямое влияние на банки не оценивалось."
    authority = re.fullmatch(r"All external signals are from developer-focused blogs, not primary (.+) sources\.",
                             limitation)
    if authority and authority[1] in _AUTHORITY_RU:
        return (
            "Внешние сведения — из блогов разработчиков, а не из первичных источников "
            f"{_AUTHORITY_RU[authority[1]]}."
        )
    return limitation


def _reader_limitations(result: EvidenceIrritatorResult, language: str) -> list[str]:
    """Project the closed reviewed vocabulary; unknown model caveats remain verbatim."""
    from digest.domain.investigation.coverage import COVERAGE, FULL_SOURCE_COVERAGE

    standard_coverage = result.coverage in {COVERAGE, FULL_SOURCE_COVERAGE}
    russian = language == "ru"
    notes = []
    for original in result.limitations:
        limitation = original.replace("‑", "-")
        if standard_coverage and limitation in _REPEATED_SCOPE:
            continue
        if (limitation == "No signal directly contradicts the claim; only one provides contextual risk."
                and len(result.ranked_signals) == 1 and result.ranked_signals[0].relation == "complicates"):
            continue  # The relation is shown explicitly beside that unchanged signal.
        if re.fullmatch(r"Ranking omitted non-counter signals: supports=\d+, context=\d+, insufficient=\d+\.",
                        limitation) or re.fullmatch(
            r"Excluded \d+ distinct search URLs that repeat known cited sources\.", limitation
        ):
            continue  # Full counts and rejected candidates remain in the result/report.
        failed = re.fullmatch(r"(\d+) of (\d+) source/query searches failed or were unavailable\.", limitation)
        ranked = re.fullmatch(r"Ranking considered only (\d+) of (\d+) validated signals\.", limitation)
        if failed:
            limitation = (
                f"Часть поисков недоступна: {failed[1]} из {failed[2]}."
                if russian else f"Some searches were unavailable: {failed[1]} of {failed[2]}."
            )
        elif ranked:
            limitation = (
                f"Оценено материалов: {ranked[1]} из {ranked[2]}."
                if russian else f"Materials assessed: {ranked[1]} of {ranked[2]}."
            )
        elif limitation == "URL liveness checks were not performed in the bounded evidence stage.":
            limitation = "Доступность ссылок не проверена." if russian else "Link availability was not checked."
        elif limitation == (
            "One query has a verified source-text anchor; neutrality and retrieval usefulness are not certified."
        ):
            limitation = (
                "Нейтральность и полезность поиска не проверены."
                if russian else "Search neutrality and usefulness were not verified."
            )
        else:
            localized = _localized_caveat(limitation, language)
            limitation = original if localized == limitation else localized
        if limitation not in notes:
            notes.append(limitation)
    return notes


def fragment_text(
    result: EvidenceIrritatorResult,
    presented: EvidenceIrritatorResult,
    origin: DeliveredInvestigationInput,
    *,
    language: str,
    notice: str,
    investigated_at: str,
) -> str:
    """Render reader evidence/qualifications; the canonical result retains operator detail."""
    labels = _FRAGMENT_LABELS.get(language, _FRAGMENT_LABELS["en"])
    narrative = cast("DeliveredNarrative", result.narratives[0])
    target = next(card for card in origin.cards if card.card_id == narrative.target_card_id)
    source_date = (reader_date(target.occurrence.published, language)
                   if target.occurrence.published else labels["unknown"])
    lines = [
        labels["heading"],
        f"{labels['origin']}: {reader_date(origin.publication_day, language)}. "
        f"{labels['investigation']}: {reader_date(investigated_at, language)}; {labels[result.status]}.",
        f"{labels['assertion']}: {presented.narratives[0].claim}",
        f"{labels[narrative.delivered_quote.field]}: {narrative.delivered_quote.text}\n"
        f"{labels['source']}: {target.canonical.link}\n"
        f"{labels['published']}: {source_date}",
        "\n".join(f"{labels['evidence']}: {quote}" for quote in narrative.quotes.values()),
    ]
    for shown, canonical in zip(presented.ranked_signals, result.ranked_signals, strict=True):
        if canonical.relation == "complicates":
            relation = (
                "Уточняет, не является прямым опровержением"
                if language == "ru" else "Complicates; not a direct contradiction"
            )
        else:
            relation = "Оспаривает утверждение" if language == "ru" else "Contradicts the claim"
        external_date = (reader_date(canonical.signal.published, language)
                         if canonical.signal.published else labels["unknown"])
        lines.append(
            f"{shown.signal.title}\n{shown.signal.url}\n{relation}: {shown.reasoning}\n"
            f"{labels['external']}: {canonical.quote}\n"
            f"{labels['external_date']}: {external_date}"
        )
    lines.append(_reader_coverage(result, language))
    limitations = _reader_limitations(result, language)
    if limitations:
        lines.append(labels["limits"] + ": " + " ".join(limitations))
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
    Paragraph and sentence boundaries are preferred when they fit.
    Concatenating decoded chunks recovers the original text exactly.
    """
    if max_units < 4:
        raise ValueError("Supplement chunk budget is too small.")

    def units(value: str) -> int:
        return len(value.encode("utf-16-le")) // 2

    chunks: list[str] = []
    current: list[str] = []
    sizes: list[int] = []
    boundaries: list[tuple[int, int]] = []
    used = 0
    previous = ""
    for token in re.split(r"(https?://\S+|\s+)", text):
        if not token:
            continue
        encoded = escape(token)
        size = units(encoded)
        if token.startswith(("https://", "http://")) and size > max_units:
            raise ValueError("Supplement URL exceeds the Telegram message budget.")
        # Oversized sentences fall back to words; oversized non-URL words to characters.
        pieces = [token] if size <= max_units else list(token)
        for piece in pieces:
            encoded = escape(piece)
            size = units(encoded)
            while used + size > max_units:
                cut = max(boundaries, default=(0, len(current)))[1]
                chunks.append("".join(current[:cut]))
                current, sizes = current[cut:], sizes[cut:]
                boundaries = [(priority, end - cut) for priority, end in boundaries if end > cut]
                used = sum(sizes)
            current.append(encoded)
            sizes.append(size)
            used += size
        # Whitespace belongs to the preceding chunk, preserving every original byte.
        if token.isspace():
            priority = (3 if "\n\n" in token else 2 if re.search(r'[.!?…][»”"\)\]]*$', previous)
                        else 1 if "\n" in token else 0)
            if priority:
                boundaries.append((priority, len(current)))
        else:
            previous = token
    if current:
        chunks.append("".join(current))
    return chunks
