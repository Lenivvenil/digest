"""Tests for src.irritator.validator."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from digest.irritator.validator import validate_signals, validate_signals_async
from tests.factories import make_signal as _make_signal


def _mock_client() -> MagicMock:
    """The legacy supplied client must not be used for public acquisition."""
    client = MagicMock(spec=httpx.AsyncClient)
    client.head = AsyncMock(side_effect=AssertionError("Legacy client must not bypass public acquisition"))
    return client


@pytest.fixture(autouse=True)
def mock_fetch(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    fetch = AsyncMock(return_value=httpx.Response(200))
    monkeypatch.setattr("digest.adapters.http.signal_liveness.fetch_public", fetch)
    return fetch


class TestValidateSignals:
    def test_dedup_by_url(self) -> None:
        signals = [
            _make_signal("https://example.com/a", "Title 1"),
            _make_signal("https://example.com/a", "Title 2"),
        ]
        result = validate_signals(signals, [])
        assert len(result) == 1

    def test_blocklist_filters_snippet(self) -> None:
        signals = [_make_signal(snippet="Celebrity endorses tech")]
        result = validate_signals(signals, ["celebrity"])
        assert result == []

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


@pytest.mark.asyncio
class TestValidateSignalsAsync:
    async def test_check_liveness_false_skips_head(self, mock_fetch: AsyncMock) -> None:
        signals = [_make_signal("https://example.com/a")]
        mock_fetch.return_value = httpx.Response(404)
        client = _mock_client()
        result = await validate_signals_async(signals, [], client, check_liveness=False)
        assert len(result) == 1
        mock_fetch.assert_not_called()
        client.head.assert_not_called()

    async def test_liveness_200_keeps_signal(self, mock_fetch: AsyncMock) -> None:
        signals = [_make_signal("https://example.com/a")]
        mock_fetch.return_value = httpx.Response(200)
        result = await validate_signals_async(signals, [], _mock_client(), check_liveness=True)
        assert len(result) == 1

    async def test_liveness_503_keeps_signal(self, mock_fetch: AsyncMock) -> None:
        signals = [_make_signal("https://example.com/a")]
        mock_fetch.return_value = httpx.Response(503)
        result = await validate_signals_async(signals, [], _mock_client(), check_liveness=True)
        assert len(result) == 1

    async def test_liveness_410_drops_signal(self, mock_fetch: AsyncMock) -> None:
        signals = [_make_signal("https://example.com/a")]
        mock_fetch.return_value = httpx.Response(410)
        result = await validate_signals_async(signals, [], _mock_client(), check_liveness=True)
        assert result == []

    async def test_liveness_401_keeps_signal(self, mock_fetch: AsyncMock) -> None:
        signals = [_make_signal("https://example.com/a")]
        mock_fetch.return_value = httpx.Response(401)
        result = await validate_signals_async(signals, [], _mock_client(), check_liveness=True)
        assert len(result) == 1

    async def test_liveness_429_keeps_signal(self, mock_fetch: AsyncMock) -> None:
        signals = [_make_signal("https://example.com/a")]
        mock_fetch.return_value = httpx.Response(429)
        result = await validate_signals_async(signals, [], _mock_client(), check_liveness=True)
        assert len(result) == 1

    async def test_liveness_invalid_url_drops_signal(self, mock_fetch: AsyncMock) -> None:
        signals = [_make_signal("https://example.com/a")]
        mock_fetch.side_effect = httpx.InvalidURL("bad url")
        client = _mock_client()
        result = await validate_signals_async(signals, [], client, check_liveness=True)
        assert result == []

    async def test_liveness_timeout_drops_signal(self, mock_fetch: AsyncMock) -> None:
        signals = [_make_signal("https://example.com/a")]
        mock_fetch.side_effect = httpx.TimeoutException("timeout")
        client = _mock_client()
        result = await validate_signals_async(signals, [], client, check_liveness=True)
        assert result == []

    async def test_liveness_transport_error_drops_signal(self, mock_fetch: AsyncMock) -> None:
        signals = [_make_signal("https://example.com/a")]
        mock_fetch.side_effect = httpx.TransportError("connect failed")
        client = _mock_client()
        result = await validate_signals_async(signals, [], client, check_liveness=True)
        assert result == []

    async def test_liveness_405_keeps_signal(self, mock_fetch: AsyncMock) -> None:
        signals = [_make_signal("https://example.com/a")]
        mock_fetch.return_value = httpx.Response(405)
        result = await validate_signals_async(signals, [], _mock_client(), check_liveness=True)
        assert len(result) == 1

    async def test_blocklist_applied_before_liveness(self, mock_fetch: AsyncMock) -> None:
        signals = [_make_signal("https://example.com/a", title="BLOCKED content")]
        mock_fetch.return_value = httpx.Response(200)
        client = _mock_client()
        result = await validate_signals_async(signals, ["blocked"], client, check_liveness=True)
        assert result == []
        mock_fetch.assert_not_called()
        client.head.assert_not_called()

    async def test_empty_signals_returns_empty(self, mock_fetch: AsyncMock) -> None:
        mock_fetch.return_value = httpx.Response(200)
        client = _mock_client()
        result = await validate_signals_async([], [], client, check_liveness=True)
        assert result == []
        mock_fetch.assert_not_called()
        client.head.assert_not_called()


def test_validation_compatibility_exports_keep_identity_and_runtime_annotations() -> None:
    from dataclasses import MISSING, fields
    from inspect import signature
    from typing import get_type_hints

    from digest.adapters.http.signal_liveness import check_signal_liveness
    from digest.application.signal_validation import validate_signals_async as application_validate
    from digest.domain.investigation.signals import Signal
    from digest.domain.investigation.validation import validate_signals as domain_validate
    from digest.irritator import Signal as public_signal
    from digest.irritator.sources import Signal as source_signal
    from digest.irritator.validator import _head_check as compatible_head

    assert Signal is public_signal is source_signal
    assert validate_signals is domain_validate
    assert validate_signals_async is application_validate
    assert compatible_head is check_signal_liveness
    expected_fields = {"url": str, "title": str, "snippet": str, "source_name": str, "published": str, "score": float}
    assert get_type_hints(Signal) == expected_fields
    assert list(signature(Signal).parameters) == list(expected_fields)
    assert all(field.default is MISSING and field.default_factory is MISSING for field in fields(Signal))
    assert get_type_hints(validate_signals) == {
        "signals": list[Signal],
        "blocklist": list[str],
        "return": list[Signal],
    }
    assert get_type_hints(validate_signals_async) == {
        "signals": list[Signal],
        "blocklist": list[str],
        "client": httpx.AsyncClient,
        "check_liveness": bool,
        "return": list[Signal],
    }
    assert signature(validate_signals_async).parameters["check_liveness"].default is False
    assert get_type_hints(check_signal_liveness)["signal"] is Signal


def test_pure_signal_validation_imports_without_search_or_http() -> None:
    import subprocess
    import sys
    from pathlib import Path

    script = """
