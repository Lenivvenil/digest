"""Pure Telegram copy, Markdown encoding, keyboards and lossless chunk coverage."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from digest.domain.catalog.articles import article_hash
from digest.presentation.supplement import signal_text, split_supplement

if TYPE_CHECKING:
    from digest.irritator import IrritatorStatus
    from digest.irritator.evidence_stage import EvidenceIrritatorResult
    from digest.radar.summarizer import ArticleSummary

# Static presentation labels follow canonical generation language, not translation targets.
_LABELS = {
    "en": {
        "feedback": ("Tap a vote button, then Start to send it. "
                     "Processed on the next digest run; private owner chat only."),
        "feedback_plain": "Votes are processed on digest runs; private owner chat only.",
        "fallback": "Or send /vote g {hash} (good) or /vote b {hash} (bad).",
        "irritator": "Irritator",
    },
    "ru": {
        "feedback": ("Нажмите оценку, затем Start (Запустить), чтобы отправить голос. "
                     "Учтём при следующем выпуске; только личный чат владельца."),
        "feedback_plain": "Оценки обрабатываются при запусках дайджеста; только личный чат владельца.",
        "fallback": "Или отправьте /vote g {hash} (полезно) либо /vote b {hash} (неполезно).",
        "irritator": "Раздражатор",
    },
}
_SPLIT_LIMIT = 3800

# Private Use Area sentinels for safe markdown conversion
_BOLD_OPEN = "\ue000"
_BOLD_CLOSE = "\ue001"
_LINK_PH_OPEN = "\ue002"
_LINK_PH_CLOSE = "\ue003"


@dataclass
class _IssueArticleRange:
    start: int
    end: int
    full_hash: str
    source: str
    covering_chunks: tuple[int, ...] = ()


@dataclass
class _IssueChunk:
    text: str
    reply_markup: dict[str, Any] | None = None


def _labels(config: Any) -> dict[str, str]:
    language = getattr(getattr(config, "radar", None), "language", "en")
    return _LABELS.get(language, _LABELS["en"])


def escape_markdownv2(text: str) -> str:
    """Escape special characters for Telegram MarkdownV2."""
    return re.sub(r"([\_*\[\]()~`>#\+\-=|{}.!])", r"\\\1", text)


def to_markdownv2(text: str) -> str:
    """Convert standard Markdown to Telegram MarkdownV2 format."""
    # 1. Protect links: [text](url) → sentinel placeholders
    links: list[tuple[str, str]] = []

    def _protect_link(m: re.Match[str]) -> str:
        link_text = escape_markdownv2(m.group(1))
        url = m.group(2).replace("\\", "\\\\").replace(")", "\\)")
        links.append((link_text, url))
        return f"{_LINK_PH_OPEN}{len(links) - 1}{_LINK_PH_CLOSE}"

    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", _protect_link, text)

    # 2. Headings: ## Heading → *Heading* (bold)
    text = re.sub(
        r"^#{1,6}\s+(.+)$",
        lambda m: f"{_BOLD_OPEN}{m.group(1)}{_BOLD_CLOSE}",
        text,
        flags=re.MULTILINE,
    )

    # 3. Bold: **text** → sentinel bold
    text = re.sub(
        r"\*\*(.+?)\*\*",
        lambda m: f"{_BOLD_OPEN}{m.group(1)}{_BOLD_CLOSE}",
        text,
    )

    # 4. Escape remaining special chars
    text = escape_markdownv2(text)

    # 5. Restore bold sentinels → MarkdownV2 bold
    text = text.replace(escape_markdownv2(_BOLD_OPEN), "*")
    text = text.replace(escape_markdownv2(_BOLD_CLOSE), "*")
    text = text.replace(_BOLD_OPEN, "*")
    text = text.replace(_BOLD_CLOSE, "*")

    # 6. Restore link sentinels
    def _restore_link(m: re.Match[str]) -> str:
        idx = int(m.group(1))
        lt, url = links[idx]
        return f"[{lt}]({url})"

    # Clean escaped sentinels first
    text = text.replace(escape_markdownv2(_LINK_PH_OPEN), _LINK_PH_OPEN)
    text = text.replace(escape_markdownv2(_LINK_PH_CLOSE), _LINK_PH_CLOSE)
    text = re.sub(
        f"{re.escape(_LINK_PH_OPEN)}(\\d+){re.escape(_LINK_PH_CLOSE)}",
        _restore_link,
        text,
    )

    return text


def split_message(text: str, max_len: int = _SPLIT_LIMIT) -> list[str]:
    """Split text into chunks at paragraph boundaries."""
    if len(text) <= max_len:
        return [text]

    chunks: list[str] = []
    current = ""

    for paragraph in text.split("\n\n"):
        candidate = f"{current}\n\n{paragraph}" if current else paragraph
        if len(candidate) <= max_len:
            current = candidate
        else:
            if current:
                chunks.append(current)
                current = ""
            # If single paragraph too long, split by newlines
            if len(paragraph) > max_len:
                for line in paragraph.split("\n"):
                    if current and len(current) + len(line) + 1 <= max_len:
                        current = f"{current}\n{line}"
                    else:
                        if current:
                            chunks.append(current)
                        # Hard split if single line too long
                        while len(line) > max_len:
                            split_at = max_len
                            while split_at > 0 and line[split_at - 1] == "\\":
                                split_at -= 1
                            if split_at == 0:
                                split_at = max_len
                            chunks.append(line[:split_at])
                            line = line[split_at:]
                        current = line
            else:
                current = paragraph

    if current:
        chunks.append(current)

    return chunks or [text]


def render_article_card(
    title: str, link: str, source: str, category: str, summary: str, config: Any,
) -> _IssueChunk:
    """Render the existing card copy and optional deep-link vote keyboard."""
    hash8 = article_hash(title, link)[:8]
    title_esc = escape_markdownv2(title)
    url_esc = link.replace("\\", "\\\\").replace(")", "\\)")
    source_esc = escape_markdownv2(source)
    cat_esc = escape_markdownv2(category)
    summary_esc = escape_markdownv2(summary)

    labels = _labels(config)
    username = getattr(config.telegram, "bot_username", "")
    notice = labels["feedback"] if username else labels["feedback_plain"]
    async_note = escape_markdownv2(notice + "\n" + labels["fallback"].format(hash=hash8))
    text = (
        f"[{title_esc}]({url_esc})\n\n"
        f"{summary_esc}\n\n"
        f"*{source_esc}* \u00b7 _{cat_esc}_\n"
        f"_{async_note}_"
    )

    keyboard: dict[str, Any] | None = {
        "inline_keyboard": [
            [
                {"text": "\U0001f44d", "url": f"https://t.me/{username}?start=vote_g_{hash8}"},
                {"text": "\U0001f44e", "url": f"https://t.me/{username}?start=vote_b_{hash8}"},
            ]
        ]
    } if username else None

    return _IssueChunk(text, keyboard)


def render_compact_issue(
    articles: list[ArticleSummary], config: Any, notice: str,
) -> tuple[list[_IssueChunk], list[_IssueArticleRange]]:
    """Prepare every chunk and map escaped article ranges before dispatch."""
    username = getattr(config.telegram, "bot_username", "")
    parts: list[str] = []
    ranges: list[_IssueArticleRange] = []
    offset = 0
    for index, article in enumerate(articles, 1):
        full_hash = article_hash(article.title, article.link)
        block = f"{index}. {article.title}\n{article.summary}\n{article.source}\n{article.link}"
        if not username:
            block += f"\n[{full_hash[:8]}]"
        if parts:
            offset += len(escape_markdownv2("\n\n"))
        end = offset + len(escape_markdownv2(block))
        ranges.append(_IssueArticleRange(offset, end, full_hash, article.source))
        parts.append(block)
        offset = end

    footer = [notice] if notice else []
    if articles:
        labels = _labels(config)
        footer.append(labels["feedback"] if username else labels["feedback_plain"])
        if not username:
            footer.append(labels["fallback"].format(hash="HASH"))
    if footer:
        parts.append("\n".join(footer))
    encoded_chunks = split_supplement("\n\n".join(parts), escape_markdownv2, _SPLIT_LIMIT)
    chunks = [_IssueChunk(text) for text in encoded_chunks]
    chunk_ranges: list[tuple[int, int]] = []
    offset = 0
    for chunk in chunks:
        chunk_ranges.append((offset, offset + len(chunk.text)))
        offset += len(chunk.text)

    for index, article_range in enumerate(ranges, 1):
        article_range.covering_chunks = tuple(
            chunk_index for chunk_index, (start, end) in enumerate(chunk_ranges)
            if start < article_range.end and article_range.start < end
        )
        if username:
            final_chunk = chunks[article_range.covering_chunks[-1]]
            if final_chunk.reply_markup is None:
                final_chunk.reply_markup = {"inline_keyboard": []}
            hash8 = article_range.full_hash[:8]
            final_chunk.reply_markup["inline_keyboard"].append([
                {"text": f"{index}👍", "url": f"https://t.me/{username}?start=vote_g_{hash8}"},
                {"text": f"{index}👎", "url": f"https://t.me/{username}?start=vote_b_{hash8}"},
            ])
    return chunks, ranges


def render_counter_signals(
    ranked_signals: list[Any], config: Any, irritator_status: IrritatorStatus | None = None,
) -> tuple[list[str], bool]:
    """Render a legacy supplement and its existing notification policy."""
    if not ranked_signals:
        if irritator_status is None:
            return [], False
        prefix = f"💢 {_labels(config)['irritator']}: "
        return (split_supplement(prefix + irritator_status.text, escape_markdownv2),
                irritator_status.level != "error")

    labels = _labels(config)
    language = getattr(getattr(config, "radar", None), "language", "en")
    text = "\n\n".join([
        f"💢🔥 {labels['irritator'].upper()} 🔥💢",
        *(signal_text(ranked, language) for ranked in ranked_signals),
    ])
    if irritator_status is not None:
        text += f"\n\nIrritator status: {irritator_status.level} — {irritator_status.text}"
    return split_supplement(text, escape_markdownv2), False


def _coverage_notice(full_source: bool, russian: bool) -> str:
    if full_source:
        return ('Охват ограничен выбранными отрывками полных статей; это не независимая проверка всех утверждений.'
                if russian else 'Limited coverage; selected full-source passages, not verification of every claim.')
    return ('Охват ограничен; RSS-выдержки, не полные статьи.' if russian
            else 'Limited coverage; RSS excerpts, not full articles.')


def render_post_delivery_supplement(
    result: EvidenceIrritatorResult, config: Any, *, full_source: bool, notice: str = "",
) -> list[str]:
    """Render the bounded post-delivery supplement without transport or state access."""
    russian = config.radar.language == 'ru'
    header = ('Ирритатор: отдельная проверка одного нарратива' if russian
              else 'Irritator: separate check of one narrative')
    outcome = {'complete': 'проверка выполнена', 'empty': 'проверка выполнена, контрсигналов не найдено',
               'incomplete': 'проверка неполная', 'error': 'проверка не выполнена'}
    status = outcome.get(result.status, 'проверка неполная') if russian else result.status
    lines = [header, status, _coverage_notice(full_source, russian)]
    if result.narratives:
        label = "Проверяем: " if russian else "Narrative checked: "
        lines.extend(label + narrative.claim for narrative in result.narratives)
    lines.extend(signal_text(ranked, config.radar.language) for ranked in result.ranked_signals)
    if notice:
        lines.append(notice)
    return split_supplement('\n\n'.join(lines), escape_markdownv2)
