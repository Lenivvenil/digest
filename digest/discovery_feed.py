"""Bounded, technical RSS/Atom validation for source discovery proposals.

Call sequentially: DNS pinning temporarily replaces process-wide resolution.
Validation says only that an endpoint safely returned a feed, not whether its
articles or publisher are useful. No entries are required for an empty feed.
"""

from __future__ import annotations

import asyncio
import xml.etree.ElementTree as ET
from urllib.parse import urljoin, urlsplit

import feedparser
import httpx

from digest._dns_pinning import pin_dns, validate_url

MAX_FEED_BYTES = 2 * 1024 * 1024
MAX_FEED_REDIRECTS = 3
FEED_VALIDATION_SECONDS = 15.0
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_ATOM_NAMESPACES = {"http://www.w3.org/2005/Atom", "http://purl.org/atom/ns#"}
_RSS_NAMESPACES = {"http://purl.org/rss/1.0/", "http://my.netscape.com/rdf/simple/0.9/"}


class FeedValidationError(ValueError):
    """The endpoint could not be technically verified as a safe RSS/Atom feed."""


class FeedValidationTimeout(FeedValidationError):
    """Feed validation exceeded its time budget; source quality is unknown."""


def _check_feed_bytes(raw: bytes) -> None:
    """Require a real, well-formed feed root, including feeds with zero entries."""
    try:
        root = ET.fromstring(raw)
    except (ET.ParseError, LookupError, ValueError) as exc:
        raise FeedValidationError("Feed response is not well-formed XML.") from exc
    is_rss = root.tag == "rss" and root.find("channel") is not None
    is_atom = root.tag in {f"{{{namespace}}}feed" for namespace in _ATOM_NAMESPACES}
    is_rdf = root.tag == "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}RDF" and any(
        root.find(f"{{{namespace}}}channel") is not None for namespace in _RSS_NAMESPACES
    )
    if not (is_rss or is_atom or is_rdf):
        raise FeedValidationError("Feed response is not an RSS or Atom document.")
    parsed = feedparser.parse(raw)
    if parsed.get("bozo") or not str(parsed.get("version", "")).startswith(("rss", "atom")):
        raise FeedValidationError("Feed response could not be parsed as RSS or Atom.")


def _check_url_syntax(url: str) -> None:
    """Reject credentials and ambiguous URL spellings before either parser sees them."""
    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        raise FeedValidationError("Feed URL is invalid.") from exc
    if parsed.username is not None or parsed.password is not None:
        raise FeedValidationError("Feed URLs must not contain credentials.")
    if "\\" in url or any(ord(char) <= 32 or ord(char) == 127 for char in url):
        raise FeedValidationError("Feed URL contains whitespace or control characters.")


async def _fetch_feed(client: httpx.AsyncClient, url: str) -> str:
    current_url = url
    for redirects in range(MAX_FEED_REDIRECTS + 1):
        _check_url_syntax(current_url)
        # DNS resolution must be covered by the aggregate deadline too.
        # Validate the spelling httpx will actually request (notably IDNA),
        # otherwise the hostname patched by pin_dns might not match the socket.
        request_url = str(httpx.URL(current_url))
        validated = await asyncio.to_thread(validate_url, request_url)
        if validated is None:
            raise FeedValidationError("Feed URL failed public-address safety validation.")
        with pin_dns(validated.hostname, validated.pinned_addrinfos):
            async with client.stream("GET", validated.url) as response:
                if response.status_code in _REDIRECT_STATUSES:
                    if redirects == MAX_FEED_REDIRECTS:
                        raise FeedValidationError("Feed exceeded the redirect limit.")
                    location = response.headers.get("location")
                    if not location:
                        raise FeedValidationError("Feed redirect has no destination.")
                    # Do not normalize paths, queries, or trailing slashes: they
                    # can identify different feeds. Validate every resulting hop.
                    try:
                        current_url = urljoin(validated.url, location)
                    except ValueError as exc:
                        raise FeedValidationError("Feed redirect destination is invalid.") from exc
                    continue
                response.raise_for_status()
                media_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                if media_type in {"text/html", "application/xhtml+xml"}:
                    raise FeedValidationError("Feed endpoint returned HTML.")
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(raw) + len(chunk) > MAX_FEED_BYTES:
                        raise FeedValidationError("Feed response exceeded the 2 MiB limit.")
                    raw.extend(chunk)
        await asyncio.to_thread(_check_feed_bytes, bytes(raw))
        return validated.url
    raise FeedValidationError("Feed exceeded the redirect limit.")  # pragma: no cover


async def validate_feed_url(url: str) -> str:
    """Return the final safe feed URL or raise a technical validation exception.

    One aggregate 15-second deadline bounds awaiting DNS, at most three redirects,
    the streamed response (at most 2 MiB decoded), and parsing. Timing out cannot
    stop an already running DNS or parser thread. There are no retries.
    Environment proxies are disabled so they cannot bypass DNS pinning.
    """
    try:
        async with asyncio.timeout(FEED_VALIDATION_SECONDS):
            async with httpx.AsyncClient(
                follow_redirects=False,
                trust_env=False,
                timeout=FEED_VALIDATION_SECONDS,
                headers={"Accept-Encoding": "identity"},
            ) as client:
                return await _fetch_feed(client, url)
    except (TimeoutError, httpx.TimeoutException) as exc:
        raise FeedValidationTimeout("Feed validation timed out.") from exc
    except (httpx.HTTPError, httpx.InvalidURL, OSError, UnicodeError) as exc:
        raise FeedValidationError("Feed endpoint could not be fetched safely.") from exc
