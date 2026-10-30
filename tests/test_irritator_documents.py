"""Synthetic offline proof for physical admission and complete document units."""

from __future__ import annotations

import asyncio
import hashlib
import json
import socket
from collections.abc import Callable
from dataclasses import FrozenInstanceError
from typing import Any
from unittest.mock import Mock

import httpx
import pytest

from digest.adapters.http import public_fetch
from digest.adapters.http.public_fetch import fetch_public
from digest.irritator import documents
from digest.irritator.documents import SourceRequestBudget, SourceRequestBudgetExceeded, acquire_document

PUBLIC = "93.184.216.34"
SECOND = "8.8.8.8"
PROSE = (
    "The synthetic document describes conditional behavior and preserves all stated qualifications. "
    "A reported failure changes the interpretation of the opening claim for the specified workload."
)
RFC_URL = "https://www.rfc-editor.org/rfc/rfc9000.html"


def _html(content: str, *, before: str = "", after: str = "") -> str:
    return f"<html><body>{before}<article>{content}</article>{after}</body></html>"


def _response(html: str = "", *, status: int = 200, **headers: str) -> httpx.Response:
    return httpx.Response(status, headers={"content-type": "text/html; charset=utf-8", **headers},
                          stream=httpx.ByteStream(html.encode()))


@pytest.fixture(autouse=True)
def dns(monkeypatch: pytest.MonkeyPatch) -> Mock:
    resolver = Mock(return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC, 0))])
    monkeypatch.setattr(socket, "getaddrinfo", resolver)
    return resolver


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
            assert kwargs["headers"].get("authorization") is None
            assert kwargs["headers"].get("cookie") is None
            result = real_client(transport=httpx.MockTransport(dispatch), **kwargs)
            clients.append(result)
            return result

        monkeypatch.setattr(public_fetch.httpx, "AsyncClient", client)
        return requests, clients

    return install


def test_shared_budget_counts_starts_and_refuses_tenth() -> None:
    budget = SourceRequestBudget()
    for _ in range(9):
        budget.consume()
    with pytest.raises(SourceRequestBudgetExceeded, match="source_request_budget_exhausted"):
        budget.consume()
    assert budget.requests_started == 9


@pytest.mark.parametrize("maximum", [-1, 10, True, 1.5])
def test_budget_cannot_expand_hard_maximum(maximum: Any) -> None:
    with pytest.raises(ValueError):
        SourceRequestBudget(maximum)


async def test_admission_runs_before_each_address_and_redirect(http: Any, dns: Mock) -> None:
    dns.return_value.append((socket.AF_INET, socket.SOCK_STREAM, 6, "", (SECOND, 0)))
    budget = SourceRequestBudget(3)

    def dispatch(request: httpx.Request) -> httpx.Response:
        assert budget.requests_started == len(requests)
        if request.url.host == PUBLIC:
            raise httpx.ConnectError("synthetic unreachable address", request=request)
        return _response(status=302, location="/next")

    requests, clients = http(dispatch)
    snapshot = await acquire_document("https://example.com/start", budget=budget)
    assert snapshot.status == "held" and snapshot.error == "source_request_budget_exhausted"
    assert snapshot.text == "" and snapshot.content_sha256 is None
    assert [request.url.host for request in requests] == [PUBLIC, SECOND, PUBLIC]
    assert budget.requests_started == 3 and all(client.is_closed for client in clients)


async def test_exhausted_budget_and_unsafe_dns_never_start_requests(http: Any, dns: Mock) -> None:
    requests, _ = http(lambda _: pytest.fail("unadmitted request"))
    empty = SourceRequestBudget(0)
    snapshot = await acquire_document("https://example.com/doc", budget=empty)
    assert snapshot.error == "source_request_budget_exhausted" and not requests
    dns.return_value.append((socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0)))
    budget = SourceRequestBudget()
    snapshot = await acquire_document("https://example.com/doc", budget=budget)
    assert snapshot.error == "unsafe_url" and not requests and budget.requests_started == 0


async def test_optional_callback_preserves_existing_fetch_callers(http: Any) -> None:
    requests, _ = http(lambda _: _response("original"))
    result = await fetch_public("https://example.com/doc", timeout=1, max_bytes=100)
    assert result.content == b"original" and len(requests) == 1


