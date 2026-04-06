"""Tests for feedback data models and functions."""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest
import respx
import httpx

from src.feedback import (
    ArticleFeedback,
    FeedbackStore,
    load_feedback,
    save_feedback,
    collect_feedback,
    get_source_feedback_score,
)


# ---------------------------------------------------------------------------
# Dataclass creation tests
# ---------------------------------------------------------------------------


def test_article_feedback_creation() -> None:
    fb = ArticleFeedback(
        article_hash="abc123",
        source_name="Test Feed",
        rating=1,
        timestamp="2026-03-18T10:00:00",
    )
    assert fb.article_hash == "abc123"
    assert fb.source_name == "Test Feed"
    assert fb.rating == 1
    assert fb.timestamp == "2026-03-18T10:00:00"


def test_feedback_store_defaults() -> None:
    store = FeedbackStore()
    assert store.ratings == []
    assert store.last_update_id == 0


def test_feedback_store_with_ratings() -> None:
    fb1 = ArticleFeedback(
        article_hash="a1",
        source_name="Feed A",
        rating=1,
        timestamp="2026-03-18T10:00:00",
    )
    fb2 = ArticleFeedback(
        article_hash="b2",
        source_name="Feed B",
        rating=-1,
        timestamp="2026-03-18T11:00:00",
    )
    store = FeedbackStore(ratings=[fb1, fb2], last_update_id=42)
    assert len(store.ratings) == 2
    assert store.last_update_id == 42
    assert store.ratings[0].source_name == "Feed A"
    assert store.ratings[1].rating == -1


# ---------------------------------------------------------------------------
# load_feedback / save_feedback round-trip
# ---------------------------------------------------------------------------


def test_load_feedback_missing_file(tmp_path: Path) -> None:
    store = load_feedback(str(tmp_path))
    assert store.ratings == []
    assert store.last_update_id == 0


def test_save_and_load_feedback_round_trip(tmp_path: Path) -> None:
    fb1 = ArticleFeedback(
        article_hash="h1",
        source_name="Source A",
        rating=1,
        timestamp="2026-03-18T10:00:00+00:00",
    )
    fb2 = ArticleFeedback(
        article_hash="h2",
        source_name="Source B",
        rating=-1,
        timestamp="2026-03-18T11:00:00+00:00",
    )
    original = FeedbackStore(ratings=[fb1, fb2], last_update_id=100)
    save_feedback(original, str(tmp_path))

    loaded = load_feedback(str(tmp_path))
    assert loaded.last_update_id == 100
    assert len(loaded.ratings) == 2
    assert loaded.ratings[0].source_name == "Source A"
    assert loaded.ratings[0].rating == 1
    assert loaded.ratings[1].source_name == "Source B"
    assert loaded.ratings[1].rating == -1


def test_load_feedback_invalid_json(tmp_path: Path) -> None:
    path = tmp_path / "feedback.json"
    path.write_text("not valid json", encoding="utf-8")
    store = load_feedback(str(tmp_path))
    assert store.ratings == []


def test_load_feedback_non_dict(tmp_path: Path) -> None:
    path = tmp_path / "feedback.json"
    path.write_text("[]", encoding="utf-8")
    store = load_feedback(str(tmp_path))
    assert store.ratings == []


def test_load_feedback_skips_bad_rating_preserves_rest(tmp_path: Path) -> None:
    """A malformed rating entry is skipped; valid entries are preserved."""
    import json

    data = {
        "last_update_id": 5,
        "ratings": [
            {
                "article_hash": "good1",
                "source_name": "Feed A",
                "rating": 1,
                "timestamp": "2026-03-18T10:00:00",
            },
            {
                "article_hash": "broken",
                # "source_name" is missing — will trigger KeyError
                "rating": 1,
                "timestamp": "2026-03-18T11:00:00",
            },
            {
                "article_hash": "good2",
                "source_name": "Feed B",
                "rating": -1,
                "timestamp": "2026-03-18T12:00:00",
            },
        ],
    }
    (tmp_path / "feedback.json").write_text(json.dumps(data), encoding="utf-8")

    store = load_feedback(str(tmp_path))
    assert len(store.ratings) == 2
    assert store.ratings[0].article_hash == "good1"
    assert store.ratings[1].article_hash == "good2"


def test_save_feedback_no_tmp_file_left(tmp_path: Path) -> None:
    """After save_feedback, the .tmp file must not exist."""
    store = FeedbackStore(last_update_id=7)
    save_feedback(store, str(tmp_path))
    tmp = tmp_path / "feedback.json.tmp"
    assert not tmp.exists()


