"""Offline contract and transport checks for Forem V1 lexical article search."""

from __future__ import annotations

import asyncio
import gzip
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import respx

from digest.domain.investigation.signals import Signal
from digest.irritator.sources import devto
from digest.irritator.sources._response import MAX_SOURCE_RESPONSE_BYTES
from digest.irritator.sources.devto import search_devto
from tests.test_source_response import _Stream

_ENDPOINT = "https://dev.to/api/articles/search"


def _article(**fields: Any) -> dict[str, Any]:
    return {
        "url": "https://dev.to/example/article",
        "title": "When evidence requires review",
        "description": "The observed benefit requires human review.",
        "published_at": "2026-10-08T10:00:00Z",
        **fields,
    }


@pytest.mark.asyncio
async def test_search_contract_and_exact_evidence() -> None:
    query = '"LLM reasoning" café C++'
    description = "Observed benefits in the evaluated sample. " * 30
    description += "\nOnly when an expert reviews every proposed action.\n"
    article = _article(
        title="  Café evidence\nwith conditions  ",
        description=description,
        canonical_url="https://unrelated.example/",
        public_reactions_count=99999,
        comments_count=1000,
        body_markdown="Not part of search evidence",
        body_html="<p>Ignored</p>",
    )
    with respx.mock as router:
        route = router.get(_ENDPOINT).respond(200, json=[article])
        async with httpx.AsyncClient() as client:
            signals = await search_devto(query, None, client)
        assert len(router.calls) == 1
    request = route.calls[0].request
    assert request.method == "GET"
    assert dict(request.url.params) == {"q": query, "page": "1", "per_page": "10"}
    assert request.headers["accept"] == "application/vnd.forem.api-v1+json"
    assert "api-key" not in request.headers and "authorization" not in request.headers
    assert request.content == b""
    assert request.extensions["timeout"] == {"connect": 10.0, "read": 10.0, "write": 10.0, "pool": 10.0}
    assert signals == [Signal(article["url"], article["title"], description, "devto", article["published_at"], 0.0)]


@pytest.mark.asyncio
async def test_invalid_query_never_reaches_network() -> None:
    with respx.mock(assert_all_called=False) as router:
        async with httpx.AsyncClient() as client:
            with pytest.raises(ValueError, match="Invalid lexical query contract"):
                await search_devto("", None, client)
        assert not router.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("articles", [[], [_article(description="")], [_article()] * 10])
async def test_empty_search_and_empty_description_are_valid(articles: list[dict[str, Any]]) -> None:
    with respx.mock:
        respx.get(_ENDPOINT).respond(200, json=articles)
        async with httpx.AsyncClient() as client:
            signals = await search_devto("evidence", None, client)
    assert len(signals) == len(articles)
    if articles:
        assert signals[0].snippet == articles[0]["description"]


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [b"<html>private body</html>", b"[", b'{"error":"private error"}'])
async def test_invalid_success_response_is_failure(body: bytes) -> None:
    with respx.mock:
        route = respx.get(_ENDPOINT).respond(200, content=body)
        async with httpx.AsyncClient() as client:
            with pytest.raises(ValueError, match=r"^Invalid DEV.to search response\.$"):
                await search_devto("evidence", None, client)
        assert route.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "article",
    [
        None,
        _article(title=2),
        _article(title=" \n\t"),
        _article(description=None),
        _article(published_at=None),
        _article(url=None),
        _article(published_at="2026-02-30T10:00:00Z"),
        _article(published_at="2026-10-08T10:00:00"),
        _article(published_at="2026-10-08T10:00:00." + "0" * 60 + "Z"),
        _article(url="javascript:alert(1)"),
        _article(url="https:///article"),
        _article(url="https://person:secret@example.com/article"),
        _article(url="https://[broken/article"),
        _article(url="https://example.com:99999/article"),
        _article(url="https://example.com/pri\nvate"),
        _article(url="https://example.com\\@other.example/article"),
        _article(url="https://example.com/" + "a" * 2048),
    ],
)
async def test_invalid_article_rejects_whole_response(article: Any) -> None:
    with respx.mock:
        respx.get(_ENDPOINT).respond(200, json=[_article(), article])
        async with httpx.AsyncClient() as client:
            with pytest.raises(ValueError, match=r"^Invalid DEV.to (article fields|article URL|publication date)\.$"):
                await search_devto("evidence", None, client)


