"""Synthetic, offline complete-text acquisition and technical coverage gates."""

from __future__ import annotations

import asyncio
import hashlib
import socket
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from digest.article_source import MAX_HTML_BYTES, extract_html, fetch_article

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
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", 0)),
        ],
    )
    # Inject a synthetic trusted bootstrap; public fixtures contain no publisher body or vendored runtime.
    monkeypatch.setattr(
        "digest.article_source._REACT_STREAM_BOOTSTRAP_HASHES",
        frozenset({hashlib.sha256(_TEST_STREAM_BOOTSTRAP.encode()).hexdigest()}),
    )


@contextmanager
def _http(handler: Any) -> Any:
    real_client = httpx.AsyncClient

    async def dispatch(request: httpx.Request) -> httpx.Response:
        result = handler(request)
        if asyncio.iscoroutine(result):
            result = await result
        if result.is_stream_consumed:
            result = httpx.Response(result.status_code, headers=result.headers,
                                    stream=httpx.ByteStream(result.content))
        return result

    with patch(
        "digest.adapters.http.public_fetch.httpx.AsyncClient",
        side_effect=lambda **kwargs: real_client(
            transport=httpx.MockTransport(dispatch),
            **kwargs,
        ),
    ) as factory:
        yield factory


def test_long_text_is_retained_in_full_including_late_qualification() -> None:
    poison = "DO_NOT_INCLUDE " * 100
    html = _html(
        content="<p>OPENING CLAIM</p>"
        + "".join(f"<p>{index}. {PARAGRAPH}</p>" for index in range(150))
        + "<aside><p>The limitation is USD only.</p></aside>"
        "<footer><p>FINAL FOOTNOTE retracts the opening claim for failed deployments.</p></footer>",
        outside=f"<nav>{poison}</nav><script>{poison}</script><footer>{poison}</footer><form>{poison}</form>",
    )
    result = extract_html(html)
    assert "DO_NOT_INCLUDE" not in result.text
    assert len(result.text) > 40000
    assert result.text.startswith("OPENING CLAIM")
    assert "The limitation is USD only." in result.text
    assert result.text.endswith("FINAL FOOTNOTE retracts the opening claim for failed deployments.")
    assert "149. " + PARAGRAPH in result.text
    assert result.extraction_status == "article"
    assert any("completeness is not guaranteed" in note for note in result.coverage_notes)


def test_tables_and_external_footnotes_are_preserved_with_boundaries() -> None:
    table = (
        "<table><caption>Measured results</caption><tr><th>Currency</th><th>Rate</th></tr>"
        "<tr><td>USD</td><td>1.5</td></tr></table>"
    )
    reference = '<sup><a href="#fn1" role="doc-noteref">1</a></sup>'
    footnote = '<footer class="footnotes"><p id="fn1">The measured rates exclude failed requests.</p></footer>'
    result = extract_html(_html(content=f"<p>{PARAGRAPH}</p>{table}{reference}", outside=footnote))
    assert "Currency | Rate" in result.text
    assert "USD | 1.5" in result.text
    assert "USD1.5" not in result.text
    assert result.text.endswith("The measured rates exclude failed requests.")


@pytest.mark.parametrize("kind", ["main", "body"])
def test_semantic_main_or_conservative_body_fallback(kind: str) -> None:
    html = _html(kind="main") if kind == "main" else _html().replace("<article>", "").replace("</article>", "")
    result = extract_html(html)
    assert result.extraction_status == kind
    assert PARAGRAPH in result.text


