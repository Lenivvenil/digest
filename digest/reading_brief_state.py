"""Checksum-bound progress and immutable, addressable public article snapshots."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from digest.article_source import FetchedArticle
from digest.radar.collector import Article, article_hash

VERSION = 1
PROMPT_VERSION = "source-passages-v3"
_HASH = re.compile(r"[0-9a-f]{64}")
_ARTICLE_HASH = re.compile(r"[0-9a-f]{32}")


def checksum(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class Selection:
    title: str
    link: str
    source: str
    category: str
    pub_date: str | None
    description: str = ""

    @classmethod
    def from_article(cls, article: Article) -> Selection:
        return cls(article.title, article.link, article.source, article.category,
                   article.pub_date.isoformat() if article.pub_date else None, article.description)

    def article(self) -> Article:
        return Article(self.title, self.link, self.description, self.source, self.category,
                       datetime.fromisoformat(self.pub_date) if self.pub_date else None)

    @property
    def identity(self) -> str:
        return article_hash(self.title, self.link)


@dataclass(frozen=True)
class Route:
    provider: str
    model: str
    input_tokens: int
    max_output_tokens: int
    prompt_version: str = PROMPT_VERSION


@dataclass(frozen=True)
class Span:
    id: int
    start: int
    end: int


@dataclass(frozen=True)
class Source:
    selection: Selection
    text: str
    body_sha256: str
    final_url: str
    fetched_at: str
    source_published: str | None
    extraction_status: str
    coverage_notes: list[str]
    spans: list[Span]


@dataclass
class PageResult:
    covered_span_ids: list[int]
    selected_span_ids: list[int]
    qualification_span_ids: list[int]
    reading_angle: str | None
    angle_span_ids: list[int]
    abstain: bool


@dataclass
class Page:
    start: int
    stop: int
    prompt_sha256: str = ""
    result: PageResult | None = None
    response: str | None = None
    response_sha256: str = ""
    finish_reason: str | None = None
    usage: dict[str, int] = field(default_factory=dict)


@dataclass
class BriefState:
    selection: Selection
    route: Route
    created_at: str
    updated_at: str
    status: str = "pending"
    attempts: int = 0  # Processing attempts; HTTP request accounting is shared in llm.py.
    error_class: str | None = None
    source_sha256: str | None = None
    pages: list[Page] = field(default_factory=list)
    exact_counts: dict[str, int] = field(default_factory=dict)
    delivered_at: str | None = None
    version: int = VERSION


def state_root(state_dir: Path) -> Path:
    root = state_dir / "reading_briefs"
    for path in (root, *root.parents):
        if path.is_symlink():
            raise ValueError("unsafe_state_path")
    root.mkdir(parents=True, exist_ok=True)
    return root


def _write(path: Path, value: Any) -> None:
    if path.is_symlink():
        raise ValueError("unsafe_state_path")
    handle, temporary = tempfile.mkstemp(prefix=".brief-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _read(path: Path) -> Any:
    if path.is_symlink() or not path.is_file():
        raise ValueError("unsafe_or_missing_state_file")
    with path.open(encoding="utf-8") as stream:
        return json.load(stream)


def save_state(state_dir: Path, state: BriefState) -> None:
    state.updated_at = now()
    payload = asdict(state)
    _write(state_root(state_dir) / f"{state.selection.identity}.json",
           {"payload": payload, "sha256": checksum(payload)})


def make_spans(text: str) -> list[Span]:
    """Give all characters stable addresses; the full manifest is sent first.

    Paragraph boundaries are preferred. Very long paragraphs get consecutive
    addresses, never an admission window or a shortened source body.
    """
    spans: list[Span] = []
    start = 0
    for paragraph in re.finditer(r"[^\n]+(?:\n+|$)|\n+", text):
        end = paragraph.end()
        while end - start > 2048:
            boundary = text.rfind(" ", start + 1024, start + 2048)
            stop = boundary + 1 if boundary >= 0 else start + 2048
            spans.append(Span(len(spans) + 1, start, stop))
            start = stop
        if start < end:
            spans.append(Span(len(spans) + 1, start, end))
            start = end
    return spans


def save_source(state_dir: Path, selection: Selection, fetched: FetchedArticle) -> tuple[str, Source]:
    source = Source(selection, fetched.text, hashlib.sha256(fetched.text.encode()).hexdigest(),
                    fetched.final_url, fetched.fetched_at, fetched.source_published,
                    fetched.extraction_status, list(fetched.coverage_notes), make_spans(fetched.text))
    _validate_source(source)
    payload = asdict(source)
    digest = checksum(payload)
    root = state_root(state_dir) / "sources"
    if root.is_symlink():
        raise ValueError("unsafe_state_path")
    root.mkdir(exist_ok=True)
    target = root / f"{digest}.json"
    if target.exists():
        if checksum(_read(target)) != digest:
            raise ValueError("source_checksum_mismatch")
    else:
        _write(target, payload)
    return digest, source


def _validate_selection(selection: Selection) -> None:
    if not all(isinstance(value, str) and value.strip()
               for value in (selection.title, selection.link, selection.source, selection.category)):
        raise ValueError("invalid_selection")
    if not isinstance(selection.description, str):
        raise ValueError("invalid_selection_description")
    if selection.pub_date is not None:
        if not isinstance(selection.pub_date, str):
            raise ValueError("invalid_selection_date")
        datetime.fromisoformat(selection.pub_date)


def _validate_source(source: Source) -> None:
    _validate_selection(source.selection)
    if not isinstance(source.text, str) or not source.text.strip():
        raise ValueError("empty_source")
    if hashlib.sha256(source.text.encode()).hexdigest() != source.body_sha256:
        raise ValueError("source_body_checksum_mismatch")
    if source.extraction_status not in {"article", "main", "body"}:
        raise ValueError("source_coverage_incomplete")
    if source.spans != make_spans(source.text):
        raise ValueError("source_span_manifest_mismatch")
    if not isinstance(source.final_url, str) or not source.final_url:
        raise ValueError("invalid_source_url")
    datetime.fromisoformat(source.fetched_at)
    if source.source_published is not None:
        datetime.fromisoformat(source.source_published)
    if not isinstance(source.coverage_notes, list) or any(not isinstance(note, str) for note in source.coverage_notes):
        raise ValueError("invalid_source_caveats")


def load_source(state_dir: Path, state: BriefState) -> Source:
    digest = state.source_sha256
    if not isinstance(digest, str) or not _HASH.fullmatch(digest):
        raise ValueError("invalid_source_digest")
    root = state_root(state_dir) / "sources"
    if root.is_symlink():
        raise ValueError("unsafe_state_path")
    payload = _read(root / f"{digest}.json")
    if checksum(payload) != digest:
        raise ValueError("source_checksum_mismatch")
    source = Source(**{**payload, "selection": Selection(**payload["selection"]),
                       "spans": [Span(**span) for span in payload["spans"]]})
    _validate_source(source)
    if source.selection != state.selection:
        raise ValueError("source_selection_mismatch")
    return source


def _validate_state(state: BriefState) -> None:
    _validate_selection(state.selection)
    if state.version != VERSION or state.status not in {"pending", "ready", "abstained", "delivered"}:
        raise ValueError("invalid_state_status")
    if type(state.attempts) is not int or state.attempts < 0:
        raise ValueError("invalid_attempt_count")
    for date in (state.created_at, state.updated_at):
        datetime.fromisoformat(date)
    if state.delivered_at is not None:
        datetime.fromisoformat(state.delivered_at)
    if state.status == "delivered" and state.delivered_at is None:
        raise ValueError("missing_delivery_acknowledgment")
    if state.error_class is not None and not isinstance(state.error_class, str):
        raise ValueError("invalid_error_class")
    if (state.route.prompt_version != PROMPT_VERSION or not isinstance(state.route.provider, str)
            or not isinstance(state.route.model, str)
            or type(state.route.input_tokens) is not int or state.route.input_tokens <= 0
            or type(state.route.max_output_tokens) is not int or state.route.max_output_tokens <= 0):
        raise ValueError("invalid_route")
    if not isinstance(state.exact_counts, dict) or any(
        not _HASH.fullmatch(key) or type(count) is not int or count <= 0 for key, count in state.exact_counts.items()
    ):
        raise ValueError("invalid_exact_count")
    for page in state.pages:
        if type(page.start) is not int or type(page.stop) is not int or page.start < 0 or page.stop <= page.start:
            raise ValueError("invalid_page_range")
        if page.prompt_sha256 and not _HASH.fullmatch(page.prompt_sha256):
            raise ValueError("invalid_page_prompt_digest")
        allowed_usage = {"prompt_tokens", "completion_tokens", "total_tokens"}
        if (not isinstance(page.usage, dict) or not set(page.usage) <= allowed_usage
                or any(type(count) is not int or count < 0 for count in page.usage.values())):
            raise ValueError("invalid_page_usage")
    if state.source_sha256 is None and (state.pages or state.exact_counts or state.status != "pending"):
        raise ValueError("missing_source")


def load_state(state_dir: Path, identity: str) -> BriefState:
    if not _ARTICLE_HASH.fullmatch(identity):
        raise ValueError("invalid_article_identity")
    envelope = _read(state_root(state_dir) / f"{identity}.json")
    payload = envelope["payload"]
    if checksum(payload) != envelope["sha256"]:
        raise ValueError("state_checksum_mismatch")
    pages = [Page(**{**page, "result": PageResult(**page["result"]) if page["result"] is not None else None})
             for page in payload["pages"]]
    state = BriefState(**{**payload, "selection": Selection(**payload["selection"]),
                          "route": Route(**payload["route"]), "pages": pages})
    _validate_state(state)
    if state.selection.identity != identity:
        raise ValueError("state_selection_mismatch")
    return state
