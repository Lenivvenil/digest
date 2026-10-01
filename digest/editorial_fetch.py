"""Sequential SSRF-safe acquisition of complete normalized public article text.

Technical limits hold work incomplete; they never judge editorial relevance.
No text windows, model calls, file writes, article-count quotas or age-out rules.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html.parser import HTMLParser
from urllib.parse import urldefrag, urljoin, urlparse

import httpx

from digest._dns_pinning import pin_dns, validate_url
from digest.radar.collector import USER_AGENT

MAX_HTML_BYTES = 2 * 1024 * 1024
MAX_REDIRECTS = 3
FETCH_SECONDS = 20.0
MIN_ARTICLE_CHARS = 40
MIN_BODY_CHARS = 800
_VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "wbr"}
_BLOCK_TAGS = {"p", "div", "section", "article", "main", "blockquote", "li", "h1", "h2", "h3", "h4", "pre", "br",
               "table", "tr", "caption"}
_SKIP_TAGS = {"script", "style", "nav", "footer", "header", "aside", "form", "button", "noscript", "iframe",
              "input", "select", "textarea", "svg", "canvas", "video", "audio", "object", "embed"}
_SKIP_NAMES = {"navigation", "navbar", "menu", "comments", "comment", "related", "newsletter", "cookie",
               "cookies", "consent", "share", "social", "advertisement", "advertisements", "breadcrumb", "breadcrumbs"}
_FETCH_LOCK: tuple[asyncio.AbstractEventLoop, asyncio.Lock] | None = None


def _sequential_lock() -> asyncio.Lock:
    global _FETCH_LOCK
    loop = asyncio.get_running_loop()
    if _FETCH_LOCK is None or _FETCH_LOCK[0] is not loop:
        _FETCH_LOCK = (loop, asyncio.Lock())
    return _FETCH_LOCK[1]


@dataclass
class _Section:
    kind: str
    chunks: list[str] = field(default_factory=list)
    paragraphs: int = 0
    linked_chars: int = 0
    closed: bool = False
    index: int = 0
    ancestors: tuple[int, ...] = ()
    ids: set[str] = field(default_factory=set)
    references: set[str] = field(default_factory=set)
    issues: set[str] = field(default_factory=set)
    has_images: bool = False


@dataclass
class _Node:
    tag: str
    skipped: bool
    section: _Section | None = None
    jsonld: bool = False


@dataclass(frozen=True)
class ExtractedArticle:
    text: str
    source_published: str | None
    extraction_status: str
    coverage_notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class FetchedArticle:
    text: str
    final_url: str
    fetched_at: str
    source_published: str | None
    extraction_status: str
    coverage_notes: tuple[str, ...] = ()


class _ArticleParser(HTMLParser):
    """Minimal HTMLParser adapter; it intentionally does not execute/render HTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[_Node] = []
        self.sections: list[_Section] = []
        self.root_opened: set[str] = set()
        self.root_closed: set[str] = set()
        self.publication_dates: list[datetime] = []
        self.paywall_detected = False
        self.global_issues: set[str] = set()

    def _active(self) -> list[_Section]:
        return [node.section for node in self.stack if node.section is not None]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if len(self.stack) >= 256:
            raise ValueError("technical_html_nesting_limit")
        values = {key.lower(): value or "" for key, value in attrs}
        if tag == "meta":
            field_name = (values.get("property") or values.get("itemprop") or values.get("name", "")).lower()
            if field_name in {"article:published_time", "datepublished", "datecreated"}:
                parsed = _parse_date(values.get("content", ""))
                if parsed is not None:
                    self.publication_dates.append(parsed)
        if tag in {"html", "body"}:
            self.root_opened.add(tag)
        tokens = set(re.split(r"[\s_-]+", (values.get("id", "") + " " + values.get("class", "")).lower()))
        if tokens & {"paywall", "metered", "subscriptionwall"}:
            self.paywall_detected = True
        style = re.sub(r"\s+", "", values.get("style", "").lower())
        inside_article = any(node.tag in {"article", "main"} and not node.skipped for node in self.stack)
        footnotes = bool(tokens & {"footnotes", "endnotes"}) or values.get("role") == "doc-endnotes"
        skip_tag = tag in _SKIP_TAGS and not (
            (inside_article or footnotes) and tag in {"aside", "header", "footer"}
        )
        skipped = (bool(self.stack and self.stack[-1].skipped) or skip_tag
                   or "hidden" in values or values.get("aria-hidden", "").lower() == "true"
                   or "display:none" in style or "visibility:hidden" in style or bool(tokens & _SKIP_NAMES))
        if not skipped and tag in _BLOCK_TAGS:
            for section in self._active():
                section.chunks.append("\n\n")
        self._observe_coverage(tag, values, tokens, skipped, inside_article)
        if not skipped and tag in {"td", "th"}:
            for active in self._active():
                active.chunks.append(" | ")
        captured = None
        if not skipped and (tag in {"article", "main", "body"} or footnotes):
            captured = _Section("footnotes" if footnotes else tag, index=len(self.sections),
                                ancestors=tuple(section.index for section in self._active()))
        if captured is not None:
            self.sections.append(captured)
        if tag not in _VOID_TAGS:
            self.stack.append(_Node(tag, skipped, captured, values.get("type") == "application/ld+json"))
        if not skipped and values.get("id"):
            for active in self._active():
                active.ids.add(values["id"])
        if tag == "p" and not skipped:
            for active in self._active():
                active.paragraphs += 1

    def _observe_coverage(
        self, tag: str, values: dict[str, str], tokens: set[str], skipped: bool, inside_article: bool,
    ) -> None:
        rel = set(values.get("rel", "").lower().split())
        if tag == "link" and "next" in rel:
            self.global_issues.add("pagination")
        if inside_article and ("next" in rel or "pagination" in tokens):
            for section in self._active():
                section.issues.add("pagination")
        if inside_article and (tokens & {"continuation", "infinite", "loadmore"}
                               or {"load", "more"} <= tokens or {"read", "more"} <= tokens
                               or any(key in values for key in
                                      ("data-next-page", "data-continuation", "data-load-more"))):
            for section in self._active():
                section.issues.add("client_continuation")
        hidden = (bool(self.stack and self.stack[-1].skipped) or "hidden" in values
                  or values.get("aria-hidden", "").lower() == "true" or bool(tokens & _SKIP_NAMES))
        if not hidden and self._active():
            critical = tag in {"svg", "canvas", "video", "audio", "iframe", "object", "embed"}
            critical |= tag in {"img", "svg"} and bool(re.search(
                r"\b(chart|graph|diagram|table|benchmark|results|figure)\b",
                values.get("alt", "") + " " + values.get("aria-label", "") + " " + " ".join(tokens), re.I,
            ))
            for section in self._active():
                if tag in {"img", "svg"}:
                    section.has_images = True
                if critical:
                    section.issues.add("unread_critical_media")
        href = values.get("href", "")
        if not skipped and tag == "a" and href.startswith("#") and (
            values.get("role") == "doc-noteref" or "footnote" in tokens
            or any(node.tag == "sup" for node in self.stack)
        ):
            for section in self._active():
                section.references.add(href[1:])

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in _VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"html", "body"}:
            self.root_closed.add(tag)
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index].tag != tag:
                continue
            node = self.stack[index]
            if node.section is not None:
                node.section.closed = True
            del self.stack[index:]
            if tag in _BLOCK_TAGS and not node.skipped:
                for section in self._active():
                    section.chunks.append("\n\n")
            break

    def handle_data(self, data: str) -> None:
        if self.stack and self.stack[-1].jsonld and re.search(
            r'"isAccessibleForFree"\s*:\s*(false|"false")', data, re.I,
        ):
            self.paywall_detected = True
        if not self.stack or self.stack[-1].skipped:
            return
        linked = any(node.tag == "a" for node in self.stack)
        for section in self._active():
            section.chunks.append(data)
            if linked:
                section.linked_chars += len(data.strip())