@pytest.mark.parametrize(
    "head,extra,outside,reason",
    [
        ('<link rel="next" href="?page=2">', "", "", "pagination"),
        ("", '<nav class="pagination"><a href="?page=2">Next page</a></nav>', "", "pagination"),
        ("", '<a rel="next" href="?page=2">Next</a>', "", "pagination"),
        ("", '<div class="load-more" data-continuation="/part2">More</div>', "", "client_continuation"),
        ("", "<canvas>Chart unavailable</canvas>", "", "unread_critical_media"),
        ("", "<svg><text>Uninspected graph</text></svg>", "", "unread_critical_media"),
        ("", '<video src="evidence.mp4"></video>', "", "unread_critical_media"),
        ("", '<sup><a href="#missing" role="doc-noteref">1</a></sup>', "", "missing_footnote_target"),
        ("", '<div class="paywall">Subscribe</div>', "", "paywall"),
        ('<script type="application/ld+json">{"isAccessibleForFree": false}</script>', "", "", "paywall"),
    ],
)
def test_unread_continuation_or_critical_content_is_held(
    head: str,
    extra: str,
    outside: str,
    reason: str,
) -> None:
    with pytest.raises(ValueError, match=reason):
        extract_html(_html(head=head, content=f"<p>{PARAGRAPH}</p>{extra}", outside=outside))


@pytest.mark.parametrize(
    "html",
    [
        _html().removesuffix("</body></html>"),
        _html().replace("</article>", ""),
        "<html><body><article></article></body></html>",
        _html(content=f"<p>{PARAGRAPH} Subscribe to continue reading.</p>"),
        _html(outside=f"<article><p>{PARAGRAPH}</p></article>"),
    ],
)
def test_http_success_does_not_establish_complete_acquisition(html: str) -> None:
    with pytest.raises(ValueError):
        extract_html(html)


@pytest.mark.asyncio
async def test_fetch_provenance_and_security_client_options() -> None:
    now = datetime(2026, 10, 1, 3, 30, tzinfo=UTC)
    html = _html(head='<meta property="article:published_time" content="2015-01-01T12:00:00+02:00">')
    with _http(lambda _: _response(html)) as factory, patch("digest.article_source._now", return_value=now):
        result = await fetch_article("https://public.example/article")
    assert result.text == extract_html(html).text
    assert result.source_published == "2015-01-01T10:00:00+00:00"
    assert result.fetched_at == now.isoformat()
    assert result.final_url == "https://public.example/article"
    assert factory.call_args.kwargs["trust_env"] is False
    assert factory.call_args.kwargs["follow_redirects"] is False
    assert factory.call_args.kwargs["limits"].max_connections == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "https://user:password@public.example/a",
        "https://public.example:8443/a",
        "http://public.example:443/a",
        "https://127.0.0.1/a",
        "https://[::1]/a",
        "https://[invalid/a",
        "https://public.example/a\nb",
        "https://public.example:99999/a",
    ],
)
async def test_unsafe_url_is_rejected_before_http(url: str) -> None:
    with _http(lambda _: pytest.fail("Unsafe URL was fetched")):
        with pytest.raises(ValueError, match="unsafe_url"):
            await fetch_article(url)


@pytest.mark.asyncio
async def test_every_redirect_is_revalidated_and_addressed_directly() -> None:
    calls = []
    dns_calls = []

    def dns(host: str, *_args: Any, **_kwargs: Any) -> list[Any]:
        dns_calls.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", 0))]

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        assert request.url.host == "93.184.216.34"
        assert request.headers["host"] in {"public.example", "second.example"}
        assert request.extensions["sni_hostname"] == request.headers["host"]
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
async def test_public_fetches_can_overlap_without_mutating_dns() -> None:
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
    assert len(results) == 8 and maximum > 1


@pytest.mark.asyncio
async def test_deadline_holds_incomplete_fetch_without_rss_or_partial_text() -> None:
    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(1)
        return _response()

    with _http(slow), patch("digest.article_source.FETCH_SECONDS", 0.005):
        with pytest.raises(TimeoutError):
            await fetch_article("https://public.example/slow")


def test_short_complete_semantic_article_is_not_rejected_by_editorial_length() -> None:
    text = "The project released its reproducible test data under an open license today."
    assert extract_html(_html(content=f"<p>{text}</p>")).text == text


def test_noncritical_hidden_visuals_do_not_create_false_coverage_holds() -> None:
    result = extract_html(_html(content=f'<p>{PARAGRAPH}</p><svg aria-hidden="true"><path/></svg>'))
    assert result.text == PARAGRAPH


