"""LLM summarization for the radar pipeline."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field

from src._sanitize import sanitize_article as _sanitize_article
from src.config import Config
from src.llm import LLMRole, complete
from src.radar.collector import Article

logger = logging.getLogger(__name__)


@dataclass
class ArticleSummary:
    """Per-article LLM summary used for individual Telegram posts."""

    title: str
    link: str
    source: str
    category: str
    summary: str


@dataclass
class CategorySummary:
    category: str
    summary_text: str
    article_count: int
    article_summaries: list[ArticleSummary] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------

PROMPT_TEMPLATES: dict[str, dict[str, str]] = {
    "ru": {
        "header": "Дайджест сегодня содержит {total} статей по {categories} категориям.",
        "role": (
            "Ты — аналитик, который готовит ежедневный дайджест новостей "
            "для Technology Architect в крупном банке."
        ),
        "instructions_analytical": (
            "Сгруппируй новости по категориям. В каждой категории выбери 3-5 самых важных статей. "
            "Для каждой статьи дай аналитический комментарий в 1-2 предложения. "
            "Заголовок статьи уже содержит ссылку в формате [Заголовок](URL) — сохрани этот формат в выводе. "
            "НЕ добавляй отдельную строку Link:. НЕ дублируй URL в тексте ссылки.\n"
            "Для 1 наиболее значимой темы в каждой категории добавь блок из трёх перспектив:\n"
            "🟢 **Оптимист** — 1 предложение\n"
            "🔴 **Скептик** — 1 предложение\n"
            "⚖️ **Реалист** — 1 предложение\n"
            "Перспективы должны представлять принципиально разные аргументы, а не просто разный тон. "
            "Второстепенные новости получают обычный комментарий без перспектив. "
            "В конце добавь раздел «Ключевые тренды дня» — 2-3 пункта, по 1 предложению каждый. "
            "Используй ## для заголовков категорий. Используй эмодзи для категорий. Пропускай нерелевантные новости."
        ),
        "instructions_analytical_no_persp": (
            "Сгруппируй новости по категориям. В каждой категории выбери 3-5 самых важных статей. "
            "Для каждой статьи дай аналитический комментарий в 1-2 предложения. "
            "Заголовок статьи уже содержит ссылку в формате [Заголовок](URL) — сохрани этот формат в выводе. "
            "НЕ добавляй отдельную строку Link:. НЕ дублируй URL в тексте ссылки. "
            "В конце добавь раздел «Ключевые тренды дня» — 2-3 пункта, по 1 предложению каждый. "
            "Используй ## для заголовков категорий. Используй эмодзи для категорий. Пропускай нерелевантные новости."
        ),
        "instructions_brief": (
            "Сгруппируй новости по категориям. Для каждой статьи дай одно предложение-комментарий. "
            "Заголовок статьи уже содержит ссылку в формате [Заголовок](URL) — сохрани этот формат. "
            "НЕ добавляй отдельную строку Link:. "
            "Используй ## для заголовков категорий. Используй markdown-форматирование. Никаких перспектив."
        ),
        "instructions_detailed": (
            "Сгруппируй новости по категориям. В каждой категории выбери 3-5 самых важных статей. "
            "Для каждой статьи дай развёрнутый аналитический комментарий с полным контекстом. "
            "Заголовок статьи уже содержит ссылку в формате [Заголовок](URL) — сохрани этот формат в выводе. "
            "НЕ добавляй отдельную строку Link:. НЕ дублируй URL в тексте ссылки.\n"
            "Для КАЖДОЙ значимой темы добавь блок из трёх перспектив:\n"
            "🟢 **Оптимист** — 1 предложение\n"
            "🔴 **Скептик** — 1 предложение\n"
            "⚖️ **Реалист** — 1 предложение\n"
            "Перспективы должны представлять принципиально разные аргументы, а не просто разный тон. "
            "В конце добавь раздел «Ключевые тренды дня» — 2-3 пункта, по 1 предложению каждый. "
            "Используй ## для заголовков категорий. Используй эмодзи для категорий. Пропускай нерелевантные новости."
        ),
        "instructions_detailed_no_persp": (
            "Сгруппируй новости по категориям. В каждой категории выбери 3-5 самых важных статей. "
            "Для каждой статьи дай развёрнутый аналитический комментарий с полным контекстом. "
            "Заголовок статьи уже содержит ссылку в формате [Заголовок](URL) — сохрани этот формат в выводе. "
            "НЕ добавляй отдельную строку Link:. НЕ дублируй URL в тексте ссылки. "
            "В конце добавь раздел «Ключевые тренды дня» — 2-3 пункта, по 1 предложению каждый. "
            "Используй ## для заголовков категорий. Используй эмодзи для категорий. Пропускай нерелевантные новости."
        ),
        "instructions_category_analytical": (
            "НЕ добавляй приветствия, вводные фразы или заключения. "
            "Начни сразу с заголовка категории в формате ## Emoji Название.\n"
            "Выбери 3-5 самых важных статей. "
            "Для каждой статьи дай аналитический комментарий в 1-2 предложения. "
            "Заголовок статьи уже содержит ссылку в формате [Заголовок](URL) — сохрани этот формат в выводе. "
            "НЕ добавляй отдельную строку Link:. НЕ дублируй URL в тексте ссылки.\n"
            "Для 1 наиболее значимой темы добавь блок из трёх перспектив:\n"
            "🟢 **Оптимист** — 1 предложение\n"
            "🔴 **Скептик** — 1 предложение\n"
            "⚖️ **Реалист** — 1 предложение\n"
            "Перспективы должны представлять принципиально разные аргументы, а не просто разный тон. "
            "Второстепенные новости получают обычный комментарий без перспектив. "
            "НЕ добавляй раздел трендов."
        ),
        "instructions_category_analytical_no_persp": (
            "НЕ добавляй приветствия, вводные фразы или заключения. "
            "Начни сразу с заголовка категории в формате ## Emoji Название.\n"
            "Выбери 3-5 самых важных статей. "
            "Для каждой статьи дай аналитический комментарий в 1-2 предложения. "
            "Заголовок статьи уже содержит ссылку в формате [Заголовок](URL) — сохрани этот формат в выводе. "
            "НЕ добавляй отдельную строку Link:. НЕ дублируй URL в тексте ссылки. "
            "НЕ добавляй раздел трендов."
        ),
        "instructions_category_brief": (
            "НЕ добавляй приветствия, вводные фразы или заключения. "
            "Начни сразу с заголовка категории в формате ## Emoji Название.\n"
            "Для каждой статьи дай одно предложение-комментарий. "
            "Заголовок статьи уже содержит ссылку в формате [Заголовок](URL) — сохрани этот формат. "
            "НЕ добавляй отдельную строку Link:. "
            "Используй markdown-форматирование. Никаких перспектив. НЕ добавляй раздел трендов."
        ),
        "instructions_category_detailed": (
            "НЕ добавляй приветствия, вводные фразы или заключения. "
            "Начни сразу с заголовка категории в формате ## Emoji Название.\n"
            "Выбери 3-5 самых важных статей. "
            "Для каждой статьи дай развёрнутый аналитический комментарий с полным контекстом. "
            "Заголовок статьи уже содержит ссылку в формате [Заголовок](URL) — сохрани этот формат в выводе. "
            "НЕ добавляй отдельную строку Link:. НЕ дублируй URL в тексте ссылки.\n"
            "Для КАЖДОЙ значимой темы добавь блок из трёх перспектив:\n"
            "🟢 **Оптимист** — 1 предложение\n"
            "🔴 **Скептик** — 1 предложение\n"
            "⚖️ **Реалист** — 1 предложение\n"
            "Перспективы должны представлять принципиально разные аргументы, а не просто разный тон. "
            "НЕ добавляй раздел трендов."
        ),
        "instructions_category_detailed_no_persp": (
            "НЕ добавляй приветствия, вводные фразы или заключения. "
            "Начни сразу с заголовка категории в формате ## Emoji Название.\n"
            "Выбери 3-5 самых важных статей. "
            "Для каждой статьи дай развёрнутый аналитический комментарий с полным контекстом. "
            "Заголовок статьи уже содержит ссылку в формате [Заголовок](URL) — сохрани этот формат в выводе. "
            "НЕ добавляй отдельную строку Link:. НЕ дублируй URL в тексте ссылки. "
            "НЕ добавляй раздел трендов."
        ),
        "instructions_trends": (
            "На основе саммари по категориям выдели 2-3 ключевых тренда дня. "
            "Каждый тренд — 1 предложение. Используй маркированный список. "
            "Озаглавь раздел «## Ключевые тренды дня»."
        ),
        "category_header": "Категория «{category}» содержит {count} статей.",
    },
    "en": {
        "header": "Today's digest contains {total} articles across {categories} categories.",
        "role": (
            "You are an analyst preparing a daily news digest "
            "for a Technology Architect at a major bank."
        ),
        "instructions_analytical": (
            "Group news by category. Within each category pick 3-5 most important articles. "
            "For each article provide a 1-2 sentence analytical comment. "
            "Article titles already contain links in [Title](URL) format — preserve this format in output. "
            "Do NOT add a separate Link: line. Do NOT duplicate the URL in link text.\n"
            "For 1 most significant topic in each category add a block of three perspectives:\n"
            "🟢 **Optimist** — 1 sentence\n"
            "🔴 **Skeptic** — 1 sentence\n"
            "⚖️ **Realist** — 1 sentence\n"
            "Perspectives must represent genuinely different reasoning, not just tonal variation. "
            "Minor news items get a regular comment without perspectives. "
            "Add a 'Key Trends of the Day' section at the end — 2-3 bullet points, 1 sentence each. "
            "Use ## for category headers. Use emoji for categories. Skip irrelevant news."
        ),
        "instructions_analytical_no_persp": (
            "Group news by category. Within each category pick 3-5 most important articles. "
            "For each article provide a 1-2 sentence analytical comment. "
            "Article titles already contain links in [Title](URL) format — preserve this format in output. "
            "Do NOT add a separate Link: line. Do NOT duplicate the URL in link text. "
            "Add a 'Key Trends of the Day' section at the end — 2-3 bullet points, 1 sentence each. "
            "Use ## for category headers. Use emoji for categories. Skip irrelevant news."
        ),
        "instructions_brief": (
            "Group news by category. For each article write one sentence comment. "
            "Article titles already contain links in [Title](URL) format — preserve this format. "
            "Do NOT add a separate Link: line. "
            "Use ## for category headers. Use markdown formatting. No perspectives."
        ),
        "instructions_detailed": (
            "Group news by category. Within each category pick 3-5 most important articles. "
            "For each article provide a detailed analytical comment with full context. "
            "Article titles already contain links in [Title](URL) format — preserve this format in output. "
            "Do NOT add a separate Link: line. Do NOT duplicate the URL in link text.\n"
            "For EVERY significant topic add a block of three perspectives:\n"
            "🟢 **Optimist** — 1 sentence\n"
            "🔴 **Skeptic** — 1 sentence\n"
            "⚖️ **Realist** — 1 sentence\n"
            "Perspectives must represent genuinely different reasoning, not just tonal variation. "
            "Add a 'Key Trends of the Day' section at the end — 2-3 bullet points, 1 sentence each. "
            "Use ## for category headers. Use emoji for categories. Skip irrelevant news."
        ),
        "instructions_detailed_no_persp": (
            "Group news by category. Within each category pick 3-5 most important articles. "
            "For each article provide a detailed analytical comment with full context. "
            "Article titles already contain links in [Title](URL) format — preserve this format in output. "
            "Do NOT add a separate Link: line. Do NOT duplicate the URL in link text. "
            "Add a 'Key Trends of the Day' section at the end — 2-3 bullet points, 1 sentence each. "
            "Use ## for category headers. Use emoji for categories. Skip irrelevant news."
        ),
        "instructions_category_analytical": (
            "Do NOT add greetings, introductory phrases, or conclusions. "
            "Start directly with the category header in format ## Emoji Name.\n"
            "Pick 3-5 most important articles. "
            "For each article provide a 1-2 sentence analytical comment. "
            "Article titles already contain links in [Title](URL) format — preserve this format in output. "
            "Do NOT add a separate Link: line. Do NOT duplicate the URL in link text.\n"
            "For 1 most significant topic add a block of three perspectives:\n"
            "🟢 **Optimist** — 1 sentence\n"
            "🔴 **Skeptic** — 1 sentence\n"
            "⚖️ **Realist** — 1 sentence\n"
            "Perspectives must represent genuinely different reasoning, not just tonal variation. "
            "Minor news items get a regular comment without perspectives. "
            "Do NOT add a trends section."
        ),
        "instructions_category_analytical_no_persp": (
            "Do NOT add greetings, introductory phrases, or conclusions. "
            "Start directly with the category header in format ## Emoji Name.\n"
            "Pick 3-5 most important articles. "
            "For each article provide a 1-2 sentence analytical comment. "
            "Article titles already contain links in [Title](URL) format — preserve this format in output. "
            "Do NOT add a separate Link: line. Do NOT duplicate the URL in link text. "
            "Do NOT add a trends section."
        ),
        "instructions_category_brief": (
            "Do NOT add greetings, introductory phrases, or conclusions. "
            "Start directly with the category header in format ## Emoji Name.\n"
            "For each article write one sentence comment. "
            "Article titles already contain links in [Title](URL) format — preserve this format. "
            "Do NOT add a separate Link: line. "
            "Use markdown formatting. No perspectives. Do NOT add a trends section."
        ),
        "instructions_category_detailed": (
            "Do NOT add greetings, introductory phrases, or conclusions. "
            "Start directly with the category header in format ## Emoji Name.\n"
            "Pick 3-5 most important articles. "
            "For each article provide a detailed analytical comment with full context. "
            "Article titles already contain links in [Title](URL) format — preserve this format in output. "
            "Do NOT add a separate Link: line. Do NOT duplicate the URL in link text.\n"
            "For EVERY significant topic add a block of three perspectives:\n"
            "🟢 **Optimist** — 1 sentence\n"
            "🔴 **Skeptic** — 1 sentence\n"
            "⚖️ **Realist** — 1 sentence\n"
            "Perspectives must represent genuinely different reasoning, not just tonal variation. "
            "Do NOT add a trends section."
        ),
        "instructions_category_detailed_no_persp": (
            "Do NOT add greetings, introductory phrases, or conclusions. "
            "Start directly with the category header in format ## Emoji Name.\n"
            "Pick 3-5 most important articles. "
            "For each article provide a detailed analytical comment with full context. "
            "Article titles already contain links in [Title](URL) format — preserve this format in output. "
            "Do NOT add a separate Link: line. Do NOT duplicate the URL in link text. "
            "Do NOT add a trends section."
        ),
        "instructions_trends": (
            "Based on the category summaries below, identify 2-3 key trends of the day. "
            "Each trend is 1 sentence. Use a bulleted list. "
            "Title the section '## Key Trends of the Day'."
        ),
        "category_header": "Category '{category}' contains {count} articles.",
    },
}

# ---------------------------------------------------------------------------
# Per-article JSON prompt (used for Telegram card delivery)
# ---------------------------------------------------------------------------

_PER_ARTICLE_INSTRUCTIONS: dict[str, str] = {
    "ru": (
        "Из списка статей ниже выбери самые важные и интересные для "
        "Technology Architect в крупном банке. "
        "Для каждой выбранной статьи напиши саммари в 2-3 предложения. "
        "Саммари должно объяснять почему это важно, а не просто пересказывать заголовок.\n"
        "Ответь ТОЛЬКО валидным JSON-массивом (без markdown-обёртки), "
        "где каждый элемент:\n"
        '{{"title": "оригинальный заголовок", "link": "url", '
        '"source": "название источника", "summary": "саммари 2-3 предложения"}}\n'
        "Выбери не более {max_articles} самых важных статей из всех категорий."
    ),
    "en": (
        "From the articles below, pick the most important and interesting ones "
        "for a Technology Architect at a major bank. "
        "For each picked article write a 2-3 sentence summary. "
        "The summary should explain why it matters, not just restate the headline.\n"
        "Reply with ONLY a valid JSON array (no markdown wrapping), "
        "where each element is:\n"
        '{{"title": "original title", "link": "url", '
        '"source": "source name", "summary": "2-3 sentence summary"}}\n'
        "Pick at most {max_articles} most important articles across all categories."
    ),
}


def _parse_article_summaries(
    text: str, category: str,
) -> list[ArticleSummary]:
    """Parse JSON array of article summaries from LLM response."""
    # Strip markdown code fences if present
    cleaned = re.sub(r"^```(?:json)?\s*", "", text.strip())
    cleaned = re.sub(r"\s*```$", "", cleaned)

    try:
        items = json.loads(cleaned)
    except json.JSONDecodeError:
        # Try to find JSON array in the response
        match = re.search(r"\[.*\]", cleaned, re.DOTALL)
        if match:
            items = json.loads(match.group())
        else:
            logger.error("Failed to parse article summaries JSON for '%s'", category)
            return []

    if not isinstance(items, list):
        return []

    result: list[ArticleSummary] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title = item.get("title", "")
        link = item.get("link", "")
        source = item.get("source", "")
        summary = item.get("summary", "")
        if title and link and summary:
            result.append(ArticleSummary(
                title=title, link=link, source=source,
                category=category, summary=summary,
            ))
    return result


def build_per_article_prompt(
    all_articles: dict[str, list[Article]],
    config: Config,
    max_articles: int = 7,
) -> list[dict[str, str]]:
    """Build LLM prompt that selects and summarizes top articles as JSON."""
    lang = config.radar.language
    tmpl = PROMPT_TEMPLATES.get(lang, PROMPT_TEMPLATES["ru"])
    role = tmpl["role"]
    instructions = _PER_ARTICLE_INSTRUCTIONS.get(
        lang, _PER_ARTICLE_INSTRUCTIONS["ru"],
    ).format(max_articles=max_articles)

    parts: list[str] = []
    for category, articles in all_articles.items():
        parts.append(f"\n## {category}\n")
        for art in articles:
            title, description, source = _sanitize_article(
                art.title, art.description, art.source,
            )
            parts.append(
                f"- [{title}]({art.link}) ({source})\n"
                f"  {description}\n"
            )

    return [
        {"role": "system", "content": role},
        {"role": "user", "content": f"{instructions}\n{''.join(parts)}"},
    ]


def build_category_prompt(
    category: str, articles: list[Article], config: Config
) -> list[dict[str, str]]:
    """Build LLM messages for a single category."""
    lang = config.radar.language
    style = config.radar.summary_style
    tmpl = PROMPT_TEMPLATES.get(lang, PROMPT_TEMPLATES["ru"])

    role = tmpl["role"]
    instructions_key = f"instructions_category_{style}"
    if not config.radar.perspectives:
        no_persp_key = f"instructions_category_{style}_no_persp"
        if no_persp_key in tmpl:
            instructions_key = no_persp_key
    instructions = tmpl.get(instructions_key, tmpl["instructions_category_analytical"])

    category_header_tmpl = tmpl.get(
        "category_header", PROMPT_TEMPLATES["en"]["category_header"]
    )
    category_header = category_header_tmpl.format(category=category, count=len(articles))

    articles_text_parts: list[str] = [f"\n## {category}\n"]
    for art in articles:
        title, description, source = _sanitize_article(art.title, art.description, art.source)
        articles_text_parts.append(
            f"- [{title}]({art.link}) ({source})\n"
            f"  {description}\n"
        )
    articles_text = "\n".join(articles_text_parts)

    user_content = f"{instructions}\n\n{category_header}\n{articles_text}"
    return [
        {"role": "system", "content": role},
        {"role": "user", "content": user_content},
    ]


def build_trends_prompt(
    category_summaries: dict[str, str], config: Config
) -> list[dict[str, str]]:
    """Build LLM messages for aggregating key trends from category summaries."""
    lang = config.radar.language
    tmpl = PROMPT_TEMPLATES.get(lang, PROMPT_TEMPLATES["ru"])

    role = tmpl["role"]
    instructions = tmpl["instructions_trends"]

    summaries_text = "\n\n".join(
        f"### {cat}\n{summary}" for cat, summary in category_summaries.items()
    )

    return [
        {"role": "system", "content": role},
        {"role": "user", "content": f"{instructions}\n\n{summaries_text}"},
    ]


async def summarize_all(
    articles_by_category: dict[str, list[Article]],
    config: Config,
) -> tuple[list[CategorySummary], str | None]:
    """Summarize all categories in parallel, then generate trends.

    Returns:
        A tuple of (category_summaries, trends_text). trends_text is None
        if there is only one category or if the trends call fails.
    """

    async def _summarize_category(category: str, articles: list[Article]) -> CategorySummary | None:
        messages = build_category_prompt(category, articles, config)
        try:
            text, _ = await complete(LLMRole.SUMMARIZE, messages, config, category=category)
            return CategorySummary(
                category=category,
                summary_text=text,
                article_count=len(articles),
            )
        except Exception as exc:
            logger.error("Failed to summarize category '%s': %s", category, exc)
            return None

    tasks = [
        _summarize_category(category, articles)
        for category, articles in articles_by_category.items()
    ]
    results = await asyncio.gather(*tasks)
    summaries = [s for s in results if s is not None]

    if not summaries:
        return [], None

    trends: str | None = None
    if len(summaries) > 1:
        summaries_dict = {s.category: s.summary_text for s in summaries}
        messages = build_trends_prompt(summaries_dict, config)
        try:
            trends, _ = await complete(LLMRole.SUMMARIZE, messages, config)
        except Exception as exc:
            logger.error("Failed to generate trends: %s", exc)

    return summaries, trends


async def pick_top_articles(
    articles_by_category: dict[str, list[Article]],
    config: Config,
    max_articles: int = 7,
) -> list[ArticleSummary]:
    """Ask LLM to pick and summarize top articles across all categories.

    Returns a flat list of :class:`ArticleSummary` sorted by the order
    the LLM chose (most important first).  Falls back to an empty list
    on any LLM / parse failure.
    """
    messages = build_per_article_prompt(articles_by_category, config, max_articles)
    try:
        text, _ = await complete(LLMRole.SUMMARIZE, messages, config)
    except Exception as exc:
        logger.error("Failed to pick top articles: %s", exc)
        return []

    parsed = _parse_article_summaries(text, "all")
    if not parsed:
        logger.warning("LLM returned no parseable article summaries")
    else:
        logger.info("LLM picked %d top articles", len(parsed))

    # Assign correct categories from the original articles
    link_to_category: dict[str, str] = {}
    for category, articles in articles_by_category.items():
        for art in articles:
            link_to_category[art.link] = category
    for a in parsed:
        if a.category == "all":
            a.category = link_to_category.get(a.link, "")

    return parsed[:max_articles]
