"""Tests for feedback data models and functions."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest
import respx

from digest.discovery import PendingSource, proposal_binding, save_pending
from digest.feedback import (
    ArticleFeedback,
    FeedbackStore,
    PendingReply,
    acknowledge_feedback,
    collect_feedback,
    get_source_feedback_score,
    load_feedback,
    save_feedback,
)

FULL_ARTICLE_A = "cdb96691fa65888074dc4008dd039e3f"
FULL_ARTICLE_B = "cdb96691b648d61a1fbaba5907095a7d"
PRIOR_POLL = "2026-10-01T08:00:00+00:00"

# ---------------------------------------------------------------------------
# load_feedback / save_feedback round-trip
# ---------------------------------------------------------------------------


def test_load_feedback_missing_file(tmp_path: Path) -> None:
    store = load_feedback(str(tmp_path))
    assert store.ratings == []
    assert store.last_update_id == 0
    assert store.pending_replies == [] and store.seen_callback_ids == [] and store.last_poll_counts == {}
    assert store.seen_message_ids == []
    assert store.last_successful_poll_at == ""


def test_save_and_load_feedback_round_trip(tmp_path: Path) -> None:
    now = datetime.now(tz=timezone.utc)
    fb1 = ArticleFeedback(
        article_hash=FULL_ARTICLE_A[:8],
        source_name="Source A",
        rating=1,
        timestamp=(now - timedelta(hours=2)).isoformat(),
    )
    fb2 = ArticleFeedback(
        article_hash=FULL_ARTICLE_A,
        source_name="Source B",
        rating=-1,
        timestamp=(now - timedelta(hours=1)).isoformat(),
    )
    original = FeedbackStore(
        ratings=[fb1, fb2],
        last_update_id=100,
        article_source_map={FULL_ARTICLE_A[:8]: "Source A", FULL_ARTICLE_A: "Source B"},
        source_decisions={"abc12345": "approved", "def67890": "rejected"},
        source_decision_bindings={"abc12345": "a" * 64, "def67890": "b" * 64},
        pending_replies=[
            PendingReply("callback", "receipt"),
            PendingReply("vote", "recorded_votes"),
            PendingReply("source", "source_decisions"),
            PendingReply("source", "unknown_source"),
        ],
        seen_callback_ids=[str(index) for index in range(1005)],
        seen_message_ids=[f"owner:{index}" for index in range(1005)],
        last_successful_poll_at=PRIOR_POLL,
    )
    save_feedback(original, str(tmp_path))

    loaded = load_feedback(str(tmp_path))
    assert loaded.ratings == original.ratings
    assert loaded.article_source_map == original.article_source_map
    assert loaded.source_decisions == {"abc12345": "approved", "def67890": "rejected"}
    assert loaded.source_decision_bindings == original.source_decision_bindings
    assert loaded.last_update_id == 100
    assert len(loaded.ratings) == 2
    assert loaded.ratings[0].source_name == "Source A"
    assert loaded.ratings[0].rating == 1
    assert loaded.ratings[1].source_name == "Source B"
    assert loaded.ratings[1].rating == -1
    assert loaded.pending_replies == original.pending_replies
    assert len(loaded.seen_callback_ids) == 1000 and loaded.seen_callback_ids[0] == "5"
    assert len(loaded.seen_message_ids) == 1000 and loaded.seen_message_ids[0] == "owner:5"
    assert loaded.last_successful_poll_at == original.last_successful_poll_at == PRIOR_POLL


def test_load_feedback_invalid_json(tmp_path: Path) -> None:
    path = tmp_path / "feedback.json"
    path.write_text("not valid json", encoding="utf-8")
    store = load_feedback(str(tmp_path))
    assert store.ratings == []
    with pytest.raises(ValueError):
        load_feedback(str(tmp_path), strict=True)


def test_load_feedback_non_dict(tmp_path: Path) -> None:
    path = tmp_path / "feedback.json"
    path.write_text("[]", encoding="utf-8")
    store = load_feedback(str(tmp_path))
    assert store.ratings == []
    with pytest.raises(ValueError):
        load_feedback(str(tmp_path), strict=True)


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
    with pytest.raises(ValueError):
        load_feedback(str(tmp_path), strict=True)


def test_save_feedback_preserves_existing_on_write(tmp_path: Path) -> None:
    """Existing feedback.json is intact before and after a successful save."""
    original = FeedbackStore(last_update_id=42)
    save_feedback(original, str(tmp_path))
    assert not (tmp_path / "feedback.json.tmp").exists()

    updated = FeedbackStore(last_update_id=99)
    save_feedback(updated, str(tmp_path))

    loaded = load_feedback(str(tmp_path))
    assert loaded.last_update_id == 99
    assert not (tmp_path / "feedback.json.tmp").exists()


# ---------------------------------------------------------------------------
# Durable collection and acknowledgment
# ---------------------------------------------------------------------------

TOKEN = "testtoken"
API = f"https://api.telegram.org/bot{TOKEN}"


@pytest.fixture(autouse=True)
def feedback_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")


def _callback(update_id: int, data: str = "fb:a:g:abcd1234", identifier: str = "") -> dict[str, Any]:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": identifier or f"callback-{update_id}",
            "data": data,
            "from": {"id": 123},
            "message": {"message_id": 1, "chat": {"id": 123, "type": "private"}},
        },
    }


def _message(update_id: int, text: str, message_id: int) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": message_id,
            "text": text,
            "from": {"id": 123},
            "chat": {"id": 123, "type": "private"},
        },
    }


def _poll(updates: list[dict[str, Any]]) -> respx.Route:
    respx.get(f"{API}/getWebhookInfo").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"url": ""}}),
    )
    return respx.post(f"{API}/getUpdates").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": updates}),
    )


def _sha(cache_dir: Path) -> str:
    return hashlib.sha256((cache_dir / "feedback.json").read_bytes()).hexdigest()


@pytest.mark.asyncio
@respx.mock
async def test_collect_feedback_per_article_good(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Persist the rating, offset and receipt before ack; replay adds no votes."""
    caplog.set_level(logging.INFO, logger="httpx")
    store = FeedbackStore(article_source_map={FULL_ARTICLE_A: "My Source"})
    route = _poll([_callback(1001, f"fb:a:g:{FULL_ARTICLE_A}")])

    def answer(request: httpx.Request) -> httpx.Response:
        durable = load_feedback(str(tmp_path), strict=True)
        assert durable.last_update_id >= 1001
        assert durable.ratings[0].article_hash == FULL_ARTICLE_A
        assert durable.ratings[0].source_name == "My Source"
        assert durable.pending_replies
        assert json.loads(request.content)["callback_query_id"] in durable.seen_callback_ids
        return httpx.Response(200, json={"ok": True})

    respx.post(f"{API}/answerCallbackQuery").mock(side_effect=answer)
    result = await collect_feedback(TOKEN, store, cache_dir=str(tmp_path))
    assert result.ratings[0].rating == 1
    assert result.pending_replies == []
    assert store.ratings == [] and store.last_update_id == 0
    route.mock(
        return_value=httpx.Response(
            200,
            json={
                "ok": True,
                "result": [
                    _callback(1001, f"fb:a:g:{FULL_ARTICLE_A}"),
                    _callback(1002, f"fb:a:g:{FULL_ARTICLE_A}", identifier="callback-1001"),
                    _callback(1003, f"fb:a:b:{FULL_ARTICLE_A}"),
                ],
            },
        )
    )
    result = await collect_feedback(TOKEN, result, cache_dir=str(tmp_path))
    assert [rating.rating for rating in result.ratings] == [1, -1]
    assert result.last_poll_counts["duplicates"] == 2
    assert result.last_update_id == 1003
    assert TOKEN not in caplog.text
    assert logging.getLogger("httpx").level == logging.INFO