async def test_full_text_is_never_truncated_and_has_immutable_provenance(http: Any) -> None:
    html = _html("<p>Opening claim.</p>" + f"<p>{PROSE}</p>" * 100
                 + "<p>Final qualification withdraws that claim for failed deployments.</p>")
    requests, _ = http(lambda _: _response(html))
    budget = SourceRequestBudget()
    snapshot = await acquire_document("https://example.com/doc", budget=budget)
    assert snapshot.status == "complete" and snapshot.error is None
    assert len(snapshot.text) > 8000 and snapshot.text.endswith("for failed deployments.")
    assert snapshot.content_sha256 == hashlib.sha256(html.encode()).hexdigest()
    assert snapshot.text_sha256 == hashlib.sha256(snapshot.text.encode()).hexdigest()
    assert snapshot.final_url == snapshot.requested_url == "https://example.com/doc"
    assert snapshot.fetched_at.endswith("+00:00") and len(requests) == budget.requests_started == 1
    with pytest.raises(FrozenInstanceError):
        snapshot.text = "mutated"  # type: ignore[misc]


async def test_links_are_bound_to_literal_content_and_are_discovery_only(http: Any) -> None:
    html = _html(f'<p>{PROSE}</p><a href="/report?q=a&amp;b=c">Original <em>report</em></a>'
                 '<a href="https://other.example/report#scope" title="Complete title"></a>'
                 '<a href="/report?q=a&amp;b=c">Duplicate URL</a>'
                 '<a href="#local">Local heading</a><a href="javascript:alert(1)">Unsafe</a>'
                 '<a href="http://127.0.0.1/secret">Private</a>'
                 '<a href="https://user:secret@example.com/">Credentials</a>'
                 '<a href="https://elsewhere.example:8443/report">Nonstandard port</a>'
                 '<a hidden href="https://hidden.example/report">Hidden</a>'
                 '<script><a href="https://script.example/report">Script</a></script>',
                 before='<nav><a href="https://nav.example/">Navigation</a></nav>',
                 after='<footer><a href="https://footer.example/">Footer</a></footer>')
    requests, _ = http(lambda _: _response(html))
    snapshot = await acquire_document("https://example.com/doc", budget=SourceRequestBudget())
    assert snapshot.status == "complete"
    assert [(link.url, link.title) for link in snapshot.links] == [
        ("https://example.com/report?q=a&b=c", "Original report"),
        ("https://other.example/report#scope", "Complete title"),
    ]
    assert all(link.link_id.startswith("link-") for link in snapshot.links)
    assert len(requests) == 1  # No linked destination was fetched or DNS-certified.


async def test_bounded_links_are_omitted_whole_with_explicit_count(http: Any) -> None:
    links = "".join(f'<a href="https://other.example/{i}">Report {i}</a>' for i in range(45))
    links += f'<a href="https://other.example/long">{"x" * 301}</a>'
    http(lambda _: _response(_html(f"<p>{PROSE * 20}</p>" + links)))
    snapshot = await acquire_document("https://example.com/doc", budget=SourceRequestBudget())
    assert snapshot.status == "complete" and len(snapshot.links) == 40
    assert snapshot.omitted_link_count == 6 and any("omitted 6" in note for note in snapshot.coverage_notes)


@pytest.mark.parametrize("tail,reason", [
    ('<div class="paywall">Subscribe</div>', "paywall_or_teaser"),
    ('<a rel="next" href="https://other.example/next">Next</a>', "coverage_incomplete:pagination"),
    ('<canvas>Graph unavailable</canvas>', "coverage_incomplete:unread_critical_media"),
])
async def test_incomplete_text_is_held_but_actual_links_remain_discovery(http: Any, tail: str, reason: str) -> None:
    html = _html(f'<p>{PROSE}</p><a href="https://other.example/paper">Paper</a>' + tail)
    http(lambda _: _response(html))
    snapshot = await acquire_document("https://example.com/doc", budget=SourceRequestBudget())
    assert snapshot.status == "held" and snapshot.error == reason and snapshot.text == ""
    assert snapshot.text_sha256 is None and snapshot.content_sha256 is not None
    assert snapshot.links[0].url == "https://other.example/paper"


