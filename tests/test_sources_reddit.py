"""Tests for digest.irritator.sources.reddit."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

from digest.irritator.sources import SourceUnavailableError
from digest.irritator.sources.reddit import search_reddit

_TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
_SEARCH_URL = "https://oauth.reddit.com/r/programming/search"
_SEARCH_URL_MULTI = "https://oauth.reddit.com/r/programming+fintech/search"


def _make_config(subreddits: list[str] | None = None) -> Any:
    class IrritatorCfg:
        reddit_subreddits = subreddits or ["programming"]

    class Cfg:
        irritator = IrritatorCfg()

    return Cfg()


def _token_response() -> dict[str, object]:
    return {"access_token": "test-token-abc", "token_type": "bearer", "expires_in": 3600}


def _reddit_response(posts: list[dict[str, object]] | None = None) -> dict[str, object]:
    if posts is None:
        posts = [
            {
                "title": "Counter view on AI",
                "permalink": "/r/programming/comments/abc/counter_view/",
                "selftext": "Detailed counter argument",
                "created_utc": 1700000000,
                "score": 100,
            },
        ]
    return {"data": {"children": [{"data": p} for p in posts]}}


@pytest.mark.asyncio
class TestSearchReddit:
    async def test_no_credentials_is_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("REDDIT_CLIENT_ID", raising=False)
        monkeypatch.delenv("REDDIT_CLIENT_SECRET", raising=False)
        with respx.mock:
            async with httpx.AsyncClient() as client:
                with pytest.raises(SourceUnavailableError):
                    await search_reddit("AI risk", _make_config(), client)
        assert respx.calls.call_count == 0

    async def test_missing_one_credential_is_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("REDDIT_CLIENT_ID", "some-id")
        monkeypatch.delenv("REDDIT_CLIENT_SECRET", raising=False)
        with respx.mock:
            async with httpx.AsyncClient() as client:
                with pytest.raises(SourceUnavailableError):
                    await search_reddit("AI risk", _make_config(), client)
        assert respx.calls.call_count == 0

    async def test_token_fetch_failure_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("REDDIT_CLIENT_ID", "id")
        monkeypatch.setenv("REDDIT_CLIENT_SECRET", "secret")
        with respx.mock:
            respx.post(_TOKEN_URL).mock(return_value=httpx.Response(401))
            async with httpx.AsyncClient() as client:
                with pytest.raises(httpx.HTTPStatusError):
                    await search_reddit("AI risk", _make_config(), client)

    @pytest.mark.parametrize("token_data", [[], {}, {"access_token": 1}, {"access_token": " "}])
    async def test_invalid_token_response_raises(
        self, monkeypatch: pytest.MonkeyPatch, token_data: Any,
    ) -> None:
        monkeypatch.setenv("REDDIT_CLIENT_ID", "id")
        monkeypatch.setenv("REDDIT_CLIENT_SECRET", "secret")
        with respx.mock:
            respx.post(_TOKEN_URL).mock(return_value=httpx.Response(200, json=token_data))
            async with httpx.AsyncClient() as client:
                with pytest.raises(ValueError, match=r"^Invalid Reddit token response\.$"):
                    await search_reddit("AI risk", _make_config(), client)

    async def test_non_json_token_response_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("REDDIT_CLIENT_ID", "id")
        monkeypatch.setenv("REDDIT_CLIENT_SECRET", "secret")
        with respx.mock:
            respx.post(_TOKEN_URL).mock(return_value=httpx.Response(200, text="private response body"))
            async with httpx.AsyncClient() as client:
                with pytest.raises(ValueError, match=r"^Invalid Reddit token response\.$"):
                    await search_reddit("AI risk", _make_config(), client)

    async def test_malformed_search_envelope_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("REDDIT_CLIENT_ID", "id")
        monkeypatch.setenv("REDDIT_CLIENT_SECRET", "secret")
        with respx.mock:
            respx.post(_TOKEN_URL).mock(return_value=httpx.Response(200, json=_token_response()))
            respx.get(_SEARCH_URL).mock(return_value=httpx.Response(200, json={"data": {"children": {}}}))
            async with httpx.AsyncClient() as client:
                with pytest.raises(ValueError, match=r"^Invalid Reddit search response\.$"):
                    await search_reddit("AI risk", _make_config(), client)

    async def test_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("REDDIT_CLIENT_ID", "id")
        monkeypatch.setenv("REDDIT_CLIENT_SECRET", "secret")
        monkeypatch.setenv("REDDIT_USERNAME", "testuser")
        with respx.mock:
            respx.post(_TOKEN_URL).mock(
                return_value=httpx.Response(200, json=_token_response())
            )
            search_route = respx.get(_SEARCH_URL).mock(
                return_value=httpx.Response(200, json=_reddit_response())
            )
            async with httpx.AsyncClient() as client:
                signals = await search_reddit("AI risk", _make_config(), client)

        assert len(signals) == 1
        assert signals[0].title == "Counter view on AI"
        assert signals[0].source_name == "reddit"
        assert signals[0].score == 100.0
        assert "bearer test-token-abc" in search_route.calls[0].request.headers["authorization"]

    async def test_multiple_subreddits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("REDDIT_CLIENT_ID", "id")
        monkeypatch.setenv("REDDIT_CLIENT_SECRET", "secret")
        cfg = _make_config(["programming", "fintech"])
        with respx.mock:
            respx.post(_TOKEN_URL).mock(
                return_value=httpx.Response(200, json=_token_response())
            )
            respx.get(_SEARCH_URL_MULTI).mock(
                return_value=httpx.Response(200, json=_reddit_response())
            )
            async with httpx.AsyncClient() as client:
                signals = await search_reddit("test", cfg, client)
        assert len(signals) == 1

    async def test_empty_results(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("REDDIT_CLIENT_ID", "id")
        monkeypatch.setenv("REDDIT_CLIENT_SECRET", "secret")
        with respx.mock:
            respx.post(_TOKEN_URL).mock(
                return_value=httpx.Response(200, json=_token_response())
            )
            respx.get(_SEARCH_URL).mock(
                return_value=httpx.Response(200, json={"data": {"children": []}})
            )
            async with httpx.AsyncClient() as client:
                signals = await search_reddit("nothing", _make_config(), client)
        assert signals == []

    async def test_http_error_on_search_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("REDDIT_CLIENT_ID", "id")
        monkeypatch.setenv("REDDIT_CLIENT_SECRET", "secret")
        with respx.mock:
            respx.post(_TOKEN_URL).mock(
                return_value=httpx.Response(200, json=_token_response())
            )
            respx.get(_SEARCH_URL).mock(return_value=httpx.Response(429))
            async with httpx.AsyncClient() as client:
                with pytest.raises(httpx.HTTPStatusError):
                    await search_reddit("fail", _make_config(), client)