@pytest.mark.asyncio
@respx.mock
async def test_collect_feedback_per_article_bad(tmp_path: Path) -> None:
    """Managed ingestion writes pending UI work without sending it."""
    store = FeedbackStore(article_source_map={"abcd1234": "My Source"})
    _poll([_callback(1001, "fb:a:b:abcd1234")])
    result = await collect_feedback(TOKEN, store, cache_dir=str(tmp_path), acknowledge=False)
    assert result.ratings[0].rating == -1
    assert result.pending_replies == [PendingReply("callback", "callback-1001")]
    assert result.pending_owner_sha256 == hashlib.sha256(b"123").hexdigest()
    assert load_feedback(str(tmp_path), strict=True) == result
    assert not any("answerCallbackQuery" in str(call.request.url) for call in respx.calls)
    # An unacknowledged prior batch is terminal UI work, never an unbounded queue.
    _poll([_callback(1002, "fb:a:g:abcd1234")])
    latest = await collect_feedback(TOKEN, result, cache_dir=str(tmp_path), acknowledge=False)
    assert [reply.identifier for reply in latest.pending_replies] == ["callback-1002"]
    assert len(latest.ratings) == 2 and latest.last_poll_counts["superseded_replies"] == 1


@pytest.mark.asyncio
@respx.mock
async def test_collect_feedback_vote_messages_are_durable_and_latest_wins(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Message replay cannot replace a later vote, even after cursor reanchoring."""
    owner_hash = hashlib.sha256(b"123").hexdigest()
    store = FeedbackStore(
        article_source_map={FULL_ARTICLE_A: "My Source"},
        seen_callback_ids=[f"{owner_hash}:100"],
    )
    route = _poll([_message(1001, f"/start vote_g_{FULL_ARTICLE_A} \n", 100)])
    result = await collect_feedback(TOKEN, store, cache_dir=str(tmp_path), acknowledge=False)
    assert store.ratings == [] and store.seen_message_ids == []
    assert get_source_feedback_score(result, "My Source") == 1.0
    assert result.seen_message_ids == [f"{owner_hash}:100"]
    assert result.pending_replies == [PendingReply("vote", "recorded_votes")]
    assert result.pending_owner_sha256 == owner_hash
    assert not any("sendMessage" in str(call.request.url) for call in respx.calls)
    with pytest.raises(ValueError, match="SHA-256"):
        await acknowledge_feedback(TOKEN, str(tmp_path), "0" * 64)

    def answer(request: httpx.Request) -> httpx.Response:
        durable = load_feedback(str(tmp_path), strict=True)
        assert durable.ratings and durable.seen_message_ids and durable.pending_replies
        assert durable.ratings[0].article_hash == FULL_ARTICLE_A
        saved = sum(reply.identifier == "recorded_votes" for reply in durable.pending_replies)
        unknown = len(durable.pending_replies) - saved
        assert json.loads(request.content) == {
            "chat_id": "123",
            "text": f"Votes saved: {saved}. Unknown articles: {unknown}.",
        }
        return httpx.Response(200, json={"ok": True})

    send = respx.post(f"{API}/sendMessage").mock(side_effect=answer)
    assert await acknowledge_feedback(TOKEN, str(tmp_path), _sha(tmp_path)) == {
        "attempted": 1,
        "ack_ok": 1,
        "ack_failed": 0,
    }
    result = load_feedback(str(tmp_path), strict=True)
    _poll(
        [
            _message(1002, f"/vote b {FULL_ARTICLE_A} \n", 101),
            _message(1003, f"/start vote_g_{FULL_ARTICLE_A}", 100),
            _message(1004, "/start vote_b_deadbeef", 102),
        ]
    )
    result = await collect_feedback(TOKEN, result, cache_dir=str(tmp_path))
    assert [rating.rating for rating in result.ratings] == [1, -1]
    assert get_source_feedback_score(result, "My Source") == 0.0
    assert result.last_poll_counts["duplicates"] == 1 and send.call_count == 2
    assert json.loads(send.calls[-1].request.content)["text"] == "Votes saved: 1. Unknown articles: 1."
    assert result.pending_replies == []

    result.cursor_observed_at = ""
    save_feedback(result, str(tmp_path), strict=True)
    _poll([_message(2, f"/start vote_g_{FULL_ARTICLE_A}", 100)])
    result = await collect_feedback(TOKEN, result, cache_dir=str(tmp_path), acknowledge=False)
    assert "offset" not in json.loads(route.calls[-1].request.content)
    assert len(result.ratings) == 2 and result.last_poll_counts["duplicates"] == 1
    assert get_source_feedback_score(result, "My Source") == 0.0

    monkeypatch.setenv("TELEGRAM_CHAT_ID", "456")
    new_owner = _message(3, f"/vote g {FULL_ARTICLE_A}", 100)
    new_owner["message"]["chat"]["id"] = new_owner["message"]["from"]["id"] = 456
    _poll([new_owner])
    result = await collect_feedback(TOKEN, result, cache_dir=str(tmp_path), acknowledge=False)
    assert len(result.ratings) == 3 and get_source_feedback_score(result, "My Source") == 1.0
    assert result.seen_message_ids[-1] == f"{hashlib.sha256(b'456').hexdigest()}:100"
    persisted = (tmp_path / "feedback.json").read_text()
    assert "/start vote_" not in persisted and "/vote " not in persisted


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize("transport", ["start", "command", "callback"])
async def test_full_vote_identity_preserves_shared_prefix_attribution(tmp_path: Path, transport: str) -> None:
    """Each owner-only route stores the exact delivered identity and source."""
    assert FULL_ARTICLE_A != FULL_ARTICLE_B and FULL_ARTICLE_A[:8] == FULL_ARTICLE_B[:8]
    bindings = {FULL_ARTICLE_A: "Source A", FULL_ARTICLE_B: "Source B"}
    updates = []
    for update_id, (identity, rating) in enumerate(
        [
            (FULL_ARTICLE_A, "g"),
            (FULL_ARTICLE_B, "b"),
            (FULL_ARTICLE_A, "b"),
        ],
        1,
    ):
        if transport == "callback":
            update = _callback(update_id, f"fb:a:{rating}:{identity}")
        else:
            text = f"/start vote_{rating}_{identity}" if transport == "start" else f"/vote {rating} {identity}"
            update = _message(update_id, text, update_id)
        updates.append(update)
    updates[-1]["callback_query" if transport == "callback" else "message"]["from"]["id"] = 456
    _poll(updates)

    result = await collect_feedback(
        TOKEN,
        FeedbackStore(article_source_map=bindings),
        cache_dir=str(tmp_path),
        acknowledge=False,
    )
    durable = load_feedback(str(tmp_path), strict=True)
    assert durable == result
    assert durable.article_source_map == bindings
    assert [(rating.article_hash, rating.source_name, rating.rating) for rating in durable.ratings] == [
        (FULL_ARTICLE_A, "Source A", 1),
        (FULL_ARTICLE_B, "Source B", -1),
    ]
    assert durable.last_poll_counts["recorded_votes"] == 2
    assert durable.last_poll_counts["rejected_owner"] == 1
    assert not any(
        endpoint in str(call.request.url) for call in respx.calls for endpoint in ("answerCallbackQuery", "sendMessage")
    )


@pytest.mark.asyncio
@respx.mock
async def test_collect_feedback_vote_messages_fail_closed(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    unknown = _message(1, "/start vote_b_deadbeef", 1)
    malformed = _message(2, "/vote g abcd1234\nextra", 2)
    quoted = _message(3, "a private reply", 3)
    quoted["message"]["reply_to_message"] = {"text": "/vote g abcd1234"}
    foreign_sender, foreign_chat, group, no_id, boolean_id = [
        _message(index, "/start vote_g_abcd1234", index) for index in range(4, 9)
    ]
    foreign_sender["message"]["from"]["id"] = 456
    foreign_chat["message"]["chat"]["id"] = 456
    group["message"]["chat"]["type"] = "group"
    del no_id["message"]["message_id"]
    boolean_id["message"]["message_id"] = True
    _poll(
        [
            unknown,
            malformed,
            quoted,
            foreign_sender,
            foreign_chat,
            group,
            no_id,
            boolean_id,
            _message(9, "/start vote_g_ABCD1234", 9),
            _message(10, "/vote x abcd1234", 10),
        ]
    )
    result = await collect_feedback(
        TOKEN,
        FeedbackStore(article_source_map={"abcd1234": "My Source"}),
        cache_dir=str(tmp_path),
        acknowledge=False,
    )
    assert result.ratings == []
    assert result.last_poll_counts["unknown_article"] == 1
    assert result.last_poll_counts["malformed"] == 5
    assert result.last_poll_counts["rejected_owner"] == 3
    assert result.last_poll_counts["ignored"] == 1
    assert len(result.seen_message_ids) == 1 and result.seen_callback_ids == []
    assert result.pending_replies == [PendingReply("vote", "unknown_article")]
    persisted = (tmp_path / "feedback.json").read_text()
    assert "deadbeef" not in persisted and "a private reply" not in persisted and "/vote " not in persisted
    assert "deadbeef" not in caplog.text and "a private reply" not in caplog.text
    send = respx.post(f"{API}/sendMessage").mock(return_value=httpx.Response(200, json={"ok": True}))
    await acknowledge_feedback(TOKEN, str(tmp_path), _sha(tmp_path))
    assert json.loads(send.calls[0].request.content)["text"] == "Votes saved: 0. Unknown articles: 1."


@pytest.mark.asyncio
@respx.mock
async def test_collect_feedback_non_feedback_callback_ignored(tmp_path: Path) -> None:
    """Foreign senders, groups, inline callbacks and malformed data fail closed."""
    foreign_sender, foreign_chat, group, inline = [_callback(index) for index in range(1, 5)]
    foreign_sender["callback_query"]["from"]["id"] = 456
    foreign_chat["callback_query"]["message"]["chat"]["id"] = 456
    group["callback_query"]["message"]["chat"]["type"] = "group"
    del inline["callback_query"]["message"]
    updates = [
        foreign_sender,
        foreign_chat,
        group,
        inline,
        _callback(5, "fb:a:g:bad"),
        _callback(6, "other:action:1"),
        _callback(7, "src:ok:not-a-hash"),
    ]
    _poll(updates)
    result = await collect_feedback(TOKEN, FeedbackStore(), cache_dir=str(tmp_path), acknowledge=False)
    assert result.ratings == [] and result.source_decisions == {} and result.pending_replies == []
    assert result.last_update_id == 7
    assert result.last_poll_counts["rejected_owner"] == 4
    assert result.last_poll_counts["malformed"] == 2
    assert result.last_poll_counts["ignored"] == 1


@pytest.mark.asyncio
@respx.mock
async def test_collect_feedback_empty_updates(tmp_path: Path) -> None:
    route = _poll([])
    store = FeedbackStore(last_successful_poll_at=PRIOR_POLL)
    started = datetime(2026, 10, 10, 8, tzinfo=timezone.utc)
    observed = started + timedelta(seconds=2)
    with (
        patch("digest.application.feedback.datetime", wraps=datetime) as clock,
        patch("digest.application.feedback.save_feedback", wraps=save_feedback) as save,
    ):
        clock.now.side_effect = [started, observed]
        result = await collect_feedback(TOKEN, store, cache_dir=str(tmp_path))
    assert clock.now.call_count == 2 and save.call_count == 1
    assert result.last_update_id == 0 and result.ratings == []
    assert route.call_count == 1
    assert len(respx.calls) == 2  # Existing webhook check and one bounded poll only.
    assert json.loads(route.calls[0].request.content)["limit"] == 100
    assert result.last_poll_counts["received"] == 0
    assert result.cursor_observed_at == ""
    assert result.last_successful_poll_at == observed.isoformat()
    assert load_feedback(str(tmp_path), strict=True).last_successful_poll_at == observed.isoformat()
    assert store.last_successful_poll_at == PRIOR_POLL


@pytest.mark.asyncio
@respx.mock
async def test_collect_feedback_per_article_unknown_hash_records_empty_source(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _poll([_callback(4001, "fb:a:g:deadbeef")])
    with caplog.at_level(logging.WARNING, logger="digest.adapters.telegram.feedback"):
        result = await collect_feedback(TOKEN, FeedbackStore(), cache_dir=str(tmp_path), acknowledge=False)
    assert result.ratings == []
    assert result.last_poll_counts["unknown_article"] == 1
    assert result.last_poll_counts["recorded_votes"] == 0
    assert result.pending_replies == [PendingReply("callback", "callback-4001", "Article can no longer be matched")]
    assert result.seen_callback_ids == ["callback-4001"]
    assert get_source_feedback_score(result, "My Source") is None
    assert "attribution unavailable" in caplog.text
    assert "deadbeef" not in caplog.text and "callback-4001" not in caplog.text


@pytest.mark.asyncio
@respx.mock
async def test_collect_feedback_legacy_callback_answered_not_recorded(tmp_path: Path) -> None:
    _poll([_callback(5001, "fb:good:0")])
    answer = respx.post(f"{API}/answerCallbackQuery").mock(return_value=httpx.Response(200, json={"ok": True}))
    result = await collect_feedback(TOKEN, FeedbackStore(), cache_dir=str(tmp_path))
    assert result.ratings == [] and result.last_update_id == 5001
    assert answer.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_collect_feedback_uses_offset(tmp_path: Path) -> None:
    """Legacy/stale cursors reanchor without acknowledging a newer ID generation."""
    store = FeedbackStore(
        last_update_id=500,
        seen_callback_ids=["old-receipt"],
        article_source_map={"abcd1234": "My Source"},
        last_successful_poll_at=PRIOR_POLL,
    )
    route = _poll([])
    result = await collect_feedback(TOKEN, store, cache_dir=str(tmp_path), acknowledge=False)
    assert "offset" not in json.loads(route.calls[0].request.content)
    assert result.last_update_id == 500 and result.cursor_observed_at == ""
    assert result.last_successful_poll_at != PRIOR_POLL
    route.mock(
        return_value=httpx.Response(
            200,
            json={
                "ok": True,
                "result": [_callback(10, identifier="old-receipt"), _callback(11)],
            },
        )
    )
    result = await collect_feedback(TOKEN, result, cache_dir=str(tmp_path), acknowledge=False)
    assert "offset" not in json.loads(route.calls[1].request.content)
    assert result.last_update_id == 11 and result.previous_update_id == 500 and result.cursor_observed_at
    assert len(result.ratings) == 1 and result.last_poll_counts["duplicates"] == 1
    route.mock(return_value=httpx.Response(200, json={"ok": True, "result": [_callback(10)]}))
    anchored = result.cursor_observed_at
    duplicate_poll_at = datetime.now(timezone.utc) + timedelta(seconds=1)
    with patch("digest.application.feedback.datetime", wraps=datetime) as clock:
        clock.now.return_value = duplicate_poll_at
        result = await collect_feedback(TOKEN, result, cache_dir=str(tmp_path), acknowledge=False)
    assert json.loads(route.calls[2].request.content)["offset"] == 12
    assert len(result.ratings) == 1 and result.cursor_observed_at == anchored
    assert result.last_successful_poll_at == duplicate_poll_at.isoformat()
    assert result.pending_replies == [] and result.last_poll_counts["superseded_replies"] == 1
    result.cursor_observed_at = (datetime.now(tz=timezone.utc) - timedelta(days=6)).isoformat()
    save_feedback(result, str(tmp_path), strict=True)
    route.mock(return_value=httpx.Response(200, json={"ok": True, "result": [_callback(2)]}))
    result = await collect_feedback(TOKEN, result, cache_dir=str(tmp_path), acknowledge=False)
    assert "offset" not in json.loads(route.calls[3].request.content)
    assert result.last_update_id == 2 and result.previous_update_id == 11


@pytest.mark.asyncio
@respx.mock
async def test_collect_feedback_handles_failed_answer_callback(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Real nested callbacks retain their votes after HTTP/API ack failures."""
    route = _poll([_callback(100), _callback(101)])
    result = await collect_feedback(
        TOKEN,
        FeedbackStore(article_source_map={"abcd1234": "My Source"}),
        cache_dir=str(tmp_path),
        acknowledge=False,
    )
    respx.post(f"{API}/answerCallbackQuery").mock(
        side_effect=[
            httpx.Response(500, json={"ok": False}),
            httpx.Response(200, json={"ok": False, "description": "query expired"}),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    with caplog.at_level(logging.WARNING, logger="digest.adapters.telegram.feedback"):
        counts = await acknowledge_feedback(TOKEN, str(tmp_path), _sha(tmp_path))
    assert counts == {"attempted": 2, "ack_ok": 0, "ack_failed": 2}
    durable = load_feedback(str(tmp_path), strict=True)
    assert len(durable.ratings) == 2 and durable.last_update_id == 101 and durable.pending_replies == []
    assert durable.last_successful_poll_at == result.last_successful_poll_at != ""
    assert "callback-" not in caplog.text and TOKEN not in caplog.text
    route.mock(return_value=httpx.Response(200, json={"ok": True, "result": [_callback(102)]}))
    result = await collect_feedback(TOKEN, durable, cache_dir=str(tmp_path))
    assert len(result.ratings) == 3 and result.last_update_id == 102
    route.mock(
        return_value=httpx.Response(
            200,
            json={
                "ok": True,
                "result": [
                    _callback(103),
                    _callback(104),
                    _message(105, "/vote g deadbeef", 105),
                    _message(106, "/vote b deadbeef", 106),
                ],
            },
        )
    )
    result = await collect_feedback(TOKEN, result, cache_dir=str(tmp_path), acknowledge=False)

    async def slow_answer(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(1)
        return httpx.Response(200, json={"ok": True})

    respx.post(f"{API}/answerCallbackQuery").mock(side_effect=slow_answer)
    real_timeout = asyncio.timeout
    with patch("digest.adapters.telegram.feedback.asyncio.timeout", side_effect=lambda _: real_timeout(0.001)):
        counts = await acknowledge_feedback(TOKEN, str(tmp_path), _sha(tmp_path))
    assert counts == {"attempted": 3, "ack_ok": 0, "ack_failed": 3}
    durable = load_feedback(str(tmp_path), strict=True)
    assert len(durable.ratings) == 5 and durable.last_update_id == 106 and durable.pending_replies == []
    assert durable.last_successful_poll_at == result.last_successful_poll_at != ""


@pytest.mark.asyncio
@respx.mock
async def test_collect_feedback_getupdates_not_ok(tmp_path: Path) -> None:
    """Reject the complete malformed envelope before recording any update."""
    store = FeedbackStore(last_update_id=10, last_successful_poll_at=PRIOR_POLL)
    save_feedback(store, str(tmp_path))
    original = (tmp_path / "feedback.json").read_bytes()
    with pytest.raises(ValueError, match="store changed"):
        await collect_feedback(TOKEN, FeedbackStore(last_update_id=999), cache_dir=str(tmp_path))
    assert not respx.calls
    route = _poll([])
    for envelope in (
        {"ok": False},
        [],
        {"ok": True, "result": "bad"},
        {"ok": True, "result": [_callback(11), {"update_id": "12"}]},
    ):
        route.mock(return_value=httpx.Response(200, json=envelope))
        with pytest.raises(ValueError):
            await collect_feedback(TOKEN, store, cache_dir=str(tmp_path))
        assert (tmp_path / "feedback.json").read_bytes() == original
        assert store.last_update_id == 10 and store.ratings == []
        assert store.last_successful_poll_at == PRIOR_POLL


@pytest.mark.asyncio
@respx.mock
async def test_collect_feedback_network_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = FeedbackStore(last_update_id=5, last_successful_poll_at=PRIOR_POLL)
    save_feedback(store, str(tmp_path), strict=True)
    original_bytes = (tmp_path / "feedback.json").read_bytes()
    route = _poll([])
    route.mock(side_effect=httpx.ConnectError("Connection refused"))
    with pytest.raises(httpx.ConnectError):
        await collect_feedback(TOKEN, store, cache_dir=str(tmp_path))
    assert store.last_update_id == 5 and store.ratings == []
    monkeypatch.delenv("TELEGRAM_CHAT_ID")
    with pytest.raises(ValueError, match="private chat"):
        await collect_feedback(TOKEN, store, cache_dir=str(tmp_path))
    assert route.call_count == 1
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    respx.get(f"{API}/getWebhookInfo").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"url": "https://example.invalid/hook"}}),
    )
    with pytest.raises(ValueError, match="webhook"):
        await collect_feedback(TOKEN, store, cache_dir=str(tmp_path))
    assert route.call_count == 1
    assert not any("deleteWebhook" in str(call.request.url) for call in respx.calls)
    assert store.last_successful_poll_at == PRIOR_POLL
    assert (tmp_path / "feedback.json").read_bytes() == original_bytes


