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
        "role": (
            "Ты — аналитик, который готовит ежедневный дайджест новостей "
            "для Technology Architect в крупном банке."
        ),
        "instructions_analytical": (
            "Сгруппируй новости по категориям. В каждой категории выбери 3-5 самых важных статей. "
            "Для каждой статьи дай аналитический комментарий в 2-3 предложения, сохрани ссылку на источник. "
            "Для 1-2 наиболее значимых тем в каждой категории добавь блок из трёх перспектив:\n"
            "🟢 **Оптимист** — сильнейший аргумент «за», почему это прорыв или возможность (1-2 предложения)\n"
            "🔴 **Скептик** — сильнейший аргумент «против», риски, почему это может не сработать (1-2 предложения)\n"
            "⚖️ **Реалист** — взвешенная оценка, что скорее всего произойдёт на самом деле (1-2 предложения)\n"
            "Перспективы должны представлять принципиально разные аргументы, а не просто разный тон. "
            "Второстепенные новости получают обычный аналитический комментарий без перспектив. "
            "В конце добавь раздел «Ключевые тренды дня» с 3-5 трендами. "
            "Используй markdown-форматирование и эмодзи для категорий. Пропускай нерелевантные новости."
        ),
        "instructions_brief": (
            "Сгруппируй новости по категориям. Для каждой статьи дай одно предложение-комментарий, "
            "сохрани ссылку на источник. Используй markdown-форматирование. Никаких перспектив."
        ),
        "instructions_detailed": (
            "Сгруппируй новости по категориям. В каждой категории выбери 3-5 самых важных статей. "
            "Для каждой статьи дай развёрнутый аналитический комментарий с полным контекстом, "
            "сохрани ссылку на источник. "
            "Для КАЖДОЙ значимой темы добавь блок из трёх перспектив:\n"
            "🟢 **Оптимист** — сильнейший аргумент «за», почему это прорыв или возможность (1-2 предложения)\n"
            "🔴 **Скептик** — сильнейший аргумент «против», риски, почему это может не сработать (1-2 предложения)\n"
            "⚖️ **Реалист** — взвешенная оценка, что скорее всего произойдёт на самом деле (1-2 предложения)\n"
            "Перспективы должны представлять принципиально разные аргументы, а не просто разный тон. "
            "В конце добавь раздел «Ключевые тренды дня» с 3-5 трендами. "
            "Используй markdown-форматирование и эмодзи для категорий. Пропускай нерелевантные новости."
        ),
    },
    "en": {
        "role": (
            "You are an analyst preparing a daily news digest "
            "for a Technology Architect at a major bank."
        ),
        "instructions_analytical": (
            "Group news by category. Within each category pick 3-5 most important articles. "
            "For each article provide a 2-3 sentence analytical comment and preserve the source link. "
            "For 1-2 most significant topics in each category add a block of three perspectives:\n"
            "🟢 **Optimist** — the strongest argument in favor, why this is a breakthrough (1-2 sentences)\n"
            "🔴 **Skeptic** — the strongest argument against, risks, why this might fail (1-2 sentences)\n"
            "⚖️ **Realist** — the balanced middle-ground, what is most likely to happen (1-2 sentences)\n"
            "Perspectives must represent genuinely different reasoning, not just tonal variation. "
            "Minor news items get a regular analytical comment without perspectives. "
            "Add a 'Key Trends of the Day' section at the end with 3-5 trends. "
            "Use markdown formatting and emoji for categories. Skip irrelevant news."
        ),
        "instructions_brief": (
            "Group news by category. For each article write one sentence comment "
            "and preserve the source link. Use markdown formatting. No perspectives."
        ),
        "instructions_detailed": (
            "Group news by category. Within each category pick 3-5 most important articles. "
            "For each article provide a detailed analytical comment with full context "
            "and preserve the source link. "
            "For EVERY significant topic add a block of three perspectives:\n"
            "🟢 **Optimist** — the strongest argument in favor, why this is a breakthrough (1-2 sentences)\n"
            "🔴 **Skeptic** — the strongest argument against, risks, why this might fail (1-2 sentences)\n"
            "⚖️ **Realist** — the balanced middle-ground, what is most likely to happen (1-2 sentences)\n"
            "Perspectives must represent genuinely different reasoning, not just tonal variation. "
            "Add a 'Key Trends of the Day' section at the end with 3-5 trends. "
            "Use markdown formatting and emoji for categories. Skip irrelevant news."
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
            pub = art.pub_date.strftime("%Y-%m-%d %H:%M UTC") if art.pub_date else "unknown date"
            articles_text_parts.append(
                f"- **{art.title}** ({art.source}, {pub})\n"
                f"  {art.description}\n"
                f"  Link: {art.link}\n"
            )

    articles_text = "\n".join(articles_text_parts)

    total = sum(len(v) for v in articles_by_category.values())
    header = (
        f"Today's digest contains {total} articles across "
        f"{len(articles_by_category)} categories.\n"
    )

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
        url = self.BASE_URL.format(model=self.model) + f"?key={self._api_key}"
        payload: dict[str, Any] = {
            "contents": [{"parts": [{"text": prompt}]}],
        }
        headers = {"content-type": "application/json"}
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

            response.raise_for_status()
            data: dict[str, Any] = response.json()
            return extract(data)

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