_TEST_STREAM_BOOTSTRAP = "$RC=function(a,b){/* synthetic trusted fixture helper */};"


def _streamed(content: str, *, call: str = '$RC("B:0","S:0")') -> str:
    return (
        '<html><body><main><!--$?--><template id="B:0"></template><p>Loading</p><!--/$--></main>'
        f'<div hidden id="S:0">{content}</div><script>{_TEST_STREAM_BOOTSTRAP}{call}</script></body></html>'
    )


def test_completed_stream_restores_source_without_unhiding_unrelated_content() -> None:
    text = f"<article><p>{PARAGRAPH}</p><footer><p>FINAL SOURCE CONDITION.</p></footer></article>"
    html = _streamed(text).replace("</body>", "<div hidden>PRIVATE HIDDEN CONTENT</div></body>")
    result = extract_html(html)
    assert result.extraction_status == "article"
    assert result.text == PARAGRAPH + "\n\nFINAL SOURCE CONDITION."
    assert "Loading" not in result.text and "PRIVATE HIDDEN CONTENT" not in result.text


@pytest.mark.parametrize(
    "damage",
    [
        "missing_call",
        "wrong_call",
        "duplicate_source",
        "clipped_source",
        "missing_boundary",
        "duplicate_call",
        "quoted_call",
    ],
)
def test_unresolved_or_ambiguous_streams_remain_incomplete(damage: str) -> None:
    html = _streamed(f"<article><p>{PARAGRAPH}</p></article>")
    if damage == "missing_call":
        html = html.replace('$RC("B:0","S:0")', "")
    elif damage == "wrong_call":
        html = html.replace('$RC("B:0","S:0")', '$RC("B:0","S:1")')
    elif damage == "duplicate_source":
        html = html.replace("</body>", '<div hidden id="S:0">Duplicate</div></body>')
    elif damage == "clipped_source":
        html = html.replace("</article></div>", "</article>")
    elif damage == "missing_boundary":
        html = html.replace("<!--/$-->", "")
    elif damage == "duplicate_call":
        html = html.replace("</body>", '<script>$RC("B:0","S:0")</script></body>')
    else:
        html = html.replace('$RC("B:0","S:0")', 'console.log(\'$RC("B:0","S:0")\')')
    with pytest.raises(ValueError, match="unresolved_streamed_content"):
        extract_html(html)


def test_dominant_main_prose_is_not_replaced_by_recommendation_article_cards() -> None:
    paragraphs = "".join(f"<p>PRIMARY SECTION {index}. {PARAGRAPH}</p>" for index in range(12))
    cards = "".join(
        f'<article><p><a href="/other/{index}">{"Linked teaser " * 5}</a>{"Unrelated preview " * 9}</p></article>'
        for index in range(3)
    )
    result = extract_html(_streamed(paragraphs + cards))
    assert result.extraction_status == "main"
    assert all(f"PRIMARY SECTION {index}." in result.text for index in range(12))
    assert "Linked teaser" not in result.text and "Unrelated preview" not in result.text


def test_article_listing_without_dominant_main_prose_remains_ambiguous() -> None:
    cards = "".join(f"<article><p>{index}. {PARAGRAPH}</p></article>" for index in range(3))
    with pytest.raises(ValueError, match="multiple_article_regions"):
        extract_html(_streamed(cards))


@pytest.mark.parametrize(
    "script_attrs",
    [
        'type="application/json"',
        'src="/external.js"',
        'type="module"',
        "nomodule",
        'language="vbscript"',
        'type="application/json" type="text/javascript"',
    ],
)
def test_inactive_or_external_script_cannot_complete_stream(script_attrs: str) -> None:
    html = _streamed(f"<article><p>{PARAGRAPH}</p></article>").replace("<script>", f"<script {script_attrs}>")
    with pytest.raises(ValueError, match="unresolved_streamed_content"):
        extract_html(html)


