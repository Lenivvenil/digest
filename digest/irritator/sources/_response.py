"""Bounded decoded response buffering for source adapters."""

from __future__ import annotations

import httpx

MAX_SOURCE_RESPONSE_BYTES = 512000


async def read_bounded_response(response: httpx.Response, maximum_bytes: int) -> None:
    """Buffer a complete decoded body or reject it without accepting a prefix.

    The bound applies to accumulated decoded bytes, not transient decompressor
    allocations. The caller owns response closure on failure or cancellation.
    """
    body = bytearray()
    async for chunk in response.aiter_bytes():
        if len(body) + len(chunk) > maximum_bytes:
            raise ValueError("Source response exceeds the response budget.")
        body.extend(chunk)
    # Match HTTPX's aread content cache without reconstructing the response:
    # its charset/default encoding and already-decoded Content-Encoding survive.
    response._content = bytes(body)
