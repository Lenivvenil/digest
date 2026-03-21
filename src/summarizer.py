"""LLM summarization module with multi-provider support."""

from __future__ import annotations

import asyncio
import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

import httpx

from src._sanitize import sanitize_article as _sanitize_article
from src.collector import Article
from src.config import Config

logger = logging.getLogger(__name__)

_RETRY_STATUSES = {429, 500, 502, 503, 504}


class LLMTruncationError(RuntimeError):
    """Raised when an LLM response was cut off due to token limit.

    The provider returned a partial result (finishReason=MAX_TOKENS or
    equivalent). The caller should treat this as a failed summarization
    and fall back to another provider or skip the category.
    """


# ---------------------------------------------------------------------------
# Provider registry (OpenAI-compatible providers)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProviderMeta:
    base_url: str
    api_key_env: str
    doc_url: str


PROVIDER_REGISTRY: dict[str, ProviderMeta] = {
    "groq": ProviderMeta(
        base_url="https://api.groq.com/openai/v1/chat/completions",
        api_key_env="GROQ_API_KEY",
        doc_url="https://console.groq.com/keys",
    ),
    "mistral": ProviderMeta(
        base_url="https://api.mistral.ai/v1/chat/completions",
        api_key_env="MISTRAL_API_KEY",
        doc_url="https://console.mistral.ai/api-keys/",
    ),
    "deepseek": ProviderMeta(
        base_url="https://api.deepseek.com/chat/completions",
        api_key_env="DEEPSEEK_API_KEY",
        doc_url="https://platform.deepseek.com/api_keys",
    ),
}


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
        # Per-category prompt templates (no "trends" section — generated separately)
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
        # Per-category prompt templates (no "trends" section — generated separately)
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
        "instructions_trends": (
            "Based on the category summaries below, identify 2-3 key trends of the day. "
            "Each trend is 1 sentence. Use a bulleted list. "
            "Title the section '## Key Trends of the Day'."
        ),
        "category_header": "Category '{category}' contains {count} articles.",
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
            title, description, source = _sanitize_article(art.title, art.description, art.source)
            articles_text_parts.append(
                f"- [{title}]({art.link}) ({source})\n"
                f"  {description}\n"
            )

    articles_text = "\n".join(articles_text_parts)

    total = sum(len(v) for v in articles_by_category.values())
    header_tmpl = tmpl.get("header", PROMPT_TEMPLATES["en"]["header"])
    header = header_tmpl.format(total=total, categories=len(articles_by_category)) + "\n"

    return f"{role}\n\n{instructions}\n\n{header}\n{articles_text}"


def build_category_prompt(
    category: str, articles: list[Article], config: Config
) -> str:
    """Build the LLM prompt for a single category."""
    lang = config.digest.language
    style = config.digest.summary_style
    tmpl = PROMPT_TEMPLATES.get(lang, PROMPT_TEMPLATES["ru"])

    role = tmpl["role"]
    instructions_key = f"instructions_category_{style}"
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

    return f"{role}\n\n{instructions}\n\n{category_header}\n{articles_text}"


def build_trends_prompt(category_summaries: dict[str, str], config: Config) -> str:
    """Build the LLM prompt for aggregating key trends from category summaries."""
    lang = config.digest.language
    tmpl = PROMPT_TEMPLATES.get(lang, PROMPT_TEMPLATES["ru"])

    role = tmpl["role"]
    instructions = tmpl["instructions_trends"]

    summaries_text = "\n\n".join(
        f"### {cat}\n{summary}" for cat, summary in category_summaries.items()
    )

    return f"{role}\n\n{instructions}\n\n{summaries_text}"


# ---------------------------------------------------------------------------
# LLM provider abstractions
# ---------------------------------------------------------------------------


class BaseLLMProvider(ABC):
    """Abstract base for LLM providers."""

    @abstractmethod
    async def summarize(self, prompt: str) -> str:
        """Send prompt to the LLM and return the generated text."""


