"""Tests for src.irritator.validator."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from digest.irritator.validator import validate_signals, validate_signals_async
from tests.factories import make_signal as _make_signal


def _mock_client(status: int | None = 200, raises: Exception | None = None) -> MagicMock:
    client = MagicMock(spec=httpx.AsyncClient)
    if raises is not None:
        client.head = AsyncMock(side_effect=raises)
    else:
        response = httpx.Response(status, request=httpx.Request("HEAD", "https://example.com"))
        client.head = AsyncMock(return_value=response)
    return client


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


@pytest.mark.asyncio
class TestValidateSignalsAsync:
    async def test_check_liveness_false_skips_head(self) -> None:
        signals = [_make_signal("https://example.com/a")]
        client = _mock_client(404)
        result = await validate_signals_async(signals, [], client, check_liveness=False)
        assert len(result) == 1
        client.head.assert_not_called()

    async def test_liveness_200_keeps_signal(self) -> None:
        signals = [_make_signal("https://example.com/a")]
        result = await validate_signals_async(signals, [], _mock_client(200), check_liveness=True)
        assert len(result) == 1

    async def test_liveness_404_drops_signal(self) -> None:
        signals = [_make_signal("https://example.com/a")]
        result = await validate_signals_async(signals, [], _mock_client(404), check_liveness=True)
        assert result == []

    async def test_liveness_503_keeps_signal(self) -> None:
        signals = [_make_signal("https://example.com/a")]
        result = await validate_signals_async(signals, [], _mock_client(503), check_liveness=True)
        assert len(result) == 1

    async def test_liveness_410_drops_signal(self) -> None:
        signals = [_make_signal("https://example.com/a")]
        result = await validate_signals_async(signals, [], _mock_client(410), check_liveness=True)
        assert result == []

    async def test_liveness_401_keeps_signal(self) -> None:
        signals = [_make_signal("https://example.com/a")]
        result = await validate_signals_async(signals, [], _mock_client(401), check_liveness=True)
        assert len(result) == 1

    async def test_liveness_429_keeps_signal(self) -> None:
        signals = [_make_signal("https://example.com/a")]
        result = await validate_signals_async(signals, [], _mock_client(429), check_liveness=True)
        assert len(result) == 1

    async def test_liveness_invalid_url_drops_signal(self) -> None:
        signals = [_make_signal("https://example.com/a")]
        client = _mock_client(raises=httpx.InvalidURL("bad url"))
        result = await validate_signals_async(signals, [], client, check_liveness=True)
        assert result == []

    async def test_liveness_timeout_drops_signal(self) -> None:
        signals = [_make_signal("https://example.com/a")]
        client = _mock_client(raises=httpx.TimeoutException("timeout"))
        result = await validate_signals_async(signals, [], client, check_liveness=True)
        assert result == []

    async def test_liveness_transport_error_drops_signal(self) -> None:
        signals = [_make_signal("https://example.com/a")]
        client = _mock_client(raises=httpx.TransportError("connect failed"))
        result = await validate_signals_async(signals, [], client, check_liveness=True)
        assert result == []

    async def test_liveness_405_keeps_signal(self) -> None:
        signals = [_make_signal("https://example.com/a")]
        result = await validate_signals_async(signals, [], _mock_client(405), check_liveness=True)
        assert len(result) == 1

    async def test_blocklist_applied_before_liveness(self) -> None:
        signals = [_make_signal("https://example.com/a", title="BLOCKED content")]
        client = _mock_client(200)
        result = await validate_signals_async(signals, ["blocked"], client, check_liveness=True)
        assert result == []
        client.head.assert_not_called()

    async def test_empty_signals_returns_empty(self) -> None:
        client = _mock_client(200)
        result = await validate_signals_async([], [], client, check_liveness=True)
        assert result == []
        client.head.assert_not_called()

    async def test_multiple_signals_parallel(self) -> None:
        signals = [_make_signal(f"https://example.com/{i}") for i in range(3)]
        client = _mock_client(200)
        result = await validate_signals_async(signals, [], client, check_liveness=True)
        assert len(result) == 3
        assert client.head.call_count == 3
