"""Bounded original-document acquisition, separate from link discovery and judgment.

Only fetched HTML supplies evidence or selectable links. A link is a discovery
candidate, not a read of its destination or proof that the destination is public.
Every destination and redirect is checked again by the public-fetch boundary.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Literal
from urllib.parse import unquote, urldefrag, urljoin, urlsplit

import httpx

from digest._serialization import restore_dataclass
from digest.adapters.http.public_fetch import PublicFetchError, UnsafePublicURL, fetch_public
from digest.article_source import (
    FETCH_SECONDS,
    MAX_HTML_BYTES,
    MAX_REDIRECTS,
    ExtractedArticle,
    ExtractionDiagnostics,
    ExtractionError,
    extract_html,
    restore_extraction_diagnostics,
)
from digest.radar.collector import USER_AGENT

MAX_DOCUMENT_LINKS = 40
MAX_LINK_TITLE_CHARS = 300
MAX_RFC_HEADINGS = 512
MAX_RFC_SECTION_TEXT_CHARS = MAX_HTML_BYTES
_VOID = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param",
                   "source", "track", "wbr"})
_INERT = frozenset({"script", "style", "template", "noscript", "nav", "header", "footer", "form", "button",
                    "svg", "iframe", "object", "embed"})
_RFC_HOSTS = frozenset({"rfc-editor.org", "www.rfc-editor.org", "datatracker.ietf.org", "tools.ietf.org"})


class SourceRequestBudgetExceeded(ValueError):
    """No further HTTP request may start in this investigation."""

    def __init__(self) -> None:
        super().__init__("source_request_budget_exhausted")


@dataclass
class SourceRequestBudget:
    """One shared admission counter, consumed synchronously at transport starts."""

    max_requests: int = 9
    requests_started: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        if type(self.max_requests) is not int or not 0 <= self.max_requests <= 9:
            raise ValueError("source_request_budget_must_be_between_zero_and_nine")

    def consume(self) -> None:
        if self.requests_started >= self.max_requests:
            raise SourceRequestBudgetExceeded()
        self.requests_started += 1


@dataclass(frozen=True)
class DocumentLink:
    link_id: str
    url: str
    title: str


@dataclass(frozen=True)
class DocumentSection:
    section_id: str
    title: str
    text: str
    url: str
    text_sha256: str


@dataclass(frozen=True)
class DocumentSnapshot:
    requested_url: str
    final_url: str | None
    fetched_at: str
    content_sha256: str | None
    text: str
    status: Literal["complete", "held", "error"]
    error: str | None = None
    links: tuple[DocumentLink, ...] = ()
    sections: tuple[DocumentSection, ...] = ()
    text_sha256: str | None = None
    extraction_status: str | None = None
    coverage_notes: tuple[str, ...] = ()
    omitted_link_count: int = 0
    extraction_diagnostics: ExtractionDiagnostics | None = None

    def __post_init__(self) -> None:
        if self.extraction_diagnostics is not None:
            # The immutable snapshot binds content-free stats to the same response bytes.
            if not isinstance(self.content_sha256, str) or re.fullmatch(r"[0-9a-f]{64}", self.content_sha256) is None:
                raise ValueError("Extraction diagnostics require a content hash.")
            restore_extraction_diagnostics(asdict(self.extraction_diagnostics))


def document_snapshot_checkpoint(value: object) -> dict[str, object]:
    """Add only the absent legacy diagnostic field in memory; never rewrite saved bytes."""
    if not isinstance(value, dict):
        raise ValueError("Invalid document checkpoint.")
    return {"extraction_diagnostics": None, **value}


def restore_document_snapshot(value: object) -> DocumentSnapshot:
    snapshot: DocumentSnapshot = restore_dataclass(document_snapshot_checkpoint(value), DocumentSnapshot)
    return snapshot


@dataclass
class _Node:
    tag: str
    attrs: dict[str, str]
    start: int
    inner_start: int
    end: int = -1
    close_start: int = -1
    hidden: bool = False
    ambiguous_attributes: bool = False
    parent: _Node | None = None
    children: list[_Node] = field(default_factory=list)
    text: list[str] = field(default_factory=list)

    @property
    def identities(self) -> tuple[str, ...]:
        keys = ("id", "name") if self.tag == "a" else ("id",)
        return tuple(dict.fromkeys(value for key in keys if (value := self.attrs.get(key))))


class _DocumentHTML(HTMLParser):
    """Keep source offsets for literal links and complete natural heading ranges."""

    def __init__(self, html: str) -> None:
        super().__init__(convert_charrefs=True)
        self.html = html
        self.lines = [0, *(match.end() for match in re.finditer("\n", html))]
        self.root = _Node("", {}, 0, 0, len(html), len(html))
        self.stack = [self.root]
        self.nodes: list[_Node] = []
        self.ambiguous_attributes = False

    def _offset(self) -> int:
        line, column = self.getpos()
        return self.lines[line - 1] + column

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if len(self.stack) > 256:
            raise ValueError("technical_html_nesting_limit")
        values = {key: value or "" for key, value in attrs}
        self.ambiguous_attributes |= len(values) != len(attrs)
        style = re.sub(r"\s+", "", values.get("style", "").lower())
        hidden = (self.stack[-1].hidden or tag in _INERT or "hidden" in values
                  or values.get("aria-hidden", "").lower() == "true"
                  or "display:none" in style or "visibility:hidden" in style)
        start = self._offset()
        node = _Node(tag, values, start, start + len(self.get_starttag_text() or ""),
                     hidden=hidden, ambiguous_attributes=len(values) != len(attrs), parent=self.stack[-1])
        self.nodes.append(node)
        self.stack[-1].children.append(node)
        if tag in _VOID:
            node.end = node.close_start = node.inner_start
        else:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            node = self.stack.pop()
            node.end = node.close_start = node.inner_start

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                node = self.stack[index]
                node.close_start = self._offset()
                end = self.html.find(">", node.close_start)
                node.end = end + 1 if end >= 0 else -1
                del self.stack[index:]
                break

    def handle_data(self, data: str) -> None:
        if not self.stack[-1].hidden:
            for node in self.stack[1:]:
                if node.tag == "a" or _heading_level(node) is not None:
                    node.text.append(data)

    def render_range(self, node: _Node, start: int, end: int) -> str:
        """Clip structural containers, never the requested natural text unit."""
        if node.end < 0:
            raise ValueError("rfc_section_incomplete_html")
        if node.end <= start or node.start >= end:
            return ""
        if start <= node.start and node.end <= end:
            return self.html[node.start:node.end]
        pieces: list[str] = []
        cursor = node.inner_start
        for child in node.children:
            if child.end < 0:
                raise ValueError("rfc_section_incomplete_html")
            left, right = max(cursor, start), min(child.start, end)
            pieces.append(self.html[left:right] if left < right else "")
            pieces.append(self.render_range(child, start, end))
            cursor = child.end
        if max(cursor, start) < min(node.close_start, end):
            pieces.append(self.html[max(cursor, start):min(node.close_start, end)])
        return (self.html[node.start:node.inner_start] + "".join(pieces)
                + self.html[node.close_start:node.end])


def _safe_document_url(value: str) -> str:
    if len(value) > 2048 or "\\" in value or any(ord(char) <= 32 or ord(char) == 127 for char in value):
        raise UnsafePublicURL()
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.port not in {None, 443 if parsed.scheme == "https" else 80}):
            raise UnsafePublicURL()
        url = httpx.URL(value)
        host = url.host.lower()
        if ("%" in host or host in {"localhost", "metadata.google.internal"}
                or host.endswith((".localhost", ".local", ".internal"))):
            raise UnsafePublicURL()
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            if "." not in host:
                raise UnsafePublicURL() from None
        else:
            if (not address.is_global or address.is_multicast or address.is_reserved
                    or isinstance(address, ipaddress.IPv6Address) and address.is_site_local):
                raise UnsafePublicURL()
        normalized = str(url)
        if len(normalized) > 2048:
            raise UnsafePublicURL()
        return normalized
    except (ValueError, UnicodeError, httpx.InvalidURL) as exc:
        raise UnsafePublicURL() from exc


def _inside(node: _Node, tags: set[str]) -> bool:
    parent = node.parent
    while parent is not None:
        if parent.tag in tags:
            return True
        parent = parent.parent
    return False


def _links(parser: _DocumentHTML, url: str, digest: str) -> tuple[tuple[DocumentLink, ...], int]:
    # A document's content links take precedence over surrounding site chrome.
    anchors = [node for node in parser.nodes
               if node.tag == "a" and not node.hidden and node.end >= 0 and not node.ambiguous_attributes]
    if any(node.tag in {"article", "main"} and not node.hidden for node in parser.nodes):
        anchors = [node for node in anchors if _inside(node, {"article", "main"})]
    bases = [node for node in parser.nodes if node.tag == "base" and "href" in node.attrs]
    base_url: str | None = url
    if bases:
        try:
            if len(bases) != 1 or bases[0].ambiguous_attributes:
                raise UnsafePublicURL()
            base_url = _safe_document_url(urljoin(url, bases[0].attrs["href"]))
        except ValueError:
            base_url = None
    links: list[DocumentLink] = []
    seen: set[str] = set()
    omitted = 0
    for anchor in anchors:
        href = anchor.attrs.get("href", "")
        if not href or href.startswith("#") or any(ord(char) <= 32 or ord(char) == 127 for char in href):
            continue
        try:
            if base_url is None and not urlsplit(href).scheme:
                continue
            target = _safe_document_url(urljoin(base_url or url, href))
        except ValueError:
            continue
        if urldefrag(target)[0] == urldefrag(url)[0] or target in seen:
            continue
        seen.add(target)
        title = " ".join("".join(anchor.text).split()) or anchor.attrs.get("title", "").strip() or target
        if len(links) >= MAX_DOCUMENT_LINKS or len(title) > MAX_LINK_TITLE_CHARS:
            omitted += 1
            continue
        identity = hashlib.sha256(f"{url}\0{digest}\0{target}\0{title}".encode()).hexdigest()[:24]
        links.append(DocumentLink("link-" + identity, target, title))
    return tuple(links), omitted


def _rfc_url(url: str) -> bool:
    parsed = urlsplit(url)
    return parsed.hostname in _RFC_HOSTS and bool(re.search(r"/rfc[0-9]+(?:\.html)?/?$", parsed.path, re.I))


def _heading_level(node: _Node) -> int | None:
    if node.hidden:
        return None
    if re.fullmatch(r"h[1-6]", node.tag):
        return int(node.tag[1])
    matches = [token for token in node.attrs.get("class", "").split() if re.fullmatch(r"h[1-6]", token)]
    return int(matches[0][1]) if node.tag == "span" and len(matches) == 1 else None


def _heading_ids(node: _Node) -> list[str]:
    identities = list(node.identities)
    for child in node.children:
        identities.extend(_heading_ids(child))
    return identities


def _section_identity(identities: list[str]) -> str | None:
    return next((value for value in identities if re.fullmatch(r"(?:section|appendix)-[A-Za-z0-9.]+", value)),
                identities[0] if identities else None)


@dataclass(frozen=True)
class _RFCSections:
    sections: tuple[DocumentSection, ...]
    fragments: dict[str, DocumentSection]
    held_fragments: dict[str, str]
    coverage_notes: tuple[str, ...]


def _section_text(parser: _DocumentHTML, body: _Node, heading: _Node, end: int) -> ExtractedArticle:
    if end <= heading.end:
        raise ValueError("rfc_section_missing_content")
    # The wrapper is structural only: text, headings and links remain original.
    fragment = parser.render_range(body, heading.start, end)
    fragment = fragment[len(parser.html[body.start:body.inner_start]):]
    fragment = fragment[:len(fragment) - len(parser.html[body.close_start:body.end])]
    extracted = extract_html("<html><body><article><p>" + fragment + "</p></article></body></html>")
    title = " ".join("".join(heading.text).split())
    if not title or not extracted.text or extracted.text == title:
        raise ValueError("rfc_section_missing_content")
    return extracted


def _section_omission(section_id: str, reason: str) -> str:
    label = section_id if len(section_id) <= 128 else (
        f"sha256:{hashlib.sha256(section_id.encode()).hexdigest()} (identity length {len(section_id)})"
    )
    return f"RFC section {label} omitted: {reason}."


def _bounded_section(
    parser: _DocumentHTML, body: _Node, heading: _Node, end: int, url: str, section_id: str, remaining: int,
) -> tuple[ExtractedArticle, str]:
    if remaining <= 0:
        raise ValueError("rfc_section_text_budget")
    try:
        section_url = _safe_document_url(urldefrag(url)[0] + "#" + section_id)
    except UnsafePublicURL as exc:
        raise ValueError("rfc_section_identity_limit_or_unsafe") from exc
    extracted = _section_text(parser, body, heading, end)
    if len(extracted.text) > remaining:
        raise ValueError("rfc_section_text_budget")
    return extracted, section_url


def _rfc_sections(parser: _DocumentHTML, url: str) -> _RFCSections:
    """Validate identity/structure globally, then assess each natural unit locally."""
    headings = [(node, level) for node in parser.nodes if (level := _heading_level(node)) is not None]
    if len(headings) > MAX_RFC_HEADINGS:
        raise ValueError("rfc_heading_limit")
    if parser.ambiguous_attributes:
        raise ValueError("rfc_ambiguous_attributes")
    identities = [identity for node in parser.nodes for identity in node.identities]
    if len(identities) != len(set(identities)):
        raise ValueError("rfc_ambiguous_heading_identity")
    bodies = [node for node in parser.nodes if node.tag == "body"]
    if len(bodies) != 1 or bodies[0].end < 0 or any(node.end < 0 for node in parser.nodes):
        raise ValueError("rfc_section_incomplete_html")
    if any(node.tag == "link" and "next" in node.attrs.get("rel", "").lower().split() for node in parser.nodes):
        raise ValueError("coverage_incomplete:pagination")
    body = bodies[0]
    sections: list[DocumentSection] = []
    by_fragment: dict[str, DocumentSection] = {}
    held: dict[str, str] = {}
    notes: list[str] = []
    admitted_chars = 0
    for index, (heading, level) in enumerate(headings):
        ids = _heading_ids(heading)
        container = heading.parent
        if (container is not None and container.tag == "section"
                and not any(container.start < earlier.start < heading.start for earlier, _ in headings)):
            ids.extend(container.identities)
        section_id = _section_identity(ids)
        if section_id is None:
            continue
        end = next((later.start for later, depth in headings[index + 1:] if depth <= level), body.close_start)
        if container is not None and container.tag == "section":
            end = min(end, container.close_start)
        try:
            extracted, section_url = _bounded_section(
                parser, body, heading, end, url, section_id, MAX_RFC_SECTION_TEXT_CHARS - admitted_chars,
            )
        except ValueError as exc:
            reason = str(exc)
            held.update((identity, reason) for identity in ids)
            notes.append(_section_omission(section_id, reason))
            continue
        title = " ".join("".join(heading.text).split())
        admitted_chars += len(extracted.text)
        section = DocumentSection(section_id, title, extracted.text, section_url,
                                  hashlib.sha256(extracted.text.encode()).hexdigest())
        sections.append(section)
        for identity in ids:
            by_fragment[identity] = section
        notes.extend(f"RFC section URL {section_url}: {note}" for note in extracted.coverage_notes[1:])
    return _RFCSections(tuple(sections), by_fragment, held, tuple(notes))


def _full_text(html: str, parser: _DocumentHTML, rfc: bool) -> ExtractedArticle:
    try:
        return extract_html(html)
    except ValueError as exc:
        # Legacy RFC HTML is a complete preformatted document, without paragraphs.
        # Reuse all existing coverage checks, adding no source text or guessed section.
        if not rfc or str(exc) != "insufficient_article_text":
            raise
        bodies = [node for node in parser.nodes if node.tag == "body"]
        if len(bodies) != 1 or bodies[0].end < 0 or not any(node.tag == "pre" for node in parser.nodes):
            raise
        body = bodies[0]
        return extract_html("<html><body><article><p>" + html[body.inner_start:body.close_start]
                            + "</p></article></body></html>")


def _rfc_evidence(
    html: str, parser: _DocumentHTML, url: str, fragment: str,
) -> tuple[ExtractedArticle, tuple[DocumentSection, ...], str]:
    extracted_sections = _rfc_sections(parser, url)
    whole: ExtractedArticle | None = None
    whole_error = ""
    try:
        whole = _full_text(html, parser, True)
    except ValueError as exc:
        whole_error = str(exc)
        local_issues = {"unread_critical_media", "missing_footnote_target", "client_continuation", "pagination"}
        issues = set(whole_error.removeprefix("coverage_incomplete:").split(","))
        if whole_error != "insufficient_article_text" and not (
            whole_error.startswith("coverage_incomplete:") and issues <= local_issues
        ):
            raise
    notes = extracted_sections.coverage_notes
    if whole_error:
        notes = (f"Whole RFC document text held: {whole_error}.",) + notes
    if fragment:
        if fragment in extracted_sections.held_fragments:
            raise ValueError("rfc_section_held:" + extracted_sections.held_fragments[fragment])
        selected = extracted_sections.fragments.get(fragment)
        if selected is None:
            raise ValueError("rfc_section_fragment_missing")
        notes = ("Complete selected RFC section text; whole-document coverage is not asserted.",) + notes
        return ExtractedArticle(selected.text, None, "rfc_section", notes), (selected,), selected.url
    if whole is None:
        if not extracted_sections.sections:
            raise ValueError("rfc_no_complete_sections:" + whole_error)
        notes = ("Only complete RFC sections are evidence; no whole-document coverage is asserted.",) + notes
        return ExtractedArticle("", None, "rfc_sections", notes), extracted_sections.sections, url
    return (ExtractedArticle(whole.text, whole.source_published, whole.extraction_status, whole.coverage_notes + notes),
            extracted_sections.sections, url)


def _failure(exc: Exception) -> tuple[Literal["held", "error"], str]:
    if isinstance(exc, SourceRequestBudgetExceeded):
        return "held", "source_request_budget_exhausted"
    if isinstance(exc, TimeoutError):
        return "held", "document_timeout"
    if isinstance(exc, httpx.HTTPStatusError):
        return "error", f"http_status_{exc.response.status_code}"
    if isinstance(exc, (UnicodeError, LookupError)):
        return "held", "document_decode_error"
    if isinstance(exc, (ValueError, PublicFetchError)):
        # These exceptions carry owned technical codes, never server text.
        return "held", str(exc)
    return "error", type(exc).__name__


async def acquire_document(url: str, *, budget: SourceRequestBudget) -> DocumentSnapshot:
    """Read complete text and verified discovery links, with no partial evidence.

    One logical document can require multiple counted HTTP starts. The caller owns
    logical discovery/evidence-read limits; this shared budget owns the physical
    starts. Complete text is never shortened to fit a later model envelope.
    RFC section-only success has empty whole text and explicit omitted-section
    coverage notes; each retained section is independently complete.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + FETCH_SECONDS
    final_url: str | None = None
    content_sha256: str | None = None
    fetched_at = datetime.now(UTC).isoformat()
    links: tuple[DocumentLink, ...] = ()
    omitted = 0
    notes: tuple[str, ...] = ()
    rfc = False
    try:
        async with asyncio.timeout(FETCH_SECONDS):
            requested = _safe_document_url(url)
            response = await fetch_public(
                requested, timeout=FETCH_SECONDS, max_bytes=MAX_HTML_BYTES, max_redirects=MAX_REDIRECTS,
                validate_hop=_safe_document_url, before_request=budget.consume,
                headers={"User-Agent": USER_AGENT, "Accept": "text/html, application/xhtml+xml"},
            )
            final_url = response.url
            fetched_at = datetime.now(UTC).isoformat()
            response.raise_for_status()
            if response.status_code != 200 or "content-range" in response.headers:
                raise ValueError("partial_or_nonarticle_response")
            media_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if media_type not in {"text/html", "application/xhtml+xml"}:
                raise ValueError("unsupported_content_type")
            content_sha256 = hashlib.sha256(response.content).hexdigest()
            html = response.content.decode(response.encoding, errors="strict")
            parser = _DocumentHTML(html)
            parser.feed(html)
            parser.close()
            links, omitted = _links(parser, final_url, content_sha256)
            if omitted:
                notes = (f"Discovery omitted {omitted} links that exceed the link-count or complete-title bound.",)
            rfc = _rfc_url(final_url)
            if _rfc_url(requested) and not rfc:
                raise ValueError("rfc_redirect_not_document")
            requested_fragment = unquote(urlsplit(requested).fragment)
            final_fragment = unquote(urlsplit(final_url).fragment)
            if rfc and requested_fragment and final_fragment and requested_fragment != final_fragment:
                raise ValueError("rfc_section_redirect_mismatch")
            fragment = requested_fragment or final_fragment
            if rfc:
                extracted, sections, final_url = _rfc_evidence(html, parser, final_url, fragment)
            else:
                extracted, sections = _full_text(html, parser, False), ()
            text = extracted.text
            extraction_status = extracted.extraction_status
            if loop.time() >= deadline:
                raise TimeoutError
            return DocumentSnapshot(
                url, final_url, fetched_at, content_sha256, text, "complete", links=links, sections=sections,
                text_sha256=hashlib.sha256(text.encode()).hexdigest() if text else None,
                extraction_status=extraction_status,
                coverage_notes=extracted.coverage_notes + notes, omitted_link_count=omitted,
                extraction_diagnostics=extracted.extraction_diagnostics if not rfc else None,
            )
    except (ValueError, httpx.HTTPError, TimeoutError, LookupError) as exc:
        status, error = _failure(exc)
        return DocumentSnapshot(url, final_url, fetched_at, content_sha256, "", status, error,
                                links=links, coverage_notes=notes, omitted_link_count=omitted,
                                extraction_diagnostics=(exc.extraction_diagnostics
                                                        if isinstance(exc, ExtractionError) and not rfc else None))