class AnthropicProvider(BaseLLMProvider):
    """Calls the Anthropic Messages API."""

    API_URL = "https://api.anthropic.com/v1/messages"
    API_VERSION = "2023-06-01"
    MAX_TOKENS = 8192

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
        blocks = data.get("content") or []
        if not blocks:
            raise RuntimeError("Anthropic API returned empty 'content' array")
        return blocks[0]["text"]


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
                "maxOutputTokens": 65536,
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
        candidates = data.get("candidates") or []
        if not candidates:
            raise RuntimeError("Gemini API returned empty 'candidates' array")
        candidate = candidates[0]
        finish_reason = candidate.get("finishReason", "STOP")
        if finish_reason == "MAX_TOKENS":
            text = candidate["content"]["parts"][0]["text"]
            raise LLMTruncationError(
                f"Gemini response was cut off at token limit (finishReason=MAX_TOKENS, "
                f"got {len(text)} chars). Use a model with a larger output token budget "
                f"or reduce the number of articles per category."
            )
        if finish_reason != "STOP":
            raise RuntimeError(
                f"Gemini generation stopped with finishReason={finish_reason!r} "
                f"(content may have been blocked by a safety filter). "
                f"Response: {data}"
            )
        return candidate["content"]["parts"][0]["text"]


class OpenAICompatibleProvider(BaseLLMProvider):
    """Calls any OpenAI-compatible chat completions API (Groq, Mistral, DeepSeek, etc.)."""

    def __init__(self, provider_name: str, model: str) -> None:
        meta = PROVIDER_REGISTRY.get(provider_name)
        if meta is None:
            raise ValueError(
                f"Unknown OpenAI-compatible provider '{provider_name}'. "
                f"Known providers: {', '.join(sorted(PROVIDER_REGISTRY))}."
            )
        self.provider_name = provider_name
        self.model = model
        self._api_url = meta.base_url
        api_key = os.environ.get(meta.api_key_env)
        if not api_key:
            raise EnvironmentError(
                f"{meta.api_key_env} environment variable is not set. "
                f"Get your key at {meta.doc_url}"
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
        return await _post_with_retry(self._api_url, headers, payload, self._extract)

    @staticmethod
    def _extract(data: dict[str, Any]) -> str:
        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError("OpenAI-compatible API returned empty 'choices' array")
        return choices[0]["message"]["content"]


class ProviderChain(BaseLLMProvider):
    """Cascading fallback: tries providers in order, uses first success."""

    def __init__(self, providers: list[tuple[str, BaseLLMProvider]]) -> None:
        self._providers = providers
        self.last_provider: str = ""
        self.last_attempts: int = 0

    async def summarize(self, prompt: str) -> str:
        errors: list[tuple[str, str]] = []
        for i, (name, provider) in enumerate(self._providers):
            try:
                result = await provider.summarize(prompt)
                self.last_provider = name
                self.last_attempts = i + 1
                if i > 0:
                    logger.warning("Primary provider failed, used fallback: %s", name)
                return result
            except Exception as exc:
                logger.warning("Provider %s failed: %s", name, exc)
                errors.append((name, str(exc)))
        error_details = "; ".join(f"{n}: {e}" for n, e in errors)
        raise RuntimeError(
            f"All {len(errors)} provider(s) failed: {error_details}"
        )


# ---------------------------------------------------------------------------
# Retry helper
# ---------------------------------------------------------------------------


_MAX_ATTEMPTS = 3


async def _post_with_retry(
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    extract: Any,
) -> str:
    """POST to url, retry up to 3 times on timeout or 5xx with exponential backoff.

    Reads Retry-After header on 429 responses. Raises on auth errors.
    """
    last_exc: Exception | None = None
    async with httpx.AsyncClient(timeout=60.0) as client:
        for attempt in range(_MAX_ATTEMPTS):
            try:
                response = await client.post(url, headers=headers, json=payload)
            except httpx.TimeoutException as exc:
                logger.warning(
                    "LLM request timed out (attempt %d/%d)", attempt + 1, _MAX_ATTEMPTS
                )
                last_exc = exc
                if attempt < _MAX_ATTEMPTS - 1:
                    await asyncio.sleep(2 ** attempt)
                continue

            if response.status_code in {401, 403}:
                raise PermissionError(
                    f"LLM API authentication failed (HTTP {response.status_code}). "
                    "Check that your API key is correct and has not expired."
                )

            if response.status_code in _RETRY_STATUSES:
                logger.warning(
                    "LLM API returned HTTP %d (attempt %d/%d)",
                    response.status_code,
                    attempt + 1,
                    _MAX_ATTEMPTS,
                )
                last_exc = httpx.HTTPStatusError(
                    f"HTTP {response.status_code}", request=response.request, response=response
                )
                if attempt < _MAX_ATTEMPTS - 1:
                    # Respect Retry-After header on 429; fall back to exponential backoff.
                    retry_after = response.headers.get("Retry-After")
                    if retry_after is not None:
                        try:
                            sleep_secs = float(retry_after)
                        except ValueError:
                            sleep_secs = 2 ** attempt
                    else:
                        sleep_secs = 2 ** attempt
                    await asyncio.sleep(sleep_secs)
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
                text = extract(data)
            except (KeyError, IndexError, TypeError) as exc:
                raise RuntimeError(
                    f"Unexpected LLM response structure — the provider returned a 200 but "
                    f"the payload did not match the expected schema (possibly blocked or empty). "
                    f"Error: {exc}. Response: {data}"
                ) from exc
            # Log token usage when available (Anthropic/Gemini/Groq/Mistral/DeepSeek all expose it).
            usage = data.get("usage") or data.get("usageMetadata") or {}
            input_t = (
                usage.get("input_tokens")
                or usage.get("prompt_tokens")
                or usage.get("promptTokenCount")
                or 0
            )
            output_t = (
                usage.get("output_tokens")
                or usage.get("completion_tokens")
                or usage.get("candidatesTokenCount")
                or 0
            )
            if input_t or output_t:
                logger.info(
                    "LLM tokens: in=%d out=%d total=%d", input_t, output_t, input_t + output_t
                )
            if not text.strip():
                raise RuntimeError("LLM returned empty summary")
            return text

    raise RuntimeError(
        f"LLM request failed after {_MAX_ATTEMPTS} attempts. Last error: {last_exc}"
    )


# ---------------------------------------------------------------------------
# Provider factory helpers
# ---------------------------------------------------------------------------


def _make_single_provider(name: str, model: str) -> BaseLLMProvider:
    """Create a single provider instance by name and model."""
    if name == "anthropic":
        return AnthropicProvider(model=model)
    if name == "gemini":
        return GeminiProvider(model=model)
    # groq, mistral, deepseek — all OpenAI-compatible
    if name in PROVIDER_REGISTRY:
        return OpenAICompatibleProvider(provider_name=name, model=model)
    raise ValueError(
        f"Unknown LLM provider '{name}'. "
        "Must be one of: anthropic, gemini, groq, mistral, deepseek."
    )


def get_provider(config: Config) -> ProviderChain:
    """Factory: returns a ProviderChain from the config providers list."""
    chain: list[tuple[str, BaseLLMProvider]] = []
    for pc in config.llm.providers:
        try:
            provider = _make_single_provider(pc.name, pc.model)
            chain.append((pc.name, provider))
        except EnvironmentError as exc:
            if len(config.llm.providers) == 1:
                # Only one provider configured — must succeed
                raise
            logger.warning(
                "Skipping provider '%s' (API key not set): %s", pc.name, exc
            )
    if not chain:
        raise EnvironmentError(
            "No LLM providers are available — all API keys are missing. "
            "Set at least one of: ANTHROPIC_API_KEY, GEMINI_API_KEY, GROQ_API_KEY, "
            "MISTRAL_API_KEY, DEEPSEEK_API_KEY."
        )
    return ProviderChain(chain)


def resolve_category_providers(
    categories: list[str],
    config: Config,
    default_chain: BaseLLMProvider | None = None,
) -> dict[str, BaseLLMProvider]:
    """Return a provider for each category, respecting routing config.

    Categories not listed in routing use the default ProviderChain.
    If a routed provider's API key is missing, falls back to the default chain.
    If *default_chain* is not provided it is built by calling get_provider(config).
    """
    if default_chain is None:
        default_chain = get_provider(config)

    # Build a lookup: category → RouteConfig
    route_map: dict[str, tuple[str, str]] = {}
    for route in config.llm.routing:
        for cat in route.categories:
            route_map[cat] = (route.provider, route.model)

    result: dict[str, BaseLLMProvider] = {}
    for cat in categories:
        if cat not in route_map:
            result[cat] = default_chain
            continue

        provider_name, model = route_map[cat]
        try:
            single = _make_single_provider(provider_name, model)
            # Wrap in a ProviderChain with default as fallback.
            # default_chain is always a ProviderChain (built by get_provider).
            default_providers = (
                list(default_chain._providers)  # type: ignore[union-attr]
                if isinstance(default_chain, ProviderChain)
                else []
            )
            result[cat] = ProviderChain([(provider_name, single)] + default_providers)
        except EnvironmentError as exc:
            logger.warning(
                "Routed provider '%s' for category '%s' has no API key (%s). "
                "Falling back to default chain.",
                provider_name,
                cat,
                exc,
            )
            result[cat] = default_chain

    return result
