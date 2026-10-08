"""Candidate continuation exercises ordinary preparation, with all external I/O mocked."""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from digest.candidate_review import load_candidate_progress
from digest.config import SourceConfig
from digest.delivery.edition import CLAIM_FILE, READY_FILE
from digest.edition_runtime import delivery_phase
from digest.feedback import FeedbackStore, save_feedback
from digest.main import _run
from digest.radar.collector import Article, SourceCollectionOutcome, _capture_candidates
from scripts.review_fixture import fixture_config


@pytest.mark.asyncio
@pytest.mark.parametrize(("fail_snapshot", "failed_feeds", "precall_capacity"), [
    (False, False, False), (True, False, False), (False, True, False), (False, False, True),
])
async def test_later_packet_reaches_real_preparation_without_replaying_confirmed_day(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail_snapshot: bool, failed_feeds: bool, precall_capacity: bool,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('TELEGRAM_CHAT_ID', '12345')
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN', 'test-token')
    config = fixture_config()
    config.review.review_led_only = True
    config.telegram.enabled = True
    config.telegram.delivery_mode = 'compact'
    config.telegram.bot_username = 'test_digest_bot'
    config.obsidian.enabled = True
    config.obsidian.output_dir = 'digests'
    config.sources = [SourceConfig('Source', 'https://example.com/feed', 'Tech', True, recency_hours=168)]
    now = datetime.now(UTC)
    articles = [Article(f'Article {i:02}', f'https://example.com/{i:02}', 'Original evidence',
                        'Source', 'Tech', now) for i in range(47)]
    save_feedback(FeedbackStore(), '.cache', strict=True)
    monkeypatch.setattr('digest.config.load_config', lambda _: config)
    monkeypatch.setattr('digest.application.run_state.collect_run_feedback',
          AsyncMock(return_value=(FeedbackStore(), True, 0)))
    monkeypatch.setattr('digest.application.run_state.apply_pending_approvals',
        lambda c, *args, **kwargs: (c, kwargs["execution"]))
    calls: list[set[str]] = []
    collection_calls = 0

    async def collect(c: Any, **kwargs: Any) -> tuple[dict[str, list[Article]], dict[str, str]]:
        nonlocal collection_calls
        collection_calls += 1
        observed = articles if collection_calls == 1 else []  # Later feed rotation must not erase saved work.
        cache_path = Path('.cache/seen_articles.json')
        cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
        inventory = kwargs['inventory']
        inventory.sources = [SourceCollectionOutcome('Source', 'https://example.com/feed', 'Tech', 3)]
        _capture_candidates(inventory, c.enabled_sources, [observed], cache, now, [], {})
        from digest.radar.collector import AllFeedsFailedError, SourceFetchMetrics

        if failed_feeds and collection_calls > 1:
            kwargs['fetch_metrics']['Source'] = SourceFetchMetrics(False, 0, 0)
            raise AllFeedsFailedError('fixture feed failure')
        kwargs['fetch_metrics']['Source'] = SourceFetchMetrics(True, len(observed), 17)
        return ({'Tech': observed[:3]} if observed else {}), cache  # Source slots do not define eligibility.

    async def model(role: Any, messages: list[dict[str, str]], c: Any, **kwargs: Any) -> tuple[str, dict[str, int]]:
        evidence = json.loads(messages[1]['content'])['evidence']['items']
        selected = evidence[-1]
        calls.append({item['evidence_id'] for item in evidence})
        dispositions = [{'evidence_id': item['evidence_id'], 'status': 'selected'}
                        if item == selected else {'evidence_id': item['evidence_id'], 'status': 'not_selected',
                                                  'reason': 'This fixture item has no mechanism relevant to the role.'}
                        for item in evidence]
        return json.dumps({'selections': [{'evidence_id': selected['evidence_id'],
                                           'reason': 'Useful fixture selection', 'quote': selected['title'],
                                           'confidence': 'high'}], 'limitations': ['Offline fixture'],
                           'dispositions': dispositions}), {}

    monkeypatch.setattr('digest.radar.collect', collect)
    monkeypatch.setattr('digest.application.review.complete', model)
    if precall_capacity:
        monkeypatch.setattr('digest.application.candidate_review.MAX_BYTES', 200000)
        with pytest.raises(ValueError, match='capacity before model'):
            await _run('config.yaml', False, False, False, prepare_only=True)
        assert not calls and not Path('.cache', READY_FILE).exists()
        assert len(load_candidate_progress().candidates) == 47
        return
    if fail_snapshot:
        from digest.edition_runtime import save_accepted_preparation

        def fail_save(*args: Any, **kwargs: Any) -> None:
            raise OSError("fixture snapshot write failure")

        monkeypatch.setattr('digest.edition_runtime.save_accepted_preparation', fail_save)
        with pytest.raises(OSError, match="snapshot write"):
            await _run('config.yaml', False, False, False, prepare_only=True)
        assert len(calls) == 1
        assert load_candidate_progress().packets[0].report is not None
        monkeypatch.setattr('digest.edition_runtime.save_accepted_preparation', save_accepted_preparation)
    first = await _run('config.yaml', False, False, False, prepare_only=True)
    assert len(calls) == 1
    assert first.edition_status == 'ready'
    first_manifest = json.loads(Path('.cache', READY_FILE).read_text())
    assert first.feeds_fetched == 1
    assert first.new_articles == (0 if fail_snapshot else 3)
    assert first_manifest['canonical_metadata']['article_count'] == 20
    assert len(first_manifest['canonical_metadata']['cards']) == 1
    assert first.digest_length == len(first_manifest['presentation_metadata']['combined']) > 0
    assert first.markdown_saved and Path(first.markdown_path).is_file()
    assert first.review_status == json.loads(Path(first.review_checkpoint).read_text())['status']
    assert first.review_status != 'not_requested'
    assert not first.telegram_sent
    assert any(path.endswith('.candidates.json') for path in first_manifest['checkpoint_refs'])
    assert len(load_candidate_progress().candidates) == 28
    assert len(list(Path('.cache/candidate_index').glob('*.json'))) == 19
    assert await delivery_phase('claim', 'config.yaml', first.ready_sha256, None) == 0
    claim_sha = hashlib.sha256(Path('.cache', CLAIM_FILE).read_bytes()).hexdigest()
    with respx.mock(assert_all_called=True) as router:
        router.post('https://api.telegram.org/bottest-token/sendMessage').mock(return_value=httpx.Response(
            200, json={'ok': True, 'result': {'message_id': 70, 'chat': {'id': 12345}}}))
        assert await delivery_phase('send', 'config.yaml', first.ready_sha256, claim_sha) == 0
    same_day = await _run('config.yaml', False, False, False, prepare_only=True)
    assert same_day.edition_status == 'confirmed' and len(calls) == 1
    assert same_day.feeds_fetched == same_day.new_articles == 0
    tomorrow = await _run('config.yaml', False, False, False, prepare_only=True,
                          edition_date=now.date() + timedelta(days=1))
    assert tomorrow.edition_status == 'pending_window'
    assert tomorrow.feeds_fetched == 1 and tomorrow.new_articles == 0
    assert len(calls) == 2 and calls[0].isdisjoint(calls[1])
    manifest = json.loads(Path('.cache', READY_FILE).read_text())
    assert manifest['canonical_metadata']['cards'][0]['link'] not in {
        item['link'] for item in first_manifest['canonical_metadata']['cards']}
    state = load_candidate_progress()
    assert sum(item.status == 'not_presented' for item in state.candidates.values()) == 7
    from digest.candidate_storage import read_candidate_header

    indexed = [read_candidate_header(path.stem, '.cache') for path in Path('.cache/candidate_index').glob('*.json')]
    assert sum(item is not None and item['status'] == 'not_selected' for item in indexed) == 38
    assert len(state.candidates) + len(indexed) == 47

    if failed_feeds:
        from digest.source_scorer import load_stats

        stats = load_stats('.cache')['Source']
        assert stats.total_fetches == 2 and stats.successful_fetches == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["truncated", "deferred", "abstained", "no_candidates"])
