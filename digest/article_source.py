"""SSRF-safe acquisition of complete normalized public article text.

Technical limits hold work incomplete; they never judge editorial relevance.
No text windows, model calls, file writes, article-count quotas or age-out rules.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html.parser import HTMLParser
from urllib.parse import urldefrag, urlparse

from digest.adapters.http.public_fetch import PublicFetchError, fetch_public
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
# Exact React queued-boundary bootstrap bytes, excluding its terminal literal call.
# Unknown framework versions remain incomplete rather than being interpreted as JS.
_REACT_STREAM_BOOTSTRAP_HASHES = frozenset({"7a3c441a297b4368e7168f53d44323873b11a160d3567f2d4695187785c22646"})


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
    outside_articles: list[str] = field(default_factory=list)
    outside_paragraphs: int = 0


@dataclass
class _Node:
    tag: str
    skipped: bool
    section: _Section | None = None
    jsonld: bool = False
    tokens: set[str] = field(default_factory=set)
    ui_control: bool = False
    decorative_svg: bool = False


@dataclass
class _StreamElement:
    tag: str
    identity: str
    start: int
    inner_start: int
    hidden: bool = False
    inert: bool = False
    inner_end: int = -1
    end: int = -1


class _StreamParser(HTMLParser):
    """Locate React's completed streamed regions; never evaluate script content."""

    def __init__(self, html: str) -> None:
        super().__init__(convert_charrefs=False)
        self.html = html
        self.lines = [0]
        for match in re.finditer("\n", html):
            self.lines.append(match.end())
        self.stack: list[_StreamElement] = []
        self.elements: dict[str, list[_StreamElement]] = {}
        self.boundaries: list[tuple[int, int, int]] = []
        self.open_boundaries: list[tuple[int, int]] = []
        self.calls: list[tuple[str, str, int]] = []
        self.script = ""
        self.active_script = False
        self.known_bootstrap = False

    def _offset(self) -> int:
        line, column = self.getpos()
        return self.lines[line - 1] + column

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if len(self.stack) >= 256:
            raise ValueError("technical_html_nesting_limit")
        values = dict(attrs)
        start = self._offset()
        element = _StreamElement(tag, values.get("id") or "", start,
                                 start + len(self.get_starttag_text() or ""), "hidden" in values,
                                 len(values) != len(attrs) or any(
                                     parent.tag in {"template", "noscript", "svg", "math"} for parent in self.stack
                                 ))
        if re.fullmatch(r"[BS]:[A-Za-z0-9_-]{1,64}", element.identity):
            self.elements.setdefault(element.identity, []).append(element)
        if tag not in _VOID_TAGS:
            self.stack.append(element)
        if tag == "script":
            self.script = ""
            classic = (values.get("type") or "").strip().lower() in {
                "", "text/javascript", "application/javascript", "text/ecmascript", "application/ecmascript",
            }
            classic &= (values.get("language") or "").strip().lower() in {"", "javascript", "ecmascript"}
            self.active_script = classic and "src" not in values and "nomodule" not in values and not element.inert
            if not self.active_script:
                self.known_bootstrap = False

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in _VOID_TAGS:
            self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if self.stack and self.stack[-1].tag == "script":
            self.script += data

    def handle_comment(self, data: str) -> None:
        if data in {"$", "$?", "$!", "$~"}:
            self.open_boundaries.append((self._offset(), self._offset() + len(data) + 7))
        elif data == "/$" and self.open_boundaries:
            start, content_start = self.open_boundaries.pop()
            self.boundaries.append((start, content_start, self._offset() + len(data) + 7))

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self.active_script:
            script = self.script.strip()
            match = re.fullmatch(r'(.*?)\$RC\("(B:[A-Za-z0-9_-]{1,64})","(S:[A-Za-z0-9_-]{1,64})"\);?',
                                 script, re.S)
            prefix = match[1].strip() if match else script
            recognized = hashlib.sha256(prefix.encode()).hexdigest() in _REACT_STREAM_BOOTSTRAP_HASHES
            if match and (recognized or not prefix and self.known_bootstrap):
                self.calls.append((match[2], match[3], self._offset()))
                self.known_bootstrap = True
            else:
                # An intervening unknown active script could redefine the helper.
                self.known_bootstrap = recognized and match is None
        if tag == "script":
            self.active_script = False
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index].tag == tag:
                element = self.stack[index]
                element.inner_end = self._offset()
                end = self.html.find(">", element.inner_end)
                element.end = end + 1 if end >= 0 else -1
                del self.stack[index:]
                break