def _now() -> datetime:
    return datetime.now(UTC)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _parse_date(value: str) -> datetime | None:
    try:
        return _utc(datetime.fromisoformat(value.strip().replace("Z", "+00:00")))
    except (ValueError, OverflowError):
        return None


def _normalized_text(section: _Section) -> str:
    return "\n\n".join(filter(None, (re.sub(r"\s+", " ", part).strip()
                                      for part in "".join(section.chunks).split("\n\n"))))


def extract_html(html: str) -> ExtractedArticle:
    parser = _ArticleParser()
    parser.feed(html)
    parser.close()
    if parser.paywall_detected:
        raise ValueError("paywall_or_teaser")
    if (parser.root_opened - parser.root_closed
            or any(not section.closed for section in parser.sections if section.kind in {"article", "main"})):
        raise ValueError("clipped_html")
    candidates: list[tuple[int, int, _Section, str]] = []
    for section in parser.sections:
        if not section.closed or section.kind == "footnotes":
            continue
        text = _normalized_text(section)
        minimum = MIN_BODY_CHARS if section.kind == "body" else MIN_ARTICLE_CHARS
        if len(text) < minimum or section.paragraphs < (3 if section.kind == "body" else 1):
            continue
        if section.linked_chars / max(1, len(text)) > (0.15 if section.kind == "body" else 0.4):
            continue
        if len(re.findall(r"[^\W\d_]", text)) < minimum // 2:
            continue
        if re.search(
            r"subscribe to (continue|read)|sign in to (continue|read)|enable javascript and cookies|"
            r"read the full article|continue reading (at|on)", text, re.I,
        ):
            continue
        candidates.append(({"article": 0, "main": 1, "body": 2}[section.kind], -len(text), section, text))
    if not candidates:
        raise ValueError("insufficient_article_text")
    candidates.sort(key=lambda candidate: (candidate[0], candidate[1]))
    selected, text = candidates[0][2:]
    if any(candidate[2].kind == selected.kind and candidate[2].index != selected.index
           and selected.index not in candidate[2].ancestors and candidate[2].index not in selected.ancestors
           for candidate in candidates):
        raise ValueError("coverage_incomplete:multiple_article_regions")
    ids = set(selected.ids)
    references = set(selected.references)
    issues = selected.issues | parser.global_issues
    for notes in parser.sections:
        if notes.kind == "footnotes" and selected.index not in notes.ancestors:
            if not notes.closed:
                raise ValueError("coverage_incomplete:clipped_footnotes")
            note_text = _normalized_text(notes)
            if note_text:
                text += "\n\n" + note_text
                ids.update(notes.ids)
                references.update(notes.references)
                issues.update(notes.issues)
    if references - ids:
        issues.add("missing_footnote_target")
    if selected.has_images and re.search(r"\b(shown|see|illustrated) in (the )?(chart|figure|diagram)\b", text, re.I):
        issues.add("unread_critical_media")
    if issues:
        raise ValueError("coverage_incomplete:" + ",".join(sorted(issues)))
    source_date = min(parser.publication_dates).isoformat() if parser.publication_dates else None
    coverage_notes = [
        "Complete normalized text from the selected HTML region; publisher completeness is not guaranteed.",
    ]
    if selected.has_images:
        coverage_notes.append("Uninspected images are present; this acquisition covers textual content only.")
    return ExtractedArticle(text, source_date, selected.kind, tuple(coverage_notes))


