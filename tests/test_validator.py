"""Tests for src.irritator.validator."""

from __future__ import annotations

from src.irritator.sources import Signal
from src.irritator.validator import validate_signals


def _make_signal(
    url: str = "https://example.com/article",
    title: str = "Test Signal",
    snippet: str = "Some content",
) -> Signal:
    return Signal(
        url=url, title=title, snippet=snippet,
        source_name="hackernews", published="2026-01-01", score=10.0,
    )


class TestValidateSignals:
    def test_dedup_by_url(self) -> None:
        signals = [
            _make_signal("https://example.com/a", "Title 1"),
            _make_signal("https://example.com/a", "Title 2"),
        ]
        result = validate_signals(signals, [])
        assert len(result) == 1

    def test_dedup_normalizes_trailing_slash(self) -> None:
        signals = [
            _make_signal("https://example.com/a"),
            _make_signal("https://example.com/a/"),
        ]
        result = validate_signals(signals, [])
        assert len(result) == 1

    def test_dedup_case_insensitive(self) -> None:
        signals = [
            _make_signal("https://Example.com/A"),
            _make_signal("https://example.com/a"),
        ]
        result = validate_signals(signals, [])
        assert len(result) == 1

    def test_blocklist_filters_title(self) -> None:
        signals = [_make_signal(title="Trump says AI is great")]
        result = validate_signals(signals, ["trump"])
        assert result == []

    def test_blocklist_filters_snippet(self) -> None:
        signals = [_make_signal(snippet="Celebrity endorses tech")]
        result = validate_signals(signals, ["celebrity"])
        assert result == []

    def test_blocklist_case_insensitive(self) -> None:
        signals = [_make_signal(title="ELECTION results")]
        result = validate_signals(signals, ["election"])
        assert result == []

    def test_blocklist_no_match_keeps_signal(self) -> None:
        signals = [_make_signal(title="AI research paper")]
        result = validate_signals(signals, ["trump", "election"])
        assert len(result) == 1

    def test_invalid_url_filtered(self) -> None:
        signals = [_make_signal(url="not-a-url")]
        result = validate_signals(signals, [])
        assert result == []

    def test_empty_url_filtered(self) -> None:
        signals = [_make_signal(url="")]
        result = validate_signals(signals, [])
        assert result == []

    def test_empty_input(self) -> None:
        result = validate_signals([], [])
        assert result == []

    def test_multiple_valid_signals(self) -> None:
        signals = [
            _make_signal("https://example.com/a", "Good signal 1"),
            _make_signal("https://example.com/b", "Good signal 2"),
        ]
        result = validate_signals(signals, [])
        assert len(result) == 2
