"""Offline streaming and compatibility checks for source response budgets."""

from __future__ import annotations

import asyncio
import gzip
from collections.abc import AsyncIterator
from unittest.mock import patch

import feedparser
import httpx
import pytest

from digest.irritator.evidence_stage import EvidenceIrritatorResult, _check_source_response, _search
from digest.irritator.query_generator import SearchQuery
from digest.irritator.sources._response import MAX_SOURCE_RESPONSE_BYTES, read_bounded_response
from digest.irritator.sources.arxiv import search_arxiv
from scripts.review_fixture import fixture_config


class _Stream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes], *, block: bool = False) -> None:
        self.chunks = chunks
        self.block = block
        self.waiting = asyncio.Event()
        self.consumed = 0
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            self.consumed += 1
            yield chunk
        if self.block:
            self.waiting.set()
            await asyncio.Event().wait()

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_hook_accepts_exact_limit_and_keeps_complete_json_body() -> None:
    body = b'{"hits": []}' + b" " * (MAX_SOURCE_RESPONSE_BYTES - len(b'{"hits": []}'))
    stream = _Stream([body[:100], body[100:]])
    response = httpx.Response(200, stream=stream)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: response),
        event_hooks={"response": [_check_source_response]},
    ) as client:
        received = await client.get("https://hn.algolia.com/api/v1/search")
    assert received is response
    assert received.content == body
    assert await received.aread() == body
    assert received.json() == {"hits": []}
    assert stream.closed and stream.consumed == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("compressed", [False, True])
async def test_hook_rejects_decoded_oversize_before_next_chunk_without_prefix_parsing(compressed: bool) -> None:
    prefix = b'{"hits": []}' + b" " * (MAX_SOURCE_RESPONSE_BYTES - len(b'{"hits": []}'))
    chunks = [gzip.compress(prefix + b" ")] if compressed else [prefix, b" "]
    stream = _Stream([*chunks, b"must not be consumed"])
    headers = {"content-encoding": "gzip"} if compressed else {}
    response = httpx.Response(200, headers=headers, stream=stream)
    config = fixture_config()
    config.irritator.sources = ["hackernews"]
    result = EvidenceIrritatorResult(1, "bundle", queries=[SearchQuery("test", "test")])

    async def existing_hook(response: httpx.Response) -> None:
        pass

    with patch("digest.irritator.evidence_stage.validate_search_response") as validate:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: response),
            event_hooks={"response": [existing_hook]},
        ) as client:
            assert await _search(result, config, client) == []
            assert client.event_hooks["response"] == [existing_hook]
    assert stream.consumed == len(chunks) and stream.closed
    assert result.source_attempts[0].status == "error"
    assert result.source_attempts[0].error_detail == "Source response exceeds the response budget."
    validate.assert_not_called()
    with pytest.raises(httpx.ResponseNotRead):
        _ = response.content


@pytest.mark.asyncio
async def test_hook_cancellation_closes_body_and_restores_existing_hooks() -> None:
    stream = _Stream([b'{"hits": ['], block=True)
    config = fixture_config()
    config.irritator.sources = ["hackernews"]
    result = EvidenceIrritatorResult(1, "bundle", queries=[SearchQuery("test", "test")])

    async def existing_hook(response: httpx.Response) -> None:
        pass

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=stream)),
        event_hooks={"response": [existing_hook]},
    ) as client:
        task = asyncio.create_task(_search(result, config, client))
        await asyncio.wait_for(stream.waiting.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert client.event_hooks["response"] == [existing_hook]
    assert stream.closed
    assert result.source_attempts[0].error == "CancelledError"


@pytest.mark.asyncio
async def test_bounded_read_preserves_cached_compression_and_default_charset() -> None:
    body = "café résumé".encode("iso-8859-1")
    compressed = gzip.compress(body)
    stream = _Stream([compressed[:10], compressed[10:]])
    headers = {"content-encoding": "gzip", "content-type": "text/plain"}
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, headers=headers, stream=stream)),
        default_encoding=lambda content: "iso-8859-1",
    ) as client:
        async with client.stream("GET", "https://example.com/") as response:
            await read_bounded_response(response, len(body))
            await read_bounded_response(response, len(body))
            assert await response.aread() == body
            assert response.text == "café résumé"
            assert response.headers["content-encoding"] == "gzip"
    assert stream.consumed == 2 and stream.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("encoding", ["utf-8", "iso-8859-1", "utf-16"])
