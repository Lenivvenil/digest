"""Synthetic, offline complete-text acquisition and technical coverage gates."""

from __future__ import annotations

import asyncio
import socket
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from digest.editorial_fetch import MAX_HTML_BYTES, extract_html, fetch_article

PARAGRAPH = (
    "This synthetic source reports a controlled experiment and explains its assumptions. "
    "The authors compare deployment methods, record observed failures and describe the evaluation methodology. "
    "The results are conditional on the measured workload and do not imply universal gains."
)


def _html(*, kind: str = "article", content: str = "", head: str = "", outside: str = "") -> str:
    text = content or "".join(f"<p>Section {index}. {PARAGRAPH}</p>" for index in range(4))
    return f"<html><head>{head}</head><body><{kind}>{text}</{kind}>{outside}</body></html>"


def _response(html: str | None = None) -> httpx.Response:
    return httpx.Response(200, text=html or _html(), headers={"content-type": "text/html; charset=utf-8"})


@pytest.fixture(autouse=True)
def _offline_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_args, **_kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", 0)),
    ])


@contextmanager
def _http(handler: Any) -> Any:
    real_client = httpx.AsyncClient
    with patch("digest.editorial_fetch.httpx.AsyncClient", side_effect=lambda **kwargs: real_client(
        transport=httpx.MockTransport(handler), **kwargs,
    )) as factory:
        yield factory


def test_long_text_is_retained_in_full_including_late_qualification() -> None:
    html = _html(content="<p>OPENING CLAIM</p>" + "".join(f"<p>{index}. {PARAGRAPH}</p>" for index in range(150))
                 + "<aside><p>The limitation is USD only.</p></aside>"
                 "<footer><p>FINAL FOOTNOTE retracts the opening claim for failed deployments.</p></footer>")
    result = extract_html(html)
    assert len(result.text) > 40000
    assert result.text.startswith("OPENING CLAIM")
    assert "The limitation is USD only." in result.text
    assert result.text.endswith("FINAL FOOTNOTE retracts the opening claim for failed deployments.")
    assert "149. " + PARAGRAPH in result.text
    assert result.extraction_status == "article"
    assert any("completeness is not guaranteed" in note for note in result.coverage_notes)


def test_tables_and_external_footnotes_are_preserved_with_boundaries() -> None:
    table = "<table><caption>Measured results</caption><tr><th>Currency</th><th>Rate</th></tr>" \
            "<tr><td>USD</td><td>1.5</td></tr></table>"
    reference = '<sup><a href="#fn1" role="doc-noteref">1</a></sup>'
    footnote = '<footer class="footnotes"><p id="fn1">The measured rates exclude failed requests.</p></footer>'
    result = extract_html(_html(content=f"<p>{PARAGRAPH}</p>{table}{reference}", outside=footnote))
    assert "Currency | Rate" in result.text
    assert "USD | 1.5" in result.text
    assert "USD1.5" not in result.text
    assert result.text.endswith("The measured rates exclude failed requests.")


def test_article_is_preferred_and_boilerplate_is_excluded() -> None:
    poison = "DO_NOT_INCLUDE " * 100
    html = _html(outside=f"<nav>{poison}</nav><script>{poison}</script><footer>{poison}</footer><form>{poison}</form>")
    result = extract_html(html)
    assert "DO_NOT_INCLUDE" not in result.text
    assert result.extraction_status == "article"


@pytest.mark.parametrize("kind", ["main", "body"])
def test_semantic_main_or_conservative_body_fallback(kind: str) -> None:
    html = _html(kind="main") if kind == "main" else _html().replace("<article>", "").replace("</article>", "")
    result = extract_html(html)
    assert result.extraction_status == kind
    assert PARAGRAPH in result.text


@pytest.mark.parametrize("head,extra,outside,reason", [
    ('<link rel="next" href="?page=2">', "", "", "pagination"),
    ("", '<nav class="pagination"><a href="?page=2">Next page</a></nav>', "", "pagination"),
    ("", '<a rel="next" href="?page=2">Next</a>', "", "pagination"),
    ("", '<div class="load-more" data-continuation="/part2">More</div>', "", "client_continuation"),
    ("", '<canvas>Chart unavailable</canvas>', "", "unread_critical_media"),
    ("", '<svg><text>Uninspected graph</text></svg>', "", "unread_critical_media"),
    ("", '<img src="chart.png" alt="Benchmark results chart">', "", "unread_critical_media"),
    ("", '<video src="evidence.mp4"></video>', "", "unread_critical_media"),
    ("", '<sup><a href="#missing" role="doc-noteref">1</a></sup>', "", "missing_footnote_target"),
    ("", '<div class="paywall">Subscribe</div>', "", "paywall"),
    ('<script type="application/ld+json">{"isAccessibleForFree": false}</script>', "", "", "paywall"),
])
def test_unread_continuation_or_critical_content_is_held(
    head: str, extra: str, outside: str, reason: str,
) -> None:
    with pytest.raises(ValueError, match=reason):
        extract_html(_html(head=head, content=f"<p>{PARAGRAPH}</p>{extra}", outside=outside))


def test_uninspected_noncritical_image_is_explicitly_labelled() -> None:
    result = extract_html(_html(content=f'<p>{PARAGRAPH}</p><img src="author.jpg" alt="Author portrait">'))
    assert any("Uninspected images" in note for note in result.coverage_notes)


@pytest.mark.parametrize("html", [
    _html().removesuffix("</body></html>"), _html().replace("</article>", ""),
    "<html><body><article></article></body></html>",
    _html(content=f"<p>{PARAGRAPH} Subscribe to continue reading.</p>"),
    _html(outside=f"<article><p>{PARAGRAPH}</p></article>"),
])
def test_http_success_does_not_establish_complete_acquisition(html: str) -> None:
    with pytest.raises(ValueError):
        extract_html(html)