@pytest.mark.asyncio
@respx.mock
async def test_collect_feedback_write_failure_preserves_store(tmp_path: Path) -> None:
    store = FeedbackStore(article_source_map={"abcd1234": "My Source"}, last_successful_poll_at=PRIOR_POLL)
    save_feedback(store, str(tmp_path))
    original = deepcopy(store)
    original_bytes = (tmp_path / "feedback.json").read_bytes()
    proposal = PendingSource("New feed", "https://example.com/new", "Tech", datetime.now(timezone.utc).isoformat())
    save_pending([proposal], str(tmp_path), strict=True)
    _poll(
        [_callback(1), _message(2, "/start vote_g_abcd1234", 2), _message(3, f"/source ok {proposal.source_hash}", 3)]
    )
    with patch("digest.adapters.storage.feedback.atomic_json_write", side_effect=OSError("disk full")):
        with pytest.raises(OSError):
            await collect_feedback(TOKEN, store, cache_dir=str(tmp_path))
    assert store == original
    assert (tmp_path / "feedback.json").read_bytes() == original_bytes
    assert not any(
        endpoint in str(call.request.url) for call in respx.calls for endpoint in ("answerCallbackQuery", "sendMessage")
    )


@pytest.mark.asyncio
@respx.mock
async def test_acknowledge_requires_exact_committed_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FeedbackStore(
        pending_replies=[PendingReply("callback", "receipt")],
        pending_owner_sha256=hashlib.sha256(b"123").hexdigest(),
    )
    save_feedback(store, str(tmp_path))
    expected = _sha(tmp_path)
    with pytest.raises(ValueError, match="SHA-256"):
        await acknowledge_feedback(TOKEN, str(tmp_path), "0" * 64)
    path = tmp_path / "feedback.json"
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="SHA-256"):
        await acknowledge_feedback(TOKEN, str(tmp_path), expected)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "456")
    with pytest.raises(ValueError, match="owner binding"):
        await acknowledge_feedback(TOKEN, str(tmp_path), _sha(tmp_path))
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    assert not respx.calls
    assert load_feedback(str(tmp_path), strict=True).pending_replies == store.pending_replies
    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(ValueError):
        await collect_feedback(TOKEN, FeedbackStore(), cache_dir=str(tmp_path))
    assert path.read_text() == "{broken" and not respx.calls
    with patch("pathlib.Path.read_bytes", side_effect=PermissionError("unreadable")):
        with pytest.raises(PermissionError):
            load_feedback(str(tmp_path), strict=True)