def _restore_streamed_html(html: str) -> str:
    """Restore complete, disjoint B:/S: pairs; hidden unrelated elements stay hidden."""
    parser = _StreamParser(html)
    parser.feed(html)
    parser.close()
    if not parser.elements and not parser.calls:
        return html
    error = "coverage_incomplete:unresolved_streamed_content"
    called_ids = [identity for target, source, _ in parser.calls for identity in (target, source)]
    if (not parser.calls or parser.open_boundaries
            or len(set(called_ids)) != len(called_ids) or set(parser.elements) != set(called_ids)):
        raise ValueError(error)
    replacements: list[tuple[int, int, str]] = []
    for target, source, call_at in parser.calls:
        if len(parser.elements[target]) != 1 or len(parser.elements[source]) != 1:
            raise ValueError(error)
        template, container = parser.elements[target][0], parser.elements[source][0]
        boundaries = [(start, end) for start, inner, end in parser.boundaries
                      if inner <= template.start < template.end < end
                      and not html[inner:template.start].strip()]
        if (template.tag != "template" or template.end < 0 or template.inner_end != template.inner_start
                or container.tag != "div" or not container.hidden or container.end < 0 or len(boundaries) != 1
                or template.inert or container.inert or template.end > call_at or container.end > call_at):
            raise ValueError(error)
        start, end = boundaries[0]
        replacements.extend(((start, end, html[container.inner_start:container.inner_end]),
                             (container.start, container.end, "")))
    replacements.sort()
    if any(left[1] > right[0] for left, right in zip(replacements, replacements[1:], strict=False)):
        raise ValueError(error)
    for start, end, replacement in reversed(replacements):
        html = html[:start] + replacement + html[end:]
    return html


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

    def _append(self, text: str) -> None:
        active = self._active()
        for section in active:
            section.chunks.append(text)
            if section.kind == "main" and not any(
                child.kind == "article" and section.index in child.ancestors for child in active
            ):
                section.outside_articles.append(text)

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
        # A separately labelled generated-summary widget is not authored article evidence.
        skip_tag |= tag in {"details", "aside"} and {"ai", "summary"} <= tokens
        skipped = (bool(self.stack and self.stack[-1].skipped) or skip_tag
                   or "hidden" in values or values.get("aria-hidden", "").lower() == "true"
                   or "display:none" in style or "visibility:hidden" in style or bool(tokens & _SKIP_NAMES))
        if not skipped and tag in _BLOCK_TAGS:
            self._append("\n\n")
        decorative_svg = self._observe_coverage(tag, values, tokens, skipped, inside_article)
        if not skipped and tag in {"td", "th"}:
            self._append(" | ")
        if not skipped and tag == "img" and values.get("alt", "").strip():
            # Publisher-supplied text alternative, not our interpretation of pixels.
            self._append("\n\nImage description: " + values["alt"].strip() + "\n\n")
        captured = None
        if not skipped and (tag in {"article", "main", "body"} or footnotes):
            captured = _Section("footnotes" if footnotes else tag, index=len(self.sections),
                                ancestors=tuple(section.index for section in self._active()))
        if captured is not None:
            self.sections.append(captured)
        if tag not in _VOID_TAGS:
            ui_control = tag == "button" or values.get("role") == "button" or tag == "a" and (
                values.get("aria-label", "").lower().startswith("share ")
                or "button" in re.split(r"[\s/_-]+", values.get("class", ""))
            )
            self.stack.append(_Node(tag, skipped, captured, values.get("type") == "application/ld+json",
                                    tokens, ui_control, decorative_svg))
        if not skipped and values.get("id"):
            for active in self._active():
                active.ids.add(values["id"])
        if tag == "p" and not skipped:
            sections = self._active()
            for active in sections:
                active.paragraphs += 1
                if active.kind == "main" and not any(
                    child.kind == "article" and active.index in child.ancestors for child in sections
                ):
                    active.outside_paragraphs += 1

    def _observe_coverage(
        self, tag: str, values: dict[str, str], tokens: set[str], skipped: bool, inside_article: bool,
    ) -> bool:
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
        style = re.sub(r"\s+", "", values.get("style", "").lower())
        hidden |= "display:none" in style or "visibility:hidden" in style
        label = values.get("alt", "") + " " + values.get("aria-label", "") + " " + values.get("title", "")
        meaningful = bool(label.strip() or re.search(r"\b(chart|graph|diagram|table|benchmark|results)\b",
                                                    " ".join(tokens), re.I))
        small = all(_icon_dimension(values.get(dimension, "")) for dimension in ("width", "height"))
        decorative = tag == "svg" and small and not meaningful and (
            any({"author", "link"} <= node.tokens for node in self.stack)
            or any(node.ui_control for node in self.stack)
        )
        if not hidden and self._active():
            critical = tag in {"svg", "canvas", "video", "audio", "iframe", "object", "embed"} and not decorative
            critical |= not decorative and tag in {"img", "svg"} and bool(re.search(
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
        return decorative and not hidden

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
                self._append("\n\n")
            break

    def handle_data(self, data: str) -> None:
        if self.stack and self.stack[-1].jsonld and re.search(
            r'"isAccessibleForFree"\s*:\s*(false|"false")', data, re.I,
        ):
            self.paywall_detected = True
        if (any(node.tag in {"title", "text", "desc"} for node in self.stack)
                and any(node.decorative_svg for node in self.stack)
                and data.strip()):
            for section in self._active():
                section.issues.add("unread_critical_media")
        if not self.stack or self.stack[-1].skipped:
            return
        linked = any(node.tag == "a" for node in self.stack)
        self._append(data)
        for section in self._active():
            if linked:
                section.linked_chars += len(data.strip())


def _now() -> datetime:
    return datetime.now(UTC)


def _icon_dimension(value: str) -> bool:
    match = re.fullmatch(r"(\d+(?:\.\d+)?)(px|em)?", value)
    return bool(match and 0 < float(match[1]) <= (2 if match[2] == "em" else 32))


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _parse_date(value: str) -> datetime | None:
    try:
        return _utc(datetime.fromisoformat(value.strip().replace("Z", "+00:00")))
    except (ValueError, OverflowError):
        return None


def _normalized_text(section: _Section) -> str:
    return _normalize_chunks(section.chunks)


def _normalize_chunks(chunks: list[str]) -> str:
    return "\n\n".join(filter(None, (re.sub(r"\s+", " ", part).strip()
                                      for part in "".join(chunks).split("\n\n"))))


def extract_html(html: str) -> ExtractedArticle:
    parser = _ArticleParser()
    parser.feed(_restore_streamed_html(html))
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
    for _, _, section, _ in list(candidates):
        if section.kind != "main":
            continue
        cards = [item for item in candidates if item[2].kind == "article" and section.index in item[2].ancestors]
        outside = _normalize_chunks(section.outside_articles)
        # Multiple short linked teasers must not outrank substantial main prose.
        # A normal primary <article>, or an article listing, keeps the old rules.
        if (len(cards) >= 2 and section.outside_paragraphs >= 3 and len(outside) >= MIN_BODY_CHARS
                and len(outside) >= 4 * sum(len(item[3]) for item in cards)
                and all(item[2].linked_chars / max(1, len(item[3])) >= 0.2 for item in cards)):
            removed_ids = {identity for child in parser.sections
                           if child.kind == "article" and section.index in child.ancestors for identity in child.ids}
            if section.references & removed_ids:
                raise ValueError("coverage_incomplete:removed_footnote_target")
            candidates = [item for item in candidates if item[2] is not section and item not in cards]
            candidates.append((-1, -len(outside), section, outside))
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


async def fetch_article(url: str) -> FetchedArticle:
    """Fetch one complete article within one acquisition and extraction budget.

    Requests do not share clients or mutate process DNS, so callers may overlap.
    Synchronous extraction cannot be preempted, but its elapsed time is checked.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + FETCH_SECONDS
    async with asyncio.timeout(FETCH_SECONDS):
        try:
            response = await fetch_public(
                url, timeout=FETCH_SECONDS, max_bytes=MAX_HTML_BYTES,
                max_redirects=MAX_REDIRECTS, validate_hop=_url_syntax,
                headers={"User-Agent": USER_AGENT, "Accept": "text/html, application/xhtml+xml"},
            )
        except PublicFetchError as exc:
            if str(exc) == "oversized_body":
                raise ValueError("oversized_html") from exc
            raise
        response.raise_for_status()
        if response.status_code != 200 or "content-range" in response.headers:
            raise ValueError("partial_or_nonarticle_response")
        media_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if media_type not in {"text/html", "application/xhtml+xml"}:
            raise ValueError("unsupported_content_type")
        html = response.content.decode(response.encoding, errors="strict")
        extracted = extract_html(html)
        if loop.time() >= deadline:
            raise TimeoutError("Article acquisition and extraction timed out.")
        return FetchedArticle(
            extracted.text, response.url, _now().isoformat(), extracted.source_published,
            extracted.extraction_status, extracted.coverage_notes,
        )
