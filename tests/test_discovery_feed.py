"""Offline safety and resource-bound checks for discovery feed validation."""

from __future__ import annotations

import asyncio
import socket
import threading
from collections.abc import AsyncIterator, Callable
from typing import Any
from unittest.mock import Mock

import httpx
import pytest

from digest import discovery_feed
from digest.discovery_feed import FeedValidationError, FeedValidationTimeout, validate_feed_url

RSS = b'<rss version="2.0"><channel><title>Empty feed</title></channel></rss>'
ATOM = b'<feed xmlns="http://www.w3.org/2005/Atom"><title>Empty feed</title></feed>'
RSS1 = (
    b'<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" '
    b'xmlns="http://purl.org/rss/1.0/"><channel rdf:about="https://example.com/">'
    b"<title>Empty feed</title></channel></rdf:RDF>"
)
PUBLIC_IP = "93.184.216.34"


@pytest.fixture(autouse=True)
def mock_dns(monkeypatch: pytest.MonkeyPatch) -> Mock:
    """Use real URL safety validation against offline, observable DNS answers."""
    resolver = Mock(return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC_IP, 0))])
    monkeypatch.setattr(socket, "getaddrinfo", resolver)
    return resolver


@pytest.fixture
def mock_http(monkeypatch: pytest.MonkeyPatch) -> Callable[[Any], list[httpx.Request]]:
    real_client = httpx.AsyncClient

    def install(handler: Any) -> list[httpx.Request]:
        requests: list[httpx.Request] = []

        async def dispatch(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            # The connection URL is the validated address; origin routing stays logical.
            assert request.url.host == PUBLIC_IP
            assert request.extensions["sni_hostname"] == request.headers["host"]
            result = handler(request)
            if asyncio.iscoroutine(result):
                result = await result
            if result.is_stream_consumed:
                result = httpx.Response(result.status_code, headers=result.headers,
                                        stream=httpx.ByteStream(result.content))
            return result  # type: ignore[no-any-return]

        def client(**kwargs: Any) -> httpx.AsyncClient:
            assert kwargs["follow_redirects"] is False
            assert kwargs["trust_env"] is False
            assert kwargs["headers"]["Accept-Encoding"] == "identity"
            return real_client(transport=httpx.MockTransport(dispatch), **kwargs)

        monkeypatch.setattr(discovery_feed.httpx, "AsyncClient", client)
        return requests

    return install


@pytest.mark.parametrize("body", [ATOM, RSS1])
async def test_valid_empty_feeds_preserve_url_identity(mock_http: Any, body: bytes) -> None:
    url = "https://example.com/Feed/?category=A&token=x%2Fy&category=B"
    requests = mock_http(lambda _: httpx.Response(200, content=body))
    assert await validate_feed_url(url) == url
    assert str(requests[0].url.copy_with(host="example.com")) == url


@pytest.mark.parametrize(
    "body",
    [
        b"<html><body>Log in</body></html>",
        b'<html><rss version="2.0"><channel/></rss></html>',
        b'<rss version="2.0"/>',
        b"<document><title>Not a feed</title></document>",
        b'<rss version="2.0"><channel>',
        b'<?xml version="1.0" encoding="invalid-encoding"?><rss version="2.0"><channel/></rss>',
        b"",
    ],
)
async def test_non_feeds_and_malformed_feeds_are_technical_errors(mock_http: Any, body: bytes) -> None:
    mock_http(lambda _: httpx.Response(200, content=body))
    with pytest.raises(FeedValidationError):
        await validate_feed_url("https://example.com/feed")


async def test_html_media_type_is_rejected(mock_http: Any) -> None:
    mock_http(lambda _: httpx.Response(200, content=RSS, headers={"content-type": "text/html; charset=UTF-8"}))
    with pytest.raises(FeedValidationError, match="HTML"):
        await validate_feed_url("https://example.com/feed")


async def test_redirects_revalidate_and_address_each_hop(mock_http: Any, mock_dns: Mock) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/start":
            return httpx.Response(301, headers={"Location": "/next"})
        if request.url.path == "/next":
            return httpx.Response(302, headers={"Location": "https://feeds.example.org/Feed/?edition=A"})
        return httpx.Response(200, content=ATOM)

    requests = mock_http(respond)
    assert await validate_feed_url("https://example.com/start") == "https://feeds.example.org/Feed/?edition=A"
    assert len(requests) == 3
    assert [call.args[0] for call in mock_dns.call_args_list] == [
        "example.com",
        "example.com",
        "feeds.example.org",
    ]


async def test_idn_uses_the_same_hostname_for_validation_and_fetch(mock_http: Any, mock_dns: Mock) -> None:
    requests = mock_http(lambda _: httpx.Response(200, content=RSS))
    assert await validate_feed_url("https://faß.example/Feed/") == "https://xn--fa-hia.example/Feed/"
    assert mock_dns.call_args.args[0] == requests[0].headers["host"] == "xn--fa-hia.example"


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/feed",
        "http://169.254.169.254/feed",
        "file:///feed.xml",
        "http://[::1]/feed",
        "https://user:password@example.com/feed",
        "https://example.com\\@127.0.0.1/feed",
        "https://example.com/\nfeed",
    ],
)
async def test_unsafe_initial_url_is_never_requested(mock_http: Any, url: str) -> None:
    requests = mock_http(lambda _: pytest.fail("An unsafe URL must never be requested"))
    with pytest.raises(FeedValidationError):
        await validate_feed_url(url)
    assert requests == []