@pytest.mark.asyncio
async def test_oversize_article_list_rejects_whole_response() -> None:
    with respx.mock:
        respx.get(_ENDPOINT).respond(200, json=[_article()] * 11)
        async with httpx.AsyncClient() as client:
            with pytest.raises(ValueError, match=r"^Invalid DEV.to search response\.$"):
                await search_devto("evidence", None, client)


@pytest.mark.asyncio
async def test_url_and_publication_limits_preserve_exact_boundary_values() -> None:
    prefix = "http://example.com/"
    url = prefix + "a" * (2048 - len(prefix))
    published = "2026-10-08T10:00:00." + "0" * 59 + "Z"
    assert len(url) == 2048 and len(published) == 80
    with respx.mock:
        respx.get(_ENDPOINT).respond(200, json=[_article(url=url, published_at=published)])
        async with httpx.AsyncClient() as client:
            signals = await search_devto("evidence", None, client)
    assert signals[0].url == url and signals[0].published == published


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [302, 429, 503])
async def test_http_errors_do_not_follow_retry_or_read(status: int) -> None:
    requests: list[httpx.Request] = []
    stream = _Stream([b"must not be consumed"])

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status, headers={"location": "https://unexpected.example/"}, stream=stream)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond), follow_redirects=True) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await search_devto("evidence", None, client)
    assert len(requests) == 1 and requests[0].url.host == "dev.to"
    assert stream.consumed == 0 and stream.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("compressed", [False, True])
@pytest.mark.parametrize("oversize", [False, True])
async def test_decoded_response_byte_limit(compressed: bool, oversize: bool) -> None:
    body = b"[]" + b" " * (MAX_SOURCE_RESPONSE_BYTES - 2 + int(oversize))
    chunks = (
        [gzip.compress(body)] if compressed else [body[:MAX_SOURCE_RESPONSE_BYTES], body[MAX_SOURCE_RESPONSE_BYTES:]]
    )
    stream = _Stream([*chunks, b"must not be consumed"] if oversize else chunks)
    response = httpx.Response(200, headers={"content-encoding": "gzip"} if compressed else {}, stream=stream)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: response)) as client:
        if oversize:
            with pytest.raises(ValueError, match="Source response exceeds the response budget"):
                await search_devto("evidence", None, client)
        else:
            assert await search_devto("evidence", None, client) == []
            assert response.content == body
    assert stream.closed and stream.consumed == len(chunks)
    if oversize:
        with pytest.raises(httpx.ResponseNotRead):
            _ = response.content


@pytest.mark.asyncio
async def test_concurrent_requests_across_clients_are_serial_and_spaced(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [0.0]
    starts: list[float] = []
    active = [0]
    original_sleep = asyncio.sleep

    async def wait(delay: float) -> None:
        clock[0] += delay
        await original_sleep(0)

    class ResponseStream(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            await original_sleep(0)
            yield b"[]"

        async def aclose(self) -> None:
            active[0] -= 1

    async def response(request: httpx.Request) -> httpx.Response:
        active[0] += 1
        assert active[0] == 1
        starts.append(clock[0])
        await original_sleep(0)
        return httpx.Response(200, request=request, stream=ResponseStream())

    monkeypatch.setattr(devto, "_requests", None)
    monkeypatch.setattr(devto, "monotonic", lambda: clock[0])
    monkeypatch.setattr(devto.asyncio, "sleep", wait)
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(response)) as first,
        httpx.AsyncClient(transport=httpx.MockTransport(response)) as second,
    ):
        await asyncio.gather(search_devto("first", None, first), search_devto("second", None, second))
    assert starts == [0.0, 2.0] and active[0] == 0


def test_request_state_resets_between_event_loops(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(devto, "_requests", None)

    async def state() -> devto._DevtoRequests:
        requests = devto._request_state()
        requests.next_start = 999999999.0
        return requests

    first = asyncio.run(state())

    async def fresh_state() -> devto._DevtoRequests:
        requests = devto._request_state()
        assert requests.next_start == 0.0
        assert requests.loop is asyncio.get_running_loop()
        assert devto._request_state() is requests
        return requests

    second = asyncio.run(fresh_state())
    assert second is not first and second.lock is not first.lock


@pytest.mark.asyncio
async def test_cancellation_closes_response_and_releases_lock() -> None:
    stream = _Stream([b"["], block=True)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=stream)),
    ) as client:
        task = asyncio.create_task(search_devto("evidence", None, client))
        await asyncio.wait_for(stream.waiting.wait(), timeout=1)
        assert devto._request_state().lock.locked()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert stream.closed and not devto._request_state().lock.locked()
