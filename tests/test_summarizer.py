"""Tests for src/summarizer.py"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.collector import Article
from src.config import Config, DeliveryConfig, DigestConfig, LLMConfig, SourceConfig
from src.summarizer import (
    AnthropicProvider,
    GeminiProvider,
    GroqProvider,
    build_prompt,
    get_provider,
)

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _make_config(
    provider: str = "anthropic",
    model: str = "claude-sonnet-4-20250514",
    language: str = "ru",
    summary_style: str = "analytical",
) -> Config:
    return Config(
        llm=LLMConfig(provider=provider, model=model),
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
# Provider selection tests
# ---------------------------------------------------------------------------


class TestGetProvider:
    def test_anthropic_provider(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        config = _make_config(provider="anthropic")
        provider = get_provider(config)
        assert isinstance(provider, AnthropicProvider)

    def test_gemini_provider(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        config = _make_config(provider="gemini", model="gemini-2.5-flash")
        provider = get_provider(config)
        assert isinstance(provider, GeminiProvider)

    def test_groq_provider(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GROQ_API_KEY", "test-key")
        config = _make_config(provider="groq", model="llama-3.3-70b-versatile")
        provider = get_provider(config)
        assert isinstance(provider, GroqProvider)

    def test_unknown_provider_raises(self) -> None:
        config = _make_config(provider="openai")
        # bypass config validation by mutating directly
        config.llm.provider = "openai"
        with pytest.raises(ValueError, match="Unknown LLM provider"):
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
            GroqProvider(model="llama-3.3-70b-versatile")


# ---------------------------------------------------------------------------
# AnthropicProvider.summarize tests
# ---------------------------------------------------------------------------


def _mock_response(status: int, body: dict) -> MagicMock:
    response = MagicMock(spec=httpx.Response)
    response.status_code = status
    response.json.return_value = body
    response.raise_for_status = MagicMock()
    response.request = MagicMock()
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

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.post = mock_post
            mock_client_cls.return_value = mock_client

            provider = AnthropicProvider(model="claude-sonnet-4-20250514")
            result = await provider.summarize("test prompt")

        assert result == "Retry success."
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_fails_after_two_timeouts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

        async def mock_post(*args, **kwargs):  # type: ignore[no-untyped-def]
            raise httpx.TimeoutException("timed out")

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.post = mock_post
            mock_client_cls.return_value = mock_client

            provider = AnthropicProvider(model="claude-sonnet-4-20250514")
            with pytest.raises(RuntimeError, match="failed after 2 attempts"):
                await provider.summarize("test prompt")

    @pytest.mark.asyncio
    async def test_retry_on_5xx(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        body = {"content": [{"text": "OK after retry."}]}
        error_response = _mock_response(503, {})
        error_response.request = MagicMock()
        success_response = _mock_response(200, body)

        call_count = 0

        async def mock_post(*args, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return error_response
            return success_response

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.post = mock_post
            mock_client_cls.return_value = mock_client

            provider = AnthropicProvider(model="claude-sonnet-4-20250514")
            result = await provider.summarize("test prompt")

        assert result == "OK after retry."
        assert call_count == 2


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

    def test_api_key_in_header_not_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GEMINI_API_KEY", "my-secret-key")
        provider = GeminiProvider(model="gemini-2.5-flash")
        url = provider.BASE_URL.format(model=provider.model)
        assert "my-secret-key" not in url
        assert "gemini-2.5-flash" in url
        assert provider._api_key == "my-secret-key"


# ---------------------------------------------------------------------------
# GroqProvider.summarize tests
# ---------------------------------------------------------------------------


class TestGroqProvider:
    @pytest.mark.asyncio
    async def test_successful_summarize(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GROQ_API_KEY", "test-key")
        body = {"choices": [{"message": {"content": "Groq result."}}]}
        mock_response = _mock_response(200, body)

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            provider = GroqProvider(model="llama-3.3-70b-versatile")
            result = await provider.summarize("test prompt")

        assert result == "Groq result."

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

            provider = GroqProvider(model="llama-3.3-70b-versatile")
            await provider.summarize("test prompt")

        assert captured_headers.get("Authorization") == "Bearer groq-secret"
