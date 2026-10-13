"""Offline prompt and provenance contracts, not a model-quality evaluation."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import patch

import pytest

from digest.adapters.models.execution import ModelExecution
from digest.application.review import run_evidence_review, run_primary_review
from digest.application.review_request import build_evidence_bundle, build_review_messages
from digest.config import ClosingConfig, Config, SourceConfig
from digest.domain.editorial.attempts import restore_review
from digest.domain.editorial.reviews import _parse_live_review, review_prompt_hash
from digest.presentation.review import primary_cards
from digest.radar.collector import Article
from digest.review_checkpoint import load_review_checkpoint
from digest.review_resume import _reusable_slots
from scripts.review_fixture import fixture_articles, fixture_config, fixture_response

REASON_QUALIFIERS = (
    "Retain each claim's attribution, uncertainty, negation, scope and material caveats in every reason. "
    "Do not turn a reported allegation into an established event or make the claim more specific than the "
    "supplied evidence; omit a claim if it cannot fit without meaning-changing qualifiers. "
)


def _sources(config: Config) -> None:
    config.sources = [
        SourceConfig(
            article.source, f"https://private.example/feed/{index}", article.category, True, priority=1 + index % 5
        )
        for index, article in enumerate(article for group in fixture_articles().values() for article in group)
    ]


def _messages(config: Config, closing: ClosingConfig | None = None) -> list[dict[str, str]]:
    return build_review_messages(
        build_evidence_bundle(fixture_articles(), config.review),
        config.review,
        config.radar.language,
        sources=config.sources,
        closing=closing,
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


@pytest.mark.parametrize(
    "closing,baseline_hash",
    [
        (None, "4beffbec078bc70d5db4ac19307d09da3b37eed715287984f3c785d53ea291e9"),
        (ClosingConfig(), "4beffbec078bc70d5db4ac19307d09da3b37eed715287984f3c785d53ea291e9"),
        (ClosingConfig(enabled=True), "82d28c198d583876b8b6a3bb28fcaf10d68c9d0925dfcf01af8002fba3d13ad8"),
    ],
    ids=["closing-absent", "closing-disabled", "closing-enabled"],
)
def test_shared_reason_qualifiers_change_only_the_exact_instruction(
    closing: ClosingConfig | None, baseline_hash: str
) -> None:
    config = fixture_config()
    _sources(config)
    messages = _messages(config, closing)
    system = messages[0]["content"]
    assert len(REASON_QUALIFIERS) == len(REASON_QUALIFIERS.encode()) == 287
    assert system.count(REASON_QUALIFIERS) == 1
    quantitative = (
        "When using a quantitative claim, retain its comparator, value, unit, statistic or percentile, "
        "and material conditions together. Prefer a short literal measurement quotation within the reason. "
        "If it cannot fit faithfully, omit the whole quantitative claim rather than dropping its qualifiers. "
    )
    assert (
        "A matching quote does not substantiate other claims in the reason. " + REASON_QUALIFIERS + quantitative
    ) in system
    assert "reason (1-2 sentences, at most 600 characters)" in system
    assert "quote (an exact non-empty excerpt from title or excerpt, at most 200 characters)" in system
    assert "a specific RSS-evidence reason of at most 240 characters" in system
    assert "selections for at most max_detailed_selections useful items in priority order" in system
    assert "Apply the same factual restraint to non-selection and duplicate reasons" in system
    assert "distinguish attributed claims and plans from achieved outcomes" in system
    if closing is not None and closing.enabled:
        assert "CARD uses the ordinary evidence_id, reason, quote and confidence fields" in system
        assert "closing.selection must not exceed D" in system
        assert "identify the evidenced positive basis" in system
        assert "Preserve caveats and distinguish announced plans from achieved outcomes" in system
    # Golden hashes captured from the exact pre-clarification source and same synthetic inputs.
    baseline = deepcopy(messages)
    baseline[0]["content"] = system.replace(REASON_QUALIFIERS, "", 1)
    assert review_prompt_hash(baseline) == baseline_hash
    assert review_prompt_hash(messages) != baseline_hash
    assert messages[1]["content"].encode() == baseline[1]["content"].encode()
    current_bytes = json.dumps(messages, sort_keys=True).encode()
    baseline_bytes = json.dumps(baseline, sort_keys=True).encode()
    assert len(current_bytes) - len(baseline_bytes) == 287


def test_authored_allegation_contrast_documents_the_structural_parser_boundary() -> None:
    # Human-authored contract examples, not model outputs or a quality benchmark.
    config = fixture_config()
    article = Article(
        "Group alleges data theft at Cedar Network",
        "https://example.com/cedar-network-allegation",
        "A research bulletin reports that a group claims it took data from Cedar Network. "
        "Cedar Network has not confirmed an incident, and the group has released no samples.",
        "Research Bulletin",
        "Operations",
        None,
    )
    bundle = build_evidence_bundle({article.category: [article]}, config.review)
    messages = build_review_messages(bundle, config.review, "en")
    assert REASON_QUALIFIERS in messages[0]["content"]
    assert json.loads(messages[1]["content"])["evidence"]["items"][0]["excerpt"] == article.description
    faithful = (
        "A bulletin reports a group's data-theft claim about Cedar Network; the company has not confirmed an "
        "incident and no samples have been released. If substantiated, the claim could matter to its "
        "operational risk assessment."
    )
    unfaithful = (
        "Cedar Network suffered a confirmed theft of payment records, demonstrating a concrete security failure."
    )
    for reason in (faithful, unfaithful):
        response = {
            "selections": [
                {
                    "evidence_id": bundle.items[0].evidence_id,
                    "reason": reason,
                    "quote": article.title,
                    "confidence": "low",
                }
            ],
            "limitations": [],
            "dispositions": [{"evidence_id": bundle.items[0].evidence_id, "status": "selected"}],
        }
        accepted, limitations, rejected = _parse_live_review(json.dumps(response), bundle)
        assert [selection.reason for selection in accepted] == [reason]
        assert not limitations and not rejected
    # Both pass structural validation; neither parser acceptance nor this prompt proves faithfulness.


@pytest.mark.asyncio
async def test_primary_and_resume_share_context_without_extra_attempts() -> None:
    execution = ModelExecution()
    config = fixture_config()
    _sources(config)
    config.review.tie_breaker = None
    expected = _messages(config)
    assert expected[0]["content"].count(REASON_QUALIFIERS) == 1
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
    assert resumed.evidence == primary.evidence and resumed.status == "complete"
    assert resumed.reviews[0].reused_from_checkpoint
    assert resumed.reviews[0].attempted_at == primary.reviews[0].attempted_at
    assert all(review.attempted_at for review in resumed.reviews)
    changed = deepcopy(config)
    changed.sources.append(SourceConfig("Unrelated", "https://private.example/unused", "Unseen", True))
    assert _reusable_slots(resumed.evidence, resumed.reviews, changed) == {"primary", "secondary"}
    for source in changed.sources:
        if source.category == config.sources[0].category:
            source.enabled = False
    assert _reusable_slots(resumed.evidence, resumed.reviews, changed) == set()
    assert all(
        review.prompt_hash == review_prompt_hash(expected)
        for review in primary.reviews + resumed.reviews
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
    current_messages = _messages(config)
    old_messages = deepcopy(current_messages)
    old_messages[0]["content"] = old_messages[0]["content"].replace(REASON_QUALIFIERS, "", 1)
    old_hash = review_prompt_hash(old_messages)
    assert old_hash == "4beffbec078bc70d5db4ac19307d09da3b37eed715287984f3c785d53ea291e9"
    assert review_prompt_hash(current_messages) != old_hash
    old = deepcopy(original)
    for review in old.reviews:
        review.prompt_hash = old_hash
    path = tmp_path / "old.review.json"
    path.write_text(json.dumps(asdict(old)))
    before = path.read_bytes()
    bundle, cached = load_review_checkpoint(path, config)
    assert [asdict(review) for review in cached] == [asdict(review) for review in old.reviews]
    assert primary_cards(restore_review(old), fixture_articles(), "en") == primary_cards(
        original_result, fixture_articles(), "en"
    )
    assert _reusable_slots(bundle, cached, config) == set()
    with (
        patch("httpx.AsyncClient", side_effect=AssertionError("Live HTTP forbidden")),
        patch("digest.application.review.complete", side_effect=fixture_response) as complete,
    ):
        resumed = await run_evidence_review(bundle, config, cached, execution=execution)
    assert complete.call_count == 2  # Existing configured slots, no repair call.
    assert all(not review.reused_from_checkpoint for review in resumed.reviews)
    assert all(review.prompt_hash != old.reviews[0].prompt_hash for review in resumed.reviews)
    assert all(review.prompt_hash == review_prompt_hash(current_messages) for review in resumed.reviews)
    assert all(call.args[1] == current_messages for call in complete.call_args_list)
    assert path.read_bytes() == before
    assert [asdict(review) for review in cached] == [asdict(review) for review in old.reviews]