# ---------------------------------------------------------------------------
# get_source_feedback_score
# ---------------------------------------------------------------------------


def test_get_source_feedback_score_all_positive() -> None:
    now = datetime.now(tz=timezone.utc).isoformat()
    store = FeedbackStore(
        ratings=[
            ArticleFeedback("h1", "Feed A", 1, now),
            ArticleFeedback("h2", "Feed A", 1, now),
            ArticleFeedback("h3", "Feed A", 1, now),
        ]
    )
    score = get_source_feedback_score(store, "Feed A")
    assert score == 1.0


def test_get_source_feedback_score_mixed() -> None:
    now = datetime.now(tz=timezone.utc).isoformat()
    store = FeedbackStore(
        ratings=[
            ArticleFeedback(FULL_ARTICLE_A, "Feed C", 1, now),
            ArticleFeedback(FULL_ARTICLE_B, "Feed C", -1, now),
        ]
    )
    score = get_source_feedback_score(store, "Feed C")
    assert score == 0.5
    # Repeated taps affect only their exact identity, even with a shared prefix.
    store.ratings.extend(
        [
            ArticleFeedback(FULL_ARTICLE_A, "Feed C", 1, now),
            ArticleFeedback(FULL_ARTICLE_A, "Feed C", -1, now),
            ArticleFeedback(FULL_ARTICLE_A, "Feed C", 1, "invalid timestamp"),
        ]
    )
    assert get_source_feedback_score(store, "Feed C") == 0.0
    assert len(store.ratings) == 5
    # A historical short token remains separate; its identity cannot be inferred.
    store.ratings.append(ArticleFeedback(FULL_ARTICLE_A[:8], "Feed C", 1, now))
    assert get_source_feedback_score(store, "Feed C") == pytest.approx(1 / 3)
    assert len(store.ratings) == 6