async def test_private_dns_answer_is_never_requested(mock_http: Any, mock_dns: Mock) -> None:
    mock_dns.return_value = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 0))]
    requests = mock_http(lambda _: pytest.fail("A private DNS answer must never be requested"))
    with pytest.raises(FeedValidationError, match="safety"):
        await validate_feed_url("https://private.example/feed")
    assert requests == []


async def test_private_redirect_is_never_requested(mock_http: Any) -> None:
    requests = mock_http(lambda _: httpx.Response(302, headers={"Location": "http://127.0.0.1/feed"}))
    with pytest.raises(FeedValidationError, match="safety"):
        await validate_feed_url("https://example.com/start")
    assert len(requests) == 1


@pytest.mark.parametrize("redirects,accepted", [(3, True), (4, False)])
async def test_redirect_limit(mock_http: Any, redirects: int, accepted: bool) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        hop = int(request.url.path[1:])
        if hop < redirects:
            return httpx.Response(307, headers={"Location": f"/{hop + 1}"})
        return httpx.Response(200, content=RSS)

    requests = mock_http(respond)
    if accepted:
        assert await validate_feed_url("https://example.com/0") == "https://example.com/3"
    else:
        with pytest.raises(FeedValidationError, match="redirect limit"):
            await validate_feed_url("https://example.com/0")
    assert len(requests) == 4


async def test_redirect_requires_location(mock_http: Any) -> None:
    mock_http(lambda _: httpx.Response(302))
    with pytest.raises(FeedValidationError, match="destination"):
        await validate_feed_url("https://example.com/feed")


async def test_malformed_redirect_is_a_technical_error(mock_http: Any) -> None:
    requests = mock_http(lambda _: httpx.Response(302, headers={"Location": "http://[invalid/feed"}))
    with pytest.raises(FeedValidationError):
        await validate_feed_url("https://example.com/feed")
    assert len(requests) == 1


class ChunkStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes], *, delay: float = 0) -> None:
        self.chunks = chunks
        self.delay = delay
        self.read_chunks = 0
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            await asyncio.sleep(self.delay)
            self.read_chunks += 1
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


async def test_stream_size_limit_stops_reading_and_closes_response(mock_http: Any) -> None:
    stream = ChunkStream([b"x" * discovery_feed.MAX_FEED_BYTES, b"x" * 65536, b"never read"])
    mock_http(lambda _: httpx.Response(200, stream=stream))
    with pytest.raises(FeedValidationError, match="2 MiB"):
        await validate_feed_url("https://example.com/feed")
    assert stream.read_chunks == 2
    assert stream.closed