@pytest.mark.parametrize("content,headers,reason", [
    ("plain text", {"content-type": "text/plain"}, "unsupported_content_type"),
    (_html(f"<p>{PROSE}</p>"), {"content-range": "bytes 0-100/9999"}, "partial_or_nonarticle_response"),
    (_html(f"<p>{PROSE}</p>").removesuffix("</body></html>"), {}, "clipped_html"),
    ("x" * (2 * 1024 * 1024 + 1), {}, "oversized_body"),
], ids=["unsupported-content-type", "partial-response", "clipped-html", "oversized-body"])
async def test_technical_failures_never_return_partial_evidence(
    http: Any, content: str, headers: dict[str, str], reason: str,
) -> None:
    http(lambda _: _response(content, **headers))
    snapshot = await acquire_document("https://example.com/doc", budget=SourceRequestBudget())
    assert snapshot.status == "held" and snapshot.error == reason and not snapshot.text


async def test_http_error_and_redirect_limit_are_bounded(http: Any) -> None:
    requests, _ = http(lambda _: _response(status=302, location="/next"))
    snapshot = await acquire_document("https://example.com/doc", budget=SourceRequestBudget())
    assert snapshot.error == "redirect_limit_or_missing_target" and len(requests) == 4
    http(lambda _: _response(status=503))
    snapshot = await acquire_document("https://example.com/doc", budget=SourceRequestBudget())
    assert snapshot.status == "error" and snapshot.error == "http_status_503"