def test_get_source_feedback_score_old_ratings_excluded() -> None:
    now = datetime.now(tz=timezone.utc)
    old = (now - timedelta(days=30)).isoformat()
    recent = now.isoformat()
    store = FeedbackStore(
        ratings=[
            ArticleFeedback("h1", "Feed D", -1, old),  # too old, excluded
            ArticleFeedback("h2", "Feed D", 1, recent),
        ]
    )
    score = get_source_feedback_score(store, "Feed D", days=14)
    assert score == 1.0


def test_get_source_feedback_score_only_old_returns_none() -> None:
    old = (datetime.now(tz=timezone.utc) - timedelta(days=30)).isoformat()
    store = FeedbackStore(
        ratings=[
            ArticleFeedback("h1", "Feed E", 1, old),
        ]
    )
    assert get_source_feedback_score(store, "Feed E", days=14) is None


# ---------------------------------------------------------------------------
# save_feedback pruning
# ---------------------------------------------------------------------------


def test_save_feedback_prunes_old_ratings(tmp_path: Path) -> None:
    """Discard old ratings while retaining both recent ratings."""
    now = datetime.now(tz=timezone.utc)
    old = ArticleFeedback("old", "Old Feed", 1, (now - timedelta(days=31)).isoformat())
    recent = ArticleFeedback("h1", "Feed A", 1, (now - timedelta(days=29)).isoformat())
    fresh = ArticleFeedback("h2", "Feed B", -1, (now - timedelta(days=1)).isoformat())
    store = FeedbackStore(ratings=[old, recent, fresh])
    save_feedback(store, str(tmp_path))

    loaded = load_feedback(str(tmp_path))
    assert loaded.ratings == [recent, fresh]