async def test_preparation_reports_technical_empty_without_failing_editorial_abstention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str,
) -> None:
    from digest.main import main
    from digest.preparation import load_preparation

    monkeypatch.chdir(tmp_path)
    config = fixture_config()
    config.review.review_led_only = True
    config.telegram.delivery_mode = "compact"
    config.sources = [SourceConfig("Source", "https://example.com/feed", "Tech", True)]
    now = datetime.now(UTC)
    observed = ([] if outcome == "no_candidates" else
                [Article("Evidence", "https://example.com/item", "Literal evidence", "Source", "Tech", now)])
    monkeypatch.setattr("digest.config.load_config", lambda _: config)
    monkeypatch.setattr("digest.application.run_state.collect_run_feedback",
          AsyncMock(return_value=(FeedbackStore(), True, 0)))
    monkeypatch.setattr("digest.application.run_state.apply_pending_approvals",
        lambda c, *args, **kwargs: (c, kwargs["execution"]))

    async def collect(c: Any, **kwargs: Any) -> tuple[dict, dict]:
        inventory = kwargs["inventory"]
        inventory.sources = [SourceCollectionOutcome("Source", "https://example.com/feed", "Tech", 3)]
        _capture_candidates(inventory, c.enabled_sources, [observed], {}, now, [], {})
        return ({"Tech": observed} if observed else {}), {}

    async def model(role: Any, messages: list[dict[str, str]], c: Any, **kwargs: Any) -> tuple[str, dict]:
        items = json.loads(messages[1]["content"])["evidence"]["items"]
        if outcome == "truncated":
            return '{"selections":[', {"finish_reason": "length"}
        return json.dumps({"selections": [], "limitations": ["Fixture explanation"], "dispositions": [
            {"evidence_id": item["evidence_id"],
             "status": "deferred" if outcome == "deferred" else "not_selected",
             "reason": "Response capacity" if outcome == "deferred" else "No relevant development in excerpt"}
            for item in items]}), {"finish_reason": "stop"}

    monkeypatch.setattr("digest.radar.collect", collect)
    completion = AsyncMock(side_effect=model)
    monkeypatch.setattr("digest.application.review.complete", completion)
    stats = await _run("config.yaml", False, False, False, prepare_only=True)
    incomplete = outcome in {"truncated", "deferred"}
    assert (stats.edition_status == "selection_incomplete") is incomplete
    assert not Path(".cache", READY_FILE).exists()
    assert (load_preparation() is not None) is (outcome == "abstained")
    if incomplete:
        progress = load_candidate_progress()
        assert all(candidate.status == "technical_pending" for candidate in progress.candidates.values())
        assert progress.packets[0].report is not None
        assert not progress.packets[0].handed_to_preparation
    if outcome == "no_candidates":
        completion.assert_not_awaited()
    monkeypatch.setattr("digest.main.run", AsyncMock(return_value=stats))
    output = tmp_path / "outputs"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    assert await main(["--config", "config.yaml", "--prepare-edition"]) == int(incomplete)
    assert f"edition_status={stats.edition_status or 'no_ready'}" in output.read_text()
