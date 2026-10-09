"""Offline proof of the real public-fetch decision and connection construction.

Only DNS and the transport/network backend are replaced. Caller parser tests do
not substitute for this boundary proof. No test opens a network connection.
"""

from __future__ import annotations

import asyncio
import gzip
import socket
import ssl
import threading
import zlib
from collections.abc import AsyncIterator, Callable
from typing import Any
from unittest.mock import Mock

import httpx
import pytest
from httpcore._backends.anyio import AnyIOBackend

from digest.adapters.http import public_fetch
from digest.adapters.http.public_fetch import PublicFetchError, UnsafePublicURL, fetch_public

PUBLIC = "93.184.216.34"
SECOND = "8.8.8.8"
V6 = "2606:4700:4700::1111"


def addresses(*values: str) -> list[Any]:
    return [(socket.AF_INET6 if ":" in value else socket.AF_INET, socket.SOCK_STREAM, 6, "", (value, 0))
            for value in values]


@pytest.fixture(autouse=True)
def dns(monkeypatch: pytest.MonkeyPatch) -> Mock:
    resolver = Mock(return_value=addresses(PUBLIC))
    monkeypatch.setattr(socket, "getaddrinfo", resolver)
    return resolver


class Stream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes], delay: float = 0) -> None:
        self.chunks = chunks
        self.delay = delay
        self.closed = False
        self.reads = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            await asyncio.sleep(self.delay)
            self.reads += 1
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


def response(body: bytes = b"complete", *, status: int = 200, **headers: str) -> httpx.Response:
    return httpx.Response(status, headers=headers, stream=Stream([body]))


@pytest.fixture
def http(monkeypatch: pytest.MonkeyPatch) -> Callable[..., tuple[list[httpx.Request], list[httpx.AsyncClient]]]:
    real_client = httpx.AsyncClient

    def install(handler: Callable[..., Any]) -> tuple[list[httpx.Request], list[httpx.AsyncClient]]:
        requests: list[httpx.Request] = []
        clients: list[httpx.AsyncClient] = []

        async def dispatch(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            result = handler(request)
            return await result if asyncio.iscoroutine(result) else result

        def client(**kwargs: Any) -> httpx.AsyncClient:
            assert kwargs["trust_env"] is False
            assert kwargs["follow_redirects"] is False
            assert kwargs["headers"]["accept-encoding"] == "identity"
            assert kwargs["timeout"].connect <= 3.0
            assert kwargs["limits"].max_keepalive_connections == 0
            result = real_client(transport=httpx.MockTransport(dispatch), **kwargs)
            clients.append(result)
            return result

        monkeypatch.setattr(public_fetch.httpx, "AsyncClient", client)
        return requests, clients

    return install


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/", "https://[fec0::1]/", "https://[64:ff9b::808:808]/", "http://224.0.0.1/",
    "https://user:password@example.com/", "https://@example.com/", "https://example.com/\npath",
    "https://example.com/\x7f", "https://example.com\\@public.example/", "https://example.com:99999/",
    "https://[2606:4700:4700::1111%25eth0]/", "file:///etc/hosts", "https://metadata.google.internal/",
])
async def test_unsafe_spelling_or_literal_never_reaches_transport(http: Any, url: str) -> None:
    requests, clients = http(lambda _: pytest.fail("Unsafe URL reached transport"))
    with pytest.raises(UnsafePublicURL, match="unsafe_url"):
        await fetch_public(url, timeout=1, max_bytes=128)
    assert requests == clients == []


@pytest.mark.parametrize("answer", [addresses(PUBLIC, "10.0.0.1"), addresses(V6, "::1"), addresses("not-an-ip"), []])
async def test_all_dns_answers_must_be_public_before_any_connection(http: Any, dns: Mock, answer: list[Any]) -> None:
    dns.return_value = answer
    requests, _ = http(lambda _: pytest.fail("Unsafe DNS reached transport"))
    with pytest.raises(UnsafePublicURL):
        await fetch_public("https://example.com/feed", timeout=1, max_bytes=128)
    assert requests == []