# ---------------------------------------------------------------------------
# article_source_map persistence
# ---------------------------------------------------------------------------


def test_article_source_map_pruned_to_1000(tmp_path: Path) -> None:
    """article_source_map is pruned to 1000 entries on save (FIFO)."""
    store = FeedbackStore(article_source_map={f"hash{i:04d}": f"Source {i}" for i in range(1200)})
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
    assert loaded.source_decisions == {}
    assert loaded.pending_replies == [] and loaded.seen_callback_ids == [] and loaded.last_poll_counts == {}
    assert loaded.seen_message_ids == []
    assert loaded.cursor_observed_at == "" and loaded.previous_update_id == 0
    assert loaded.last_successful_poll_at == ""


@pytest.mark.parametrize("timestamp", [None, 1, []])
def test_successful_poll_metadata_requires_a_string(tmp_path: Path, timestamp: object) -> None:
    path = tmp_path / "feedback.json"
    path.write_text(json.dumps({"last_successful_poll_at": timestamp}))
    with pytest.raises(ValueError, match="Invalid feedback metadata"):
        load_feedback(str(tmp_path), strict=True)
    assert load_feedback(str(tmp_path)) == FeedbackStore()
    path.write_text(json.dumps({"last_successful_poll_at": "unusable legacy timestamp"}))
    assert load_feedback(str(tmp_path), strict=True).last_successful_poll_at == "unusable legacy timestamp"


# ---------------------------------------------------------------------------
# source_decisions persistence
# ---------------------------------------------------------------------------


def test_source_decisions_default_empty(tmp_path: Path) -> None:
    store = FeedbackStore()
    save_feedback(store, str(tmp_path))
    loaded = load_feedback(str(tmp_path))
    assert loaded.source_decisions == {}
    assert loaded.source_decision_bindings == {}


@pytest.mark.asyncio
@respx.mock
async def test_collect_feedback_src_ok_callback(tmp_path: Path) -> None:
    now = datetime.now(timezone.utc).isoformat()
    accepted = PendingSource("Accepted", "https://example.com/accept", "Tech", now)
    rejected = PendingSource("Rejected", "https://example.com/reject", "Tech", now)
    save_pending([accepted, rejected], str(tmp_path), strict=True)
    _poll(
        [
            _callback(10001, f"src:ok:{accepted.source_hash}"),
            _callback(10002, f"src:no:{rejected.source_hash}"),
            _callback(10003, "src:ok:deadbeef"),
        ]
    )
    result = await collect_feedback(TOKEN, FeedbackStore(), cache_dir=str(tmp_path), acknowledge=False)
    assert result.source_decisions == {accepted.source_hash: "approved", rejected.source_hash: "rejected"}
    assert result.source_decision_bindings == {
        proposal.source_hash: proposal_binding(proposal) for proposal in (accepted, rejected)
    }
    assert result.ratings == []
    assert result.last_poll_counts["source_decisions"] == 2
    assert result.last_poll_counts["unknown_source"] == 1
    assert [reply.text for reply in result.pending_replies] == [
        "Decision saved",
        "Decision saved",
        "Proposal unavailable or expired",
    ]


