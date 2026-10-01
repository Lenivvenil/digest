"""Tests for compute_bubble_report() and the /bubble bot command handler."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
import respx

from digest.feedback import ArticleFeedback, FeedbackStore, collect_feedback
from digest.source_scorer import (
    DailySnapshot,
    SourceStateEntry,
    SourceStateStore,
    SourceStats,
    _diversity_score,
    compute_bubble_report,
    load_source_category_map,
    save_source_category_map,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 4, 28, 10, 0, 0, tzinfo=timezone.utc)


def _make_stats(
    name: str, included_per_day: list[int]
) -> SourceStats:
    history = [
        DailySnapshot(
            date=f"2026-04-{i + 1:02d}",
            articles_found=c,
            articles_included=c,
            fetch_ok=True,
        )
        for i, c in enumerate(included_per_day)
    ]
    return SourceStats(
        name=name,
        total_fetches=len(included_per_day),
        successful_fetches=len(included_per_day),
        total_articles_found=sum(included_per_day),
        articles_included_in_digest=sum(included_per_day),
        history=history,
    )


def _rating(rating: int, days_ago: int = 0) -> ArticleFeedback:
    # Anchored to wall-clock now (not _NOW) because compute_bubble_report's
    # 14-day window uses datetime.now() — anchoring to a fixed past date
    # would make the test silently rot once the suite runs on a later day.
    ts = (datetime.now(tz=timezone.utc) - timedelta(days=days_ago)).isoformat()
    return ArticleFeedback(
        article_hash="abc", source_name="S", rating=rating, timestamp=ts
    )


# ---------------------------------------------------------------------------
# _diversity_score
# ---------------------------------------------------------------------------


def test_diversity_score_empty() -> None:
    score, label = _diversity_score({})
    assert score == 0.0
    assert label == "No data"


def test_diversity_score_single_source() -> None:
    stats = {"Only": _make_stats("Only", [5, 5, 5, 5, 5, 5, 5])}
    score, label = _diversity_score(stats)
    assert score == 0.0
    assert label == "Single source"


def test_diversity_score_equal_two_sources() -> None:
    stats = {
        "A": _make_stats("A", [5, 5, 5, 5, 5, 5, 5]),
        "B": _make_stats("B", [5, 5, 5, 5, 5, 5, 5]),
    }
    score, label = _diversity_score(stats)
    assert abs(score - 100.0) < 0.01
    assert label == "Diverse"


def test_diversity_score_concentrated() -> None:
    stats = {
        "Dominant": _make_stats("Dominant", [100, 100, 100, 100, 100, 100, 100]),
        "Minor": _make_stats("Minor", [1, 1, 1, 1, 1, 1, 1]),
    }
    score, label = _diversity_score(stats)
    assert score < 40.0
    assert label == "Concentrated"


def test_diversity_score_five_equal_sources() -> None:
    stats = {name: _make_stats(name, [4, 4, 4, 4, 4, 4, 4]) for name in "ABCDE"}
    score, label = _diversity_score(stats)
    assert abs(score - 100.0) < 0.01
    assert label == "Diverse"


def test_diversity_score_uses_last_7_days_only() -> None:
    # Source A has lots of old articles but none recent; B has recent articles only.
    old = [10] * 30  # 30 days of history
    recent_for_a = old[:-7] + [0] * 7  # last 7 days: 0
    stats = {
        "A": _make_stats("A", recent_for_a),
        "B": _make_stats("B", [0] * 23 + [5] * 7),
    }
    score_a, _ = _diversity_score({"A": stats["A"]})
    assert score_a == 0.0  # A has 0 articles in last 7d → no data
    score_ab, _ = _diversity_score(stats)
    assert score_ab == 0.0  # Only B has recent articles → single active source


# ---------------------------------------------------------------------------
# compute_bubble_report
# ---------------------------------------------------------------------------


def test_compute_bubble_report_empty_data() -> None:
    """Empty inputs must not raise and must include all section markers."""
    store = FeedbackStore()
    stats: dict[str, SourceStats] = {}
    state = SourceStateStore()
    report = compute_bubble_report(store, stats, state)
    assert "Filter Bubble Report" in report
    assert "Diversity:" in report
    assert "Feedback (14d):" in report
    assert "never" in report


def test_compute_bubble_report_known_diversity() -> None:
    store = FeedbackStore(last_digest_time="2026-04-28 06:00 UTC")
    stats = {name: _make_stats(name, [3, 3, 3, 3, 3, 3, 3]) for name in "ABCD"}
    state = SourceStateStore()
    report = compute_bubble_report(store, stats, state)
    assert "Diverse" in report
    assert "100/100" in report


def test_compute_bubble_report_feedback_14d_window() -> None:
    """Ratings older than 14 days must not count."""
    store = FeedbackStore(
        ratings=[
            _rating(1, days_ago=3),
            _rating(1, days_ago=3),
            _rating(-1, days_ago=20),  # older than 14d — excluded
        ]
    )
    report = compute_bubble_report(store, {}, SourceStateStore())
    assert "2 votes" in report
    assert "+2" in report
    assert "-0" in report


def test_compute_bubble_report_last_digest_time_parsed() -> None:
    store = FeedbackStore(last_digest_time="2026-04-28 06:00 UTC")
    report = compute_bubble_report(store, {}, SourceStateStore())
    assert "2026-04-28 06:00 UTC" in report
    assert "h ago" in report


def test_compute_bubble_report_last_digest_time_invalid() -> None:
    store = FeedbackStore(last_digest_time="not-a-date")
    report = compute_bubble_report(store, {}, SourceStateStore())
    assert "not-a-date" in report  # gracefully shows raw value


def test_compute_bubble_report_source_health() -> None:
    state = SourceStateStore(
        sources={
            "Grad": SourceStateEntry(graduated=True),
            "Dem": SourceStateEntry(demoted=True),
            "Trial": SourceStateEntry(trial_started="2026-04-01"),
        }
    )
    report = compute_bubble_report(FeedbackStore(), {}, state)
    assert "1 graduated" in report
    assert "1 trial" in report
    assert "1 demoted" in report


def test_compute_bubble_report_output_under_4096_chars() -> None:
    """Even with many sources, output must fit in one Telegram message."""
    stats = {f"Source{i}": _make_stats(f"Source{i}", [i + 1] * 7) for i in range(50)}
    store = FeedbackStore(
        ratings=[_rating(1, days_ago=1) for _ in range(200)]
    )
    state = SourceStateStore(
        sources={f"Source{i}": SourceStateEntry(graduated=True) for i in range(50)}
    )
    report = compute_bubble_report(store, stats, state)
    assert len(report) < 4096


def test_compute_bubble_report_top_sources_capped_at_5() -> None:
    stats = {f"S{i}": _make_stats(f"S{i}", [i + 1] * 7) for i in range(10)}
    report = compute_bubble_report(FeedbackStore(), stats, SourceStateStore())
    assert report.count("  S") <= 5


def test_compute_bubble_report_with_category_map_shows_topics() -> None:
    """When category_map provided, report shows 'Your bubble' by topic, not source names."""
    stats = {
        "TechFeed": _make_stats("TechFeed", [10] * 7),
        "FinFeed": _make_stats("FinFeed", [5] * 7),
        "SecFeed": _make_stats("SecFeed", [5] * 7),
    }
    category_map = {"TechFeed": "AI & LLM", "FinFeed": "FinTech", "SecFeed": "FinTech"}
    report = compute_bubble_report(FeedbackStore(), stats, SourceStateStore(), category_map=category_map)
    assert "Your bubble" in report
    assert "AI & LLM" in report
    assert "FinTech" in report
    assert "Top sources" not in report


def test_compute_bubble_report_category_percentages_sum_to_100() -> None:
    stats = {
        "A": _make_stats("A", [3] * 7),
        "B": _make_stats("B", [1] * 7),
    }
    category_map = {"A": "Tech", "B": "Finance"}
    report = compute_bubble_report(FeedbackStore(), stats, SourceStateStore(), category_map=category_map)
    assert "75%" in report  # A: 3/(3+1) = 75%
    assert "25%" in report  # B: 1/(3+1) = 25%


def test_compute_bubble_report_empty_category_map_falls_back_to_sources() -> None:
    """Empty category_map (no data yet) falls back to top-sources display."""
    stats = {"Feed": _make_stats("Feed", [5] * 7)}
    report = compute_bubble_report(FeedbackStore(), stats, SourceStateStore(), category_map={})
    assert "Top sources" in report
    assert "Your bubble" not in report


# ---------------------------------------------------------------------------
# save_source_category_map / load_source_category_map
# ---------------------------------------------------------------------------


def test_save_load_source_category_map_round_trip(tmp_path: Path) -> None:
    from digest.config import SourceConfig
    sources = [
        SourceConfig(name="HN", url="https://hn.com", category="Tech", enabled=True),
        SourceConfig(name="PYMNTS", url="https://pymnts.com", category="FinTech", enabled=True),
    ]
    save_source_category_map(sources, str(tmp_path))
    loaded = load_source_category_map(str(tmp_path))
    assert loaded == {"HN": "Tech", "PYMNTS": "FinTech"}


def test_load_source_category_map_missing_file(tmp_path: Path) -> None:
    result = load_source_category_map(str(tmp_path))
    assert result == {}


# ---------------------------------------------------------------------------
# collect_feedback /bubble integration
# ---------------------------------------------------------------------------

_WEBHOOK_OK = {"ok": True, "result": {"url": "", "pending_update_count": 0}}
_DELETE_OK = {"ok": True}


def _bubble_update(chat_id: int = 999) -> dict[str, object]:
    return {
        "update_id": 5001,
        "message": {
            "message_id": 1,
            "from": {"id": chat_id},
            "chat": {"id": chat_id, "type": "private"},
            "text": "/bubble",
        },
    }


@pytest.mark.asyncio
@respx.mock
async def test_collect_feedback_bubble_command_sends_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Receiving /bubble from the owner should trigger a sendMessage with a non-empty report."""
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "999")
    token = "tok"
    store = FeedbackStore()

    respx.get(f"https://api.telegram.org/bot{token}/getWebhookInfo").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"url": "", "pending_update_count": 0}})
    )
    respx.post(f"https://api.telegram.org/bot{token}/deleteWebhook").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    respx.post(f"https://api.telegram.org/bot{token}/getUpdates").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": [_bubble_update()]})
    )
    send_mock = respx.post(f"https://api.telegram.org/bot{token}/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
    )

    result = await collect_feedback(token, store, cache_dir=str(tmp_path))

    assert send_mock.called
    sent_body = send_mock.calls[0].request.content
    import json as _json
    payload = _json.loads(sent_body)
    assert payload["chat_id"] == "999"
    assert "Filter Bubble Report" in payload["text"]
    assert result.last_update_id == 5001


