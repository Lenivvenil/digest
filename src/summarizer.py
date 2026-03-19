"""LLM summarization module with multi-provider support."""

from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from typing import Any

import httpx

from src.collector import Article
from src.config import Config

logger = logging.getLogger(__name__)

_RETRY_STATUSES = {429, 500, 502, 503, 504}

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
    },
}


def build_prompt(articles_by_category: dict[str, list[Article]], config: Config) -> str:
    """Build the LLM prompt from grouped articles and config settings."""
    lang = config.digest.language
    style = config.digest.summary_style
    tmpl = PROMPT_TEMPLATES.get(lang, PROMPT_TEMPLATES["ru"])

    role = tmpl["role"]
    instructions_key = f"instructions_{style}"
    instructions = tmpl.get(instructions_key, tmpl["instructions_analytical"])

    articles_text_parts: list[str] = []
    for category, articles in articles_by_category.items():
        articles_text_parts.append(f"\n## {category}\n")
        for art in articles:
            articles_text_parts.append(
                f"- [{art.title}]({art.link}) ({art.source})\n"
                f"  {art.description}\n"
            )

    articles_text = "\n".join(articles_text_parts)

    total = sum(len(v) for v in articles_by_category.values())
    header_tmpl = tmpl.get("header", PROMPT_TEMPLATES["en"]["header"])
    header = header_tmpl.format(total=total, categories=len(articles_by_category)) + "\n"

    return f"{role}\n\n{instructions}\n\n{header}\n{articles_text}"


class BaseLLMProvider(ABC):
    """Abstract base for LLM providers."""

    @abstractmethod
    async def summarize(self, prompt: str) -> str:
        """Send prompt to the LLM and return the generated text."""


class AnthropicProvider(BaseLLMProvider):
    """Calls the Anthropic Messages API."""

    API_URL = "https://api.anthropic.com/v1/messages"
    API_VERSION = "2023-06-01"
    MAX_TOKENS = 4096

    def __init__(self, model: str) -> None:
        self.model = model
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise EnvironmentError(
                "ANTHROPIC_API_KEY environment variable is not set. "
                "Get your key at https://console.anthropic.com/"
            )
        self._api_key = api_key

    async def summarize(self, prompt: str) -> str:
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.MAX_TOKENS,
            "messages": [{"role": "user", "content": prompt}],
        }
        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": self.API_VERSION,
            "content-type": "application/json",
        }
        return await _post_with_retry(self.API_URL, headers, payload, self._extract)

    @staticmethod
    def _extract(data: dict[str, Any]) -> str:
        return data["content"][0]["text"]


class GeminiProvider(BaseLLMProvider):
    """Calls the Google Gemini generateContent API."""

    BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

    def __init__(self, model: str) -> None:
        self.model = model
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise EnvironmentError(
                "GEMINI_API_KEY environment variable is not set. "
                "Get your key at https://aistudio.google.com/app/apikey"
            )
        self._api_key = api_key

    async def summarize(self, prompt: str) -> str:
        url = self.BASE_URL.format(model=self.model)
        payload: dict[str, Any] = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "maxOutputTokens": 8192,
                "temperature": 0.7,
            },
        }
        headers = {
            "content-type": "application/json",
            "x-goog-api-key": self._api_key,
        }
        return await _post_with_retry(url, headers, payload, self._extract)

    @staticmethod
    def _extract(data: dict[str, Any]) -> str:
        return data["candidates"][0]["content"]["parts"][0]["text"]


class GroqProvider(BaseLLMProvider):
    """Calls the Groq OpenAI-compatible chat completions API."""

    API_URL = "https://api.groq.com/openai/v1/chat/completions"

    def __init__(self, model: str) -> None:
        self.model = model
        api_key = os.environ.get("GROQ_API_KEY")
        if not api_key:
            raise EnvironmentError(
                "GROQ_API_KEY environment variable is not set. "
                "Get your key at https://console.groq.com/keys"
            )
        self._api_key = api_key

    async def summarize(self, prompt: str) -> str:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "content-type": "application/json",
        }
        return await _post_with_retry(self.API_URL, headers, payload, self._extract)

    @staticmethod
    def _extract(data: dict[str, Any]) -> str:
        return data["choices"][0]["message"]["content"]


async def _post_with_retry(
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    extract: Any,
) -> str:
    """POST to url, retry once on timeout or 5xx. Raise on auth errors."""
    last_exc: Exception | None = None
    async with httpx.AsyncClient(timeout=60.0) as client:
        for attempt in range(2):
            try:
                response = await client.post(url, headers=headers, json=payload)
            except httpx.TimeoutException as exc:
                logger.warning("LLM request timed out (attempt %d/2)", attempt + 1)
                last_exc = exc
                continue

            if response.status_code in {401, 403}:
                raise PermissionError(
                    f"LLM API authentication failed (HTTP {response.status_code}). "
                    "Check that your API key is correct and has not expired."
                )

            if response.status_code in _RETRY_STATUSES:
                logger.warning(
                    "LLM API returned HTTP %d (attempt %d/2)", response.status_code, attempt + 1
                )
                last_exc = httpx.HTTPStatusError(
                    f"HTTP {response.status_code}", request=response.request, response=response
                )
                continue

            if response.status_code >= 400:
                logger.error(
                    "LLM API error (HTTP %d): %s", response.status_code, response.text
                )
            response.raise_for_status()
            try:
                data: dict[str, Any] = response.json()
            except ValueError as exc:
                raise RuntimeError(
                    f"LLM API returned a non-JSON response (status {response.status_code}). "
                    f"The provider may be returning an HTML error page. Error: {exc}"
                ) from exc
            try:
                return extract(data)
            except (KeyError, IndexError, TypeError) as exc:
                raise RuntimeError(
                    f"Unexpected LLM response structure — the provider returned a 200 but "
                    f"the payload did not match the expected schema (possibly blocked or empty). "
                    f"Error: {exc}. Response: {data}"
                ) from exc

    raise RuntimeError(
        f"LLM request failed after 2 attempts. Last error: {last_exc}"
    )


def get_provider(config: Config) -> BaseLLMProvider:
    """Factory: returns the appropriate LLM provider based on config."""
    provider_name = config.llm.provider
    model = config.llm.model

    if provider_name == "anthropic":
        return AnthropicProvider(model=model)
    if provider_name == "gemini":
        return GeminiProvider(model=model)
    if provider_name == "groq":
        return GroqProvider(model=model)

    raise ValueError(
        f"Unknown LLM provider '{provider_name}'. "
        "Must be one of: anthropic, gemini, groq."
    )
