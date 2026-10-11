"""Checksum-bound progress and immutable, addressable public article snapshots."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from digest.article_source import FetchedArticle
from digest.radar.collector import Article, article_hash
from digest.reading_brief_tokens import ESTIMATOR_VERSION, GPT_HASH

VERSION = 1
PROMPT_VERSION = "source-passages-v4"
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


@dataclass(frozen=True)
class PageResult:
    covered_span_ids: tuple[int, ...]
    selected_span_ids: tuple[int, ...]
    qualification_span_ids: tuple[int, ...]
    reading_angle: str | None
    angle_span_ids: tuple[int, ...]
    abstain: bool


@dataclass(frozen=True)
class PageCompletion:
    response: str
    result: PageResult
    page_route_spelling: Literal["null", "explicit"]


@dataclass(frozen=True)
class RequestAttempt:
    """Durable adapter intent; an accepted generation owns its completed output."""

    kind: Literal["count", "generate"]
    route: Route
    start: int
    stop: int
    source_sha256: str
    request_sha256: str
    reserved_at: str
    status: Literal["reserved", "accepted", "definite_failed", "unknown"] = "reserved"
    finished_at: str | None = None
    error_class: str | None = None
    response_sha256: str = ""
    finish_reason: str | None = None
    usage: tuple[tuple[str, int], ...] = ()
    exact_count: int | None = None
    completion: PageCompletion | None = None


@dataclass(frozen=True)
class PendingRequest:
    """Prospective scheduling context, never proof of a supplying generation."""

    route: Route | None = None
    request_sha256: str = ""


@dataclass(frozen=True)
class LegacyCompletedEvidence:
    """History-0 output with no recorded attempt; no request lifecycle is invented."""

    source_sha256: str
    route: Route
    request_sha256: str
    response: str
    result: PageResult
    finish_reason: str | None
    usage: tuple[tuple[str, int], ...]
    page_route_spelling: Literal["null", "explicit"]


@dataclass
class Page:
    start: int
    stop: int
    work: PendingRequest | LegacyCompletedEvidence | None = field(default_factory=PendingRequest)
    request_attempts: list[RequestAttempt] = field(default_factory=list)
    request_history_version: int = 1
    legacy_count_request_sha256: str = ""


@dataclass(frozen=True)
class _CompletedPage:
    """Read-only access to one immutable owner, without copied completion facts."""

    owner: RequestAttempt | LegacyCompletedEvidence

    @property
    def result(self) -> PageResult:
        if isinstance(self.owner, LegacyCompletedEvidence):
            return self.owner.result
        assert self.owner.completion is not None
        return self.owner.completion.result

    @property
    def response(self) -> str:
        if isinstance(self.owner, LegacyCompletedEvidence):
            return self.owner.response
        assert self.owner.completion is not None
        return self.owner.completion.response

    @property
    def page_route_spelling(self) -> Literal["null", "explicit"]:
        if isinstance(self.owner, LegacyCompletedEvidence):
            return self.owner.page_route_spelling
        assert self.owner.completion is not None
        return self.owner.completion.page_route_spelling

    @property
    def route(self) -> Route:
        return self.owner.route

    @property
    def request_sha256(self) -> str:
        return self.owner.request_sha256

    @property
    def source_sha256(self) -> str:
        return self.owner.source_sha256

    @property
    def finish_reason(self) -> str | None:
        return self.owner.finish_reason

    @property
    def usage(self) -> tuple[tuple[str, int], ...]:
        return self.owner.usage

    @property
    def response_sha256(self) -> str:
        return _response_envelope(self.source_sha256, self.route, self.request_sha256,
                                  self.response, self.finish_reason, dict(self.usage))


def completed(page: Page) -> _CompletedPage | None:
    if isinstance(page.work, LegacyCompletedEvidence):
        if page.request_history_version != 0 or page.request_attempts:
            raise ValueError("result_attempt_binding_mismatch")
        return _CompletedPage(page.work)
    owners = [attempt for attempt in page.request_attempts if attempt.completion is not None]
    if not owners:
        if page.work is None:
            raise ValueError("result_attempt_binding_mismatch")
        return None
    generations = [attempt for attempt in page.request_attempts
                   if attempt.kind == "generate" and attempt.status != "definite_failed"]
    if (len(owners) != 1 or page.work is not None or len(generations) != 1
            or generations[0] is not owners[0] or owners[0].status != "accepted"
            or (owners[0].start, owners[0].stop) != (page.start, page.stop)):
        raise ValueError("result_attempt_binding_mismatch")
    owner = owners[0]
    assert owner.completion is not None
    if owner.response_sha256 != checksum(owner.completion.response):
        raise ValueError("result_attempt_binding_mismatch")
    return _CompletedPage(owner)


def _complete_page(
    page: Page, attempt_index: int, text: str, result: PageResult,
    page_route_spelling: Literal["null", "explicit"],
) -> None:
    attempt = page.request_attempts[attempt_index]
    if attempt.kind != "generate" or attempt.status != "accepted":
        raise ValueError("result_attempt_binding_mismatch")
    page.request_attempts[attempt_index] = replace(
        attempt, completion=PageCompletion(text, result, page_route_spelling),
    )
    page.work = None


def _response_envelope(
    source_sha256: str | None, route: Route, request_sha256: str,
    response: str | None, finish_reason: str | None, usage: dict[str, int],
) -> str:
    return checksum({"source": source_sha256, "route": asdict(route), "prompt": request_sha256,
                     "response": response, "finish_reason": finish_reason, "usage": usage})


def _result_wire(result: PageResult) -> dict[str, Any]:
    return {"covered_span_ids": list(result.covered_span_ids),
            "selected_span_ids": list(result.selected_span_ids),
            "qualification_span_ids": list(result.qualification_span_ids),
            "reading_angle": result.reading_angle, "angle_span_ids": list(result.angle_span_ids),
            "abstain": result.abstain}


def _attempt_wire(attempt: RequestAttempt) -> dict[str, Any]:
    return {"kind": attempt.kind, "route": asdict(attempt.route), "start": attempt.start, "stop": attempt.stop,
            "source_sha256": attempt.source_sha256, "request_sha256": attempt.request_sha256,
            "reserved_at": attempt.reserved_at, "status": attempt.status, "finished_at": attempt.finished_at,
            "error_class": attempt.error_class, "response_sha256": attempt.response_sha256,
            "finish_reason": attempt.finish_reason, "usage": dict(attempt.usage), "exact_count": attempt.exact_count}


def page_wire(state: BriefState, page: Page) -> dict[str, Any]:
    """Project the old wire contract at every persistence and identity boundary."""
    value = completed(page)
    pending = page.work if isinstance(page.work, PendingRequest) else None
    route = value.route if value and value.page_route_spelling == "explicit" else pending.route if pending else None
    return {"start": page.start, "stop": page.stop,
            "prompt_sha256": value.request_sha256 if value else pending.request_sha256 if pending else "",
            "result": _result_wire(value.result) if value else None,
            "response": value.response if value else None,
            "response_sha256": value.response_sha256 if value else "",
            "finish_reason": value.finish_reason if value else None,
            "usage": dict(value.usage) if value else {}, "route": asdict(route) if route else None,
            "request_attempts": [_attempt_wire(attempt) for attempt in page.request_attempts],
            "request_history_version": page.request_history_version,
            "legacy_count_request_sha256": page.legacy_count_request_sha256}


def state_wire(state: BriefState) -> dict[str, Any]:
    return {"selection": asdict(state.selection), "route": asdict(state.route), "created_at": state.created_at,
            "updated_at": state.updated_at, "status": state.status, "attempts": state.attempts,
            "error_class": state.error_class, "source_sha256": state.source_sha256,
            "pages": [page_wire(state, page) for page in state.pages], "exact_counts": dict(state.exact_counts),
            "delivered_at": state.delivered_at, "version": state.version,
            "admissions": {key: dict(value) for key, value in state.admissions.items()}}


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
    # Versioned estimates are never reinterpreted as exact tokenizer counts.
    admissions: dict[str, dict[str, int | str]] = field(default_factory=dict)


def _legacy_generation_unknown(state: BriefState, page: Page) -> bool:
    pending = page.work
    if (not isinstance(pending, PendingRequest) or page.request_history_version != 0
            or not pending.request_sha256 or page.request_attempts):
        return False
    route = pending.route or state.route
    if route.provider == "gemini":
        count = state.exact_counts.get(pending.request_sha256, route.input_tokens + 1)
    else:
        admission = state.admissions.get(pending.request_sha256, {})
        count = int(admission.get("input_estimate", route.input_tokens + 1))
    # The old writer persisted admission before generation, but not generation
    # intent. A successfully admitted unfinished page has an unrecorded outcome.
    return count <= route.input_tokens


def has_unresolved_generation(state: BriefState) -> bool:
    """A generation intent or accepted response cannot authorize another call."""
    return any(completed(page) is None and (_legacy_generation_unknown(state, page)
               or any(attempt.kind == "generate" and attempt.status != "definite_failed"
                      for attempt in page.request_attempts)) for page in state.pages)


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
    payload = state_wire(state)
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


def _validate_attempt(attempt: RequestAttempt, state: BriefState) -> None:
    if attempt.kind not in {"count", "generate"} or attempt.status not in {
        "reserved", "accepted", "definite_failed", "unknown",
    }:
        raise ValueError("invalid_request_attempt")
    if (type(attempt.start) is not int or type(attempt.stop) is not int
            or attempt.start < 0 or attempt.stop <= attempt.start
            or not isinstance(attempt.source_sha256, str) or attempt.source_sha256 != state.source_sha256
            or not isinstance(attempt.request_sha256, str) or not _HASH.fullmatch(attempt.request_sha256)):
        raise ValueError("invalid_request_binding")
    route = attempt.route
    if (route.prompt_version != PROMPT_VERSION or not isinstance(route.provider, str)
            or not isinstance(route.model, str) or type(route.input_tokens) is not int or route.input_tokens <= 0
            or type(route.max_output_tokens) is not int or route.max_output_tokens <= 0):
        raise ValueError("invalid_request_route")
    datetime.fromisoformat(attempt.reserved_at)
    if attempt.finished_at is not None:
        datetime.fromisoformat(attempt.finished_at)
    if (attempt.status == "reserved") != (attempt.finished_at is None):
        raise ValueError("invalid_request_completion")
    if (attempt.error_class is not None and not isinstance(attempt.error_class, str)
            or attempt.finish_reason is not None and not isinstance(attempt.finish_reason, str)
            or not isinstance(attempt.response_sha256, str)
            or attempt.response_sha256 and not _HASH.fullmatch(attempt.response_sha256)
            or attempt.exact_count is not None and (type(attempt.exact_count) is not int or attempt.exact_count <= 0)
            or not isinstance(attempt.usage, tuple)
            or not set(dict(attempt.usage)) <= {"prompt_tokens", "completion_tokens", "total_tokens"}
            or any(type(count) is not int or count < 0 for _, count in attempt.usage)):
        raise ValueError("invalid_request_metadata")


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
    if not isinstance(state.admissions, dict):
        raise ValueError("invalid_admission")
    for digest, record in state.admissions.items():
        if (not isinstance(digest, str) or not _HASH.fullmatch(digest)
                or not isinstance(record, dict)
                or set(record) != {"method", "tokenizer_sha256", "local_input_count", "input_estimate",
                                   "framing_reserve", "output_reserve", "request_allowance"}
                or record["method"] != ESTIMATOR_VERSION or record["tokenizer_sha256"] != GPT_HASH
                or any(type(value) is not int or value <= 0 for key, value in record.items()
                       if key not in {"method", "tokenizer_sha256"})):
            raise ValueError("invalid_admission")
    for page in state.pages:
        if type(page.request_history_version) is not int or page.request_history_version not in {0, 1}:
            raise ValueError("invalid_request_history_version")
        if page.legacy_count_request_sha256 and not _HASH.fullmatch(page.legacy_count_request_sha256):
            raise ValueError("invalid_legacy_count_request")
        for attempt in page.request_attempts:
            _validate_attempt(attempt, state)
        value = completed(page)
        pending = page.work if isinstance(page.work, PendingRequest) else None
        route = value.route if value else pending.route if pending else None
        if route is not None and (
            route.prompt_version != PROMPT_VERSION or type(route.input_tokens) is not int
            or route.input_tokens <= 0 or type(route.max_output_tokens) is not int or route.max_output_tokens <= 0
        ):
            raise ValueError("invalid_page_route")
        if type(page.start) is not int or type(page.stop) is not int or page.start < 0 or page.stop <= page.start:
            raise ValueError("invalid_page_range")
        prompt = value.request_sha256 if value else pending.request_sha256 if pending else ""
        if prompt and not _HASH.fullmatch(prompt):
            raise ValueError("invalid_page_prompt_digest")
        if value is not None:
            _validate_usage(dict(value.usage), "invalid_page_usage")
    if state.source_sha256 is None and (
        state.pages or state.exact_counts or state.admissions or state.status != "pending"
    ):
        raise ValueError("missing_source")


def _validate_usage(usage: Any, error: str) -> None:
    if (not isinstance(usage, dict) or not set(usage) <= {"prompt_tokens", "completion_tokens", "total_tokens"}
            or any(type(count) is not int or count < 0 for count in usage.values())):
        raise ValueError(error)


def _decode_result(raw: Any) -> PageResult:
    if not isinstance(raw, dict):
        raise ValueError("invalid_result_schema")
    ids = ("covered_span_ids", "selected_span_ids", "qualification_span_ids", "angle_span_ids")
    if any(not isinstance(raw.get(key), list) or any(type(item) is not int for item in raw[key]) for key in ids):
        raise ValueError("invalid_source_span_ids")
    if raw.get("reading_angle") is not None and not isinstance(raw["reading_angle"], str):
        raise ValueError("invalid_reading_angle")
    if type(raw.get("abstain")) is not bool:
        raise ValueError("invalid_abstention")
    return PageResult(**{**raw, **{key: tuple(raw[key]) for key in ids}})


def decode_page(raw: dict[str, Any], state: BriefState) -> Page:
    """Verify redundant old-wire evidence before retaining its single owner.

    This boundary reads no source, reconstructs no request and accepts no findings.
    Those checks still belong to source-loaded progress validation.
    """
    if not isinstance(raw, dict):
        raise ValueError("invalid_page")
    unknown = set(raw) - {"start", "stop", "prompt_sha256", "result", "response", "response_sha256",
                          "finish_reason", "usage", "route", "request_attempts", "request_history_version",
                          "legacy_count_request_sha256"}
    if unknown:
        raise TypeError("Unexpected reading page fields")
    attempts = []
    for record in raw.get("request_attempts", []):
        if not isinstance(record, dict):
            raise ValueError("invalid_request_attempt")
        _validate_usage(record.get("usage", {}), "invalid_request_metadata")
        if "completion" in record:
            raise TypeError("Unexpected request attempt fields")
        attempt = RequestAttempt(**{**record, "route": Route(**record["route"]),
                                     "usage": tuple(record.get("usage", {}).items())})
        _validate_attempt(attempt, state)
        attempts.append(attempt)
    raw_route = raw.get("route")
    if raw_route is not None and not isinstance(raw_route, dict):
        raise ValueError("invalid_page_route")
    route = Route(**raw_route) if raw_route is not None else None
    if route is not None and (not isinstance(route.provider, str) or not isinstance(route.model, str)
                              or not isinstance(route.prompt_version, str)
                              or type(route.input_tokens) is not int or type(route.max_output_tokens) is not int):
        raise ValueError("invalid_page_route")
    prompt = raw.get("prompt_sha256", "")
    if not isinstance(prompt, str):
        raise ValueError("invalid_page_prompt_digest")
    page = Page(raw["start"], raw["stop"], PendingRequest(route, prompt), attempts,
                raw.get("request_history_version", 1 if "request_attempts" in raw else 0),
                raw.get("legacy_count_request_sha256", ""))
    usage = raw.get("usage", {})
    _validate_usage(usage, "invalid_page_usage")
    if raw["result"] is None:
        if (raw.get("response") is not None or raw.get("response_sha256", "") != ""
                or raw.get("finish_reason") is not None or usage):
            raise ValueError("invalid_pending_completion")
        return page
    result = _decode_result(raw["result"])
    response, finish = raw.get("response"), raw.get("finish_reason")
    if (not isinstance(state.source_sha256, str)
            or finish is not None and not isinstance(finish, str)):
        raise ValueError("result_response_binding_mismatch")
    effective_route = route or state.route
    if (not isinstance(response, str) or raw.get("response_sha256", "") != _response_envelope(
        state.source_sha256, effective_route, prompt, response, finish, usage,
    )):
        raise ValueError("result_response_binding_mismatch")
    spelling: Literal["null", "explicit"] = "explicit" if route is not None else "null"
    if page.request_history_version == 0 and not attempts:
        page.work = LegacyCompletedEvidence(state.source_sha256, effective_route, prompt,
                                             response, result, finish, tuple(usage.items()), spelling)
        return page
    candidates = [index for index, attempt in enumerate(attempts)
                  if attempt.kind == "generate" and attempt.status != "definite_failed"]
    if len(candidates) != 1:
        raise ValueError("result_attempt_binding_mismatch")
    index = candidates[0]
    attempt = attempts[index]
    if (attempt.status != "accepted" or (attempt.start, attempt.stop) != (page.start, page.stop)
            or attempt.route != effective_route or attempt.request_sha256 != prompt
            or attempt.response_sha256 != checksum(response) or attempt.finish_reason != finish
            or dict(attempt.usage) != usage):
        raise ValueError("result_attempt_binding_mismatch")
    _complete_page(page, index, response, result, spelling)
    return page


def load_state(state_dir: Path, identity: str) -> BriefState:
    if not _ARTICLE_HASH.fullmatch(identity):
        raise ValueError("invalid_article_identity")
    envelope = _read(state_root(state_dir) / f"{identity}.json")
    payload = envelope["payload"]
    if checksum(payload) != envelope["sha256"]:
        raise ValueError("state_checksum_mismatch")
    state = BriefState(**{**payload, "selection": Selection(**payload["selection"]),
                          "route": Route(**payload["route"]), "pages": []})
    state.pages = [decode_page(page, state) for page in payload["pages"]]
    _validate_state(state)
    if state.selection.identity != identity:
        raise ValueError("state_selection_mismatch")
    return state
