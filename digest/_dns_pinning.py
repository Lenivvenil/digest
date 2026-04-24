"""SSRF-prevention helpers: URL validation and DNS pinning.

_validate_url() resolves a URL's hostname at validation time and checks that
every resulting IP is globally routable.  _pin_dns() then patches
socket.getaddrinfo() so the subsequent HTTP request connects to the same IPs
that were checked, preventing DNS rebinding attacks.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ValidatedURL:
    """Result of URL validation with pinned DNS resolution."""

    url: str
    hostname: str
    pinned_addrinfos: list[tuple[int, int, int, str, Any]]


def validate_url(url: str) -> ValidatedURL | None:
    """Validate URL safety by checking all resolved IPs are globally routable.

    Returns a ValidatedURL with pinned DNS results if safe, or None if the URL
    resolves to non-global addresses (private, loopback, multicast, CGNAT, etc.).
    The pinned addrinfos must be used for the actual fetch to prevent DNS rebinding.
    """
    import ipaddress
    import socket
    from urllib.parse import urlparse

    try:
        parsed = urlparse(url)
    except Exception:
        return None

    if parsed.scheme not in ("http", "https"):
        return None

    hostname = parsed.hostname
    if not hostname:
        return None

    # Normalize IDN hostnames to ASCII/punycode so that pin_dns
    # matches the form that httpx/socket will actually use.
    try:
        hostname = hostname.encode("idna").decode("ascii")
    except (UnicodeError, UnicodeDecodeError):
        return None

    # Reject out-of-range ports early (parsed.port raises ValueError)
    try:
        _ = parsed.port
    except ValueError:
        return None

    # Block well-known cloud metadata endpoints
    if hostname in ("localhost", "metadata.google.internal"):
        return None

    def _is_safe(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
        return addr.is_global and not addr.is_multicast

    # Use is_global as allowlist — rejects private, loopback, link-local,
    # reserved, CGNAT (100.64/10), and any other non-routable space.
    # Multicast is excluded separately (Python considers it "global").
    pinned_addrinfos: list[tuple[int, int, int, str, Any]] = []
    try:
        addr = ipaddress.ip_address(hostname)
        if not _is_safe(addr):
            return None
        # IP literal — synthesize a single addrinfo entry
        family = socket.AF_INET6 if addr.version == 6 else socket.AF_INET
        sockaddr: Any = (
            (str(addr), 0, 0, 0) if addr.version == 6 else (str(addr), 0)
        )
        pinned_addrinfos = [(family, socket.SOCK_STREAM, 0, "", sockaddr)]
    except ValueError:
        # hostname is a DNS name — resolve and check all resulting IPs
        try:
            addrinfos = socket.getaddrinfo(
                hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM
            )
        except (socket.gaierror, UnicodeError):
            return None
        for family, type_, proto, canonname, sockaddr in addrinfos:
            ip_str = str(sockaddr[0])
            try:
                addr = ipaddress.ip_address(ip_str)
                if not _is_safe(addr):
                    return None
                pinned_addrinfos.append((family, type_, proto, canonname, sockaddr))
            except ValueError:
                return None

    if not pinned_addrinfos:
        return None

    return ValidatedURL(url=url, hostname=hostname, pinned_addrinfos=pinned_addrinfos)


def pin_dns(
    hostname: str, addrinfos: list[tuple[int, int, int, str, Any]]
) -> contextlib.AbstractContextManager[None]:
    """Pin DNS resolution for *hostname* to pre-validated addresses.

    Prevents DNS rebinding by ensuring httpx connects to the same IPs
    that were checked during validation. Safe for sequential async I/O
    (one outstanding fetch at a time).
    """
    import socket as _socket

    @contextlib.contextmanager
    def _ctx() -> Iterator[None]:
        original = _socket.getaddrinfo

        def _pinned(
            host: str | bytes,
            port: int | str | None,
            family: int = 0,
            type: int = 0,  # noqa: A002
            proto: int = 0,
            flags: int = 0,
        ) -> list[tuple[int, int, int, str, Any]]:
            _host = host.decode("ascii") if isinstance(host, bytes) else host
            if _host == hostname:
                p = int(port) if port is not None and str(port).isdigit() else 0
                result: list[tuple[int, int, int, str, Any]] = []
                for af, st, pr, cn, sa in addrinfos:
                    if af == _socket.AF_INET:
                        result.append((af, st, pr, cn, (sa[0], p)))
                    else:
                        result.append((af, st, pr, cn, (sa[0], p, sa[2], sa[3])))
                return result
            return original(host, port, family, type, proto, flags)  # type: ignore[return-value]

        _socket.getaddrinfo = _pinned  # type: ignore[assignment]
        try:
            yield
        finally:
            _socket.getaddrinfo = original

    return _ctx()