async def test_dns_failure_is_an_explicit_unsafe_outcome(http: Any, dns: Mock) -> None:
    dns.side_effect = socket.gaierror("offline")
    requests, _ = http(lambda _: pytest.fail("Failed DNS reached transport"))
    with pytest.raises(UnsafePublicURL):
        await fetch_public("https://example.com/", timeout=1, max_bytes=128)
    assert not requests


async def test_relative_and_cross_origin_redirects_preserve_logical_identity(http: Any, dns: Mock) -> None:
    def redirect(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/start":
            return response(status=302, location="../Feed/?q=x%2Fy#part")
        if request.headers["host"] == "example.com":
            return response(status=307, location="https://xn--fa-hia.example:8443/final/?x=A&x=B")
        return response()

    requests, clients = http(redirect)
    result = await fetch_public("https://example.com/start", timeout=1, max_bytes=128)
    assert result.url == "https://xn--fa-hia.example:8443/final/?x=A&x=B"
    assert [r.headers["host"] for r in requests] == ["example.com", "example.com", "xn--fa-hia.example:8443"]
    assert [r.extensions["sni_hostname"] for r in requests] == ["example.com", "example.com", "xn--fa-hia.example"]
    assert [r.url.host for r in requests] == [PUBLIC] * 3
    assert requests[1].url.raw_path == b"/Feed/?q=x%2Fy"
    assert [call.args[0] for call in dns.call_args_list] == ["example.com", "example.com", "xn--fa-hia.example"]
    assert len({id(client) for client in clients}) == 3 and all(client.is_closed for client in clients)
    assert result.content == b"complete"


@pytest.mark.parametrize("location", ["http://127.0.0.1/secret", "https://example.com/\nsecret", "http://[broken"])
async def test_unsafe_redirect_never_sends_second_request(http: Any, location: str) -> None:
    requests, clients = http(lambda _: response(status=302, location=location))
    with pytest.raises((UnsafePublicURL, httpx.RemoteProtocolError)):
        await fetch_public("https://example.com/", timeout=1, max_bytes=128)
    assert len(requests) == 1 and clients[0].is_closed


async def test_rebinding_does_not_change_connected_ip_and_next_hop_rechecks(http: Any, dns: Mock) -> None:
    dns.side_effect = [addresses(PUBLIC), addresses("127.0.0.1")]
    requests, _ = http(lambda request: response(status=302, location="/again"))
    with pytest.raises(UnsafePublicURL):
        await fetch_public("https://example.com/", timeout=1, max_bytes=128)
    assert len(requests) == 1 and requests[0].url.host == PUBLIC
    assert socket.getaddrinfo is dns


async def test_ipv6_literal_uses_bracketed_host_port_without_dns(http: Any, dns: Mock) -> None:
    requests, _ = http(lambda _: response())
    result = await fetch_public(f"https://[{V6}]:8443/path", timeout=1, max_bytes=128)
    assert result.url == f"https://[{V6}]:8443/path"
    assert requests[0].headers["host"] == f"[{V6}]:8443"
    assert requests[0].extensions["sni_hostname"] == V6
    dns.assert_not_called()


async def test_connection_fallback_uses_checked_addresses_under_one_budget(http: Any, dns: Mock) -> None:
    dns.return_value = addresses(V6, PUBLIC, PUBLIC)

    def connect(request: httpx.Request) -> httpx.Response:
        if request.url.host == V6:
            raise httpx.ConnectTimeout("unreachable", request=request)
        return response()

    requests, clients = http(connect)
    assert (await fetch_public("https://example.com/", timeout=1, max_bytes=128)).content == b"complete"
    assert [r.url.host for r in requests] == [V6, PUBLIC]
    assert all(client.is_closed for client in clients)
    dns.assert_called_once()


@pytest.mark.parametrize("error", [
    httpx.ReadError("read"), httpx.ReadTimeout("read"), httpx.RemoteProtocolError("protocol"),
])
async def test_nonconnection_failure_never_falls_through_to_another_ip(http: Any, dns: Mock, error: Exception) -> None:
    dns.return_value = addresses(PUBLIC, SECOND)

    def fail(_: httpx.Request) -> httpx.Response:
        raise error

    requests, clients = http(fail)
    with pytest.raises(type(error)):
        await fetch_public("https://example.com/", timeout=1, max_bytes=128)
    assert len(requests) == 1 and clients[0].is_closed


async def test_error_status_returns_logical_metadata_without_consuming_body(http: Any, dns: Mock) -> None:
    dns.return_value = addresses(PUBLIC, SECOND)
    stream = Stream([b"must not be read"])
    requests, _ = http(lambda _: httpx.Response(429, headers={"retry-after": "2"}, stream=stream))
    result = await fetch_public("https://example.com/path", timeout=1, max_bytes=128)
    assert len(requests) == 1 and stream.reads == 0 and stream.closed
    assert result.content == b"" and result.headers["retry-after"] == "2"
    with pytest.raises(httpx.HTTPStatusError) as raised:
        result.raise_for_status()
    assert str(raised.value.request.url) == "https://example.com/path"
    assert raised.value.response.is_closed


@pytest.mark.parametrize("status", [200, 302, 404, 410, 503])
async def test_head_never_redirects_reads_or_checks_body_length(http: Any, status: int) -> None:
    stream = Stream([b"must not be read"])
    requests, clients = http(lambda _: httpx.Response(
        status, headers={"location": "http://127.0.0.1/", "content-length": "invalid"}, stream=stream,
    ))
    result = await fetch_public("https://example.com/", method="HEAD", timeout=1, max_bytes=0)
    assert result.status_code == status and result.content == b""
    assert len(requests) == 1 and requests[0].method == "HEAD"
    assert stream.reads == 0 and stream.closed and clients[0].is_closed


@pytest.mark.parametrize("encoding", ["identity", "gzip", "x-gzip", "deflate", "raw-deflate"])
async def test_supported_encoding_is_decoded_once_with_exact_body_cap(http: Any, encoding: str) -> None:
    body = b"A" * 128
    if encoding in {"gzip", "x-gzip"}:
        raw = gzip.compress(body)
    elif encoding == "deflate":
        raw = zlib.compress(body)
    elif encoding == "raw-deflate":
        raw = zlib.compress(body)[2:-4]
        encoding = "deflate"
    else:
        raw = body
    _, clients = http(lambda _: response(raw, **{"content-encoding": encoding, "content-length": str(len(raw))}))
    result = await fetch_public("https://example.com/", timeout=1, max_bytes=128)
    assert result.content == body and clients[0].is_closed
    result.raise_for_status()


@pytest.mark.parametrize("kind,reason", [
    ("raw", "oversized_body"), ("gzip_bomb", "oversized_body"), ("declared", "content_length"),
    ("clipped", "clipped_http_body"), ("truncated", "invalid_content_encoding"),
    ("trailing", "invalid_content_encoding"), ("members", "invalid_content_encoding"),
    ("chain", "unsupported_content_encoding"), ("unknown", "unsupported_content_encoding"),
    ("corrupt", "invalid_content_encoding"),
])
async def test_resource_or_encoding_failure_never_returns_partial_evidence(http: Any, kind: str, reason: str) -> None:
    raw, headers = b"A" * 129, {}
    if kind == "gzip_bomb":
        raw, headers = gzip.compress(b"A" * 20000), {"content-encoding": "gzip"}
        assert len(raw) < 128  # Prove decoded overflow, independently of the raw cap.
    elif kind == "declared":
        headers = {"content-length": "129"}
    elif kind == "clipped":
        raw, headers = b"short", {"content-length": "100"}
    elif kind in {"truncated", "trailing", "members"}:
        raw, headers = gzip.compress(b"complete"), {"content-encoding": "gzip"}
        raw = raw[:-1] if kind == "truncated" else raw + (b"junk" if kind == "trailing" else raw)
    elif kind in {"chain", "unknown", "corrupt"}:
        raw = b"invalid"
        headers = {"content-encoding": {"chain": "gzip, deflate", "unknown": "br", "corrupt": "gzip"}[kind]}
    stream = Stream([raw])
    _, clients = http(lambda _: httpx.Response(200, headers=headers, stream=stream))
    with pytest.raises(PublicFetchError, match=reason):
        await fetch_public("https://example.com/", timeout=1, max_bytes=128)
    assert stream.closed and clients[0].is_closed


async def test_slow_stream_and_dns_share_aggregate_deadline(http: Any, dns: Mock) -> None:
    stream = Stream([b"a"] * 5, delay=0.03)
    _, clients = http(lambda _: httpx.Response(200, stream=stream))
    with pytest.raises(TimeoutError):
        await fetch_public("https://example.com/", timeout=0.05, max_bytes=128)
    assert stream.reads < 5 and stream.closed and clients[0].is_closed


async def test_dns_timeout_does_not_issue_request_after_worker_finishes(http: Any, dns: Mock) -> None:
    release = threading.Event()

    def resolve(*_: Any) -> list[Any]:
        release.wait(timeout=1)
        return addresses(PUBLIC)

    dns.side_effect = resolve
    requests, _ = http(lambda _: pytest.fail("Timed-out lookup reached transport"))
    try:
        with pytest.raises(TimeoutError):
            await fetch_public("https://example.com/", timeout=0.01, max_bytes=128)
    finally:
        release.set()
    await asyncio.sleep(0.02)
    assert requests == [] and socket.getaddrinfo is dns


async def test_overlapping_origins_and_cancellation_do_not_share_authority(http: Any, dns: Mock) -> None:
    entered = {"one.example": asyncio.Event(), "two.example": asyncio.Event()}
    release = asyncio.Event()
    streams: dict[str, Stream] = {}

    class WaitingStream(Stream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            entered[self.chunks[0].decode()].set()
            await release.wait()
            yield b"complete"

    def resolve(host: str, *_: Any) -> list[Any]:
        return addresses(PUBLIC if host == "one.example" else SECOND)

    def dispatch(request: httpx.Request) -> httpx.Response:
        host = request.headers["host"]
        assert request.url.host == (PUBLIC if host == "one.example" else SECOND)
        streams[host] = WaitingStream([host.encode()])
        return httpx.Response(200, stream=streams[host])

    dns.side_effect = resolve
    _, clients = http(dispatch)
    one = asyncio.create_task(fetch_public("https://one.example/", timeout=1, max_bytes=128))
    two = asyncio.create_task(fetch_public("https://two.example/", timeout=1, max_bytes=128))
    try:
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in entered.values())), timeout=0.5)
        one.cancel()
        with pytest.raises(asyncio.CancelledError):
            await one
        assert streams["one.example"].closed and not streams["two.example"].closed
        assert socket.getaddrinfo is dns
        release.set()
        assert (await two).url == "https://two.example/"
    finally:
        release.set()
        await asyncio.gather(one, two, return_exceptions=True)
    assert all(client.is_closed for client in clients)


