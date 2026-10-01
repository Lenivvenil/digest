"""Receipt-bound primary output; model and transport operations are always mocked."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from digest.config import Config, SourceConfig
from digest.editorial_pipeline import deliver, prepare
from digest.editorial_state import (
    EditorialState,
    Generation,
    admit_articles,
    generation_id,
    load_state,
    make_chunks,
    save_body,
    store_state,
)
from digest.editorial_worker import next_task, parse_final, source_node
from digest.radar.collector import Article, _load_cache, article_hash
from scripts.review_fixture import fixture_config


def ready_state(directory: Path, config: Config, count: int = 1) -> list[str]:
    state = EditorialState()
    identities = []
    body = "A synthetic system removes a coordination step.\n\nThe result excludes network partitions."
    for index in range(count):
        original = Article(f"Source {index}", f"https://example.com/{index}", "Original RSS description", "Test",
                           "Architecture", datetime.now(UTC))
        admit_articles(state, [original])
        identity = article_hash(original.title, original.link)
        identities.append(identity)
        article = state.articles[identity]
        article.body_sha256 = save_body(directory, body)
        article.chunks = make_chunks(body)
        article.final_url = original.link
        article.fetched_at = datetime.now(UTC).isoformat()
        article.extraction_status = "article"
        model = config.review.primary
        gid = generation_id(article.body_sha256, model.provider, model.model)
        generation = Generation(gid, model.provider, model.model, article.body_sha256)
        task = next_task(article, generation, body)
        assert task is not None and task.stage == "source"
        node = source_node(article, generation, body)
        generation.nodes[task.task_key] = node
        final_task = next_task(article, generation, body)
        assert final_task is not None and final_task.stage == "final"
        fact_id = limit_id = "S0"
        raw_final = {
            "decision": "ready", "reason": "", "value_score": 8,
            "value_rationale": "Конкретный архитектурный выбор и границы результата.",
            "event_key": f"Синтетический эксперимент координации {index}",
            "fact": {"text": "Система убирает дополнительный этап координации.", "claim_ids": [fact_id]},
            "inference": {"text": "Это может уменьшить задержку при сохранении условий эксперимента.",
                          "claim_ids": [fact_id]},
            "limitation": {"text": "Эксперимент не проверяет сетевые разделения.", "claim_ids": [limit_id]},
            "why_read": {"text": "В статье описаны условия, при которых можно убрать этап координации.",
                         "claim_ids": [fact_id]},
        }
        generation.final = parse_final(final_task, json.dumps(raw_final), {})
        article.generations[gid] = generation
    store_state(state, directory)
    return identities


@pytest.fixture
def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Config, Path, Path]:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "synthetic-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "synthetic-destination")
    monkeypatch.delenv("GITHUB_RUN_ID", raising=False)
    monkeypatch.delenv("GITHUB_RUN_ATTEMPT", raising=False)
    config = fixture_config()
    config.radar.language = "ru"  # This fixture stores Russian draft text and generations.
    config.sources = [SourceConfig("Test", "https://example.com/feed", "Architecture", True)]
    config.telegram.enabled = config.telegram.required = config.obsidian.enabled = True
    config.obsidian.output_dir = "digests"
    path = tmp_path / "config.yaml"
    path.write_text("synthetic config identity")
    return config, path, tmp_path / ".cache/editorial"


def test_reservation_is_durable_and_repeat_prepare_cannot_claim_same_cards(setup: tuple) -> None:
    config, path, state_dir = setup
    identities = ready_state(state_dir, config)
    with patch("digest.editorial_pipeline._config", return_value=config):
        attempt = prepare(path, state_dir)
        assert attempt is not None
        assert prepare(path, state_dir) is None
    state = load_state(state_dir)
    assert state.articles[identities[0]].delivery_state == "reserved"
    assert state.articles[identities[0]].delivery_attempt_id == attempt.stem
    assert _load_cache() == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["sent", "rejected", "timeout", "server_error", "malformed", "missing_message_id"])
async def test_confirmed_failed_and_unknown_are_distinct_and_only_success_consumes_dedup(
    setup: tuple, kind: str,
) -> None:
    config, path, state_dir = setup
    identities = ready_state(state_dir, config)
    client = AsyncMock()
    client.__aenter__.return_value = client
    if kind == "sent":
        client.post.return_value = httpx.Response(200, json={"ok": True, "result": {"message_id": 123}})
    elif kind == "rejected":
        client.post.return_value = httpx.Response(400, json={"ok": False})
    elif kind == "server_error":
        client.post.return_value = httpx.Response(500, json={"ok": False})
    elif kind == "malformed":
        client.post.return_value = httpx.Response(200, text="invalid")
    elif kind == "missing_message_id":
        client.post.return_value = httpx.Response(200, json={"ok": True, "result": {}})
    else:
        client.post.side_effect = httpx.ReadTimeout("synthetic timeout")
    with (patch("digest.editorial_pipeline._config", return_value=config),
          patch("digest.editorial_pipeline.httpx.AsyncClient", return_value=client),
          patch("digest.editorial_pipeline.asyncio.sleep", AsyncMock())):
        attempt = prepare(path, state_dir)
        assert attempt
        assert await deliver(path, state_dir, attempt) == (0 if kind == "sent" else 1)
        with pytest.raises(ValueError, match="already executed"):
            await deliver(path, state_dir, attempt)
        next_attempt = prepare(path, state_dir)
    client.post.assert_awaited_once()
    expected = "delivered" if kind == "sent" else "confirmed_failed" if kind == "rejected" else "unknown"
    state = load_state(state_dir)
    # A confirmed rejection alone can be claimed for a later distinct attempt.
    assert state.articles[identities[0]].delivery_state == ("reserved" if kind == "rejected" else expected)
    assert bool(next_attempt) is (kind == "rejected")
    assert (identities[0] in _load_cache()) is (kind == "sent")
    assert json.loads(attempt.read_text())["cards"][0]["outcome"] == expected
    if kind == "sent":
        checkpoints = list(Path("digests").glob("*.review.json"))
        assert len(checkpoints) == 1
        raw = json.loads(checkpoints[0].read_text())
        assert raw["reviews"] == [] and raw["third_model_reason"] == "editorial_source_context_only"
        assert "Original RSS description" in raw["evidence"]["items"][0]["excerpt"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["config", "destination", "run", "rerun", "card", "duplicate"])
async def test_changed_reservation_is_rejected_before_any_http(
    setup: tuple, monkeypatch: pytest.MonkeyPatch, mutation: str,
) -> None:
    config, path, state_dir = setup
    ready_state(state_dir, config)
    with patch("digest.editorial_pipeline._config", return_value=config):
        attempt = prepare(path, state_dir)
        assert attempt
        if mutation == "config":
            path.write_text("changed")
        elif mutation == "destination":
            monkeypatch.setenv("TELEGRAM_CHAT_ID", "changed")
        elif mutation == "run":
            monkeypatch.setenv("GITHUB_RUN_ID", "changed")
        elif mutation == "rerun":
            monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
        else:
            record = json.loads(attempt.read_text())
            if mutation == "card":
                record["cards"][0]["card"]["summary"] = "Invented replacement"
            else:
                record["cards"] *= 2
            attempt.write_text(json.dumps(record))
        with patch("digest.editorial_pipeline.httpx.AsyncClient") as client:
            with pytest.raises(ValueError):
                await deliver(path, state_dir, attempt)
            client.assert_not_called()


def test_transport_budget_leaves_extra_analysed_articles_ready(setup: tuple) -> None:
    config, path, state_dir = setup
    config.telegram.max_messages = 1
    identities = ready_state(state_dir, config, count=4)
    with patch("digest.editorial_pipeline._config", return_value=config):
        assert prepare(path, state_dir)
    states = [load_state(state_dir).articles[identity].delivery_state for identity in identities]
    assert states.count("reserved") == 1 and states.count("pending") == 3


def test_missing_credentials_do_not_reserve_work(setup: tuple, monkeypatch: pytest.MonkeyPatch) -> None:
    config, path, state_dir = setup
    identities = ready_state(state_dir, config)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN")
    with patch("digest.editorial_pipeline._config", return_value=config):
        with pytest.raises(ValueError, match="not configured"):
            prepare(path, state_dir)
    assert load_state(state_dir).articles[identities[0]].delivery_state == "pending"


@pytest.mark.asyncio
async def test_one_oversized_payload_does_not_block_other_verified_cards(setup: tuple) -> None:
    from digest.editorial_pipeline import _telegram_payload

    config, path, state_dir = setup
    identities = ready_state(state_dir, config, count=2)
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.post.return_value = httpx.Response(200, json={"ok": True, "result": {"message_id": 123}})

    def payload(card, identity, used_config):
        if card.title == "Source 0":
            raise ValueError("Editorial card exceeds Telegram transport limit; no truncation performed.")
        return _telegram_payload(card, identity, used_config)

    with (patch("digest.editorial_pipeline._config", return_value=config),
          patch("digest.editorial_pipeline._telegram_payload", side_effect=payload),
          patch("digest.editorial_pipeline.httpx.AsyncClient", return_value=client),
          patch("digest.editorial_pipeline.asyncio.sleep", AsyncMock())):
        attempt = prepare(path, state_dir)
        assert attempt
        assert await deliver(path, state_dir, attempt) == 1
    client.post.assert_awaited_once()
    state = load_state(state_dir)
    assert state.articles[identities[0]].delivery_state == "confirmed_failed"
    assert state.articles[identities[1]].delivery_state == "delivered"
    assert set(_load_cache()) == {identities[1]}
    record = json.loads(attempt.read_text())
    assert record["cards"][0]["error"] == "payload_transport_limit"
    checkpoint = next(Path("digests").glob("*.review.json"))
    assert [item["evidence_id"] for item in json.loads(checkpoint.read_text())["evidence"]["items"]] == [identities[1]]


@pytest.mark.asyncio
async def test_archive_failure_keeps_delivery_receipt_and_repairs_without_resend(setup: tuple) -> None:
    from digest.editorial_pipeline import repair_archives

    config, path, state_dir = setup
    identities = ready_state(state_dir, config)
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.post.return_value = httpx.Response(200, json={"ok": True, "result": {"message_id": 123}})
    with (patch("digest.editorial_pipeline._config", return_value=config),
          patch("digest.editorial_pipeline.httpx.AsyncClient", return_value=client),
          patch("digest.editorial_pipeline.asyncio.sleep", AsyncMock())):
        attempt = prepare(path, state_dir)
        assert attempt
        with patch("digest.editorial_pipeline._write_generated", side_effect=OSError("synthetic disk error")):
            assert await deliver(path, state_dir, attempt) == 1
        assert load_state(state_dir).articles[identities[0]].delivery_state == "delivered"
        assert identities[0] in _load_cache()
        record = json.loads(attempt.read_text())
        assert record["cards"][0]["outcome"] == "delivered" and record["archive_state"] == "pending"
        assert repair_archives(path, state_dir) == 0
        assert repair_archives(path, state_dir) == 0
        assert prepare(path, state_dir) is None
    client.post.assert_awaited_once()
    assert len(list(Path("digests").glob("*.md"))) == 1
    assert len(list(Path("digests").glob("*.review.json"))) == 1
    assert json.loads(attempt.read_text())["archive_state"] == "complete"


@pytest.mark.asyncio
async def test_verification_case_admission_is_report_only_and_does_not_change_dedup(
    setup: tuple, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from digest.editorial_pipeline import work

    config, path, state_dir = setup
    source_file = path.parent / "sources.json"
    source_file.write_text(json.dumps([{
        "title": "Specific verification source", "url": "https://example.com/article",
        "source": "Test", "category": "Architecture", "published": None,
    }]))
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN")
    monkeypatch.delenv("TELEGRAM_CHAT_ID")
    with (patch("digest.editorial_pipeline._config", return_value=config),
          patch("digest.editorial_pipeline.collect", AsyncMock(side_effect=AssertionError("Explicit cases"))),
          patch("digest.editorial_worker.complete", AsyncMock(side_effect=AssertionError("Zero call allowance"))),
          patch("digest.editorial_pipeline.httpx.AsyncClient", side_effect=AssertionError("No delivery"))):
        assert await work(path, state_dir, path.parent / "report", max_calls=0,
                          verification_sources=source_file) == 0
    state = load_state(state_dir)
    assert len(state.articles) == 1
    assert all(item.delivery_state == "pending" for item in state.articles.values())
    assert _load_cache() == {}
    metadata = json.loads((path.parent / "report/editorial-pass.json").read_text())
    assert metadata["source_scope"] == "explicit_verification_cases"
    assert metadata["telegram_used"] is False and metadata["dedup_written"] is False
    assert metadata["processing"]["pending"] == 1
