"""Tests for compute_bubble_report() and the /bubble bot command handler."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from inspect import Parameter, signature
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest
import respx

from digest.application import source_scoring
from digest.domain.catalog import source_rules
from digest.domain.catalog.source_rules import recent_source_inclusions, source_inclusion_evenness
from digest.feedback import ArticleFeedback, FeedbackStore, collect_feedback
from digest.presentation.bubble import compute_bubble_report
from digest.source_scorer import (
    DailySnapshot,
    SourceStateEntry,
    SourceStateStore,
    SourceStats,
    _diversity_score,
    load_source_category_map,
    load_stats,
    save_source_category_map,
)
from digest.source_scorer import (
    compute_bubble_report as public_compute_bubble_report,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 4, 28, 10, 0, 0, tzinfo=timezone.utc)


def _make_stats(name: str, included_per_day: list[int]) -> SourceStats:
    history = [
        DailySnapshot(
            date=(_NOW.date() - timedelta(days=len(included_per_day) - i - 1)).isoformat(),
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
    ts = (_NOW - timedelta(days=days_ago)).isoformat()
    return ArticleFeedback(article_hash="abc", source_name="S", rating=rating, timestamp=ts)


# ---------------------------------------------------------------------------
# _diversity_score
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("counts", "label", "score"), [
    ({}, "No recorded inclusions", 0.0),
    ({"Only": 35}, "Single source", 0.0),
    ({"A": 35, "B": 35}, "Even", 100.0),
    ({name: 28 for name in "ABCDE"}, "Even", 100.0),
    ({"Dominant": 700, "Minor": 7}, "Concentrated", 8.013604730),
    ({"A": 9, "B": 1}, "Moderate", 46.899559359),
])
def test_source_inclusion_evenness(counts: dict[str, int], label: str, score: float) -> None:
    assert source_inclusion_evenness(counts) == (pytest.approx(score), label)
    stats = {name: _make_stats(name, [count]) for name, count in counts.items()}
    assert _diversity_score(stats, today=_NOW.date()) == (pytest.approx(score), label)


def _dated_stats(name: str, rows: list[tuple[str, int]]) -> SourceStats:
    return SourceStats(name=name, history=[DailySnapshot(day, count, count, True) for day, count in rows])


def test_recent_source_inclusions_uses_calendar_dates_not_positions() -> None:
    # More than seven unsorted rows, sparse peers and different final recorded dates.
    stats = {
        "A": _dated_stats("A", [
            ("2026-04-28", 2), ("2026-04-21", 100), ("2026-04-29", 100),
            ("2026-04-26", 3), ("2026-04-19", 100), ("2026-04-22", 5),
            ("2026-04-25", 7), ("2026-04-27", 11), ("2026-04-20", 100),
        ]),
        "B": _dated_stats("B", [("2026-04-23", 13), ("2026-04-26", 15)]),
        "Stale": _dated_stats("Stale", [("2026-04-20", 100)]),
    }
    counts = recent_source_inclusions(stats, today=_NOW.date())
    assert list(counts.items()) == [("A", 28), ("B", 28)]
    assert _diversity_score(stats, today=_NOW.date()) == (pytest.approx(100.0), "Even")
    report = compute_bubble_report(FeedbackStore(), stats, SourceStateStore(), now=_NOW)
    assert "  A: 28 art\n  B: 28 art" in report
    assert "Stale" not in report


def test_private_score_requires_date_and_public_report_samples_once(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _diversity_score is source_rules._diversity_score
    assert signature(_diversity_score).parameters["today"].kind is Parameter.KEYWORD_ONLY
    with pytest.raises(TypeError, match="today"):
        _diversity_score({})
    clock = Mock()
    clock.now.return_value = _NOW
    monkeypatch.setattr(source_scoring, "datetime", clock)
    stats = {"A": _make_stats("A", [3])}
    report = public_compute_bubble_report(FeedbackStore(), stats, SourceStateStore(), {"A": "Tech"})
    clock.now.assert_called_once_with(tz=timezone.utc)
    assert report == compute_bubble_report(FeedbackStore(), stats, SourceStateStore(), {"A": "Tech"}, now=_NOW)


# ---------------------------------------------------------------------------
# compute_bubble_report
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rows", [[], [("2026-04-21", 5)], [("2026-04-29", 5)], [("2026-04-28", 0)]])
def test_compute_bubble_report_no_recorded_inclusions(rows: list[tuple[str, int]]) -> None:
    stats = {"A": _dated_stats("A", rows)} if rows else {}
    assert recent_source_inclusions(stats, today=_NOW.date()) == {}
    report = compute_bubble_report(FeedbackStore(), stats, SourceStateStore(), now=_NOW)
    assert "Filter Bubble Report" in report
    assert "Window: 2026-04-22 to 2026-04-28 (7 UTC calendar dates)" in report
    assert "Source-inclusion evenness: No recorded inclusions (0/100)" in report
    assert "Recorded source counts only; missing or invalid dates are unobserved." in report
    assert "This does not measure publisher independence or viewpoint diversity." in report
    assert "Top sources" not in report
    assert "Category inclusions" not in report
    assert "Feedback (14d): 0 votes" in report
    assert "never" in report


@pytest.mark.parametrize(("observation", "utc_observation"), [
    ("2027-01-01T00:30:00+02:00", "2026-12-31T22:30:00+00:00"),
    ("2026-12-31T23:30:00-02:00", "2027-01-01T01:30:00+00:00"),
])
def test_compute_bubble_report_uses_observed_utc_dates(observation: str, utc_observation: str) -> None:
    observed = datetime.fromisoformat(observation)
    utc_now = datetime.fromisoformat(utc_observation)
    today = utc_now.date()
    stats = {"A": _dated_stats("A", [
        ((today - timedelta(days=6)).isoformat(), 2), (today.isoformat(), 3),
        ((today - timedelta(days=7)).isoformat(), 100), ((today + timedelta(days=1)).isoformat(), 100),
    ])}
    report = compute_bubble_report(FeedbackStore(), stats, SourceStateStore(), now=observed)
    assert f"Generated: {utc_now:%Y-%m-%d %H:%M UTC}" in report
    assert f"Window: {today - timedelta(days=6)} to {today} (7 UTC calendar dates)" in report
    assert "  A: 5 art" in report
    assert "Single source (0/100)" in report


def test_permissive_stats_report_omits_unusable_dates_without_repair(tmp_path: Path) -> None:
    def row(day: object, count: int) -> dict[str, object]:
        return {"date": day, "articles_found": count, "articles_included": count, "fetch_ok": True}

    raw = {
        "A": {"history": [row("2026-04-28", 3), row("2026-04-22", 6)]},
        "B": {"history": [row("2026-04-28", 2), row("2026-04-28", 1)]},
        "Unobserved": {"history": [row(day, 100) for day in [
            "2026-04-21", "2026-04-29", "not-a-date", 42, None,
            "20260428", "2026-W18-2", "2026-4-28", "2026-04-28T00:00:00",
        ]] + [{"articles_found": 100, "articles_included": 100, "fetch_ok": True}]},
    }
    path = tmp_path / "source_stats.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    before_bytes = path.read_bytes()
    stats = load_stats(str(tmp_path))
    before_stats = deepcopy(stats)
    assert len(stats["Unobserved"].history) == 9  # Missing date is skipped by the unchanged loader.
    assert recent_source_inclusions(stats, today=_NOW.date()) == {"A": 9, "B": 3}
    for categories in (None, {"A": "Tech", "Unobserved": "Unused"}):
        report = compute_bubble_report(FeedbackStore(), stats, SourceStateStore(), categories, now=_NOW)
        assert "Source-inclusion evenness: Even (81/100)" in report
        assert "Unobserved" not in report
        assert "Unused" not in report
        if categories:
            assert "Category inclusions (7 UTC dates):\n  Tech: 9 art (75%)\n  Other: 3 art (25%)" in report
        else:
            assert "Top sources (7 UTC dates):\n  A: 9 art\n  B: 3 art" in report
    assert stats == before_stats
    assert path.read_bytes() == before_bytes


def test_compute_bubble_report_known_evenness() -> None:
    store = FeedbackStore(last_digest_time="2026-04-28 06:00 UTC")
    stats = {name: _make_stats(name, [3, 3, 3, 3, 3, 3, 3]) for name in "ABCD"}
    state = SourceStateStore()
    report = compute_bubble_report(store, stats, state, now=_NOW)
    assert "Source-inclusion evenness: Even" in report
    assert "100/100" in report
    assert "2026-04-28 06:00 UTC" in report
    assert "(4h ago)" in report


def test_compute_bubble_report_feedback_14d_window() -> None:
    """Ratings older than 14 days must not count."""
    store = FeedbackStore(
        ratings=[
            _rating(1, days_ago=3),
            _rating(1, days_ago=3),
            _rating(-1, days_ago=20),  # older than 14d — excluded
        ]
    )
    report = compute_bubble_report(store, {}, SourceStateStore(), now=_NOW)
    assert "2 votes" in report
    assert "+2" in report
    assert "-0" in report


def test_compute_bubble_report_last_digest_time_invalid() -> None:
    store = FeedbackStore(last_digest_time="not-a-date")
    report = compute_bubble_report(store, {}, SourceStateStore(), now=_NOW)
    assert "not-a-date" in report  # gracefully shows raw value


def test_compute_bubble_report_source_health() -> None:
    state = SourceStateStore(
        sources={
            "Grad": SourceStateEntry(graduated=True),
            "Dem": SourceStateEntry(demoted=True),
            "Trial": SourceStateEntry(trial_started="2026-04-01"),
        }
    )
    report = compute_bubble_report(FeedbackStore(), {}, state, now=_NOW)
    assert "1 graduated" in report
    assert "1 trial" in report
    assert "1 demoted" in report


def test_compute_bubble_report_output_under_4096_chars() -> None:
    """Even with many sources, output must fit in one Telegram message."""
    stats = {f"Source{i}": _make_stats(f"Source{i}", [i + 1] * 7) for i in range(50)}
    store = FeedbackStore(ratings=[_rating(1, days_ago=1) for _ in range(200)])
    state = SourceStateStore(sources={f"Source{i}": SourceStateEntry(graduated=True) for i in range(50)})
    report = compute_bubble_report(store, stats, state, now=_NOW)
    assert len(report) < 4096
    assert report.count("  Source") == 5
    assert "  Source49: 350 art" in report
    assert "  Source45: 322 art" in report
    assert "  Source44:" not in report


def test_compute_bubble_report_with_category_map_shows_topics() -> None:
    """When category_map is provided, group the same recorded counts by category."""
    stats = {
        "TechFeed": _make_stats("TechFeed", [10] * 7),
        "FinFeed": _make_stats("FinFeed", [5] * 7),
        "SecFeed": _make_stats("SecFeed", [5] * 7),
    }
    category_map = {"TechFeed": "AI & LLM", "FinFeed": "FinTech", "SecFeed": "FinTech"}
    report = compute_bubble_report(FeedbackStore(), stats, SourceStateStore(), category_map=category_map, now=_NOW)
    assert "Category inclusions (7 UTC dates):" in report
    assert "AI & LLM" in report
    assert "FinTech" in report
    assert "Top sources" not in report


def test_compute_bubble_report_category_percentages_sum_to_100() -> None:
    stats = {
        "A": _make_stats("A", [3] * 7),
        "B": _make_stats("B", [1] * 7),
    }
    category_map = {"A": "Tech", "B": "Finance"}
    report = compute_bubble_report(FeedbackStore(), stats, SourceStateStore(), category_map=category_map, now=_NOW)
    assert "75%" in report  # A: 3/(3+1) = 75%
    assert "25%" in report  # B: 1/(3+1) = 25%


def test_compute_bubble_report_empty_category_map_falls_back_to_sources() -> None:
    """Empty category_map (no data yet) falls back to top-sources display."""
    stats = {"Feed": _make_stats("Feed", [5] * 7)}
    report = compute_bubble_report(FeedbackStore(), stats, SourceStateStore(), category_map={}, now=_NOW)
    assert "Top sources" in report
    assert "Category inclusions" not in report


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
async def test_collect_feedback_bubble_command_sends_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
async def test_collect_feedback_bubble_unknown_chat_id_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
async def test_collect_feedback_bubble_empty_cache_no_crash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty cache_dir must not crash — sends a report with 'No recorded inclusions'."""
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
    assert "No recorded inclusions (0/100)" in payload["text"]


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
async def test_collect_feedback_status_unknown_chat_id_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