@pytest.mark.parametrize(
    "script",
    [
        '$RC("B:0","S:0")',
        '$RC=function(a,b){}; //;$RC("B:0","S:0")',
        _TEST_STREAM_BOOTSTRAP + '//;$RC("B:0","S:0")',
        _TEST_STREAM_BOOTSTRAP + '/*;$RC("B:0","S:0")',
    ],
)
def test_undefined_unknown_or_commented_replacement_is_not_executed(script: str) -> None:
    html = _streamed(f"<article><p>{PARAGRAPH}</p></article>")
    html = html.replace(_TEST_STREAM_BOOTSTRAP + '$RC("B:0","S:0")', script)
    with pytest.raises(ValueError, match="unresolved_streamed_content"):
        extract_html(html)


@pytest.mark.parametrize("wrapper", ["template", "noscript"])
def test_inert_stream_container_is_not_a_live_dom_target(wrapper: str) -> None:
    html = _streamed(f"<article><p>{PARAGRAPH}</p></article>")
    html = html.replace('<div hidden id="S:0">', f'<{wrapper}><div hidden id="S:0">')
    html = html.replace("</article></div>", f"</article></div></{wrapper}>")
    with pytest.raises(ValueError, match="unresolved_streamed_content"):
        extract_html(html)


def test_standalone_replacement_requires_prior_exact_active_bootstrap() -> None:
    html = _streamed(f"<article><p>{PARAGRAPH}</p></article>")
    html = html.replace(_TEST_STREAM_BOOTSTRAP, _TEST_STREAM_BOOTSTRAP + "</script><script>")
    assert extract_html(html).text == PARAGRAPH
    html = html.replace("</script><script>", "</script><script>unknownRuntime()</script><script>")
    with pytest.raises(ValueError, match="unresolved_streamed_content"):
        extract_html(html)


def test_replacement_before_its_dom_targets_exist_remains_incomplete() -> None:
    html = _streamed(f"<article><p>{PARAGRAPH}</p></article>")
    script = f'<script>{_TEST_STREAM_BOOTSTRAP}$RC("B:0","S:0")</script>'
    html = html.replace(script, "").replace("<body>", "<body>" + script)
    with pytest.raises(ValueError, match="unresolved_streamed_content"):
        extract_html(html)


def test_removed_recommendation_cannot_supply_a_missing_source_footnote() -> None:
    paragraphs = "".join(f"<p>PRIMARY SECTION {index}. {PARAGRAPH}</p>" for index in range(12))
    paragraphs += '<sup><a role="doc-noteref" href="#fn1">1</a></sup>'
    cards = "".join(
        f'<article><p><a href="/other/{index}">{"Linked teaser " * 5}</a>'
        f"{'Unrelated preview ' * 9}</p>"
        + ('<p id="fn1">Required source limitation omitted with this card.</p>' if index == 0 else "")
        + "</article>"
        for index in range(3)
    )
    with pytest.raises(ValueError, match="removed_footnote_target"):
        extract_html(_streamed(paragraphs + cards))


@pytest.mark.parametrize(
    "visual",
    [
        '<svg style="display: none;"><path/></svg>',
        '<svg style="visibility: hidden;"><path/></svg>',
        '<a role="button"><svg width="16" height="16" role="presentation" class="figure"><path/></svg></a>',
        '<div id="author-link"><a href="/author"><svg width="16" height="16"><path/></svg></a></div>',
        '<a aria-label="Share this article"><svg width="24" height="24"><path/></svg></a>',
        '<a class="group/button"><span><svg width="1em" height="1em"><path/></svg></span></a>',
    ],
)
def test_hidden_or_identified_decorative_svg_does_not_block_text(visual: str) -> None:
    assert extract_html(_html(content=f"<p>{PARAGRAPH}</p>{visual}")).text == PARAGRAPH


def test_static_image_text_alternative_is_preserved_without_claiming_pixel_review() -> None:
    description = "The client connects directly to the service over an encrypted connection."
    result = extract_html(
        _html(
            content=f'<p>{PARAGRAPH}</p><figure><img alt="{description}">'
            "<figcaption>Architecture illustration.</figcaption></figure>"
        )
    )
    assert f"Image description: {description}" in result.text
    assert "Architecture illustration." in result.text
    assert "Uninspected images are present; this acquisition covers textual content only." in result.coverage_notes


