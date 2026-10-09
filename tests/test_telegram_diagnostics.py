"""The actual Telegram HTTP boundary keeps credentials out of diagnostics."""

from __future__ import annotations

import asyncio
import json
import logging
import traceback
from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from digest.adapters.telegram import delivery, diagnostics, discovery, feedback, prepared
from digest.domain.catalog.proposals import PendingSource
from digest.domain.editorial.summaries import ArticleSummary
from digest.domain.feedback.values import FeedbackStore, PendingReply
from digest.irritator import IrritatorStatus
from digest.irritator.evidence_stage import EvidenceIrritatorResult
from scripts.review_fixture import fixture_config
from tests.factories import make_ranked_signal

TOKEN = "123456789:synthetic-secret-DO-NOT-LOG"
API = f"https://api.telegram.org/bot{TOKEN}/sendMessage"


def assert_safe_logs(caplog: pytest.LogCaptureFixture) -> None:
    assert TOKEN not in caplog.text
    for record in caplog.records:
        assert TOKEN not in str(record.msg)
        assert TOKEN not in repr(record.args)
        assert TOKEN not in (record.exc_text or "")
        assert record.exc_info is None


async def test_request_preserves_wire_and_response_but_sanitizes_diagnostics(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    sent: list[httpx.Request] = []

    def transport(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={"ok": True}, headers={"X-Outcome": "preserved"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        response = await diagnostics.request(client, "POST", API, json={"text": "exact", "chat_id": "123"})
    assert len(sent) == 1
    assert str(sent[0].url) == API
    assert json.loads(sent[0].content) == {"text": "exact", "chat_id": "123"}
    assert response.status_code == 200 and response.json() == {"ok": True}
    assert response.headers["X-Outcome"] == "preserved"
    assert response.request is not sent[0]
    assert TOKEN not in str(response.request.url)
    assert not response.request.content
    assert "HTTP Request: POST" in caplog.text and "200 OK" in caplog.text
    assert_safe_logs(caplog)


@pytest.mark.parametrize("status", [302, 429, 503])
async def test_status_errors_do_not_interpolate_untrusted_reason_or_location(status: int) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                status,
                headers={"Location": API},
                extensions={"reason_phrase": TOKEN.encode()},
                json={"ok": False, "description": TOKEN},
            )
        )
    ) as client:
        response = await diagnostics.request(client, "POST", API)
    with pytest.raises(httpx.HTTPStatusError) as caught:
        diagnostics.raise_for_status(response)
    rendered = "".join(traceback.format_exception(caught.value))
    assert TOKEN not in rendered and f"HTTP {status}" in rendered
    assert caught.value.response is response
    assert response.headers["Location"] == API and response.json()["description"] == TOKEN


@pytest.mark.parametrize("error_type", [httpx.ReadTimeout, httpx.ConnectError, OSError])
async def test_transport_errors_keep_type_without_credential_traceback(
    error_type: type[Exception],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)

    def transport(request: httpx.Request) -> httpx.Response:
        try:
            raise ValueError(f"private predecessor {TOKEN}")
        except ValueError as cause:
            error = error_type(f"failed {API}")
            error.add_note(f"unsafe detail {TOKEN}")
            raise error from cause

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        with pytest.raises(error_type) as caught:
            await diagnostics.request(client, "POST", API)
    assert type(caught.value) is error_type
    assert TOKEN not in "".join(traceback.format_exception(caught.value))
    assert "Telegram POST" in str(caught.value)
    logging.getLogger("httpx").error("failure", exc_info=(type(caught.value), caught.value, caught.value.__traceback__))
    assert_safe_logs(caplog)


async def test_concrete_dependency_loggers_redact_raw_records_without_level_changes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    levels = {name: logging.getLogger(name).level for name in diagnostics._HTTP_LOGGERS}

    def transport(request: httpx.Request) -> httpx.Response:
        for name in diagnostics._HTTP_LOGGERS:
            try:
                raise ValueError(f"unsafe header {TOKEN}")
            except ValueError:
                logging.getLogger(name).debug("wire %s", request.url, exc_info=True, stack_info=True)
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        await diagnostics.request(client, "GET", API)
    assert levels == {name: logging.getLogger(name).level for name in levels}
    assert set(diagnostics._HTTP_LOGGERS) <= {r.name for r in caplog.records}
    assert_safe_logs(caplog)