def test_save_feedback_preserves_existing_on_write(tmp_path: Path) -> None:
    """Existing feedback.json is intact before and after a successful save."""
    original = FeedbackStore(last_update_id=42)
    save_feedback(original, str(tmp_path))

    updated = FeedbackStore(last_update_id=99)
    save_feedback(updated, str(tmp_path))

    loaded = load_feedback(str(tmp_path))
    assert loaded.last_update_id == 99
    assert not (tmp_path / "feedback.json.tmp").exists()


# ---------------------------------------------------------------------------
# collect_feedback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_collect_feedback_per_article_good() -> None:
    """fb:a:g:HASH callback records a good rating for the article's source."""
    token = "testtoken"
    store = FeedbackStore(article_source_map={"abcd1234": "My Source"})

    respx.post(f"https://api.telegram.org/bot{token}/deleteWebhook").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    respx.get(f"https://api.telegram.org/bot{token}/getUpdates").mock(
        return_value=httpx.Response(200, json={
            "ok": True,
            "result": [
                {
                    "update_id": 1001,
                    "callback_query": {
                        "id": "cq1",
                        "data": "fb:a:g:abcd1234",
                        "from": {"id": 123},
                    },
                }
            ],
        })
    )
    respx.post(f"https://api.telegram.org/bot{token}/answerCallbackQuery").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )

    result = await collect_feedback(token, store)
    assert result.last_update_id == 1001
    assert len(result.ratings) == 1
    assert result.ratings[0].rating == 1
    assert result.ratings[0].article_hash == "abcd1234"
    assert result.ratings[0].source_name == "My Source"


@pytest.mark.asyncio
@respx.mock
async def test_collect_feedback_per_article_bad() -> None:
    """fb:a:b:HASH callback records a bad rating for the article's source."""
    token = "testtoken"
    store = FeedbackStore(article_source_map={"ef567890": "Other Feed"})

    respx.post(f"https://api.telegram.org/bot{token}/deleteWebhook").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    respx.get(f"https://api.telegram.org/bot{token}/getUpdates").mock(
        return_value=httpx.Response(200, json={
            "ok": True,
            "result": [
                {
                    "update_id": 2001,
                    "callback_query": {
                        "id": "cq2",
                        "data": "fb:a:b:ef567890",
                        "from": {"id": 123},
                    },
                }
            ],
        })
    )
    respx.post(f"https://api.telegram.org/bot{token}/answerCallbackQuery").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )

    result = await collect_feedback(token, store)
    assert len(result.ratings) == 1
    assert result.ratings[0].rating == -1
    assert result.ratings[0].source_name == "Other Feed"


@pytest.mark.asyncio
@respx.mock
async def test_collect_feedback_non_feedback_callback_ignored() -> None:
    token = "testtoken"
    store = FeedbackStore()

    respx.post(f"https://api.telegram.org/bot{token}/deleteWebhook").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    respx.get(f"https://api.telegram.org/bot{token}/getUpdates").mock(
        return_value=httpx.Response(200, json={
            "ok": True,
            "result": [
                {
                    "update_id": 3001,
                    "callback_query": {
                        "id": "cq3",
                        "data": "other:action:1",
                        "from": {"id": 123},
                    },
                }
            ],
        })
    )

    result = await collect_feedback(token, store)
    assert result.last_update_id == 3001
    assert len(result.ratings) == 0


@pytest.mark.asyncio
@respx.mock
async def test_collect_feedback_empty_updates() -> None:
    token = "testtoken"
    store = FeedbackStore()

    respx.post(f"https://api.telegram.org/bot{token}/deleteWebhook").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    respx.get(f"https://api.telegram.org/bot{token}/getUpdates").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": []})
    )

    result = await collect_feedback(token, store)
    assert result.last_update_id == 0
    assert len(result.ratings) == 0


@pytest.mark.asyncio
@respx.mock
async def test_collect_feedback_per_article_unknown_hash_records_empty_source() -> None:
    """Per-article callback with unknown hash records rating with empty source_name."""
    token = "testtoken"
    store = FeedbackStore()  # empty article_source_map

    respx.post(f"https://api.telegram.org/bot{token}/deleteWebhook").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    respx.get(f"https://api.telegram.org/bot{token}/getUpdates").mock(
        return_value=httpx.Response(200, json={
            "ok": True,
            "result": [
                {
                    "update_id": 4001,
                    "callback_query": {
                        "id": "cq4",
                        "data": "fb:a:g:deadbeef",
                        "from": {"id": 123},
                    },
                }
            ],
        })
    )
    respx.post(f"https://api.telegram.org/bot{token}/answerCallbackQuery").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )

    result = await collect_feedback(token, store)
    assert result.last_update_id == 4001
    assert len(result.ratings) == 1
    assert result.ratings[0].source_name == ""
    assert result.ratings[0].rating == 1


