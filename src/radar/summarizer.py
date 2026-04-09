"""LLM summarization for the radar pipeline."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from src._sanitize import sanitize_article as _sanitize_article
from src.config import Config
from src.llm import LLMRole, complete
from src.radar.collector import Article

logger = logging.getLogger(__name__)


@dataclass
class CategorySummary:
    category: str
    summary_text: str
    article_count: int


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
            "Используй эмодзи для заголовка категории. НЕ добавляй раздел трендов."
        ),
        "instructions_category_analytical_no_persp": (
            "Выбери 3-5 самых важных статей. "
            "Для каждой статьи дай аналитический комментарий в 1-2 предложения. "
            "Заголовок статьи уже содержит ссылку в формате [Заголовок](URL) — сохрани этот формат в выводе. "
            "НЕ добавляй отдельную строку Link:. НЕ дублируй URL в тексте ссылки. "
            "Используй эмодзи для заголовка категории. НЕ добавляй раздел трендов."
        ),
        "instructions_category_brief": (
            "Для каждой статьи дай одно предложение-комментарий. "
            "Заголовок статьи уже содержит ссылку в формате [Заголовок](URL) — сохрани этот формат. "
            "НЕ добавляй отдельную строку Link:. "
            "Используй markdown-форматирование. Никаких перспектив. НЕ добавляй раздел трендов."
        ),
        "instructions_category_detailed": (
            "Выбери 3-5 самых важных статей. "
            "Для каждой статьи дай развёрнутый аналитический комментарий с полным контекстом. "
            "Заголовок статьи уже содержит ссылку в формате [Заголовок](URL) — сохрани этот формат в выводе. "
            "НЕ добавляй отдельную строку Link:. НЕ дублируй URL в тексте ссылки.\n"
            "Для КАЖДОЙ значимой темы добавь блок из трёх перспектив:\n"
            "🟢 **Оптимист** — 1 предложение\n"
            "🔴 **Скептик** — 1 предложение\n"
            "⚖️ **Реалист** — 1 предложение\n"
            "Перспективы должны представлять принципиально разные аргументы, а не просто разный тон. "
            "Используй эмодзи для заголовка категории. НЕ добавляй раздел трендов."
        ),
        "instructions_category_detailed_no_persp": (
            "Выбери 3-5 самых важных статей. "
            "Для каждой статьи дай развёрнутый аналитический комментарий с полным контекстом. "
            "Заголовок статьи уже содержит ссылку в формате [Заголовок](URL) — сохрани этот формат в выводе. "
            "НЕ добавляй отдельную строку Link:. НЕ дублируй URL в тексте ссылки. "
            "Используй эмодзи для заголовка категории. НЕ добавляй раздел трендов."
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
            "Use emoji for the category header. Do NOT add a trends section."
        ),
        "instructions_category_analytical_no_persp": (
            "Pick 3-5 most important articles. "
            "For each article provide a 1-2 sentence analytical comment. "
            "Article titles already contain links in [Title](URL) format — preserve this format in output. "
            "Do NOT add a separate Link: line. Do NOT duplicate the URL in link text. "
            "Use emoji for the category header. Do NOT add a trends section."
        ),
        "instructions_category_brief": (
            "For each article write one sentence comment. "
            "Article titles already contain links in [Title](URL) format — preserve this format. "
            "Do NOT add a separate Link: line. "
            "Use markdown formatting. No perspectives. Do NOT add a trends section."
        ),
        "instructions_category_detailed": (
            "Pick 3-5 most important articles. "
            "For each article provide a detailed analytical comment with full context. "
            "Article titles already contain links in [Title](URL) format — preserve this format in output. "
            "Do NOT add a separate Link: line. Do NOT duplicate the URL in link text.\n"
            "For EVERY significant topic add a block of three perspectives:\n"
            "🟢 **Optimist** — 1 sentence\n"
            "🔴 **Skeptic** — 1 sentence\n"
            "⚖️ **Realist** — 1 sentence\n"
            "Perspectives must represent genuinely different reasoning, not just tonal variation. "
            "Use emoji for the category header. Do NOT add a trends section."
        ),
        "instructions_category_detailed_no_persp": (
            "Pick 3-5 most important articles. "
            "For each article provide a detailed analytical comment with full context. "
            "Article titles already contain links in [Title](URL) format — preserve this format in output. "
            "Do NOT add a separate Link: line. Do NOT duplicate the URL in link text. "
            "Use emoji for the category header. Do NOT add a trends section."
        ),
        "instructions_trends": (
            "Based on the category summaries below, identify 2-3 key trends of the day. "
            "Each trend is 1 sentence. Use a bulleted list. "
            "Title the section '## Key Trends of the Day'."
        ),
        "category_header": "Category '{category}' contains {count} articles.",
    },
}


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
            text, _ = await complete(LLMRole.SUMMARIZE, messages, config)
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