async def test_exact_size_limit_is_allowed(mock_http: Any) -> None:
    raw = RSS + b" " * (discovery_feed.MAX_FEED_BYTES - len(RSS))
    url = "https://example.com/Feed/?category=A&token=x%2Fy&category=B"
    requests = mock_http(lambda _: httpx.Response(200, stream=ChunkStream([raw])))
    assert await validate_feed_url(url) == url
    assert str(requests[0].url.copy_with(host="example.com")) == url


@pytest.mark.parametrize("status", [403, 429, 503])
async def test_http_errors_are_not_retried(mock_http: Any, status: int) -> None:
    requests = mock_http(lambda _: httpx.Response(status, headers={"Retry-After": "1"}))
    with pytest.raises(FeedValidationError):
        await validate_feed_url("https://example.com/feed")
    assert len(requests) == 1


async def test_transport_timeout_is_distinct_and_not_retried(mock_http: Any) -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    requests = mock_http(timeout)
    with pytest.raises(FeedValidationTimeout):
        await validate_feed_url("https://example.com/feed")
    assert len(requests) == 1


async def test_aggregate_deadline_interrupts_a_trickling_body(mock_http: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(discovery_feed, "FEED_VALIDATION_SECONDS", 0.2)
    stream = ChunkStream([RSS[:10], RSS[10:20], RSS[20:]], delay=0.1)
    mock_http(lambda _: httpx.Response(200, stream=stream))
    with pytest.raises(FeedValidationTimeout):
        await validate_feed_url("https://example.com/feed")
    assert stream.read_chunks < 3
    assert stream.closed


async def test_aggregate_deadline_includes_dns(mock_http: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(discovery_feed, "FEED_VALIDATION_SECONDS", 0.01)
    release = threading.Event()

    def slow_dns(*_args: Any, **_kwargs: Any) -> None:
        release.wait(timeout=1)

    monkeypatch.setattr(socket, "getaddrinfo", slow_dns)
    requests = mock_http(lambda _: pytest.fail("Timed-out DNS must not be followed by HTTP"))
    try:
        with pytest.raises(FeedValidationTimeout):
            await validate_feed_url("https://example.com/feed")
    finally:
        release.set()
    assert requests == []


async def test_aggregate_deadline_covers_the_whole_redirect_chain(
    mock_http: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(discovery_feed, "FEED_VALIDATION_SECONDS", 0.05)

    async def slow_redirect(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.02)
        return httpx.Response(302, headers={"Location": f"/{int(request.url.path[1:]) + 1}"})

    requests = mock_http(slow_redirect)
    with pytest.raises(FeedValidationTimeout):
        await validate_feed_url("https://example.com/0")
    assert len(requests) < 4


async def test_aggregate_deadline_also_bounds_parser_thread(
    mock_http: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(discovery_feed, "FEED_VALIDATION_SECONDS", 0.05)
    mock_http(lambda _: httpx.Response(200, content=RSS))
    entered = threading.Event()
    release = threading.Event()

    def slow_parse(_: bytes) -> None:
        entered.set()
        release.wait(timeout=1)

    monkeypatch.setattr(discovery_feed, "_check_feed_bytes", slow_parse)
    try:
        with pytest.raises(FeedValidationTimeout):
            await validate_feed_url("https://example.com/feed")
        assert entered.is_set()
    finally:
        release.set()


async def test_invalid_content_encoding_has_a_technical_feed_error(mock_http: Any) -> None:
    mock_http(lambda _: httpx.Response(
        200, headers={"content-encoding": "gzip"}, stream=httpx.ByteStream(b"invalid compressed body"),
    ))
    with pytest.raises(FeedValidationError, match="invalid or incomplete content encoding"):
        await validate_feed_url("https://example.com/feed")
