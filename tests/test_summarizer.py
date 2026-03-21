"""Tests for src/summarizer.py"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.collector import Article
from src.config import (
    Config,
    DeliveryConfig,
    DigestConfig,
    LLMConfig,
    ProviderConfig,
    RouteConfig,
    SourceConfig,
)
from src.summarizer import (
    AnthropicProvider,
    GeminiProvider,
    OpenAICompatibleProvider,
    ProviderChain,
    build_category_prompt,
    build_prompt,
    build_trends_prompt,
    get_provider,
    resolve_category_providers,
)

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _make_config(
    provider: str = "anthropic",
    model: str = "claude-sonnet-4-20250514",
    language: str = "ru",
    summary_style: str = "analytical",
    routing: list[RouteConfig] | None = None,
) -> Config:
    return Config(
        llm=LLMConfig(
            providers=[ProviderConfig(name=provider, model=model)],
            routing=routing or [],
        ),
        delivery=DeliveryConfig(telegram=False, markdown_to_repo=False, markdown_dir="digests"),
        digest=DigestConfig(
            language=language,
            max_articles_per_source=5,
            max_total_articles=30,
            summary_style=summary_style,
        ),
        sources=[
            SourceConfig(name="Test Source", url="https://example.com/rss", category="AI", enabled=True)
        ],
    )


def _make_article(
    title: str = "Test Article",
    link: str = "https://example.com/article",
    description: str = "A short description.",
    source: str = "Test Source",
    category: str = "AI",
    pub_date: datetime | None = None,
) -> Article:
    return Article(
        title=title,
        link=link,
        description=description,
        source=source,
        category=category,
        pub_date=pub_date or datetime(2026, 3, 15, 8, 0, tzinfo=timezone.utc),
    )


def _make_articles_by_category() -> dict[str, list[Article]]:
    return {
        "AI": [
            _make_article("AI Breakthrough", "https://example.com/ai1", "AI news 1.", "Source A"),
            _make_article("New LLM", "https://example.com/ai2", "AI news 2.", "Source B"),
        ],
        "Banking": [
            _make_article("Fintech Merger", "https://example.com/fin1", "Finance news.", "Source C", "Banking"),
        ],
    }


# ---------------------------------------------------------------------------
# build_prompt tests
# ---------------------------------------------------------------------------


class TestBuildPrompt:
    def test_contains_article_titles(self) -> None:
        articles = _make_articles_by_category()
        config = _make_config()
        prompt = build_prompt(articles, config)
        assert "AI Breakthrough" in prompt
        assert "New LLM" in prompt
        assert "Fintech Merger" in prompt

    def test_contains_category_headers(self) -> None:
        articles = _make_articles_by_category()
        config = _make_config()
        prompt = build_prompt(articles, config)
        assert "AI" in prompt
        assert "Banking" in prompt

    def test_contains_links(self) -> None:
        articles = _make_articles_by_category()
        config = _make_config()
        prompt = build_prompt(articles, config)
        assert "https://example.com/ai1" in prompt
        assert "https://example.com/fin1" in prompt

    def test_analytical_style_mentions_perspectives(self) -> None:
        articles = _make_articles_by_category()
        config = _make_config(summary_style="analytical")
        prompt = build_prompt(articles, config)
        assert "Оптимист" in prompt or "Optimist" in prompt

    def test_brief_style_no_perspectives(self) -> None:
        articles = _make_articles_by_category()
        config = _make_config(summary_style="brief")
        prompt = build_prompt(articles, config)
        assert "Оптимист" not in prompt
        assert "Optimist" not in prompt

    def test_detailed_style_mentions_perspectives(self) -> None:
        articles = _make_articles_by_category()
        config = _make_config(summary_style="detailed")
        prompt = build_prompt(articles, config)
        assert "Оптимист" in prompt or "Optimist" in prompt

    def test_english_language(self) -> None:
        articles = _make_articles_by_category()
        config = _make_config(language="en")
        prompt = build_prompt(articles, config)
        assert "analyst" in prompt.lower() or "Optimist" in prompt

    def test_russian_language(self) -> None:
        articles = _make_articles_by_category()
        config = _make_config(language="ru")
        prompt = build_prompt(articles, config)
        assert "аналитик" in prompt.lower()

    def test_empty_articles(self) -> None:
        config = _make_config(language="en")
        prompt = build_prompt({}, config)
        assert "0 articles" in prompt

    def test_article_count_in_prompt(self) -> None:
        articles = _make_articles_by_category()
        config = _make_config(language="en")
        prompt = build_prompt(articles, config)
        assert "3 articles" in prompt

    def test_article_count_in_russian_prompt(self) -> None:
        articles = _make_articles_by_category()
        config = _make_config(language="ru")
        prompt = build_prompt(articles, config)
        assert "3 статей" in prompt

    def test_article_without_pub_date(self) -> None:
        articles = {"AI": [_make_article(pub_date=None)]}
        # override pub_date to None
        articles["AI"][0] = Article(
            title="No Date", link="https://x.com", description="desc",
            source="S", category="AI", pub_date=None,
        )
        config = _make_config()
        prompt = build_prompt(articles, config)
        assert "No Date" in prompt  # title is still in prompt
        assert "unknown date" not in prompt  # date no longer included


# ---------------------------------------------------------------------------
# build_category_prompt tests
# ---------------------------------------------------------------------------


class TestBuildCategoryPrompt:
    def test_single_category_contains_articles(self) -> None:
        articles = [
            _make_article("AI Breakthrough", "https://example.com/ai1", "AI news 1."),
            _make_article("New LLM", "https://example.com/ai2", "AI news 2."),
        ]
        config = _make_config()
        prompt = build_category_prompt("AI", articles, config)
        assert "AI Breakthrough" in prompt
        assert "New LLM" in prompt
        assert "https://example.com/ai1" in prompt

    def test_contains_category_name(self) -> None:
        articles = [_make_article()]
        config = _make_config()
        prompt = build_category_prompt("Banking", articles, config)
        assert "Banking" in prompt

    def test_no_trends_instruction_in_prompt(self) -> None:
        articles = [_make_article()]
        config = _make_config(summary_style="analytical")
        prompt = build_category_prompt("AI", articles, config)
        # Per-category prompts must NOT contain a trends instruction
        assert "тренды дня" not in prompt.lower()
        assert "key trends of the day" not in prompt.lower()

    def test_brief_style_no_perspectives(self) -> None:
        articles = [_make_article()]
        config = _make_config(summary_style="brief")
        prompt = build_category_prompt("AI", articles, config)
        assert "Оптимист" not in prompt
        assert "Optimist" not in prompt

    def test_analytical_style_mentions_perspectives(self) -> None:
        articles = [_make_article()]
        config = _make_config(summary_style="analytical")
        prompt = build_category_prompt("AI", articles, config)
        assert "Оптимист" in prompt or "Optimist" in prompt

    def test_english_language(self) -> None:
        articles = [_make_article()]
        config = _make_config(language="en")
        prompt = build_category_prompt("AI", articles, config)
        assert "analyst" in prompt.lower()

    def test_article_count_in_header(self) -> None:
        articles = [_make_article(), _make_article("Second", "https://example.com/2", "desc2")]
        config = _make_config(language="en")
        prompt = build_category_prompt("AI", articles, config)
        assert "2" in prompt


# ---------------------------------------------------------------------------
# build_trends_prompt tests
# ---------------------------------------------------------------------------


class TestBuildTrendsPrompt:
    def test_aggregation_contains_all_summaries(self) -> None:
        category_summaries = {
            "AI": "AI is advancing rapidly.",
            "Banking": "Fintech is consolidating.",
        }
        config = _make_config()
        prompt = build_trends_prompt(category_summaries, config)
        assert "AI is advancing rapidly." in prompt
        assert "Fintech is consolidating." in prompt

    def test_contains_trends_instruction_ru(self) -> None:
        category_summaries = {"AI": "summary"}
        config = _make_config(language="ru")
        prompt = build_trends_prompt(category_summaries, config)
        assert "тренд" in prompt.lower()

    def test_contains_trends_instruction_en(self) -> None:
        category_summaries = {"AI": "summary"}
        config = _make_config(language="en")
        prompt = build_trends_prompt(category_summaries, config)
        assert "trend" in prompt.lower()

    def test_category_names_as_headers(self) -> None:
        category_summaries = {"Banking & Fintech": "some content"}
        config = _make_config()
        prompt = build_trends_prompt(category_summaries, config)
        assert "Banking & Fintech" in prompt


# ---------------------------------------------------------------------------
# Provider selection tests
# ---------------------------------------------------------------------------


class TestGetProvider:
    def test_anthropic_provider(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        config = _make_config(provider="anthropic")
        chain = get_provider(config)
        assert isinstance(chain, ProviderChain)
        assert isinstance(chain._providers[0][1], AnthropicProvider)

    def test_gemini_provider(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        config = _make_config(provider="gemini", model="gemini-2.5-flash")
        chain = get_provider(config)
        assert isinstance(chain, ProviderChain)
        assert isinstance(chain._providers[0][1], GeminiProvider)

    def test_groq_provider(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GROQ_API_KEY", "test-key")
        config = _make_config(provider="groq", model="llama-3.3-70b-versatile")
        chain = get_provider(config)
        assert isinstance(chain, ProviderChain)
        assert isinstance(chain._providers[0][1], OpenAICompatibleProvider)

    def test_unknown_provider_raises(self) -> None:
        config = _make_config(provider="anthropic")
        # Bypass validation by mutating the providers list directly
        config.llm.providers[0] = ProviderConfig(name="openai", model="gpt-4")
        with pytest.raises(ValueError, match="Unknown"):
            get_provider(config)

    def test_missing_anthropic_key_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        with pytest.raises(EnvironmentError, match="ANTHROPIC_API_KEY"):
            AnthropicProvider(model="claude-sonnet-4-20250514")

    def test_missing_gemini_key_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        with pytest.raises(EnvironmentError, match="GEMINI_API_KEY"):
            GeminiProvider(model="gemini-2.5-flash")

    def test_missing_groq_key_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GROQ_API_KEY", raising=False)
        with pytest.raises(EnvironmentError, match="GROQ_API_KEY"):
            OpenAICompatibleProvider(provider_name="groq", model="llama-3.3-70b-versatile")


# ---------------------------------------------------------------------------
# AnthropicProvider.summarize tests
# ---------------------------------------------------------------------------


def _mock_response(status: int, body: dict, retry_after: str | None = None) -> MagicMock:
    response = MagicMock(spec=httpx.Response)
    response.status_code = status
    response.json.return_value = body
    response.raise_for_status = MagicMock()
    response.request = MagicMock()
    headers_mock = MagicMock()
    headers_mock.get.return_value = retry_after
    response.headers = headers_mock
    return response


class TestAnthropicProvider:
    @pytest.mark.asyncio
    async def test_successful_summarize(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        body = {"content": [{"text": "Summary result."}]}
        mock_response = _mock_response(200, body)

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            provider = AnthropicProvider(model="claude-sonnet-4-20250514")
            result = await provider.summarize("test prompt")

        assert result == "Summary result."

    @pytest.mark.asyncio
    async def test_auth_error_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "bad-key")
        mock_response = _mock_response(401, {"error": "Unauthorized"})

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            provider = AnthropicProvider(model="claude-sonnet-4-20250514")
            with pytest.raises(PermissionError, match="authentication failed"):
                await provider.summarize("test prompt")

    @pytest.mark.asyncio
    async def test_retry_on_timeout_then_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        body = {"content": [{"text": "Retry success."}]}
        success_response = _mock_response(200, body)

        call_count = 0

        async def mock_post(*args, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise httpx.TimeoutException("timed out")
            return success_response

        with patch("httpx.AsyncClient") as mock_client_cls, \
             patch("src.summarizer.asyncio.sleep", new=AsyncMock()) as mock_sleep:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.post = mock_post
            mock_client_cls.return_value = mock_client

            provider = AnthropicProvider(model="claude-sonnet-4-20250514")
            result = await provider.summarize("test prompt")

        assert result == "Retry success."
        assert call_count == 2
        mock_sleep.assert_called_once_with(1)  # 2**0

    @pytest.mark.asyncio
    async def test_fails_after_two_timeouts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

        async def mock_post(*args, **kwargs):  # type: ignore[no-untyped-def]
            raise httpx.TimeoutException("timed out")

        with patch("httpx.AsyncClient") as mock_client_cls, \
             patch("src.summarizer.asyncio.sleep", new=AsyncMock()):
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.post = mock_post
            mock_client_cls.return_value = mock_client

            provider = AnthropicProvider(model="claude-sonnet-4-20250514")
            with pytest.raises(RuntimeError, match="failed after 3 attempts"):
                await provider.summarize("test prompt")

    @pytest.mark.asyncio
    async def test_empty_content_array_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        body = {"content": []}
        mock_response = _mock_response(200, body)

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            provider = AnthropicProvider(model="claude-sonnet-4-20250514")
            with pytest.raises(RuntimeError, match="empty 'content'"):
                await provider.summarize("test prompt")

    @pytest.mark.asyncio
    async def test_retry_on_5xx(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        body = {"content": [{"text": "OK after retry."}]}
        error_response = _mock_response(503, {})
        success_response = _mock_response(200, body)

        call_count = 0

        async def mock_post(*args, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return error_response
            return success_response

        with patch("httpx.AsyncClient") as mock_client_cls, \
             patch("src.summarizer.asyncio.sleep", new=AsyncMock()) as mock_sleep:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.post = mock_post
            mock_client_cls.return_value = mock_client

            provider = AnthropicProvider(model="claude-sonnet-4-20250514")
            result = await provider.summarize("test prompt")

        assert result == "OK after retry."
        assert call_count == 2
        mock_sleep.assert_called_once_with(1)  # 2**0, no Retry-After header


# ---------------------------------------------------------------------------
# GeminiProvider.summarize tests
# ---------------------------------------------------------------------------


class TestGeminiProvider:
    @pytest.mark.asyncio
    async def test_successful_summarize(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        body = {"candidates": [{"content": {"parts": [{"text": "Gemini result."}]}}]}
        mock_response = _mock_response(200, body)

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            provider = GeminiProvider(model="gemini-2.5-flash")
            result = await provider.summarize("test prompt")

        assert result == "Gemini result."

    @pytest.mark.asyncio
    async def test_auth_error_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GEMINI_API_KEY", "bad-key")
        mock_response = _mock_response(403, {"error": "Forbidden"})

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            provider = GeminiProvider(model="gemini-2.5-flash")
            with pytest.raises(PermissionError, match="authentication failed"):
                await provider.summarize("test prompt")

    @pytest.mark.asyncio
    async def test_empty_candidates_array_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        body = {"candidates": []}
        mock_response = _mock_response(200, body)

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            provider = GeminiProvider(model="gemini-2.5-flash")
            with pytest.raises(RuntimeError, match="empty 'candidates'"):
                await provider.summarize("test prompt")

    def test_api_key_in_header_not_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GEMINI_API_KEY", "my-secret-key")
        provider = GeminiProvider(model="gemini-2.5-flash")
        url = provider.BASE_URL.format(model=provider.model)
        assert "my-secret-key" not in url
        assert "gemini-2.5-flash" in url
        assert provider._api_key == "my-secret-key"


# ---------------------------------------------------------------------------
# OpenAICompatibleProvider tests (replaces TestGroqProvider)
# ---------------------------------------------------------------------------


class TestOpenAICompatibleProvider:
    @pytest.mark.asyncio
    async def test_success_groq(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GROQ_API_KEY", "test-key")
        body = {"choices": [{"message": {"content": "Groq result."}}]}
        mock_response = _mock_response(200, body)

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            provider = OpenAICompatibleProvider(provider_name="groq", model="llama-3.3-70b-versatile")
            result = await provider.summarize("test prompt")

        assert result == "Groq result."

    @pytest.mark.asyncio
    async def test_success_mistral(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MISTRAL_API_KEY", "test-key")
        body = {"choices": [{"message": {"content": "Mistral result."}}]}
        mock_response = _mock_response(200, body)

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            provider = OpenAICompatibleProvider(provider_name="mistral", model="mistral-small")
            result = await provider.summarize("test prompt")

        assert result == "Mistral result."

    def test_missing_key_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GROQ_API_KEY", raising=False)
        with pytest.raises(EnvironmentError, match="GROQ_API_KEY"):
            OpenAICompatibleProvider(provider_name="groq", model="llama-3.3-70b-versatile")

    def test_missing_mistral_key_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
        with pytest.raises(EnvironmentError, match="MISTRAL_API_KEY"):
            OpenAICompatibleProvider(provider_name="mistral", model="mistral-small")

    def test_unknown_provider_name_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown OpenAI-compatible provider"):
            OpenAICompatibleProvider(provider_name="openai", model="gpt-4")

    @pytest.mark.asyncio
    async def test_bearer_token_in_headers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GROQ_API_KEY", "groq-secret")
        body = {"choices": [{"message": {"content": "ok"}}]}
        mock_response = _mock_response(200, body)
        captured_headers: dict = {}

        async def mock_post(url: str, headers: dict, json: dict) -> MagicMock:
            captured_headers.update(headers)
            return mock_response

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.post = mock_post
            mock_client_cls.return_value = mock_client

            provider = OpenAICompatibleProvider(provider_name="groq", model="llama-3.3-70b-versatile")
            await provider.summarize("test prompt")

        assert captured_headers.get("Authorization") == "Bearer groq-secret"

    @pytest.mark.asyncio
    async def test_empty_choices_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GROQ_API_KEY", "test-key")
        body = {"choices": []}
        mock_response = _mock_response(200, body)

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            provider = OpenAICompatibleProvider(provider_name="groq", model="llama-3.3-70b-versatile")
            with pytest.raises(RuntimeError, match="empty 'choices'"):
                await provider.summarize("test prompt")


# ---------------------------------------------------------------------------
# ProviderChain tests
# ---------------------------------------------------------------------------


class TestProviderChain:
    @pytest.mark.asyncio
    async def test_primary_succeeds_fallback_not_called(self) -> None:
        primary = AsyncMock()
        primary.summarize = AsyncMock(return_value="primary result")
        fallback = AsyncMock()
        fallback.summarize = AsyncMock(return_value="fallback result")

        chain = ProviderChain([("primary", primary), ("fallback", fallback)])
        result = await chain.summarize("prompt")

        assert result == "primary result"
        primary.summarize.assert_called_once()
        fallback.summarize.assert_not_called()
        assert chain.last_provider == "primary"
        assert chain.last_attempts == 1

    @pytest.mark.asyncio
    async def test_fallback_used_on_primary_failure(self) -> None:
        primary = AsyncMock()
        primary.summarize = AsyncMock(side_effect=RuntimeError("primary failed"))
        fallback = AsyncMock()
        fallback.summarize = AsyncMock(return_value="fallback result")

        chain = ProviderChain([("primary", primary), ("fallback", fallback)])
        result = await chain.summarize("prompt")

        assert result == "fallback result"
        assert chain.last_provider == "fallback"
        assert chain.last_attempts == 2

    @pytest.mark.asyncio
    async def test_all_providers_fail_raises(self) -> None:
        p1 = AsyncMock()
        p1.summarize = AsyncMock(side_effect=RuntimeError("p1 failed"))
        p2 = AsyncMock()
        p2.summarize = AsyncMock(side_effect=RuntimeError("p2 failed"))

        chain = ProviderChain([("p1", p1), ("p2", p2)])
        with pytest.raises(RuntimeError, match="All 2 provider"):
            await chain.summarize("prompt")

    @pytest.mark.asyncio
    async def test_single_provider_chain(self) -> None:
        provider = AsyncMock()
        provider.summarize = AsyncMock(return_value="single result")

        chain = ProviderChain([("only", provider)])
        result = await chain.summarize("prompt")
        assert result == "single result"
        assert chain.last_attempts == 1


# ---------------------------------------------------------------------------
# Sprint 5: empty summary and Gemini safety filter
# ---------------------------------------------------------------------------


class TestEmptySummaryRaises:
    @pytest.mark.asyncio
    async def test_empty_summary_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """_post_with_retry raises RuntimeError when LLM returns whitespace-only text."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        body = {"content": [{"text": "   "}]}
        mock_response = _mock_response(200, body)

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            provider = AnthropicProvider(model="claude-sonnet-4-20250514")
            with pytest.raises(RuntimeError, match="empty summary"):
                await provider.summarize("test prompt")