@pytest.mark.parametrize("hook_enabled", [False, True])
async def test_arxiv_preserves_httpx_charset_and_feedparser_input(encoding: str, hook_enabled: bool) -> None:
    text = (
        f'<?xml version="1.0" encoding="{encoding}"?>'
        '<feed xmlns="http://www.w3.org/2005/Atom"><entry>'
        '<title>Café evidence</title><link href="https://arxiv.org/abs/1234.5678"/>'
        "<summary>Résumé with a final condition.</summary></entry></feed>"
    )
    body = text.encode(encoding)
    headers = {"content-type": f"application/atom+xml; charset={encoding}", "content-encoding": "gzip"}
    compressed = gzip.compress(body)
    reference = httpx.Response(200, content=compressed, headers=headers)
    expected_feed = feedparser.parse(reference.text)
    stream = _Stream([compressed[:17], compressed[17:]])
    response = httpx.Response(200, headers=headers, stream=stream)
    with patch("digest.irritator.sources.arxiv.feedparser.parse", wraps=feedparser.parse) as parse:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: response),
            event_hooks={"response": [_check_source_response] if hook_enabled else []},
        ) as client:
            signals = await search_arxiv("evidence", None, client)
    parse.assert_called_once_with(reference.text)
    assert response.content == body
    assert signals[0].title == expected_feed.entries[0].title == "Café evidence"
    assert signals[0].snippet == expected_feed.entries[0].summary == "Résumé with a final condition."
    assert stream.closed and stream.consumed == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("oversize", [False, True])
async def test_legacy_arxiv_enforces_exact_byte_limit_without_truncation(oversize: bool) -> None:
    body = b'<feed xmlns="http://www.w3.org/2005/Atom"/>'
    body += b" " * (MAX_SOURCE_RESPONSE_BYTES - len(body))
    stream = _Stream([body, b" ", b"must not be consumed"] if oversize else [body])
    with patch("digest.irritator.sources.arxiv.feedparser.parse", wraps=feedparser.parse) as parse:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=stream)),
        ) as client:
            if oversize:
                with pytest.raises(ValueError, match="Source response exceeds the response budget"):
                    await search_arxiv("test", None, client)
            else:
                assert await search_arxiv("test", None, client) == []
    assert stream.closed
    if oversize:
        assert stream.consumed == 2
        parse.assert_not_called()
    else:
        parse.assert_called_once_with(body.decode())


@pytest.mark.asyncio
async def test_legacy_arxiv_cancellation_closes_stream_and_releases_lock() -> None:
    from digest.irritator.sources import arxiv

    stream = _Stream([b'<feed xmlns="http://www.w3.org/2005/Atom">'], block=True)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=stream)),
    ) as client:
        task = asyncio.create_task(search_arxiv("test", None, client))
        await asyncio.wait_for(stream.waiting.wait(), timeout=1)
        assert arxiv._request_state().lock.locked()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert stream.closed and not arxiv._request_state().lock.locked()


@pytest.mark.asyncio
async def test_legacy_arxiv_rejects_redirect_before_following_or_reading_body() -> None:
    requests: list[httpx.Request] = []
    stream = _Stream([b"must not be consumed"])

    def redirect(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(302, headers={"location": "https://unexpected.example/"}, stream=stream)

    async with httpx.AsyncClient(transport=httpx.MockTransport(redirect), follow_redirects=True) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await search_arxiv("test", None, client)
    assert len(requests) == 1 and requests[0].url.host == "export.arxiv.org"
    assert stream.consumed == 0 and stream.closed
