"""Tests for src/radar/summarizer.py"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

if TYPE_CHECKING:
    from src.radar.collector import Article

import pytest

from src.config import (
    Config,
    FiltersConfig,
    IrritatorConfig,
    LLMConfig,
    ObsidianConfig,
    ProviderConfig,
    RadarConfig,
    SourceConfig,
    TelegramConfig,
)
from src.radar.summarizer import (
    build_category_prompt,
    build_trends_prompt,
    summarize_all,
)
from tests.factories import make_article

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(
    language: str = "ru",
    summary_style: str = "analytical",
    perspectives: bool = False,
) -> Config:
    return Config(
        llm=LLMConfig(providers=[ProviderConfig(name="groq", model="llama-3.3-70b-versatile")]),
        radar=RadarConfig(
            language=language,
            summary_style=summary_style,
            perspectives=perspectives,
        ),
        irritator=IrritatorConfig(),
        sources=[SourceConfig(name="Test", url="https://example.com/feed", category="Tech", enabled=True)],
        filters=FiltersConfig(),
        telegram=TelegramConfig(enabled=False),
        obsidian=ObsidianConfig(enabled=False),
    )


def _make_article(
    title: str = "Test Article",
    link: str = "https://example.com/1",
    description: str = "Article description.",
    source: str = "TestSource",
    category: str = "Tech",
) -> Article:
    return make_article(title=title, link=link, description=description, source=source, category=category)


def _make_articles_by_category() -> dict[str, list[Article]]:
    return {
        "AI": [
            _make_article(title="AI Article One", category="AI"),
            _make_article(title="AI Article Two", link="https://example.com/2", category="AI"),
        ],
        "Banking": [
            _make_article(title="Banking Article", link="https://example.com/3", category="Banking"),
        ],
    }


# ---------------------------------------------------------------------------
# TestBuildCategoryPrompt
# ---------------------------------------------------------------------------


class TestBuildCategoryPrompt:
    def test_returns_system_and_user_messages(self) -> None:
        config = _make_config()
        articles = [_make_article()]
        messages = build_category_prompt("Tech", articles, config)
        assert len(messages) == 2
        assert messages[0]["role"] == "system"
        assert messages[1]["role"] == "user"

    def test_contains_category_name_and_articles(self) -> None:
        config = _make_config()
        articles = [_make_article(title="My Article", link="https://example.com/1")]
        messages = build_category_prompt("Tech", articles, config)
        user_content = messages[1]["content"]
        assert "Tech" in user_content
        assert "My Article" in user_content
        assert "https://example.com/1" in user_content

    def test_analytical_style_has_perspectives_instructions(self) -> None:
        config = _make_config(language="ru", summary_style="analytical", perspectives=True)
        articles = [_make_article()]
        messages = build_category_prompt("Tech", articles, config)
        user_content = messages[1]["content"]
        assert "Оптимист" in user_content or "Optimist" in user_content

    def test_brief_style_no_perspectives(self) -> None:
        config = _make_config(summary_style="brief", perspectives=True)
        articles = [_make_article()]
        messages = build_category_prompt("Tech", articles, config)
        user_content = messages[1]["content"]
        assert "Оптимист" not in user_content
        assert "Optimist" not in user_content

    def test_language_ru_uses_russian_templates(self) -> None:
        config = _make_config(language="ru")
        articles = [_make_article()]
        messages = build_category_prompt("Tech", articles, config)
        system_content = messages[0]["content"]
        assert "аналитик" in system_content.lower() or "дайджест" in system_content.lower()

    def test_language_en_uses_english_templates(self) -> None:
        config = _make_config(language="en")
        articles = [_make_article()]
        messages = build_category_prompt("Tech", articles, config)
        system_content = messages[0]["content"]
        assert "analyst" in system_content.lower() or "digest" in system_content.lower()

    def test_no_perspectives_flag_uses_no_persp_template(self) -> None:
        # perspectives=True adds perspectives block; perspectives=False must not
        config_with = _make_config(summary_style="analytical", perspectives=True)
        config_without = _make_config(summary_style="analytical", perspectives=False)
        articles = [_make_article()]
        msg_with = build_category_prompt("Tech", articles, config_with)
        msg_without = build_category_prompt("Tech", articles, config_without)
        # The no-persp variant should not contain perspective markers
        assert "Оптимист" not in msg_without[1]["content"]
        assert "Optimist" not in msg_without[1]["content"]
        # The normal variant should contain them
        assert "Оптимист" in msg_with[1]["content"] or "Optimist" in msg_with[1]["content"]


# ---------------------------------------------------------------------------
# TestBuildTrendsPrompt
# ---------------------------------------------------------------------------


class TestBuildTrendsPrompt:
    def test_returns_system_and_user_messages(self) -> None:
        config = _make_config()
        summaries = {"AI": "AI summary.", "Banking": "Banking summary."}
        messages = build_trends_prompt(summaries, config)
        assert len(messages) == 2
        assert messages[0]["role"] == "system"
        assert messages[1]["role"] == "user"

    def test_contains_category_summaries(self) -> None:
        config = _make_config()
        summaries = {"AI": "AI summary.", "Banking": "Banking summary."}
        messages = build_trends_prompt(summaries, config)
        user_content = messages[1]["content"]
        assert "AI" in user_content
        assert "AI summary." in user_content
        assert "Banking summary." in user_content

    def test_uses_trends_instructions_ru(self) -> None:
        config = _make_config(language="ru")
        summaries = {"AI": "AI summary."}
        messages = build_trends_prompt(summaries, config)
        user_content = messages[1]["content"]
        assert "тренд" in user_content.lower()

    def test_uses_trends_instructions_en(self) -> None:
        config = _make_config(language="en")
        summaries = {"AI": "AI summary."}
        messages = build_trends_prompt(summaries, config)
        user_content = messages[1]["content"]
        assert "trend" in user_content.lower()


# ---------------------------------------------------------------------------
# TestSummarizeAll
# ---------------------------------------------------------------------------


class TestSummarizeAll:
    @pytest.mark.asyncio
    async def test_success_two_categories(self) -> None:
        config = _make_config()
        articles_by_cat = _make_articles_by_category()

        mock_complete = AsyncMock(
            side_effect=[
                ("Summary for AI", {}),
                ("Summary for Banking", {}),
                ("Trends text", {}),
            ]
        )
        with patch("src.radar.summarizer.complete", mock_complete):
            summaries, trends = await summarize_all(articles_by_cat, config)

        assert len(summaries) == 2
        summary_texts = {s.category: s.summary_text for s in summaries}
        assert summary_texts["AI"] == "Summary for AI"
        assert summary_texts["Banking"] == "Summary for Banking"
        assert trends == "Trends text"

    @pytest.mark.asyncio
    async def test_article_counts_are_correct(self) -> None:
        config = _make_config()
        articles_by_cat = _make_articles_by_category()

        mock_complete = AsyncMock(
            side_effect=[
                ("Summary for AI", {}),
                ("Summary for Banking", {}),
                ("Trends text", {}),
            ]
        )
        with patch("src.radar.summarizer.complete", mock_complete):
            summaries, _ = await summarize_all(articles_by_cat, config)

        counts = {s.category: s.article_count for s in summaries}
        assert counts["AI"] == 2
        assert counts["Banking"] == 1

    @pytest.mark.asyncio
    async def test_partial_failure_one_category(self) -> None:
        config = _make_config()
        articles_by_cat = _make_articles_by_category()

        call_count = 0

        async def _side_effect(*args: object, **kwargs: object) -> tuple[str, dict[str, object]]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("LLM error")
            return ("Summary", {})

        mock_complete = AsyncMock(side_effect=_side_effect)
        with patch("src.radar.summarizer.complete", mock_complete):
            summaries, trends = await summarize_all(articles_by_cat, config)

        # Only one category succeeded, so trends should be None (len(summaries) <= 1)
        assert len(summaries) == 1
        assert trends is None

    @pytest.mark.asyncio
    async def test_all_categories_fail(self) -> None:
        config = _make_config()
        articles_by_cat = _make_articles_by_category()

        mock_complete = AsyncMock(side_effect=RuntimeError("LLM error"))
        with patch("src.radar.summarizer.complete", mock_complete):
            summaries, trends = await summarize_all(articles_by_cat, config)

        assert summaries == []
        assert trends is None

    @pytest.mark.asyncio
    async def test_single_category_no_trends(self) -> None:
        config = _make_config()
        articles_by_cat = {"AI": [_make_article(category="AI")]}

        mock_complete = AsyncMock(return_value=("Summary for AI", {}))
        with patch("src.radar.summarizer.complete", mock_complete):
            summaries, trends = await summarize_all(articles_by_cat, config)

        assert len(summaries) == 1
        assert trends is None
        # complete should be called exactly once (no trends call)
        assert mock_complete.call_count == 1

    @pytest.mark.asyncio
    async def test_trends_failure_returns_none(self) -> None:
        config = _make_config()
        articles_by_cat = _make_articles_by_category()

        call_count = 0

        async def _side_effect(*args: object, **kwargs: object) -> tuple[str, dict[str, object]]:
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                return ("Summary", {})
            raise RuntimeError("trends LLM error")

        mock_complete = AsyncMock(side_effect=_side_effect)
        with patch("src.radar.summarizer.complete", mock_complete):
            summaries, trends = await summarize_all(articles_by_cat, config)

        assert len(summaries) == 2
        assert trends is None

    @pytest.mark.asyncio
    async def test_passes_messages_to_complete(self) -> None:
        config = _make_config(language="en")
        articles_by_cat = {"Tech": [_make_article(title="Tech Article", category="Tech")]}

        mock_complete = AsyncMock(return_value=("Summary", {}))
        with patch("src.radar.summarizer.complete", mock_complete):
            await summarize_all(articles_by_cat, config)

        assert mock_complete.call_count == 1
        _role, messages, _cfg = mock_complete.call_args[0]
        assert isinstance(messages, list)
        assert len(messages) == 2
        assert messages[0]["role"] == "system"
        assert messages[1]["role"] == "user"
        assert "Tech Article" in messages[1]["content"]