async def test_real_httpcore_connects_ip_but_uses_original_tls_identity_and_host(
    monkeypatch: pytest.MonkeyPatch, dns: Mock,
) -> None:
    """Exercise HTTPX/httpcore, replacing only socket I/O, not TLS configuration."""
    calls: list[tuple[str, int]] = []
    written = bytearray()
    contexts: list[ssl.SSLContext] = []
    closed: list[bool] = []

    class WireStream:
        pending = b"HTTP/1.1 200 OK\r\nContent-Length: 3\r\n\r\nyes"

        async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
            data, self.pending = self.pending[:max_bytes], self.pending[max_bytes:]
            return data

        async def write(self, buffer: bytes, timeout: float | None = None) -> None:
            written.extend(buffer)

        async def aclose(self) -> None:
            closed.append(True)

        async def start_tls(self, ssl_context: ssl.SSLContext, server_hostname: str,
                            timeout: float | None = None) -> WireStream:
            assert server_hostname == "xn--fa-hia.example"
            assert ssl_context.check_hostname is True
            assert ssl_context.verify_mode == ssl.CERT_REQUIRED
            contexts.append(ssl_context)
            return self

        def get_extra_info(self, name: str) -> None:
            return None

    async def connect(self: Any, host: str, port: int, **kwargs: Any) -> WireStream:
        calls.append((host, port))
        assert kwargs["timeout"] <= 3.0
        return WireStream()

    # An inherited proxy must never become an alternate connection target.
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9999")
    monkeypatch.setattr(AnyIOBackend, "connect_tcp", connect)
    result = await fetch_public("https://faß.example:8443/A?q=x%2Fy", timeout=1, max_bytes=128)
    assert calls == [(PUBLIC, 8443)]
    assert contexts and closed
    assert b"GET /A?q=x%2Fy HTTP/1.1\r\n" in written
    assert b"Host: xn--fa-hia.example:8443\r\n" in written
    assert result.url == "https://xn--fa-hia.example:8443/A?q=x%2Fy" and result.content == b"yes"
    dns.assert_called_once_with("xn--fa-hia.example", None, socket.AF_UNSPEC, socket.SOCK_STREAM)