@pytest.mark.asyncio
@respx.mock
async def test_collect_feedback_bubble_unknown_chat_id_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A /bubble from an unknown chat_id must be silently dropped — no sendMessage."""
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1111")  # owner is 1111
    token = "tok_auth"
    store = FeedbackStore()

    respx.get(f"https://api.telegram.org/bot{token}/getWebhookInfo").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"url": "", "pending_update_count": 0}})
    )
    respx.post(f"https://api.telegram.org/bot{token}/deleteWebhook").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    respx.post(f"https://api.telegram.org/bot{token}/getUpdates").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": [_bubble_update(chat_id=999)]})
    )
    send_mock = respx.post(f"https://api.telegram.org/bot{token}/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
    )

    result = await collect_feedback(token, store, cache_dir=str(tmp_path))

    assert not send_mock.called
    assert result.last_update_id == 5001  # update still acked


@pytest.mark.asyncio
@respx.mock
async def test_collect_feedback_bubble_empty_cache_no_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty cache_dir must not crash — sends a report with 'No data'."""
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    token = "tok2"
    store = FeedbackStore()

    respx.get(f"https://api.telegram.org/bot{token}/getWebhookInfo").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"url": "", "pending_update_count": 0}})
    )
    respx.post(f"https://api.telegram.org/bot{token}/deleteWebhook").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    respx.post(f"https://api.telegram.org/bot{token}/getUpdates").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": [_bubble_update(chat_id=42)]})
    )
    send_mock = respx.post(f"https://api.telegram.org/bot{token}/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 2}})
    )

    await collect_feedback(token, store, cache_dir=str(tmp_path))

    assert send_mock.called
    import json as _json
    payload = _json.loads(send_mock.calls[0].request.content)
    assert "Filter Bubble Report" in payload["text"]


