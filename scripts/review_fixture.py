"""Offline contract demo. Synthetic responses only; never calls any provider.

Run: python -m scripts.review_fixture --output /tmp/digest-review-fixture
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any
from unittest.mock import patch

from digest.adapters.models.execution import ModelExecution
from digest.application.review import run_blind_review
from digest.config import (
    Config,
    FiltersConfig,
    IrritatorConfig,
    LLMConfig,
    ObsidianConfig,
    RadarConfig,
    ReviewConfig,
    ReviewModelConfig,
    TelegramConfig,
)
from digest.domain.catalog.articles import Article
from digest.domain.editorial.reviews import BlindReviewReport
from digest.presentation.review import render_review


def fixture_config() -> Config:
    return Config(
        llm=LLMConfig(providers=[]), radar=RadarConfig(language="en"),
        irritator=IrritatorConfig(), sources=[], filters=FiltersConfig(),
        telegram=TelegramConfig(enabled=False), obsidian=ObsidianConfig(enabled=False),
        review=ReviewConfig(enabled=True, tie_breaker=ReviewModelConfig("groq", "qwen/qwen3.8-27b")),
    )


def fixture_articles() -> dict[str, list[Article]]:
    raw = json.loads((Path(__file__).parents[1] / "tests/fixtures/blind_review.json").read_text())
    grouped: dict[str, list[Article]] = {}
    for item in raw["articles"]:
        article = Article(**item, pub_date=None)
        grouped.setdefault(article.category, []).append(article)
    return grouped


async def fixture_response(
    role: Any, messages: list[dict[str, str]], config: Any, **kwargs: Any,
) -> tuple[str, dict[str, Any]]:
    """The only adapter used in this demo: maps fixture URLs to evidence IDs."""
    data = json.loads((Path(__file__).parents[1] / "tests/fixtures/blind_review.json").read_text())
    task = json.loads(messages[1]["content"])
    by_url = {item["url"]: item["evidence_id"] for item in task["evidence"]["items"]}
    answers = data["responses"][kwargs["provider_override"].model]
    selections = [{"evidence_id": by_url[a["url"]], **{k: v for k, v in a.items() if k != "url"}} for a in answers]
    return json.dumps({"selections": selections, "limitations": ["Synthetic fixture, not model quality evidence."]}), {}


async def run_fixture(*, execution: ModelExecution) -> BlindReviewReport:
    with patch("digest.application.review.complete", side_effect=fixture_response):
        return await run_blind_review(fixture_articles(), fixture_config(), execution=execution)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = asyncio.run(run_fixture(execution=ModelExecution()))
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "review.json").write_text(json.dumps(asdict(report), ensure_ascii=False, indent=2) + "\n")
    (args.output / "review.md").write_text("# OFFLINE SYNTHETIC FIXTURE\n" + render_review(report) + "\n")
    print(f"Offline fixture: {report.status}; {len(report.reviews)} model slots; overlap={report.selection_overlap}")


if __name__ == "__main__":
    main()