async def test_overlapping_requests_and_cancellation_keep_separate_diagnostic_contexts(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    other_token = "987654321:another-synthetic-secret"
    entered = asyncio.Event()
    release = asyncio.Event()

    async def transport(request: httpx.Request) -> httpx.Response:
        if TOKEN in str(request.url):
            entered.set()
            await release.wait()
        logging.getLogger("httpcore.http11").debug("header %s", request.url)
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        pending = asyncio.create_task(diagnostics.request(client, "POST", API))
        await entered.wait()
        response = await diagnostics.request(client, "GET", API.replace(TOKEN, other_token))
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert response.status_code == 200
    assert other_token not in caplog.text
    assert diagnostics._ACTIVE_TOKEN.get() == ""
    assert_safe_logs(caplog)


_OPERATIONS = ["status", "cards", "compact", "counter", "post_delivery", "prepared", "discovery", "poll", "ack"]


@pytest.mark.parametrize("operation", _OPERATIONS)
@pytest.mark.parametrize("reject", [False, True], ids=["success", "http_rejection"])
async def test_all_telegram_entrypoints_use_real_httpx_safe_diagnostics(
    operation: str,
    reject: bool,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    monkeypatch.setattr(delivery.asyncio, "sleep", AsyncMock())
    sent: list[httpx.Request] = []
    real_client = httpx.AsyncClient

    def transport(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        if reject:
            return httpx.Response(403, json={"ok": False}, extensions={"reason_phrase": TOKEN.encode()})
        result: Any = {"message_id": 7, "chat": {"id": 123}}
        if request.url.path.endswith("getWebhookInfo"):
            result = {"url": ""}
        elif request.url.path.endswith("getUpdates"):
            result = []
        return httpx.Response(200, json={"ok": True, "result": result})

    def client(**kwargs: Any) -> httpx.AsyncClient:
        return real_client(transport=httpx.MockTransport(transport), trust_env=False, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    config = fixture_config()
    config.telegram.enabled = True
    article = ArticleSummary("Title", "https://example.com/story", "Source", "AI", "Summary")
    signal = make_ranked_signal()
    store = FeedbackStore(pending_replies=[PendingReply("callback", "id", "saved"), PendingReply("command", "/status")])
    calls: dict[str, Callable[[], Any]] = {
        "status": lambda: delivery.send_status_message("status"),
        "cards": lambda: delivery.send_article_cards({}, config, top_articles=[article]),
        "compact": lambda: delivery.send_compact_issue([article], config),
        "counter": lambda: delivery.send_counter_signals([signal], config, IrritatorStatus("done", "complete")),
        "post_delivery": lambda: delivery.send_post_delivery_supplement(
            EvidenceIrritatorResult(1, "bundle", status="complete", ranked_signals=[signal]),
            config,
        ),
        "discovery": lambda: discovery.send_source_approval_message(
            PendingSource("Name", "https://example.com/rss", "Tech", "2026-10-09"),
            TOKEN,
            "123",
        ),
        "poll": lambda: feedback.poll_updates(TOKEN, offset=None),
        "ack": lambda: feedback.send_replies(TOKEN, store, "123", lambda _: "status"),
    }
    if operation == "prepared":
        async with client() as session:
            outcome, message_id = await prepared.send_prepared_chunk(
                session, TOKEN, {"text": "exact"}, "123", timeout_seconds=3
            )
        assert (outcome, message_id) == (("failed", None) if reject else ("confirmed", 7))
    else:
        try:
            result = await calls[operation]()
        except httpx.HTTPStatusError as exc:
            assert reject and operation in {"status", "counter", "post_delivery", "poll"}
            assert TOKEN not in "".join(traceback.format_exception(exc))
        else:
            if operation == "compact":
                assert result.outcome == ("failed" if reject else "sent")
            elif operation == "cards":
                assert result.failed == int(reject) and result.sent == int(not reject)
            elif operation == "discovery":
                assert result.status == ("rejected" if reject else "confirmed")
            elif operation == "ack":
                assert result["ack_failed"] == (2 if reject else 0)
    assert sent and all(TOKEN in str(r.url) for r in sent)
    assert len(sent) == (
        3
        if reject and operation in {"status", "cards", "counter"}
        else 2
        if operation == "ack" or (operation == "poll" and not reject)
        else 1
    )
    assert "HTTP Request:" in caplog.text
    assert_safe_logs(caplog)


async def test_invalid_retry_header_does_not_escape_in_exception_text() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                429,
                headers={"Retry-After": TOKEN},
                json={"ok": False},
            )
        )
    ) as client:
        with pytest.raises(ValueError, match="Invalid Telegram Retry-After") as caught:
            await delivery._send_chunk(client, API, "123", "exact")
    assert TOKEN not in "".join(traceback.format_exception(caught.value))


def test_unrelated_http_logging_keeps_original_record_fields() -> None:
    record = logging.LogRecord("httpx", logging.INFO, __file__, 1, "Other request %s", ("safe",), None)
    assert diagnostics._safe_record(record)
    assert record.msg == "Other request %s" and record.args == ("safe",)


def test_unrelated_http_exception_record_is_unchanged() -> None:
    try:
        raise RuntimeError("unrelated provider failure")
    except RuntimeError as error:
        record = logging.LogRecord(
            "httpx", logging.ERROR, __file__, 1, "Other %s", ("failure",), (type(error), error, error.__traceback__)
        )
    original = vars(record).copy()
    assert diagnostics._safe_record(record)
    assert vars(record) == original


def test_outside_context_telegram_exception_is_redacted() -> None:
    error = ValueError(f"failure {API} echoed {TOKEN}")
    record = logging.LogRecord("httpx", logging.ERROR, __file__, 1, "Request failed", (), (type(error), error, None))
    assert diagnostics._safe_record(record)
    assert record.exc_info is None
    assert TOKEN not in (record.exc_text or "")
    assert "ValueError" in (record.exc_text or "")


@pytest.mark.parametrize(
    "error,expected",
    [
        (ExceptionGroup("transport " + TOKEN, [httpx.ReadTimeout("nested " + TOKEN)]), RuntimeError),
        (UnicodeDecodeError("utf-8", b"x", 0, 1, TOKEN), ValueError),
    ],
)
async def test_structured_transport_errors_keep_catch_category_without_raw_fields(
    error: Exception,
    expected: type[Exception],
) -> None:
    def transport(request: httpx.Request) -> httpx.Response:
        raise error

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        with pytest.raises(expected) as caught:
            await diagnostics.request(client, "POST", API)
    assert TOKEN not in str(caught.value)
    assert TOKEN not in "".join(traceback.format_exception(caught.value))
    assert type(error).__name__ in str(caught.value)


def test_outside_context_redaction_shares_url_tokens_across_record_fields() -> None:
    error = ValueError("echoed " + TOKEN)
    record = logging.LogRecord("httpx", logging.ERROR, __file__, 1, "Request %s", (API,), (type(error), error, None))
    assert diagnostics._safe_record(record)
    assert TOKEN not in str(record.msg) + (record.exc_text or "")
    assert record.args == () and record.exc_info is None