@pytest.mark.asyncio
@respx.mock
async def test_collect_feedback_legacy_callback_answered_not_recorded() -> None:
    """Legacy fb:good:N callbacks are answered (spinner dismissed) but not recorded."""
    token = "testtoken"
    store = FeedbackStore()

    respx.post(f"https://api.telegram.org/bot{token}/deleteWebhook").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    respx.get(f"https://api.telegram.org/bot{token}/getUpdates").mock(
        return_value=httpx.Response(200, json={
            "ok": True,
            "result": [
                {
                    "update_id": 5001,
                    "callback_query": {
                        "id": "cq5",
                        "data": "fb:good:0",
                        "from": {"id": 123},
                    },
                }
            ],
        })
    )
    respx.post(f"https://api.telegram.org/bot{token}/answerCallbackQuery").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )

    result = await collect_feedback(token, store)
    assert result.last_update_id == 5001
    # Legacy callback answered but NOT recorded
    assert len(result.ratings) == 0


@pytest.mark.asyncio
@respx.mock
async def test_collect_feedback_uses_offset() -> None:
    token = "testtoken"
    store = FeedbackStore(last_update_id=500)

    route = respx.get(f"https://api.telegram.org/bot{token}/getUpdates").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": []})
    )

    await collect_feedback(token, store)

    # Verify offset parameter was sent
    request = route.calls[0].request
    assert "offset=501" in str(request.url)


@pytest.mark.asyncio
@respx.mock
async def test_collect_feedback_handles_failed_answer_callback() -> None:
    """Failed answer callback should not crash collection; offset still updates."""
    token = "testtoken"
    store = FeedbackStore(
        last_update_id=0,
        last_digest_sources=["TestFeed"],  # For legacy format (no digest_id)
    )

    updates_response = {
        "ok": True,
        "result": [
            {
                "update_id": 100,
                "callback_query": {
                    "id": "q1",
                    "from": {"id": 123},
                    "data": "fb:a:g:abcd1234",
                },
                "message": {"message_id": 1, "chat": {"id": 456}, "text": "test"},
            }
        ],
    }

    respx.post(f"https://api.telegram.org/bot{token}/deleteWebhook").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    respx.get(f"https://api.telegram.org/bot{token}/getUpdates").mock(
        return_value=httpx.Response(200, json=updates_response)
    )

    # Mock answer endpoint to fail for this update
    respx.post(f"https://api.telegram.org/bot{token}/answerCallbackQuery").mock(
        return_value=httpx.Response(500, json={"ok": False})
    )

    updated_store = await collect_feedback(token, store)

    # Despite the failed answer callback, last_update_id should advance
    # (because the exception handler in line 172-175 catches it and continues)
    assert updated_store.last_update_id == 100


@pytest.mark.asyncio
@respx.mock
async def test_collect_feedback_getupdates_not_ok() -> None:
    """When getUpdates returns ok=false, store is returned unchanged."""
    token = "testtoken"
    store = FeedbackStore(last_update_id=10, last_digest_sources=["Feed1"])

    respx.post(f"https://api.telegram.org/bot{token}/deleteWebhook").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    respx.get(f"https://api.telegram.org/bot{token}/getUpdates").mock(
        return_value=httpx.Response(200, json={"ok": False, "description": "Unauthorized"})
    )

    result = await collect_feedback(token, store)
    assert result.last_update_id == 10
    assert result.ratings == []


@pytest.mark.asyncio
@respx.mock
async def test_collect_feedback_network_error() -> None:
    """Network error during getUpdates should be caught and store returned unchanged."""
    token = "testtoken"
    store = FeedbackStore(last_update_id=5, last_digest_sources=["Feed1"])

    respx.post(f"https://api.telegram.org/bot{token}/deleteWebhook").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    respx.get(f"https://api.telegram.org/bot{token}/getUpdates").mock(
        side_effect=httpx.ConnectError("Connection refused")
    )

    result = await collect_feedback(token, store)
    assert result.last_update_id == 5
    assert result.ratings == []


# ---------------------------------------------------------------------------
# get_source_feedback_score
# ---------------------------------------------------------------------------


def test_get_source_feedback_score_unknown_source() -> None:
    store = FeedbackStore()
    assert get_source_feedback_score(store, "Unknown") is None