@pytest.mark.asyncio
async def test_fetch_provenance_and_security_client_options() -> None:
    now = datetime(2026, 10, 1, 3, 30, tzinfo=UTC)
    html = _html(head='<meta property="article:published_time" content="2026-09-30T12:00:00+02:00">')
    with _http(lambda _: _response(html)) as factory, patch("digest.editorial_fetch._now", return_value=now):
        result = await fetch_article("https://public.example/article")
    assert result.text == extract_html(html).text
    assert result.source_published == "2026-09-30T10:00:00+00:00"
    assert result.fetched_at == now.isoformat()
    assert result.final_url == "https://public.example/article"
    assert factory.call_args.kwargs["trust_env"] is False
    assert factory.call_args.kwargs["follow_redirects"] is False
    assert factory.call_args.kwargs["limits"].max_connections == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "https://user:password@public.example/a",
    "https://public.example:8443/a", "http://public.example:443/a", "https://127.0.0.1/a",
    "https://[::1]/a",
    "https://[invalid/a", "https://public.example/a\nb", "https://public.example:99999/a",
])
async def test_unsafe_url_is_rejected_before_http(url: str) -> None:
    with _http(lambda _: pytest.fail("Unsafe URL was fetched")):
        with pytest.raises(ValueError, match="unsafe_url"):
            await fetch_article(url)


@pytest.mark.asyncio
async def test_every_redirect_is_revalidated_and_pinned_dns_is_used() -> None:
    calls = []
    dns_calls = []

    def dns(host: str, *_args: Any, **_kwargs: Any) -> list[Any]:
        dns_calls.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", 0))]

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        assert socket.getaddrinfo(request.url.host, 443)[0][4] == ("93.184.216.34", 443)
        if len(calls) == 1:
            return httpx.Response(302, headers={"location": "https://second.example/final"})
        return _response()

    with _http(handler), patch("socket.getaddrinfo", side_effect=dns):
        result = await fetch_article("https://public.example/start")
    assert result.final_url == "https://second.example/final"
    assert dns_calls == ["public.example", "second.example"]
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_private_redirect_is_not_requested() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(302, headers={"location": "http://127.0.0.1/secret"})

    with _http(handler):
        with pytest.raises(ValueError, match="unsafe_url"):
            await fetch_article("https://public.example/start")
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_redirect_limit_prevents_an_unbounded_chain() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(302, headers={"location": f"/hop{len(calls)}"})

    with _http(handler):
        with pytest.raises(ValueError, match="redirect_limit"):
            await fetch_article("https://public.example/start")
    assert len(calls) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["declared_large", "actual_large", "partial", "range", "wrong_type", "clipped"])
async def test_technical_response_limits_never_return_a_partial_body(kind: str) -> None:
    headers = {"content-type": "text/html"}
    body, status = _html().encode(), 200
    if kind == "declared_large":
        headers["content-length"] = str(MAX_HTML_BYTES + 1)
    elif kind == "actual_large":
        body = b"x" * (MAX_HTML_BYTES + 1)
    elif kind == "partial":
        status = 206
    elif kind == "range":
        headers["content-range"] = "bytes 0-20/1000"
    elif kind == "wrong_type":
        headers["content-type"] = "application/pdf"
    else:
        headers["content-length"] = str(len(body) + 15)
    with _http(lambda _: httpx.Response(status, content=body, headers=headers)):
        with pytest.raises(ValueError):
            await fetch_article("https://public.example/article")


@pytest.mark.asyncio
async def test_public_fetches_are_sequential_even_when_called_concurrently() -> None:
    active = maximum = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal active, maximum
        active += 1
        maximum = max(active, maximum)
        await asyncio.sleep(0.005)
        active -= 1
        return _response()

    with _http(handler):
        results = await asyncio.gather(*(fetch_article(f"https://public.example/{index}") for index in range(8)))
    assert len(results) == 8 and maximum == 1


@pytest.mark.asyncio
async def test_deadline_holds_incomplete_fetch_without_rss_or_partial_text() -> None:
    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(1)
        return _response()

    with _http(slow), patch("digest.editorial_fetch.FETCH_SECONDS", 0.005):
        with pytest.raises(TimeoutError):
            await fetch_article("https://public.example/slow")


def test_short_complete_semantic_article_is_not_rejected_by_editorial_length() -> None:
    text = "The project released its reproducible test data under an open license today."
    assert extract_html(_html(content=f"<p>{text}</p>")).text == text


def test_noncritical_hidden_visuals_do_not_create_false_coverage_holds() -> None:
    result = extract_html(_html(content=f'<p>{PARAGRAPH}</p><svg aria-hidden="true"><path/></svg>'))
    assert result.text == PARAGRAPH


@pytest.mark.asyncio
async def test_mixed_public_private_dns_answers_are_rejected_without_request() -> None:
    addresses = [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (ip, 0)) for ip in ("93.184.216.34", "127.0.0.1")]
    with (
        _http(lambda _: pytest.fail("Unsafe DNS result was fetched")),
        patch("socket.getaddrinfo", return_value=addresses),
    ):
        with pytest.raises(ValueError, match="unsafe_url"):
            await fetch_article("https://public.example/article")


@pytest.mark.asyncio
async def test_original_publication_age_is_preserved_without_acquisition_age_out() -> None:
    html = _html(head='<meta property="article:published_time" content="2015-01-01T00:00:00Z">')
    with _http(lambda _: _response(html)):
        result = await fetch_article("https://public.example/already-admitted")
    assert result.source_published == "2015-01-01T00:00:00+00:00"
    assert result.text == extract_html(html).text