@pytest.mark.asyncio
@respx.mock
async def test_collect_feedback_bubble_send_failure_does_not_propagate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If sendMessage fails for /bubble, the exception must be swallowed (logged only)."""
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "7")
    token = "tok3"
    store = FeedbackStore()

    respx.get(f"https://api.telegram.org/bot{token}/getWebhookInfo").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"url": "", "pending_update_count": 0}})
    )
    respx.post(f"https://api.telegram.org/bot{token}/deleteWebhook").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    respx.post(f"https://api.telegram.org/bot{token}/getUpdates").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": [_bubble_update(chat_id=7)]})
    )
    respx.post(f"https://api.telegram.org/bot{token}/sendMessage").mock(
        side_effect=httpx.NetworkError("connection refused")
    )

    result = await collect_feedback(token, store, cache_dir=str(tmp_path))
    assert result.last_update_id == 5001  # update was still processed


def _status_update(chat_id: int = 999) -> dict[str, object]:
    return {
        "update_id": 6001,
        "message": {
            "message_id": 2,
            "from": {"id": chat_id},
            "chat": {"id": chat_id, "type": "private"},
            "text": "/status",
        },
    }


@pytest.mark.asyncio
@respx.mock
async def test_collect_feedback_status_unknown_chat_id_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A /status from an unknown chat_id must be silently dropped — no sendMessage."""
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1111")
    token = "tok_status_auth"
    store = FeedbackStore()

    respx.get(f"https://api.telegram.org/bot{token}/getWebhookInfo").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"url": "", "pending_update_count": 0}})
    )
    respx.post(f"https://api.telegram.org/bot{token}/deleteWebhook").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    respx.post(f"https://api.telegram.org/bot{token}/getUpdates").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": [_status_update(chat_id=999)]})
    )
    send_mock = respx.post(f"https://api.telegram.org/bot{token}/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
    )

    result = await collect_feedback(token, store, cache_dir=str(tmp_path))

    assert not send_mock.called
    assert result.last_update_id == 6001