def test_get_source_feedback_score_all_positive() -> None:
    now = datetime.now(tz=timezone.utc).isoformat()
    store = FeedbackStore(ratings=[
        ArticleFeedback("h1", "Feed A", 1, now),
        ArticleFeedback("h2", "Feed A", 1, now),
        ArticleFeedback("h3", "Feed A", 1, now),
    ])
    score = get_source_feedback_score(store, "Feed A")
    assert score == 1.0


def test_get_source_feedback_score_all_negative() -> None:
    now = datetime.now(tz=timezone.utc).isoformat()
    store = FeedbackStore(ratings=[
        ArticleFeedback("h1", "Feed B", -1, now),
        ArticleFeedback("h2", "Feed B", -1, now),
    ])
    score = get_source_feedback_score(store, "Feed B")
    assert score == 0.0


def test_get_source_feedback_score_mixed() -> None:
    now = datetime.now(tz=timezone.utc).isoformat()
    store = FeedbackStore(ratings=[
        ArticleFeedback("h1", "Feed C", 1, now),
        ArticleFeedback("h2", "Feed C", -1, now),
    ])
    score = get_source_feedback_score(store, "Feed C")
    assert score == 0.5


def test_get_source_feedback_score_old_ratings_excluded() -> None:
    now = datetime.now(tz=timezone.utc)
    old = (now - timedelta(days=30)).isoformat()
    recent = now.isoformat()
    store = FeedbackStore(ratings=[
        ArticleFeedback("h1", "Feed D", -1, old),  # too old, excluded
        ArticleFeedback("h2", "Feed D", 1, recent),
    ])
    score = get_source_feedback_score(store, "Feed D", days=14)
    assert score == 1.0


def test_get_source_feedback_score_only_old_returns_none() -> None:
    old = (datetime.now(tz=timezone.utc) - timedelta(days=30)).isoformat()
    store = FeedbackStore(ratings=[
        ArticleFeedback("h1", "Feed E", 1, old),
    ])
    assert get_source_feedback_score(store, "Feed E", days=14) is None


# ---------------------------------------------------------------------------
# save_feedback pruning
# ---------------------------------------------------------------------------


def test_save_feedback_prunes_old_ratings(tmp_path: Path) -> None:
    """save_feedback should discard ratings older than 30 days."""
    old_ts = (datetime.now(tz=timezone.utc) - timedelta(days=31)).isoformat()
    fresh_ts = datetime.now(tz=timezone.utc).isoformat()
    store = FeedbackStore(ratings=[
        ArticleFeedback("h1", "Feed A", 1, old_ts),
        ArticleFeedback("h2", "Feed B", -1, fresh_ts),
    ])
    save_feedback(store, str(tmp_path))

    loaded = load_feedback(str(tmp_path))
    assert len(loaded.ratings) == 1
    assert loaded.ratings[0].source_name == "Feed B"


def test_save_feedback_keeps_all_recent_ratings(tmp_path: Path) -> None:
    """save_feedback should keep all ratings within 30 days."""
    now = datetime.now(tz=timezone.utc)
    store = FeedbackStore(ratings=[
        ArticleFeedback("h1", "Feed A", 1, (now - timedelta(days=29)).isoformat()),
        ArticleFeedback("h2", "Feed B", -1, (now - timedelta(days=1)).isoformat()),
    ])
    save_feedback(store, str(tmp_path))

    loaded = load_feedback(str(tmp_path))
    assert len(loaded.ratings) == 2


# ---------------------------------------------------------------------------
# article_source_map persistence
# ---------------------------------------------------------------------------


def test_article_source_map_round_trip(tmp_path: Path) -> None:
    """article_source_map is saved and loaded correctly."""
    store = FeedbackStore(article_source_map={"abcd1234": "Feed A", "ef567890": "Feed B"})
    save_feedback(store, str(tmp_path))
    loaded = load_feedback(str(tmp_path))
    assert loaded.article_source_map == {"abcd1234": "Feed A", "ef567890": "Feed B"}


def test_article_source_map_pruned_to_1000(tmp_path: Path) -> None:
    """article_source_map is pruned to 1000 entries on save (FIFO)."""
    store = FeedbackStore(
        article_source_map={f"hash{i:04d}": f"Source {i}" for i in range(1200)}
    )
    save_feedback(store, str(tmp_path))
    loaded = load_feedback(str(tmp_path))
    assert len(loaded.article_source_map) == 1000
    # Newest 1000 entries kept (hash0200 .. hash1199)
    assert "hash0000" not in loaded.article_source_map
    assert "hash1199" in loaded.article_source_map


