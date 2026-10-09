"""One public-only, connection-scoped and resource-bounded HTTP acquisition.

Only GET bodies and no-redirect HEAD metadata are supported. Parsers and retry
policies belong to callers. No process-global resolver or environment proxy is used.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urljoin, urlsplit

import httpx

_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_CONNECT_SECONDS = 3.0
_RAW_CHUNK_BYTES = 64 * 1024


class PublicFetchError(ValueError):
    """A technical acquisition bound failed; no partial evidence is returned."""


class UnsafePublicURL(PublicFetchError):
    """The requested URL or any resolved address is outside public acquisition."""

    def __init__(self) -> None:
        super().__init__("unsafe_url")


@dataclass(frozen=True)
class PublicResponse:
    """Closed response metadata and one bounded, already-decoded body.

    Headers describe the wire response; content must not be decoded again using
    Content-Encoding. The URL is the logical origin, never the transport IP.
    """

    url: str
    status_code: int
    headers: httpx.Headers
    content: bytes
    encoding: str

    def raise_for_status(self) -> None:
        # Empty diagnostic content avoids decoding the real body a second time.
        httpx.Response(
            self.status_code, headers=self.headers,
            request=httpx.Request("GET", self.url), content=b"",
        ).raise_for_status()


def _check_spelling(value: str) -> None:
    # Run before either URL parser can strip control characters or normalize them.
    if "\\" in value or any(ord(char) <= 32 or ord(char) == 127 for char in value):
        raise UnsafePublicURL()


def _logical_url(value: str, validate_hop: Callable[[str], str] | None) -> httpx.URL:
    _check_spelling(value)
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None):
            raise UnsafePublicURL()
        _ = parsed.port
        if validate_hop is not None:
            value = validate_hop(value)
        url = httpx.URL(value)
        if "%" in url.host or url.raw_host in {b"localhost", b"metadata.google.internal"}:
            raise UnsafePublicURL()
        return url
    except (ValueError, httpx.InvalidURL, UnicodeError) as exc:
        raise UnsafePublicURL() from exc


def _public_addresses(host: str) -> list[str]:
    """Resolve once and reject the entire answer if any address is nonpublic."""
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        try:
            answers = socket.getaddrinfo(host, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        except (socket.gaierror, UnicodeError) as exc:
            raise UnsafePublicURL() from exc
        addresses = [str(answer[4][0]) for answer in answers]
    else:
        addresses = [str(literal)]
    if not addresses:
        raise UnsafePublicURL()
    unique: list[str] = []
    for text in addresses:
        try:
            address = ipaddress.ip_address(text)
        except ValueError as exc:
            raise UnsafePublicURL() from exc
        # Python 3.12 considers fec0::/10 global despite it being site-local.
        # Reserved translation prefixes are deliberately excluded as well.
        if (not address.is_global or address.is_multicast or address.is_reserved
                or (isinstance(address, ipaddress.IPv6Address) and address.is_site_local)
                or "%" in text):
            raise UnsafePublicURL()
        normalized = str(address)
        if normalized not in unique:
            unique.append(normalized)
    return unique


def _decode_body(raw: bytes, encoding: str, limit: int) -> bytes:
    encoding = encoding.strip().lower()
    if encoding in {"", "identity"}:
        return raw
    if encoding not in {"gzip", "x-gzip", "deflate"}:
        raise PublicFetchError("unsupported_content_encoding")
    windows = (zlib.MAX_WBITS | 16,) if encoding in {"gzip", "x-gzip"} else (zlib.MAX_WBITS, -zlib.MAX_WBITS)
    for window in windows:
        decoder = zlib.decompressobj(window)
        try:
            decoded = decoder.decompress(raw, limit + 1)
        except zlib.error:
            # Some publishers use raw deflate rather than the zlib wrapper.
            continue
        if len(decoded) > limit or decoder.unconsumed_tail:
            raise PublicFetchError("oversized_body")
        if not decoder.eof or decoder.unused_data:
            raise PublicFetchError("invalid_content_encoding")
        return decoded
    raise PublicFetchError("invalid_content_encoding")


async def _read_body(response: httpx.Response, limit: int) -> bytes:
    declared = response.headers.get("content-length")
    if declared is not None and (
        not declared.isascii() or not declared.isdigit()
        or len(declared.lstrip("0")) > len(str(limit)) or int(declared.lstrip("0") or "0") > limit
    ):
        raise PublicFetchError("oversized_or_invalid_content_length")
    raw = bytearray()
    # Unlike aiter_bytes, this never invokes an unbounded HTTPX decoder. HTTPX
    # may read ahead one bounded chunk before our accumulator rejects overflow.
    async for chunk in response.aiter_raw(chunk_size=_RAW_CHUNK_BYTES):
        if len(raw) + len(chunk) > limit:
            raise PublicFetchError("oversized_body")
        raw.extend(chunk)
    if declared is not None and len(raw) != int(declared.lstrip("0") or "0"):
        raise PublicFetchError("clipped_http_body")
    return _decode_body(bytes(raw), response.headers.get("content-encoding", ""), limit)


async def _fetch_hop(
    url: httpx.URL, addresses: list[str], *, method: Literal["GET", "HEAD"],
    deadline: float, max_bytes: int, headers: dict[str, str] | None,
) -> PublicResponse:
    loop = asyncio.get_running_loop()
    request_headers = httpx.Headers(headers)
    # Let HTTPX construct correct ASCII IDNA / bracketed IPv6 / port authority.
    request_headers["Host"] = httpx.Request(method, url).headers["Host"]
    request_headers["Accept-Encoding"] = "identity"
    for index, address in enumerate(addresses):
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise TimeoutError
        received_headers = False
        try:
            async with httpx.AsyncClient(
                trust_env=False, follow_redirects=False, headers=request_headers,
                timeout=httpx.Timeout(remaining, connect=min(_CONNECT_SECONDS, remaining)),
                limits=httpx.Limits(max_connections=1, max_keepalive_connections=0),
            ) as client:
                async with client.stream(
                    method, url.copy_with(host=address),
                    extensions={"sni_hostname": url.raw_host.decode("ascii")},
                ) as response:
                    received_headers = True
                    body = b""
                    if method == "GET" and 200 <= response.status_code < 300:
                        body = await _read_body(response, max_bytes)
                    result = PublicResponse(
                        str(url), response.status_code, httpx.Headers(response.headers),
                        body, response.encoding or "utf-8",
                    )
            if loop.time() >= deadline:
                raise TimeoutError
            return result
        except (httpx.ConnectError, httpx.ConnectTimeout):
            if received_headers or index == len(addresses) - 1:
                raise
    raise UnsafePublicURL()  # Empty resolutions are rejected before this operation.


async def fetch_public(
    url: str, *, timeout: float, max_bytes: int, max_redirects: int = 3,
    method: Literal["GET", "HEAD"] = "GET", headers: dict[str, str] | None = None,
    validate_hop: Callable[[str], str] | None = None,
) -> PublicResponse:
    """Acquire a public URL under one total deadline, without ambient authority.

    DNS awaits are bounded, but an already-running OS resolver thread cannot be
    stopped. Bounded synchronous decode is checked on return, not preempted.
    HEAD returns status/headers only; GET redirects never consume response bodies.
    TCP and TLS establishment each have a three-second subdeadline. Address
    fallback occurs only while the shared total budget has time remaining.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    async with asyncio.timeout(timeout):
        current = _logical_url(url, validate_hop)
        for redirects in range(max_redirects + 1):
            addresses = await asyncio.to_thread(_public_addresses, current.raw_host.decode("ascii"))
            response = await _fetch_hop(
                current, addresses, method=method, deadline=deadline, max_bytes=max_bytes, headers=headers,
            )
            if method == "HEAD" or response.status_code not in _REDIRECT_STATUSES:
                return response
            location = response.headers.get("location")
            if not location or redirects == max_redirects:
                raise PublicFetchError("redirect_limit_or_missing_target")
            _check_spelling(location)
            try:
                destination = urljoin(str(current), location)
            except ValueError as exc:
                raise UnsafePublicURL() from exc
            current = _logical_url(destination, validate_hop)
    raise PublicFetchError("redirect_limit_or_missing_target")