class TestGeminiSafetyFilter:
    @pytest.mark.asyncio
    async def test_gemini_safety_filter_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """GeminiProvider raises RuntimeError when finishReason is SAFETY."""
        monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
        body = {
            "candidates": [
                {
                    "finishReason": "SAFETY",
                    "content": {"parts": [{"text": ""}]},
                }
            ]
        }
        mock_response = _mock_response(200, body)

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            provider = GeminiProvider(model="gemini-2.5-flash")
            with pytest.raises(RuntimeError, match="SAFETY"):
                await provider.summarize("test prompt")

    @pytest.mark.asyncio
    async def test_gemini_max_tokens_returns_partial(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GeminiProvider returns partial text with a warning when finishReason is MAX_TOKENS."""
        monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
        partial_text = "Partial digest content that was cut off"
        body = {
            "candidates": [
                {
                    "finishReason": "MAX_TOKENS",
                    "content": {"parts": [{"text": partial_text}]},
                }
            ]
        }
        mock_response = _mock_response(200, body)

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            provider = GeminiProvider(model="gemini-2.5-flash")
            result = await provider.summarize("test prompt")
            assert result == partial_text


# ---------------------------------------------------------------------------
# resolve_category_providers tests
# ---------------------------------------------------------------------------


class TestResolveCategoryProviders:
    def test_unrouted_uses_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        config = _make_config(provider="anthropic")
        result = resolve_category_providers(["AI", "Banking"], config)
        assert "AI" in result
        assert "Banking" in result
        assert isinstance(result["AI"], ProviderChain)

    def test_routing_assigns_separate_chain(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        monkeypatch.setenv("GEMINI_API_KEY", "gemini-key")
        config = _make_config(
            provider="anthropic",
            routing=[
                RouteConfig(categories=["AI"], provider="gemini", model="gemini-2.5-flash"),
            ],
        )
        result = resolve_category_providers(["AI", "Banking"], config)
        assert isinstance(result["AI"], ProviderChain)
        # Routed AI should have gemini as primary
        ai_chain = result["AI"]
        assert ai_chain._providers[0][0] == "gemini"
        # Unrouted Banking uses default (anthropic)
        assert isinstance(result["Banking"], ProviderChain)

    def test_unrouted_category_uses_default_chain(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        monkeypatch.setenv("GEMINI_API_KEY", "gemini-key")
        config = _make_config(
            provider="anthropic",
            routing=[
                RouteConfig(categories=["AI"], provider="gemini", model="gemini-2.5-flash"),
            ],
        )
        result = resolve_category_providers(["AI", "Banking"], config)
        banking_chain = result["Banking"]
        assert banking_chain._providers[0][0] == "anthropic"

    def test_missing_key_for_routed_provider_falls_back(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        monkeypatch.delenv("GROQ_API_KEY", raising=False)
        config = _make_config(
            provider="anthropic",
            routing=[
                RouteConfig(categories=["AI"], provider="groq", model="llama-3.3-70b-versatile"),
            ],
        )
        result = resolve_category_providers(["AI"], config)
        # Should fall back to default (anthropic) chain
        ai_chain = result["AI"]
        assert ai_chain._providers[0][0] == "anthropic"
