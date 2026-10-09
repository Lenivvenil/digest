"""Credential-safe diagnostics around existing Telegram HTTP operations.

This boundary owns no retries, response interpretation or delivery policy. The sent
request is untouched; only returned diagnostic request metadata is replaced.
"""

from __future__ import annotations

import logging
import re
from contextvars import ContextVar
from typing import Any, Literal

import httpx

# These are the concrete emitting loggers in the reviewed httpcore 1.0.9 graph.
# Ancestor filters do not run for propagated records. No temporary level changes.
_HTTP_LOGGERS = (
    "httpx",
    "httpcore",
    "httpcore.connection",
    "httpcore.http11",
    "httpcore.http2",
    "httpcore.proxy",
    "httpcore.socks",
)
_TOKEN_URL = re.compile(r"(https?://api\.telegram\.org/bot)([^/\s\"'?#]+)", re.IGNORECASE)
_ACTIVE_TOKEN: ContextVar[str] = ContextVar("telegram_diagnostic_token", default="")


def _redact(text: str, tokens: set[str] | None = None) -> str:
    tokens = (tokens or set()) | {match[2] for match in _TOKEN_URL.finditer(text)}
    if token := _ACTIVE_TOKEN.get():
        tokens.add(token)
    text = _TOKEN_URL.sub(r"\1[redacted]", text)
    for token in tokens:
        text = text.replace(token, "[redacted]")
    return text


def _safe_record(record: logging.LogRecord) -> bool:
    """Sanitize Telegram records before handlers; leave unrelated records intact."""
    message = record.getMessage()
    exception_text = record.exc_text or (
        logging.Formatter().formatException(record.exc_info) if record.exc_info else ""
    )
    telegram_error = bool(record.exc_info and getattr(record.exc_info[1], "_telegram_diagnostic", False))
    if not (
        _ACTIVE_TOKEN.get()
        or telegram_error
        or any(_TOKEN_URL.search(value) for value in (message, exception_text, record.stack_info or ""))
    ):
        return True
    tokens = {match[2] for match in _TOKEN_URL.finditer("\n".join((message, exception_text, record.stack_info or "")))}
    record.msg = _redact(message, tokens)
    record.args = ()
    if record.exc_info or record.exc_text:
        record.exc_text = _redact(exception_text, tokens)
        record.exc_info = None
    if record.stack_info:
        record.stack_info = _redact(record.stack_info, tokens)
    return True


def _install_filters() -> None:
    for name in _HTTP_LOGGERS:
        logger = logging.getLogger(name)
        if _safe_record not in logger.filters:
            logger.addFilter(_safe_record)


def _request_metadata(request: httpx.Request) -> httpx.Request:
    """No body, headers or credential-bearing extensions cross into diagnostics."""
    return httpx.Request(request.method, _redact(str(request.url)))


def raise_for_status(response: httpx.Response) -> None:
    """Keep HTTPX status semantics without interpolating headers/reason/body."""
    if response.is_success:
        return
    error = httpx.HTTPStatusError(
        f"Telegram HTTP {response.status_code}",
        request=response.request,
        response=response,
    )
    error.__dict__["_telegram_diagnostic"] = True
    raise error


async def request(
    client: httpx.AsyncClient,
    method: Literal["GET", "POST"],
    url: str,
    *,
    json: dict[str, Any] | None = None,
    timeout: float | None = None,
) -> httpx.Response:
    """Perform exactly the caller's one request with safe diagnostic metadata."""
    _install_filters()
    token = url.partition("/bot")[2].partition("/")[0]
    active = _ACTIVE_TOKEN.set(token)
    try:
        kwargs: dict[str, Any] = {}
        if json is not None:
            kwargs["json"] = json
        if timeout is not None:
            kwargs["timeout"] = timeout
        call = client.get if method == "GET" else client.post
        try:
            response = await call(url, **kwargs)
        except Exception as exc:
            metadata = httpx.Request(method, _redact(url))
            status = f" HTTP {exc.response.status_code}" if isinstance(exc, httpx.HTTPStatusError) else ""
            message = f"Telegram {method}{status} failed ({type(exc).__name__})"
            safe: Exception
            if isinstance(exc, httpx.HTTPStatusError):
                exc.response.request = metadata
                safe = httpx.HTTPStatusError(message, request=metadata, response=exc.response)
            elif isinstance(exc, httpx.RequestError):
                safe = type(exc)(message, request=metadata)
            elif isinstance(exc, httpx.HTTPError):
                safe = httpx.HTTPError(message)
            elif isinstance(exc, TimeoutError):
                safe = TimeoutError(message)
            elif isinstance(exc, OSError):
                safe = OSError(message)
            elif isinstance(exc, ValueError):
                safe = ValueError(message)
            else:
                # Unknown/structured exceptions remain outside the existing
                # retry/unknown catch categories, without retaining unsafe text.
                safe = RuntimeError(message)
            safe.__dict__["_telegram_diagnostic"] = True
            raise safe from None
        response.request = _request_metadata(response.request)
        return response
    finally:
        _ACTIVE_TOKEN.reset(active)