def _url_syntax(url: str) -> str:
    try:
        parsed = urlparse(url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.port not in {None, 443 if parsed.scheme == "https" else 80}
                or len(url) > 2048 or any(ord(character) < 33 for character in url)):
            raise ValueError("unsafe_url")
    except ValueError as exc:
        raise ValueError("unsafe_url") from exc
    return urldefrag(url)[0]


async def _fetch_with_client(client: httpx.AsyncClient, url: str) -> FetchedArticle:
    current = _url_syntax(url)
    async with asyncio.timeout(FETCH_SECONDS):
        for redirects in range(MAX_REDIRECTS + 1):
            validated = await asyncio.to_thread(validate_url, current)
            if validated is None:
                raise ValueError("unsafe_url")
            with pin_dns(validated.hostname, validated.pinned_addrinfos):
                async with client.stream("GET", current, follow_redirects=False, timeout=FETCH_SECONDS) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        target = response.headers.get("location")
                        if not target or redirects == MAX_REDIRECTS:
                            raise ValueError("redirect_limit_or_missing_target")
                        current = _url_syntax(urljoin(current, target))
                        continue
                    response.raise_for_status()
                    if response.status_code != 200 or "content-range" in response.headers:
                        raise ValueError("partial_or_nonarticle_response")
                    media_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                    if media_type not in {"text/html", "application/xhtml+xml"}:
                        raise ValueError("unsupported_content_type")
                    declared_size = response.headers.get("content-length")
                    if declared_size is not None and (
                        not declared_size.isdigit() or int(declared_size) > MAX_HTML_BYTES
                    ):
                        raise ValueError("oversized_or_invalid_content_length")
                    chunks: list[bytes] = []
                    size = 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > MAX_HTML_BYTES:
                            raise ValueError("oversized_html")
                        chunks.append(chunk)
                    if (declared_size is not None and not response.headers.get("content-encoding")
                            and size != int(declared_size)):
                        raise ValueError("clipped_http_body")
                    html = b"".join(chunks).decode(response.encoding or "utf-8", errors="strict")
                    extracted = extract_html(html)
                    return FetchedArticle(
                        extracted.text, str(response.url), _now().isoformat(), extracted.source_published,
                        extracted.extraction_status, extracted.coverage_notes,
                    )
    raise ValueError("redirect_limit_or_missing_target")



async def fetch_article(url: str) -> FetchedArticle:
    """Fetch one article. Failed or ambiguous acquisition raises a technical error.

    The process-wide sequential lock protects the existing DNS-pinning helper.
    Queue execution limits belong to the caller, independently of body length.
    """
    async with _sequential_lock(), httpx.AsyncClient(
        follow_redirects=False, trust_env=False, timeout=FETCH_SECONDS,
        limits=httpx.Limits(max_connections=1, max_keepalive_connections=0),
        headers={"User-Agent": USER_AGENT, "Accept": "text/html, application/xhtml+xml",
                 "Accept-Encoding": "identity"},
    ) as client:
        return await _fetch_with_client(client, url)
