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
async def test_collect_feedback_good_rating() -> None:
    token = "testtoken"
    store = FeedbackStore()

    respx.get(f"https://api.telegram.org/bot{token}/getUpdates").mock(
        return_value=httpx.Response(200, json={
            "ok": True,
            "result": [
                {
                    "update_id": 1001,
                    "callback_query": {
                        "id": "cq1",
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
    assert result.last_update_id == 1001
    assert len(result.ratings) == 1
    assert result.ratings[0].rating == 1
    assert result.ratings[0].article_hash == "fb:good:0"


@pytest.mark.asyncio
@respx.mock
async def test_collect_feedback_bad_rating() -> None:
    token = "testtoken"
    store = FeedbackStore()

    respx.get(f"https://api.telegram.org/bot{token}/getUpdates").mock(
        return_value=httpx.Response(200, json={
            "ok": True,
            "result": [
                {
                    "update_id": 2001,
                    "callback_query": {
                        "id": "cq2",
                        "data": "fb:bad:0",
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


@pytest.mark.asyncio
@respx.mock
async def test_collect_feedback_non_feedback_callback_ignored() -> None:
    token = "testtoken"
    store = FeedbackStore()

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

    respx.get(f"https://api.telegram.org/bot{token}/getUpdates").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": []})
    )

    result = await collect_feedback(token, store)
    assert result.last_update_id == 0
    assert len(result.ratings) == 0


@pytest.mark.asyncio
@respx.mock
async def test_collect_feedback_four_part_format_with_digest_id() -> None:
    """4-part callback (fb:good:N:digest_id) attributes feedback to correct sources."""
    token = "testtoken"
    store = FeedbackStore(
        digest_sources_map={"20260318_101530": ["Source A", "Source B"]},
    )

    respx.get(f"https://api.telegram.org/bot{token}/getUpdates").mock(
        return_value=httpx.Response(200, json={
            "ok": True,
            "result": [
                {
                    "update_id": 4001,
                    "callback_query": {
                        "id": "cq4",
                        "data": "fb:good:0:20260318_101530",
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
    # Should create one rating per source in the digest
    assert len(result.ratings) == 2
    source_names = {r.source_name for r in result.ratings}
    assert source_names == {"Source A", "Source B"}
    assert all(r.rating == 1 for r in result.ratings)


@pytest.mark.asyncio
@respx.mock
async def test_collect_feedback_unknown_digest_id_records_unscoped() -> None:
    """4-part callback with unknown digest_id records unscoped feedback."""
    token = "testtoken"
    store = FeedbackStore()

    respx.get(f"https://api.telegram.org/bot{token}/getUpdates").mock(
        return_value=httpx.Response(200, json={
            "ok": True,
            "result": [
                {
                    "update_id": 5001,
                    "callback_query": {
                        "id": "cq5",
                        "data": "fb:bad:0:unknown_digest_id",
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
    assert len(result.ratings) == 1
    assert result.ratings[0].source_name == ""
    assert result.ratings[0].rating == -1


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
                    "data": "fb:good:0",  # Valid format: fb:{good|bad}:{chunk_index}
                },
                "message": {"message_id": 1, "chat": {"id": 456}, "text": "test"},
            }
        ],
    }

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
# Per-category feedback (5-part callback_data)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_collect_feedback_category_good() -> None:
    """5-part fb:cat:good callback attributes rating to sources in that category."""
    from src.feedback import _category_hash

    token = "testtoken"
    cat_hash = _category_hash("AI & LLM")
    digest_id = "20260320_120000"
    store = FeedbackStore(
        digest_category_sources_map={
            digest_id: {"AI & LLM": ["Source A", "Source B"]}
        }
    )

    respx.get(f"https://api.telegram.org/bot{token}/getUpdates").mock(
        return_value=httpx.Response(200, json={
            "ok": True,
            "result": [
                {
                    "update_id": 6001,
                    "callback_query": {
                        "id": "cq6",
                        "data": f"fb:cat:good:{cat_hash}:{digest_id}",
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
    assert result.last_update_id == 6001
    assert len(result.ratings) == 2
    assert {r.source_name for r in result.ratings} == {"Source A", "Source B"}
    assert all(r.rating == 1 for r in result.ratings)


@pytest.mark.asyncio
@respx.mock
async def test_collect_feedback_category_bad() -> None:
    """5-part fb:cat:bad callback records negative rating for sources in category."""
    from src.feedback import _category_hash

    token = "testtoken"
    cat_hash = _category_hash("Fintech")
    digest_id = "20260320_130000"
    store = FeedbackStore(
        digest_category_sources_map={
            digest_id: {"Fintech": ["Finextra"]}
        }
    )

    respx.get(f"https://api.telegram.org/bot{token}/getUpdates").mock(
        return_value=httpx.Response(200, json={
            "ok": True,
            "result": [
                {
                    "update_id": 7001,
                    "callback_query": {
                        "id": "cq7",
                        "data": f"fb:cat:bad:{cat_hash}:{digest_id}",
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
    assert result.last_update_id == 7001
    assert len(result.ratings) == 1
    assert result.ratings[0].source_name == "Finextra"
    assert result.ratings[0].rating == -1


@pytest.mark.asyncio
@respx.mock
async def test_collect_feedback_category_unknown_hash_records_unscoped() -> None:
    """5-part callback with unrecognised category hash records unscoped feedback."""
    token = "testtoken"
    digest_id = "20260320_140000"
    store = FeedbackStore(
        digest_category_sources_map={
            digest_id: {"Cloud": ["AWS Blog"]}
        }
    )

    respx.get(f"https://api.telegram.org/bot{token}/getUpdates").mock(
        return_value=httpx.Response(200, json={
            "ok": True,
            "result": [
                {
                    "update_id": 8001,
                    "callback_query": {
                        "id": "cq8",
                        "data": f"fb:cat:good:deadbeef:{digest_id}",
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
    assert result.last_update_id == 8001
    assert len(result.ratings) == 1
    assert result.ratings[0].source_name == ""
    assert result.ratings[0].rating == 1


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