@pytest.mark.asyncio
@respx.mock
async def test_source_messages_bind_current_proposals_before_one_batch_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    accepted = PendingSource("Accepted", "https://example.com/accept", "Tech", now)
    rejected = PendingSource("Rejected", "https://example.com/reject", "Tech", now)
    save_pending([accepted, rejected], str(tmp_path), strict=True)
    route = _poll(
        [
            _message(1, f"/start source_ok_{accepted.source_hash}", 10),
            _message(2, f"/source no {rejected.source_hash}", 11),
            _message(3, "/source ok deadbeef", 12),
            _message(4, f"/source no {accepted.source_hash}", 10),
        ]
    )
    result = await collect_feedback(TOKEN, FeedbackStore(), cache_dir=str(tmp_path), acknowledge=False)
    assert result.source_decisions == {accepted.source_hash: "approved", rejected.source_hash: "rejected"}
    assert result.source_decision_bindings == {
        proposal.source_hash: proposal_binding(proposal) for proposal in (accepted, rejected)
    }
    assert result.last_poll_counts["source_decisions"] == 2
    assert result.last_poll_counts["unknown_source"] == 1 and result.last_poll_counts["duplicates"] == 1
    assert len(result.pending_replies) == 3 and len(result.seen_message_ids) == 3
    assert not any("sendMessage" in str(call.request.url) for call in respx.calls)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "456")
    with pytest.raises(ValueError, match="owner binding"):
        await acknowledge_feedback(TOKEN, str(tmp_path), _sha(tmp_path))
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")

    def answer(request: httpx.Request) -> httpx.Response:
        durable = load_feedback(str(tmp_path), strict=True)
        assert durable == result
        assert json.loads(request.content) == {
            "chat_id": "123",
            "text": "Source decisions saved: 2. Unavailable or expired proposals: 1.",
        }
        return httpx.Response(200, json={"ok": True})

    send = respx.post(f"{API}/sendMessage").mock(side_effect=answer)
    assert await acknowledge_feedback(TOKEN, str(tmp_path), _sha(tmp_path)) == {
        "attempted": 1,
        "ack_ok": 1,
        "ack_failed": 0,
    }
    assert send.call_count == 1
    result = load_feedback(str(tmp_path), strict=True)
    result.cursor_observed_at = ""
    save_feedback(result, str(tmp_path), strict=True)
    _poll([_message(1, f"/source no {accepted.source_hash}", 10)])
    replayed = await collect_feedback(TOKEN, result, cache_dir=str(tmp_path), acknowledge=False)
    assert "offset" not in json.loads(route.calls[-1].request.content)
    assert replayed.source_decisions == result.source_decisions
    assert replayed.last_poll_counts["duplicates"] == 1 and replayed.pending_replies == []


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize("transport", ["start", "command", "callback"])
async def test_unreadable_proposals_preserve_source_batch_until_repaired(
    transport: str,
    tmp_path: Path,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    proposal = PendingSource("New", "https://example.com/new", "Tech", now)
    owner_hash = hashlib.sha256(b"123").hexdigest()
    store = FeedbackStore(
        last_update_id=43,
        cursor_observed_at=now,
        article_source_map={"abcd1234": "My Source"},
        seen_message_ids=[f"{owner_hash}:1"],
        seen_callback_ids=["prior-callback"],
        pending_replies=[PendingReply("vote", "unknown_article")],
        pending_owner_sha256=owner_hash,
        last_successful_poll_at=PRIOR_POLL,
    )
    save_feedback(store, str(tmp_path), strict=True)
    original_bytes = (tmp_path / "feedback.json").read_bytes()
    (tmp_path / "pending_sources.json").write_text("{broken", encoding="utf-8")
    hash8 = proposal.source_hash
    source_update = (
        _callback(45, f"src:ok:{hash8}")
        if transport == "callback"
        else _message(45, f"/start source_ok_{hash8}" if transport == "start" else f"/source ok {hash8}", 45)
    )
    vote_update = _message(44, "/vote g abcd1234", 44)
    _poll([vote_update, source_update])
    with pytest.raises(ValueError):
        await collect_feedback(TOKEN, store, cache_dir=str(tmp_path))
    assert (tmp_path / "feedback.json").read_bytes() == original_bytes
    assert store == load_feedback(str(tmp_path), strict=True)
    assert not any(
        endpoint in str(call.request.url) for call in respx.calls for endpoint in ("answerCallbackQuery", "sendMessage")
    )

    # Article voting does not depend on proposal state.
    _poll([vote_update])
    voted = await collect_feedback(TOKEN, store, cache_dir=str(tmp_path), acknowledge=False)
    assert voted.last_update_id == 44 and len(voted.ratings) == 1
    assert (tmp_path / "pending_sources.json").read_text() == "{broken"
    save_pending([proposal], str(tmp_path), strict=True)
    _poll([vote_update, source_update])
    repaired = await collect_feedback(TOKEN, voted, cache_dir=str(tmp_path), acknowledge=False)
    assert repaired.last_update_id == 45 and len(repaired.ratings) == 1
    assert repaired.source_decisions == {hash8: "approved"}
    assert repaired.source_decision_bindings == {hash8: proposal_binding(proposal)}
    assert repaired.last_poll_counts["duplicates"] == 1 and repaired.last_poll_counts["source_decisions"] == 1


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize(
    "transport,case",
    [
        ("command", "missing"),
        ("command", "stale"),
        ("command", "future"),
        ("command", "duplicate"),
        ("command", "wrong_hash"),
        ("start", "stale"),
        ("callback", "stale"),
    ],
)
async def test_source_decisions_reject_unavailable_proposals(
    case: str,
    transport: str,
    tmp_path: Path,
) -> None:
    now = datetime.now(timezone.utc)
    proposal = PendingSource("Feed", "https://example.com/feed", "Tech", now.isoformat())
    if case == "stale":
        proposal.discovered_at = (now - timedelta(days=31)).isoformat()
    elif case == "future":
        proposal.discovered_at = (now + timedelta(days=1)).isoformat()
    elif case == "wrong_hash":
        proposal.source_hash = "12345678"
    pending = [] if case == "missing" else [proposal, proposal] if case == "duplicate" else [proposal]
    hash8 = proposal.source_hash
    update = (
        _callback(1, f"src:ok:{hash8}")
        if transport == "callback"
        else _message(1, f"/start source_ok_{hash8}" if transport == "start" else f"/source ok {hash8}", 1)
    )
    _poll([update])
    with patch("digest.application.feedback.load_pending", return_value=pending):
        result = await collect_feedback(TOKEN, FeedbackStore(), cache_dir=str(tmp_path), acknowledge=False)
    assert result.source_decisions == {} and result.source_decision_bindings == {}
    assert result.last_poll_counts["unknown_source"] == 1


@pytest.mark.asyncio
@respx.mock
async def test_source_messages_reject_foreign_owner_and_malformed_commands(tmp_path: Path) -> None:
    proposal = PendingSource("Feed", "https://example.com/feed", "Tech", datetime.now(timezone.utc).isoformat())
    save_pending([proposal], str(tmp_path), strict=True)
    hash8 = proposal.source_hash
    foreign_sender, foreign_chat, group = [_message(i, f"/source ok {hash8}", i) for i in range(1, 4)]
    foreign_sender["message"]["from"]["id"] = 456
    foreign_chat["message"]["chat"]["id"] = 456
    group["message"]["chat"]["type"] = "group"
    no_id = _message(4, f"/source ok {hash8}", 4)
    del no_id["message"]["message_id"]
    _poll(
        [
            foreign_sender,
            foreign_chat,
            group,
            no_id,
            _message(5, f"/source ok {hash8}\nextra", 5),
            _message(6, f"/start source_yes_{hash8}", 6),
        ]
    )
    result = await collect_feedback(TOKEN, FeedbackStore(), cache_dir=str(tmp_path), acknowledge=False)
    assert result.source_decisions == {} and result.source_decision_bindings == {}
    assert result.last_poll_counts["rejected_owner"] == 3 and result.last_poll_counts["malformed"] == 3
    assert result.pending_replies == [] and result.seen_message_ids == []


@pytest.mark.asyncio
@respx.mock
async def test_collect_feedback_status_command(tmp_path: Path) -> None:
    """Only recognized owner commands become durable, body-free UI receipts."""
    store = FeedbackStore(last_digest_time="2026-03-20 08:00 UTC", last_digest_sources=["A", "B", "C"])
    messages = []
    for update_id, text, sender, chat_type in (
        (9001, "/status", 123, "private"),
        (9002, "/bubble", 123, "private"),
        (9003, "/status", 456, "private"),
        (9004, "/status", 123, "group"),
        (9005, "a private message", 123, "private"),
    ):
        messages.append(
            {
                "update_id": update_id,
                "message": {
                    "text": text,
                    "chat": {"id": 123, "type": chat_type},
                    "from": {"id": sender},
                },
            }
        )
    _poll(messages)
    result = await collect_feedback(TOKEN, store, cache_dir=str(tmp_path), acknowledge=False)
    assert result.pending_replies == [PendingReply("command", "/status"), PendingReply("command", "/bubble")]
    persisted = (tmp_path / "feedback.json").read_text()
    assert "a private message" not in persisted and '"chat"' not in persisted and '"from"' not in persisted
    send = respx.post(f"{API}/sendMessage").mock(return_value=httpx.Response(200, json={"ok": True}))
    counts = await acknowledge_feedback(TOKEN, str(tmp_path), _sha(tmp_path))
    assert counts == {"attempted": 2, "ack_ok": 2, "ack_failed": 0}
    status = json.loads(send.calls[0].request.content)
    assert status["chat_id"] == "123" and "2026-03-20 08:00 UTC" in status["text"] and "3" in status["text"]
    assert json.loads(send.calls[1].request.content)["text"]


@pytest.mark.asyncio
async def test_managed_cli_exports_exact_saved_hash_without_ack_and_rejects_uncommitted_ack(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from unittest.mock import AsyncMock

    from digest.feedback_poll import main

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "synthetic-test-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "999")
    output = tmp_path / "step-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    cache = tmp_path / "cache"
    save_feedback(FeedbackStore(last_successful_poll_at=PRIOR_POLL), str(cache), strict=True)
    now = datetime(2026, 10, 2, 8, tzinfo=timezone.utc)
    observed = now - timedelta(seconds=2)
    previous_log = f"Previous retained successful local collection: at={PRIOR_POLL} age_seconds=86400"
    caplog.set_level(logging.INFO)

    async def collected(_token, store, *, cache_dir, acknowledge):
        assert acknowledge is False
        assert caplog.messages == [previous_log]
        store.last_poll_counts = {"received": 1, "recorded_votes": 1}
        store.last_successful_poll_at = observed.isoformat()
        save_feedback(store, cache_dir, strict=True)
        # Hash actual bytes, not a reserialization, and log their retained observation.
        with (Path(cache_dir) / "feedback.json").open("ab") as handle:
            handle.write(b"\n")
        store.last_successful_poll_at = "not-the-persisted-observation"
        return store

    with (
        patch("digest.feedback_poll.collect_feedback", side_effect=collected) as collect,
        patch("digest.feedback_poll.acknowledge_feedback", AsyncMock()) as ack,
        patch("digest.feedback_poll.datetime", wraps=datetime) as clock,
        patch.object(Path, "read_bytes", autospec=True, side_effect=Path.read_bytes) as reads,
    ):
        clock.now.return_value = now
        assert await main(["collect", "--cache-dir", str(cache)]) == 0
        collect.assert_awaited_once()
        ack.assert_not_called()
        assert [call.args[0] for call in reads.call_args_list] == [cache / "feedback.json"] * 2
    saved = (cache / "feedback.json").read_bytes()
    assert output.read_text() == f"feedback_sha256={hashlib.sha256(saved).hexdigest()}\n"
    assert caplog.messages == [
        previous_log,
        f"Persisted successful local collection: at={observed.isoformat()} age_seconds=2",
    ]
    assert capsys.readouterr().out == '{"counts": {"received": 1, "recorded_votes": 1}, "stage": "collect"}\n'
    with patch("httpx.AsyncClient", side_effect=AssertionError("No uncommitted acknowledgement")):
        assert await main(["ack", "--cache-dir", str(cache), "--expected-sha256", "0" * 64]) == 1
    assert (cache / "feedback.json").read_bytes() == saved


@pytest.mark.parametrize("timestamp, expected", [
    ("2026-10-02T10:00:00+02:00", "at=2026-10-02T08:00:00+00:00 age_seconds=1"),
    ("2026-10-02T08:00:01+00:00", "at=2026-10-02T08:00:01+00:00 age_seconds=0"),
    ("", "at=unknown age_seconds=unknown reason=missing"),
    ("private\ninvalid" * 1000, "at=unknown age_seconds=unknown reason=malformed"),
    ("2026-10-02T08:00:00", "at=unknown age_seconds=unknown reason=naive"),
    ("2026-10-02T08:00:02+00:00", "at=unknown age_seconds=unknown reason=future"),
    ("0001-01-01T00:00:00+01:00", "at=unknown age_seconds=unknown reason=malformed"),
])
def test_local_collection_observation_is_bounded_and_does_not_guess_age(
    timestamp: str, expected: str, caplog: pytest.LogCaptureFixture,
) -> None:
    from digest.feedback_poll import _log_local_collection

    with caplog.at_level(logging.INFO):
        _log_local_collection(timestamp, now=datetime(2026, 10, 2, 8, 0, 1, 500000, timezone.utc), previous=True)
    assert caplog.messages == [f"Previous retained successful local collection: {expected}"]


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize("failure", ["poll", "output"])
async def test_managed_collection_failure_keeps_historical_log_without_reread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str], failure: str,
) -> None:
    from digest.feedback_poll import main

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path))  # Opening a directory fails after saving.
    save_feedback(FeedbackStore(last_successful_poll_at=PRIOR_POLL), str(tmp_path), strict=True)
    original_bytes = (tmp_path / "feedback.json").read_bytes()
    route = _poll([])
    if failure == "poll":
        route.mock(side_effect=httpx.ConnectError("private failure details"))
    with (
        caplog.at_level(logging.INFO),
        patch.object(Path, "read_bytes", autospec=True, side_effect=Path.read_bytes) as reads,
    ):
        assert await main(["collect", "--cache-dir", str(tmp_path)]) == 1
        assert reads.call_count == (2 if failure == "poll" else 3)
    assert route.call_count == 1 and len(respx.calls) == 2
    observations = [message for message in caplog.messages if "successful local collection" in message]
    assert len(observations) == 1 and observations[0].startswith("Previous retained successful local collection:")
    assert "private failure details" not in caplog.text and TOKEN not in caplog.text
    assert capsys.readouterr().out == ""
    if failure == "poll":
        assert (tmp_path / "feedback.json").read_bytes() == original_bytes
    else:
        retained = load_feedback(str(tmp_path), strict=True)
        assert retained.last_successful_poll_at != PRIOR_POLL
        assert retained.last_poll_counts["received"] == 0 and retained.cursor_observed_at == ""