async def test_total_deadline_includes_parsing_and_cancellation_propagates(
    http: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def delayed(_: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.2)
        return _response(_html(f"<p>{PROSE}</p>"))

    _, clients = http(delayed)
    monkeypatch.setattr(documents, "FETCH_SECONDS", 0.01)
    snapshot = await acquire_document("https://example.com/doc", budget=SourceRequestBudget())
    assert snapshot.error == "document_timeout" and all(client.is_closed for client in clients)
    monkeypatch.setattr(documents, "FETCH_SECONDS", 1)
    task = asyncio.create_task(acquire_document("https://example.com/doc", budget=SourceRequestBudget()))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert all(client.is_closed for client in clients)


def _rfc_html() -> str:
    return _html(
        f'<section id="section-6"><h2 id="name-messages">6. Messages</h2><p>Parent scope. {PROSE}</p>'
        f'<section id="section-6.1"><h3 id="name-conditional">6.1. Conditional messages</h3><p>{PROSE}</p>'
        '<p>Late qualification applies only to failures, as specified in <a href="#section-7">Section 7</a>.</p>'
        f'<section id="section-6.1.1"><h4>6.1.1. Nested case</h4><p>Nested caveat. {PROSE}</p></section>'
        f'</section><section id="section-6.2"><h3>6.2. Other messages</h3><p>{PROSE}</p></section></section>'
        f'<section id="section-7"><h2>7. Unrelated next section</h2><p>{PROSE}</p></section>'
    )


async def test_rfc_whole_document_exposes_only_actual_complete_natural_sections(http: Any) -> None:
    http(lambda _: _response(_rfc_html()))
    snapshot = await acquire_document(RFC_URL, budget=SourceRequestBudget())
    assert snapshot.status == "complete", snapshot.error
    assert "Unrelated next section" in snapshot.text
    section = next(section for section in snapshot.sections if section.section_id == "section-6.1")
    assert section.title == "6.1. Conditional messages"
    assert "Late qualification" in section.text and "Section 7" in section.text
    assert "Nested caveat" in section.text and "Other messages" not in section.text
    assert section.url == RFC_URL + "#section-6.1"
    assert section.text_sha256 == hashlib.sha256(section.text.encode()).hexdigest()
    assert len(snapshot.sections) == 5


@pytest.mark.parametrize("fragment", ["section-6.1", "name-conditional"])
async def test_rfc_fragment_selects_entire_section_and_keeps_cross_references(http: Any, fragment: str) -> None:
    requests, _ = http(lambda _: _response(_rfc_html()))
    snapshot = await acquire_document(RFC_URL + "#" + fragment, budget=SourceRequestBudget())
    assert snapshot.status == "complete" and snapshot.extraction_status == "rfc_section", snapshot.error
    assert snapshot.text.startswith("6.1. Conditional messages")
    assert "Nested caveat" in snapshot.text and "Unrelated next section" not in snapshot.text
    assert snapshot.final_url == RFC_URL + "#section-6.1"
    assert len(snapshot.sections) == 1 and requests[0].url.raw_path == b"/rfc/rfc9000.html"


async def test_legacy_rfc_heading_spans_use_literal_html_sections(http: Any) -> None:
    html = ('<html><body><pre><span class="h2"><a id="section-6">6.</a> Messages</span>\n\n'
            + PROSE + '\n\n<span class="h3"><a id="section-6.1">6.1.</a> Conditional messages</span>\n\n'
            + PROSE + '\n\nLate qualification remains here.\n\n'
            '<span class="h3"><a id="section-6.2">6.2.</a> Other messages</span>\n\n'
            + PROSE + '</pre></body></html>')
    http(lambda _: _response(html))
    snapshot = await acquire_document(RFC_URL + "#section-6.1", budget=SourceRequestBudget())
    assert snapshot.status == "complete", snapshot.error
    assert snapshot.text.startswith("6.1. Conditional messages")
    assert snapshot.text.endswith("Late qualification remains here.")
    assert "Other messages" not in snapshot.text


@pytest.mark.parametrize("html,fragment,reason", [
    (_rfc_html(), "section-99", "rfc_section_fragment_missing"),
    (_rfc_html().replace('id="section-6.2"', 'id="section-6.1"'), "section-6.1",
     "rfc_ambiguous_heading_identity"),
    (_rfc_html().replace("</section>", "", 1), "section-6.1", "rfc_section_incomplete_html"),
    (_rfc_html().replace('<h3 id="name-conditional">', '<h3 id="name-conditional" id="other">'),
     "section-6.1", "rfc_ambiguous_attributes"),
], ids=["missing-fragment", "duplicate-heading-id", "unclosed-section", "duplicate-attributes"])
async def test_rfc_missing_ambiguous_or_incomplete_section_is_held(
    http: Any, html: str, fragment: str, reason: str,
) -> None:
    http(lambda _: _response(html))
    snapshot = await acquire_document(RFC_URL + "#" + fragment, budget=SourceRequestBudget())
    assert snapshot.status == "held" and snapshot.error == reason and not snapshot.text and not snapshot.sections


async def test_unknown_rfc_without_fragment_keeps_whole_text(http: Any) -> None:
    http(lambda _: _response(_html(f"<p>{PROSE}</p><p>Final qualification remains present.</p>")))
    snapshot = await acquire_document("https://www.rfc-editor.org/rfc/rfc999999.html", budget=SourceRequestBudget())
    assert snapshot.status == "complete" and snapshot.sections == ()
    assert snapshot.text.endswith("Final qualification remains present.")


async def test_shared_budget_refuses_tenth_actual_document_request(http: Any) -> None:
    requests, _ = http(lambda _: _response(_html(f"<p>{PROSE}</p>")))
    budget = SourceRequestBudget()
    snapshots = [await acquire_document(f"https://example.com/doc/{index}", budget=budget) for index in range(10)]
    assert all(snapshot.status == "complete" for snapshot in snapshots[:9])
    assert snapshots[9].error == "source_request_budget_exhausted"
    assert len(requests) == budget.requests_started == 9


async def test_article_without_links_does_not_substitute_surrounding_links(http: Any) -> None:
    html = _html(f"<p>{PROSE}</p>", before='<div><a href="https://other.example/sidebar">Sidebar</a></div>')
    http(lambda _: _response(html))
    snapshot = await acquire_document("https://example.com/doc", budget=SourceRequestBudget())
    assert snapshot.status == "complete" and snapshot.links == ()


@pytest.mark.parametrize("base,expected", [
    ('<base href="https://other.example/base/">', ["https://other.example/base/paper", "https://absolute.example/a"]),
    ('<base href="http://127.0.0.1/">', ["https://absolute.example/a"]),
    ('<base href="/one/"><base href="/two/">', ["https://absolute.example/a"]),
])
async def test_discovery_resolves_only_unambiguous_safe_html_base(http: Any, base: str, expected: list[str]) -> None:
    html = _html(f'<p>{PROSE}</p><a href="paper">Paper</a><a href="https://absolute.example/a">Absolute</a>'
                 '<a href="https://first.example/a" href="https://last.example/a">Ambiguous</a>')
    html = html.replace("<body>", f"<head>{base}</head><body>")
    http(lambda _: _response(html))
    snapshot = await acquire_document("https://example.com/doc", budget=SourceRequestBudget())
    assert snapshot.status == "complete"
    assert [link.url for link in snapshot.links] == expected


@pytest.mark.parametrize("location,reason", [
    ("https://example.com/article", "rfc_redirect_not_document"),
    (RFC_URL.replace("www.", "") + "#section-7", "rfc_section_redirect_mismatch"),
])
async def test_requested_rfc_section_is_not_silently_replaced_on_redirect(
    http: Any, location: str, reason: str,
) -> None:
    def dispatch(_: httpx.Request) -> httpx.Response:
        return _response(status=302, location=location) if len(requests) == 1 else _response(_rfc_html())

    requests, _ = http(dispatch)
    snapshot = await acquire_document(RFC_URL + "#section-6.1", budget=SourceRequestBudget())
    assert snapshot.status == "held" and snapshot.error == reason and not snapshot.text


async def test_redirect_without_fragment_keeps_requested_rfc_section(http: Any) -> None:
    destination = RFC_URL.replace("www.", "")

    def dispatch(_: httpx.Request) -> httpx.Response:
        return _response(status=302, location=destination) if len(requests) == 1 else _response(_rfc_html())

    requests, _ = http(dispatch)
    snapshot = await acquire_document(RFC_URL + "#section-6.1", budget=SourceRequestBudget())
    assert snapshot.status == "complete", snapshot.error
    assert snapshot.text.startswith("6.1. Conditional messages")
    assert snapshot.final_url == destination + "#section-6.1"


async def test_admission_exception_cannot_be_retried_as_address_failure(http: Any, dns: Mock) -> None:
    dns.return_value.append((socket.AF_INET, socket.SOCK_STREAM, 6, "", (SECOND, 0)))
    requests, clients = http(lambda _: pytest.fail("admission refused this request"))
    admission = Mock(side_effect=httpx.ConnectError("synthetic admission error"))
    with pytest.raises(httpx.ConnectError, match="synthetic admission error"):
        await fetch_public("https://example.com/doc", timeout=1, max_bytes=100, before_request=admission)
    admission.assert_called_once_with()
    assert requests == [] and len(clients) == 1 and clients[0].is_closed


async def test_legal_repeated_metadata_names_are_not_fragment_identities(http: Any) -> None:
    metadata = ('<head><meta name="author" content="One"><meta name="author" content="Two">'
                '<meta name="keyword" content="alpha"><meta name="keyword" content="beta">'
                '<meta name="section-6.1" content="not an HTML anchor"></head>')
    html = _rfc_html().replace("<body>", metadata + "<body>")
    http(lambda _: _response(html))
    snapshot = await acquire_document(RFC_URL + "#section-6.1", budget=SourceRequestBudget())
    assert snapshot.status == "complete", snapshot.error
    assert snapshot.text.startswith("6.1. Conditional messages")


@pytest.mark.parametrize("anchors", [
    '<a name="repeated">One</a><a name="repeated">Two</a>',
    '<a name="section-6.1">Duplicate actual legacy anchor</a>',
    '<meta id="section-6.1" content="duplicate actual id">',
])
async def test_actual_ids_and_legacy_anchor_names_remain_globally_unique(http: Any, anchors: str) -> None:
    http(lambda _: _response(_rfc_html().replace("<body>", "<body>" + anchors)))
    snapshot = await acquire_document(RFC_URL + "#section-6.1", budget=SourceRequestBudget())
    assert snapshot.status == "held" and snapshot.error == "rfc_ambiguous_heading_identity"
    assert not snapshot.text and not snapshot.sections


def _rfc_with_bad_section(extra: str) -> str:
    return _rfc_html().replace('<h3>6.2. Other messages</h3>',
                               '<h3 id="name-other">6.2. Other messages</h3>' + extra)


@pytest.mark.parametrize("extra,reason", [
    ('<canvas>Uninspected diagram</canvas>', "unread_critical_media"),
    ('<div class="load-more" data-next-page="/next">Continuation not acquired</div>', "client_continuation"),
    ('<sup><a href="#missing" role="doc-noteref">Unresolved footnote</a></sup>', "missing_footnote_target"),
])
async def test_independent_complete_rfc_sections_survive_section_local_coverage_holds(
    http: Any, extra: str, reason: str,
) -> None:
    http(lambda _: _response(_rfc_with_bad_section(extra)))
    snapshot = await acquire_document(RFC_URL, budget=SourceRequestBudget())
    assert snapshot.status == "complete" and snapshot.extraction_status == "rfc_sections", snapshot.error
    assert snapshot.text == "" and snapshot.text_sha256 is None
    assert {section.section_id for section in snapshot.sections} == {"section-6.1", "section-6.1.1", "section-7"}
    section = next(section for section in snapshot.sections if section.section_id == "section-6.1")
    assert "Late qualification" in section.text and "Nested caveat" in section.text
    assert "Other messages" not in section.text
    assert any(f"section-6.2 omitted: coverage_incomplete:{reason}" in note for note in snapshot.coverage_notes)
    assert any(f"section-6 omitted: coverage_incomplete:{reason}" in note for note in snapshot.coverage_notes)
    assert any("whole-document coverage" in note for note in snapshot.coverage_notes)


@pytest.mark.parametrize("fragment", ["section-6.2", "name-other", "section-6"])
async def test_explicit_held_section_never_substitutes_an_independent_section(http: Any, fragment: str) -> None:
    http(lambda _: _response(_rfc_with_bad_section('<canvas>Uninspected diagram</canvas>')))
    snapshot = await acquire_document(RFC_URL + "#" + fragment, budget=SourceRequestBudget())
    assert snapshot.status == "held"
    assert snapshot.error == "rfc_section_held:coverage_incomplete:unread_critical_media"
    assert not snapshot.text and not snapshot.sections


async def test_explicit_complete_section_survives_unrelated_media_and_preserves_end(http: Any) -> None:
    http(lambda _: _response(_rfc_with_bad_section('<canvas>Uninspected diagram</canvas>')))
    snapshot = await acquire_document(RFC_URL + "#section-6.1", budget=SourceRequestBudget())
    assert snapshot.status == "complete" and snapshot.extraction_status == "rfc_section", snapshot.error
    assert len(snapshot.sections) == 1 and snapshot.sections[0].section_id == "section-6.1"
    assert "Late qualification" in snapshot.text and snapshot.text.endswith(PROSE)
    assert "Other messages" not in snapshot.text and snapshot.text_sha256 is not None
    assert any("Whole RFC document text held" in note for note in snapshot.coverage_notes)


@pytest.mark.parametrize("change,reason", [
    ("pagination", "coverage_incomplete:pagination"),
    ("truncation", "rfc_section_incomplete_html"),
    ("attribute", "rfc_ambiguous_attributes"),
    ("duplicate_id", "rfc_ambiguous_heading_identity"),
    ("paywall", "paywall_or_teaser"),
])
async def test_section_fallback_never_weakens_global_safety_and_completeness_checks(
    http: Any, change: str, reason: str,
) -> None:
    html = _rfc_with_bad_section('<canvas>Uninspected diagram</canvas>')
    if change == "pagination":
        html = html.replace("<body>", '<head><link rel="next" href="/next"></head><body>')
    elif change == "truncation":
        html = html.removesuffix("</body></html>")
    elif change == "attribute":
        html = html.replace('<h3 id="name-conditional">', '<h3 id="name-conditional" id="other">')
    elif change == "duplicate_id":
        html = html.replace('id="section-6.2"', 'id="section-6.1"')
    else:
        html = html.replace("<body>", '<body><div class="paywall">Global access restriction</div>')
    http(lambda _: _response(html))
    snapshot = await acquire_document(RFC_URL + "#section-6.1", budget=SourceRequestBudget())
    assert snapshot.status == "held" and snapshot.error == reason
    assert not snapshot.text and not snapshot.sections


async def test_empty_natural_section_is_omitted_without_aborting_other_sections(http: Any) -> None:
    html = _rfc_html().replace("</article>", '<section id="section-8"><h2>8. Empty</h2></section></article>')
    http(lambda _: _response(html))
    snapshot = await acquire_document(RFC_URL, budget=SourceRequestBudget())
    assert snapshot.status == "complete" and len(snapshot.sections) == 5
    assert any("section-8 omitted: rfc_section_missing_content" in note for note in snapshot.coverage_notes)
    held = await acquire_document(RFC_URL + "#section-8", budget=SourceRequestBudget())
    assert held.status == "held" and held.error == "rfc_section_held:rfc_section_missing_content"
    assert held.text == "" and held.sections == ()


async def test_section_text_budget_omits_whole_units_and_preserves_held_fragment_mapping(
    http: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    http(lambda _: _response(_rfc_html()))
    baseline = await acquire_document(RFC_URL, budget=SourceRequestBudget())
    selected = next(section for section in baseline.sections if section.section_id == "section-6.1")
    monkeypatch.setattr(documents, "MAX_RFC_SECTION_TEXT_CHARS", len(selected.text))
    bounded = await acquire_document(RFC_URL, budget=SourceRequestBudget())
    assert bounded.status == "complete" and bounded.sections == (selected,)
    assert sum(len(section.text) for section in bounded.sections) == len(selected.text)
    assert bounded.text == baseline.text  # The independently complete whole text is not shortened.
    assert any("section-6 omitted: rfc_section_text_budget" in note for note in bounded.coverage_notes)
    assert any("section-7 omitted: rfc_section_text_budget" in note for note in bounded.coverage_notes)
    held = await acquire_document(RFC_URL + "#section-7", budget=SourceRequestBudget())
    assert held.status == "held" and held.error == "rfc_section_held:rfc_section_text_budget"
    assert not held.text and not held.sections


async def test_section_urls_and_long_omission_identities_are_bounded(http: Any) -> None:
    identity = "x" * 2100
    extra = f'<section id="{identity}"><h2>8. Invalid identity</h2><p>{PROSE}</p></section>'
    http(lambda _: _response(_rfc_html().replace("</article>", extra + "</article>")))
    snapshot = await acquire_document(RFC_URL, budget=SourceRequestBudget())
    assert snapshot.status == "complete" and len(snapshot.sections) == 5
    note = next(note for note in snapshot.coverage_notes if "rfc_section_identity_limit_or_unsafe" in note)
    assert hashlib.sha256(identity.encode()).hexdigest() in note and "identity length 2100" in note
    assert len(note) < 200 and identity not in note
    assert all(len(section.url) <= 2048 for section in snapshot.sections)


def test_normalized_document_url_must_fit_existing_length_limit() -> None:
    from digest.adapters.http.public_fetch import UnsafePublicURL
    from digest.irritator.documents import _safe_document_url

    url = "https://public.example/document#" + "é" * 1000
    assert len(url) < 2048
    with pytest.raises(UnsafePublicURL):
        _safe_document_url(url)


@pytest.mark.parametrize("content, status, reason", [
    ("<p>Too short.</p>", "held", "insufficient_article_text"),
    (f"<p>{PROSE}</p><iframe src='https://example.com/private?token=secret'></iframe>",
     "held", "coverage_incomplete:unread_critical_media"),
    (f"<p>{PROSE}</p>", "complete", None),
])
async def test_extraction_diagnostics_bind_same_bytes_without_changing_evidence(
    http: Any, content: str, status: str, reason: str | None,
) -> None:
    from dataclasses import asdict, replace

    from digest._serialization import canonical_json_bytes
    from digest.irritator.research import discovery_links, document_coverage, document_signals

    html = _html(content)
    requests, _ = http(lambda _: _response(html))
    budget = SourceRequestBudget()
    snapshot = await acquire_document("https://example.com/doc", budget=budget)
    assert snapshot.status == status and snapshot.error == reason
    assert len(requests) == budget.requests_started == 1
    assert snapshot.content_sha256 == hashlib.sha256(html.encode()).hexdigest()
    assert snapshot.extraction_diagnostics is not None
    raw = canonical_json_bytes(asdict(snapshot.extraction_diagnostics))
    assert b"secret" not in raw and b"https" not in raw and b"<" not in raw
    assert len(raw) < 10_000
    assert documents.restore_document_snapshot(json.loads(canonical_json_bytes(asdict(snapshot)))) == snapshot
    legacy = replace(snapshot, extraction_diagnostics=None)
    for project in (discovery_links, document_coverage):
        assert project(snapshot) == project(legacy)
    assert document_signals(snapshot, "question", "title") == document_signals(legacy, "question", "title")
    if status == "held":
        assert snapshot.text == "" and snapshot.text_sha256 is None
    else:
        assert snapshot.text == PROSE and snapshot.text_sha256 == hashlib.sha256(PROSE.encode()).hexdigest()


def test_document_diagnostic_legacy_restore_and_strict_bounds() -> None:
    from dataclasses import asdict

    from digest._serialization import canonical_json_bytes
    from digest.article_source import extract_html

    stats = extract_html(_html(f"<p>{PROSE}</p>")).extraction_diagnostics
    assert stats is not None
    snapshot = documents.DocumentSnapshot("https://example.com/", None, "synthetic", "a" * 64,
                                          PROSE, "complete", extraction_diagnostics=stats)
    payload = json.loads(canonical_json_bytes(asdict(snapshot)))
    legacy = {key: value for key, value in payload.items() if key != "extraction_diagnostics"}
    frozen = canonical_json_bytes(legacy)
    assert documents.restore_document_snapshot(legacy).extraction_diagnostics is None
    assert canonical_json_bytes(legacy) == frozen
    for mutate in (
        lambda value: value.update(content_sha256=None),
        lambda value: value["extraction_diagnostics"].update(version=True),
        lambda value: value["extraction_diagnostics"].update(secret="token"),
        lambda value: value["extraction_diagnostics"]["regions"][0].update(characters=2**40),
        lambda value: value["extraction_diagnostics"]["regions"][0].update(kind="https://secret"),
        lambda value: value.update(unowned="field"),
    ):
        bad = json.loads(canonical_json_bytes(payload))
        mutate(bad)
        with pytest.raises(ValueError):
            documents.restore_document_snapshot(bad)


async def test_rfc_transformed_regions_do_not_mislabel_original_response_diagnostics(http: Any) -> None:
    html = "<html><body><pre>" + PROSE * 10 + "</pre></body></html>"
    requests, _ = http(lambda _: _response(html))
    snapshot = await acquire_document(RFC_URL, budget=SourceRequestBudget())
    assert snapshot.content_sha256 == hashlib.sha256(html.encode()).hexdigest()
    assert snapshot.extraction_diagnostics is None and len(requests) == 1


@pytest.mark.parametrize("extra, status, reason", [
    ('<template><svg></svg><p>Inert, not evidence.</p></template>', "complete", None),
    ('<a role="button"><svg width="16" height="16"><text style="display:none">Not visible</text></svg></a>',
     "complete", None),
    ('<template for="marker"><p>Unresolved replacement.</p></template>',
     "held", "coverage_incomplete:unread_template_content"),
    ('<svg><text>Visible evidence</text></svg>', "held", "coverage_incomplete:unread_critical_media"),
])
async def test_structural_media_fix_keeps_document_bytes_and_single_start(
    http: Any, extra: str, status: str, reason: str | None,
) -> None:
    html = _html(f"<p>{PROSE}</p>" + extra)
    requests, _ = http(lambda _: _response(html))
    budget = SourceRequestBudget(1)
    snapshot = await acquire_document("https://example.com/structure", budget=budget)
    assert len(requests) == budget.requests_started == 1
    assert snapshot.status == status and snapshot.error == reason
    assert snapshot.content_sha256 == hashlib.sha256(html.encode()).hexdigest()
    assert snapshot.extraction_diagnostics is not None
    assert snapshot.text == (PROSE if status == "complete" else "")