def test_article_source_map_missing_key_loads_empty(tmp_path: Path) -> None:
    """Feedback file without article_source_map key loads as empty dict."""
    import json as _json
    data = {"last_update_id": 5, "ratings": []}
    (tmp_path / "feedback.json").write_text(_json.dumps(data), encoding="utf-8")
    loaded = load_feedback(str(tmp_path))
    assert loaded.article_source_map == {}


# ---------------------------------------------------------------------------
# source_decisions persistence
# ---------------------------------------------------------------------------


def test_source_decisions_round_trip(tmp_path: Path) -> None:
    store = FeedbackStore(source_decisions={"abc12345": "approved", "def67890": "rejected"})
    save_feedback(store, str(tmp_path))
    loaded = load_feedback(str(tmp_path))
    assert loaded.source_decisions == {"abc12345": "approved", "def67890": "rejected"}


def test_source_decisions_default_empty(tmp_path: Path) -> None:
    store = FeedbackStore()
    save_feedback(store, str(tmp_path))
    loaded = load_feedback(str(tmp_path))
    assert loaded.source_decisions == {}


def test_source_decisions_missing_key_loads_empty(tmp_path: Path) -> None:
    """Feedback file without source_decisions key loads as empty dict (backward compat)."""
    import json as _json
    data = {"last_update_id": 5, "ratings": []}
    (tmp_path / "feedback.json").write_text(_json.dumps(data), encoding="utf-8")
    loaded = load_feedback(str(tmp_path))
    assert loaded.source_decisions == {}


@pytest.mark.asyncio
@respx.mock
async def test_collect_feedback_src_ok_callback() -> None:
    """src:ok:HASH callback stores 'approved' decision."""
    token = "testtoken"
    store = FeedbackStore()

    respx.post(f"https://api.telegram.org/bot{token}/deleteWebhook").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    respx.get(f"https://api.telegram.org/bot{token}/getUpdates").mock(
        return_value=httpx.Response(200, json={
            "ok": True,
            "result": [
                {
                    "update_id": 10001,
                    "callback_query": {
                        "id": "cq10",
                        "data": "src:ok:abcd1234",
                        "from": {"id": 123},
                    },
                }
            ],
        })
    )
    respx.post(f"https://api.telegram.org/bot{token}/answerCallbackQuery").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )

    result = await collect_feedback(token, store)
    assert result.last_update_id == 10001
    assert result.source_decisions == {"abcd1234": "approved"}
    assert len(result.ratings) == 0  # no digest ratings added


@pytest.mark.asyncio
@respx.mock
async def test_collect_feedback_src_no_callback() -> None:
    """src:no:HASH callback stores 'rejected' decision."""
    token = "testtoken"
    store = FeedbackStore()

    respx.post(f"https://api.telegram.org/bot{token}/deleteWebhook").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    respx.get(f"https://api.telegram.org/bot{token}/getUpdates").mock(
        return_value=httpx.Response(200, json={
            "ok": True,
            "result": [
                {
                    "update_id": 10002,
                    "callback_query": {
                        "id": "cq11",
                        "data": "src:no:efgh5678",
                        "from": {"id": 123},
                    },
                }
            ],
        })
    )
    respx.post(f"https://api.telegram.org/bot{token}/answerCallbackQuery").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )

    result = await collect_feedback(token, store)
    assert result.last_update_id == 10002
    assert result.source_decisions == {"efgh5678": "rejected"}


# ---------------------------------------------------------------------------
# /status command handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_collect_feedback_status_command() -> None:
    """/status message triggers a sendMessage reply with digest info."""
    token = "testtoken"
    chat_id = "99999"
    store = FeedbackStore(
        last_digest_time="2026-03-20 08:00 UTC",
        last_digest_sources=["Source A", "Source B", "Source C"],
    )

    respx.post(f"https://api.telegram.org/bot{token}/deleteWebhook").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    respx.get(f"https://api.telegram.org/bot{token}/getUpdates").mock(
        return_value=httpx.Response(200, json={
            "ok": True,
            "result": [
                {
                    "update_id": 9001,
                    "message": {
                        "message_id": 1,
                        "text": "/status",
                        "chat": {"id": int(chat_id)},
                        "from": {"id": 123},
                    },
                }
            ],
        })
    )
    send_route = respx.post(f"https://api.telegram.org/bot{token}/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )

    result = await collect_feedback(token, store)
    assert result.last_update_id == 9001
    assert len(result.ratings) == 0  # no feedback ratings added for /status

    assert send_route.called
    sent_payload = send_route.calls[0].request
    import json as _json
    body = _json.loads(sent_payload.content)
    assert body["chat_id"] == chat_id
    assert "2026-03-20 08:00 UTC" in body["text"]
    assert "3" in body["text"]  # source count