async def test_connect_error_during_body_read_does_not_retry_another_address(http: Any, dns: Mock) -> None:
    dns.return_value = addresses(PUBLIC, SECOND)

    class BrokenStream(Stream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield b"start"
            raise httpx.ConnectError("after headers")

    stream = BrokenStream([])
    requests, _ = http(lambda _: httpx.Response(200, stream=stream))
    with pytest.raises(httpx.ConnectError):
        await fetch_public("https://example.com/", timeout=1, max_bytes=128)
    assert len(requests) == 1 and stream.closed


async def test_exhausted_connections_propagate_last_connection_failure(http: Any, dns: Mock) -> None:
    dns.return_value = addresses(PUBLIC, SECOND)

    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(request.url.host, request=request)

    requests, clients = http(fail)
    with pytest.raises(httpx.ConnectError, match=SECOND):
        await fetch_public("https://example.com/", timeout=1, max_bytes=128)
    assert [request.url.host for request in requests] == [PUBLIC, SECOND]
    assert all(client.is_closed for client in clients)


async def test_redirect_chain_shares_total_deadline(http: Any) -> None:
    async def redirect(_: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.03)
        return response(status=302, location="/again")

    requests, clients = http(redirect)
    with pytest.raises(TimeoutError):
        await fetch_public("https://example.com/", timeout=0.05, max_bytes=128)
    assert len(requests) == 2 and all(client.is_closed for client in clients)


@pytest.mark.parametrize("value", ["-1", "1, 1", "9" * 5000])
async def test_malformed_or_huge_content_length_is_classified_before_read(http: Any, value: str) -> None:
    stream = Stream([b"unread"])
    http(lambda _: httpx.Response(200, headers={"content-length": value}, stream=stream))
    with pytest.raises(PublicFetchError, match="oversized_or_invalid_content_length"):
        await fetch_public("https://example.com/", timeout=1, max_bytes=128)
    assert stream.reads == 0 and stream.closed


async def test_feed_logical_url_keeps_fragment_without_sending_it_in_http_target(http: Any) -> None:
    requests, _ = http(lambda _: response())
    result = await fetch_public("https://example.com/feed/?q=x%2Fy#section", timeout=1, max_bytes=128)
    assert result.url == "https://example.com/feed/?q=x%2Fy#section"
    assert requests[0].url.raw_path == b"/feed/?q=x%2Fy"
