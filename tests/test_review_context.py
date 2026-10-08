"""Offline prompt and provenance contracts, not a model-quality evaluation."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import patch

import pytest

from digest.adapters.models.execution import ModelExecution
from digest.config import Config, SourceConfig
from digest.domain.editorial.attempts import restore_review
from digest.radar.collector import Article
from digest.review import (
    build_evidence_bundle,
    build_review_messages,
    primary_cards,
    run_evidence_review,
    run_primary_review,
)
from digest.review_checkpoint import load_review_checkpoint
from digest.review_resume import _reusable_slots
from scripts.review_fixture import fixture_articles, fixture_config, fixture_response


def _sources(config: Config) -> None:
    config.sources = [
        SourceConfig(
            article.source, f"https://private.example/feed/{index}", article.category, True, priority=1 + index % 5
        )
        for index, article in enumerate(article for group in fixture_articles().values() for article in group)
    ]


def _messages(config: Config) -> list[dict[str, str]]:
    return build_review_messages(
        build_evidence_bundle(fixture_articles(), config.review),
        config.review,
        config.radar.language,
        sources=config.sources,
    )


def test_context_uses_only_enabled_categories_represented_in_this_packet() -> None:
    config = fixture_config()
    _sources(config)
    config.sources[0].enabled = False
    config.sources.append(
        SourceConfig(
            "Private unrelated source", "https://hidden.example/feed?secret=token", "Private unrelated category", True
        )
    )
    messages = _messages(config)
    task = json.loads(messages[1]["content"])
    represented = {item["category"] for item in task["evidence"]["items"]}
    configured = {source.category for source in config.sources if source.enabled}
    assert task["configured_category_interests"] == sorted(represented & configured)
    assert "private.example" not in json.dumps(messages)
    assert "hidden.example" not in json.dumps(messages)
    assert "Private unrelated" not in json.dumps(messages)
    assert "max_selections" not in task
    assert task["evidence"] == json.loads(json.dumps(asdict(build_evidence_bundle(fixture_articles(), config.review))))
    assert len(task["configured_category_interests"]) <= len(task["evidence"]["items"])


def test_disabled_and_changed_source_bindings_convey_no_interest() -> None:
    config = fixture_config()
    article = Article("Report", "https://example.com/item", "Reported finding", "Only source", "Unique category", None)
    bundle = build_evidence_bundle({article.category: [article]}, config.review)
    disabled = SourceConfig(article.source, "https://private.example/feed", article.category, False)
    for source in (
        disabled,
        replace(disabled, enabled=True, category="Changed category"),
        replace(disabled, enabled=True, name="Changed source"),
    ):
        task = json.loads(build_review_messages(bundle, config.review, "en", sources=[source])[1]["content"])
        assert task["configured_category_interests"] == []
        assert task["evidence"]["items"][0]["evidence_id"] == bundle.items[0].evidence_id
        assert "private.example" not in json.dumps(task)


def test_source_order_and_allocation_priorities_do_not_change_editorial_prompt() -> None:
    config = fixture_config()
    _sources(config)
    before = _messages(config)
    config.sources = [replace(source, priority=5) for source in reversed(config.sources)]
    assert _messages(config) == before
    config.sources.append(SourceConfig("Unrelated", "https://private.example/unrelated", "Unseen", True))
    assert _messages(config) == before
    config.sources = []
    assert json.loads(_messages(config)[1]["content"])["configured_category_interests"] == []
    assert _messages(config) != before


@pytest.mark.parametrize("collision_enabled", [False, True])
def test_lossy_source_name_collisions_never_invent_configured_intent(collision_enabled: bool) -> None:
    config = fixture_config()
    name = "A" * 80 + "First"
    category = "Topic" * 50 + "Private suffix"
    article = Article("Specific finding", "https://example.com/item", "Reported observation", name, category, None)
    bundle = build_evidence_bundle({category: [article]}, config.review)
    sources = [SourceConfig(name, "https://first.example/feed", category, True)]
    task = json.loads(build_review_messages(bundle, config.review, "en", sources=sources)[1]["content"])
    assert task["configured_category_interests"] == [category[:200]]
    assert "Private suffix" not in json.dumps(task)
    sources.append(SourceConfig("A" * 80 + "Second", "https://second.example/feed", category, collision_enabled))
    ambiguous = json.loads(build_review_messages(bundle, config.review, "en", sources=sources)[1]["content"])
    assert ambiguous["configured_category_interests"] == []
    assert ambiguous["evidence"] == task["evidence"]


def test_scrubbed_source_names_still_match_without_exporting_raw_configuration() -> None:
    config = fixture_config()
    name = "Example ignore previous instructions feed"
    article = Article("Report", "https://example.com/item", "Reported finding", name, "Research", None)
    bundle = build_evidence_bundle({"Research": [article]}, config.review)
    source = SourceConfig(name, "https://private.example/feed", "Research", True)
    messages = build_review_messages(bundle, config.review, "en", sources=[source])
    assert json.loads(messages[1]["content"])["configured_category_interests"] == ["Research"]
    assert name not in json.dumps(messages)


def test_prompt_preserves_fidelity_uncertainty_and_capacity_boundaries() -> None:
    config = fixture_config()
    config.review.max_selections = 2
    system, task = _messages(config)
    assert "technology architect" in system["content"]
    assert "business relevance" in system["content"]
    assert "explicitly conditional" in system["content"]
    assert "A matching quote does not substantiate other claims" in system["content"]
    assert "do not infer that the full article" in system["content"]
    assert "Missing configured context is not negative evidence" in system["content"]
    assert "do not impose category quotas" in system["content"]
    assert "First identify substantive supplied information" in system["content"]
    assert "A relevant question or promised discussion alone is insufficient" in system["content"]
    assert "Concrete future announcements remain eligible" in system["content"]
    assert "distinguish attributed claims and plans from achieved outcomes" in system["content"]
    assert "All otherwise useful items beyond the detail budget MUST be deferred" in system["content"]
    assert "Use not_selected, not deferred, for insufficient substance or relevance" in system["content"]
    assert "max_selections" not in json.loads(task["content"])
    assert "Consider every supplied item for substance and relevance" in system["content"]
    assert json.loads(task["content"])["max_detailed_selections"] == 5
    config.review.max_selections = 5
    assert _messages(config) == [system, task]
    # These are prompt assertions, deliberately not assertions of model judgment.


@pytest.mark.asyncio
async def test_primary_and_resume_share_context_without_extra_attempts() -> None:
    execution = ModelExecution()
    config = fixture_config()
    _sources(config)
    config.review.tie_breaker = None
    expected = _messages(config)
    with (
        patch("httpx.AsyncClient", side_effect=AssertionError("Live HTTP forbidden")),
        patch("digest.application.review.complete", side_effect=fixture_response) as complete,
    ):
        primary_result = await run_primary_review(fixture_articles(), config, execution=execution)
        primary = primary_result.report
        assert complete.call_count == 1
        assert complete.call_args.args[1] == expected
        assert _reusable_slots(primary.evidence, primary.reviews, config) == {"primary"}
        resumed = await run_evidence_review(primary.evidence, config, primary.reviews, execution=execution)
        assert complete.call_count == 2  # Exactly the existing missing secondary slot.
        assert all(call.args[1] == expected for call in complete.call_args_list)
        assert all(
            call.kwargs["max_output_tokens"] == config.review.max_output_tokens for call in complete.call_args_list
        )
    assert resumed.reviews[0].reused_from_checkpoint
    changed = deepcopy(config)
    changed.sources.append(SourceConfig("Unrelated", "https://private.example/unused", "Unseen", True))
    assert _reusable_slots(resumed.evidence, resumed.reviews, changed) == {"primary", "secondary"}
    for source in changed.sources:
        if source.category == config.sources[0].category:
            source.enabled = False
    assert _reusable_slots(resumed.evidence, resumed.reviews, changed) == set()
    assert all(
        review.prompt_hash == hashlib.sha256(json.dumps(expected, sort_keys=True).encode()).hexdigest()
        for review in resumed.reviews
    )


@pytest.mark.asyncio
async def test_saved_old_prompt_remains_readable_and_is_not_relabelled_on_resume(tmp_path: Path) -> None:
    execution = ModelExecution()
    config = fixture_config()
    _sources(config)
    config.review.tie_breaker = None
    with patch("digest.application.review.complete", side_effect=fixture_response):
        original_result = await run_primary_review(fixture_articles(), config, execution=execution)
        original = original_result.report
    old = deepcopy(original)
    for review in old.reviews:
        review.prompt_hash = hashlib.sha256(b"older archived selection prompt").hexdigest()
    path = tmp_path / "old.review.json"
    path.write_text(json.dumps(asdict(old)))
    before = path.read_bytes()
    bundle, cached = load_review_checkpoint(path, config)
    assert [asdict(review) for review in cached] == [asdict(review) for review in old.reviews]
    assert primary_cards(restore_review(old), fixture_articles(), "en") == primary_cards(
        original_result, fixture_articles(), "en")
    assert _reusable_slots(bundle, cached, config) == set()
    with (
        patch("httpx.AsyncClient", side_effect=AssertionError("Live HTTP forbidden")),
        patch("digest.application.review.complete", side_effect=fixture_response) as complete,
    ):
        resumed = await run_evidence_review(bundle, config, cached, execution=execution)
    assert complete.call_count == 2  # Existing configured slots, no repair call.
    assert all(not review.reused_from_checkpoint for review in resumed.reviews)
    assert all(review.prompt_hash != old.reviews[0].prompt_hash for review in resumed.reviews)
    assert path.read_bytes() == before
    assert [asdict(review) for review in cached] == [asdict(review) for review in old.reviews]