import importlib.abc
import sys

class BlockEffects(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        forbidden = ('httpx', 'digest.irritator', 'digest.adapters', 'digest.application')
        if any(fullname == name or fullname.startswith(name + '.') for name in forbidden):
            raise AssertionError('Pure signal validation imported ' + fullname)

sys.meta_path.insert(0, BlockEffects())
from digest.domain.investigation.signals import Signal
from digest.domain.investigation.validation import validate_signals
signal = Signal('https://example.com/a', 'title', 'snippet', 'source', '', 0.0)
assert validate_signals([signal], []) == [signal]
"""
    subprocess.run([sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[1], check=True)


def test_first_seen_url_stays_consumed_after_blocklist_filtering() -> None:
    blocked = _make_signal("HTTPS://Example.com/A///", title="prefixBLOCKEDsuffix")
    repeated = _make_signal("https://example.com/a", title="Allowed later duplicate")
    first = _make_signal("ftp://example.com/b")
    second = _make_signal("https://example.com/c?query=1")
    assert validate_signals([blocked, first, repeated, second], ["blocked"]) == [first, second]


@pytest.mark.asyncio
async def test_malformed_url_parse_error_propagates_before_any_head(mock_fetch: AsyncMock) -> None:
    signals = [_make_signal("https://[invalid")]
    client = _mock_client()
    with pytest.raises(ValueError, match="Invalid IPv6 URL"):
        validate_signals(signals, [])
    with pytest.raises(ValueError, match="Invalid IPv6 URL"):
        await validate_signals_async(signals, [], client, check_liveness=True)
    client.head.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [ValueError("bad URL"), RuntimeError("unexpected client failure")])
async def test_head_retains_existing_error_boundary(error: Exception, mock_fetch: AsyncMock) -> None:
    signals = [_make_signal("https://example.com/a")]
    mock_fetch.side_effect = error
    client = _mock_client()
    if isinstance(error, ValueError):
        assert await validate_signals_async(signals, [], client, check_liveness=True) == []
    else:
        with pytest.raises(RuntimeError, match="unexpected client failure"):
            await validate_signals_async(signals, [], client, check_liveness=True)


@pytest.mark.asyncio
async def test_liveness_is_bounded_to_ten_and_retains_input_order_and_request_options(mock_fetch: AsyncMock) -> None:
    import asyncio
    from unittest.mock import call

    signals = [_make_signal(f"https://example.com/{index}") for index in range(12)]
    entered: list[int] = []
    completed: list[int] = []
    release = [asyncio.Event() for _ in signals]
    capacity_reached = asyncio.Event()
    all_entered = asyncio.Event()

    async def head(url: str, *, method: str, max_bytes: int, timeout: float) -> httpx.Response:
        index = int(url.rsplit("/", 1)[1])
        entered.append(index)
        if len(entered) == 10:
            capacity_reached.set()
        if len(entered) == len(signals):
            all_entered.set()
        await release[index].wait()
        completed.append(index)
        return httpx.Response(404 if index == 2 else 403, request=httpx.Request("HEAD", url))

    client = _mock_client()
    mock_fetch.side_effect = head
    task = asyncio.create_task(validate_signals_async(signals, [], client, check_liveness=True))
    try:
        await asyncio.wait_for(capacity_reached.wait(), timeout=1)
        await asyncio.sleep(0)
        assert len(entered) == 10
        for index in reversed(range(10)):
            release[index].set()
            await asyncio.sleep(0)
        await asyncio.wait_for(all_entered.wait(), timeout=1)
        release[11].set()
        await asyncio.sleep(0)
        release[10].set()
        result = await asyncio.wait_for(task, timeout=1)
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert completed != list(range(12))
    assert result == [signal for index, signal in enumerate(signals) if index != 2]
    assert mock_fetch.await_args_list == [
        call(signal.url, method="HEAD", timeout=5.0, max_bytes=0) for signal in signals
    ]
    client.head.assert_not_called()


@pytest.mark.asyncio
async def test_liveness_aggregate_deadline_drops_signal(mock_fetch: AsyncMock) -> None:
    mock_fetch.side_effect = TimeoutError()
    assert await validate_signals_async([_make_signal()], [], _mock_client(), check_liveness=True) == []


@pytest.mark.asyncio
async def test_supplied_client_cannot_bypass_public_destination_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    from digest.adapters.http.public_fetch import fetch_public

    monkeypatch.setattr("digest.adapters.http.signal_liveness.fetch_public", fetch_public)
    client = MagicMock(spec=httpx.AsyncClient)
    client.head = AsyncMock(return_value=httpx.Response(200))
    signals = [_make_signal("http://127.0.0.1/private")]
    assert await validate_signals_async(signals, [], client, check_liveness=True) == []
    client.head.assert_not_called()