def test_descriptive_alt_does_not_automatically_resolve_unread_diagram_coverage() -> None:
    html = _html(content=f'<p>{PARAGRAPH}</p><img alt="A benchmark chart shows separate experimental results.">')
    with pytest.raises(ValueError, match="unread_critical_media"):
        extract_html(html)


def test_separate_generated_summary_widget_is_not_authored_evidence() -> None:
    html = _html(
        content=f'<details class="publisher-ai-summary"><p>Unverified generated claim.</p></details><p>{PARAGRAPH}</p>'
    )
    assert extract_html(html).text == PARAGRAPH


@pytest.mark.parametrize(
    "label,contents",
    [
        ('aria-label="Benchmark chart"', "<path/>"),
        ('class="diagram"', "<path/>"),
        ("", "<title>Benchmark graph</title><path/>"),
    ],
)
def test_small_control_context_cannot_hide_meaningful_svg(label: str, contents: str) -> None:
    html = _html(
        content=f'<p>{PARAGRAPH}</p><a aria-label="Share this article">'
        f'<svg width="16" height="16" {label}>{contents}</svg></a>'
    )
    with pytest.raises(ValueError, match="unread_critical_media"):
        extract_html(html)


@pytest.mark.parametrize(
    "width,height,contents",
    [
        ("800", "600", "<path/>"),
        ("16", "16", "<text>Failed requests: 97%</text>"),
        ("16", "16", "<text><tspan>Failed requests: 97%</tspan></text>"),
        ("16", "16", "<desc>Failed requests: 97%</desc>"),
    ],
)
def test_author_or_presentation_marker_does_not_hide_source_evidence(width: str, height: str, contents: str) -> None:
    html = _html(
        content=f'<p>{PARAGRAPH}</p><div id="author-link"><a href="/author">'
        f'<svg width="{width}" height="{height}" role="presentation">{contents}</svg></a></div>'
    )
    with pytest.raises(ValueError, match="unread_critical_media"):
        extract_html(html)


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
@pytest.mark.parametrize("target", ["https://second.example:8443/final", "https://second.example/" + "a" * 2048])
async def test_redirect_keeps_article_specific_url_policy(target: str) -> None:
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(302, headers={"location": target})

    with _http(handler):
        with pytest.raises(ValueError, match="unsafe_url"):
            await fetch_article("https://public.example/start")
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_article_fragment_is_removed_from_request_and_provenance() -> None:
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _response()

    with _http(handler):
        result = await fetch_article("https://public.example/article#section")
    assert result.final_url == "https://public.example/article"
    assert requests[0].url.fragment == ""


@pytest.mark.asyncio
async def test_synchronous_extraction_elapsed_time_is_checked_before_return() -> None:
    import time

    from digest.adapters.http.public_fetch import PublicResponse

    def slow_extract(html: str) -> Any:
        result = extract_html(html)
        time.sleep(0.03)
        return result

    response = PublicResponse(
        "https://public.example/article", 200, httpx.Headers({"content-type": "text/html"}),
        _html().encode(), "utf-8",
    )
    with (
        patch("digest.article_source.fetch_public", AsyncMock(return_value=response)),
        patch("digest.article_source.FETCH_SECONDS", 0.02),
        patch("digest.article_source.extract_html", side_effect=slow_extract),
    ):
        with pytest.raises(TimeoutError, match="extraction timed out"):
            await fetch_article("https://public.example/article")


@pytest.mark.asyncio
async def test_oversized_decoded_html_retains_article_failure_reason() -> None:
    import gzip

    compressed = gzip.compress(b"x" * (MAX_HTML_BYTES + 1))
    with _http(lambda _: httpx.Response(
        200, headers={"content-type": "text/html", "content-encoding": "gzip"},
        stream=httpx.ByteStream(compressed),
    )):
        with pytest.raises(ValueError, match="oversized_html"):
            await fetch_article("https://public.example/article")
