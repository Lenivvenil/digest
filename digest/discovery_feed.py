"""Bounded, technical RSS/Atom validation for source discovery proposals.

Validation says only that an endpoint safely returned a feed, not whether its
articles or publisher are useful. No entries are required for an empty feed.
"""

from __future__ import annotations

import asyncio
import xml.etree.ElementTree as ET

import feedparser
import httpx

from digest.adapters.http.public_fetch import PublicFetchError, UnsafePublicURL, fetch_public

MAX_FEED_BYTES = 2 * 1024 * 1024
MAX_FEED_REDIRECTS = 3
FEED_VALIDATION_SECONDS = 15.0
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


async def validate_feed_url(url: str) -> str:
    """Return the final safe feed URL or raise a technical validation exception.

    One aggregate 15-second deadline covers public acquisition and parsing.
    Both raw and decoded bodies are capped at 2 MiB. Timing out cannot stop
    an already running DNS or parser thread. There are no retries.
    """
    try:
        async with asyncio.timeout(FEED_VALIDATION_SECONDS):
            response = await fetch_public(
                url, timeout=FEED_VALIDATION_SECONDS, max_bytes=MAX_FEED_BYTES,
                max_redirects=MAX_FEED_REDIRECTS,
            )
            response.raise_for_status()
            media_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if media_type in {"text/html", "application/xhtml+xml"}:
                raise FeedValidationError("Feed endpoint returned HTML.")
            await asyncio.to_thread(_check_feed_bytes, response.content)
            return response.url
    except (TimeoutError, httpx.TimeoutException) as exc:
        raise FeedValidationTimeout("Feed validation timed out.") from exc
    except UnsafePublicURL as exc:
        raise FeedValidationError("Feed URL failed public-address safety validation.") from exc
    except PublicFetchError as exc:
        messages = {
            "oversized_body": "Feed response exceeded the 2 MiB limit.",
            "oversized_or_invalid_content_length": "Feed response has an invalid or oversized 2 MiB Content-Length.",
            "clipped_http_body": "Feed response body is incomplete.",
            "redirect_limit_or_missing_target": "Feed exceeded the redirect limit or has no destination.",
            "unsupported_content_encoding": "Feed response uses an unsupported content encoding.",
            "invalid_content_encoding": "Feed response has an invalid or incomplete content encoding.",
        }
        raise FeedValidationError(messages.get(str(exc), "Feed endpoint could not be fetched safely.")) from exc
    except (httpx.HTTPError, httpx.InvalidURL, OSError, UnicodeError) as exc:
        raise FeedValidationError("Feed endpoint could not be fetched safely.") from exc
