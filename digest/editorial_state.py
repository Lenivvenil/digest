"""Versioned durable editorial progress; public-source bodies stay in runtime storage."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import types
from dataclasses import MISSING, asdict, dataclass, field, fields, is_dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, get_args, get_origin, get_type_hints
from urllib.parse import urlparse

from digest._util import atomic_json_write
from digest.config import Config
from digest.radar.collector import Article, article_hash
from digest.radar.summarizer import ArticleSummary

STATE_VERSION = 1
CHUNKING_VERSION = "complete-offsets-v1"
PROMPT_VERSION = "russian-source-ids-v5"
MAX_STATE_BYTES = 32 * 1024 * 1024
MAX_BODY_BYTES = 2 * 1024 * 1024
CHUNK_WEIGHT = 7500  # ASCII = 1, other characters = 3; complete coverage, not an article cap.
_HEX = re.compile(r"[0-9a-f]{64}")


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def content_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    index: int
    start: int
    end: int
    text_sha256: str


@dataclass(frozen=True)
class Span:
    chunk_id: str
    start: int
    end: int
    quote: str
    typography_normalized: bool = False
    offset_recovered: bool = False


@dataclass(frozen=True)
class Claim:
    claim_id: str
    kind: Literal["fact", "qualification", "source"]
    text: str
    spans: tuple[Span, ...] = ()
    supports: tuple[str, ...] = ()


@dataclass(frozen=True)
class AnalysisNode:
    node_id: str
    task_key: str
    stage: Literal["chunk", "reduce", "source", "collect"]
    input_node_ids: tuple[str, ...]
    chunk_ids: tuple[str, ...]
    claims: tuple[Claim, ...]
    empty_reason: str
    prompt_hash: str
    response_sha256: str
    generated_at: str
    usage: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class EditorialField:
    text: str
    claim_ids: tuple[str, ...]


@dataclass(frozen=True)
class FinalEditorial:
    decision: Literal["ready", "rejected"]
    root_node_id: str
    fact: EditorialField | None
    inference: EditorialField | None
    limitation: EditorialField | None
    why_read: EditorialField | None
    reason: str
    prompt_hash: str
    response_sha256: str
    generated_at: str
    usage: dict[str, int] = field(default_factory=dict)
    value_score: int = 0
    value_rationale: str = ""
    event_key: str = ""


@dataclass
class Attempt:
    attempt_id: str
    stage: str
    task_key: str
    prompt_hash: str
    started_at: str
    status: Literal["started", "success", "failed", "unknown"] = "started"
    error: str = ""
    retry_at: str | None = None
    usage: dict[str, int] = field(default_factory=dict)
    response_sha256: str | None = None
    rejected_output: str | None = None


@dataclass
class Generation:
    generation_id: str
    provider: str
    model: str
    body_sha256: str
    chunking_version: str = CHUNKING_VERSION
    prompt_version: str = PROMPT_VERSION
    nodes: dict[str, AnalysisNode] = field(default_factory=dict)
    final: FinalEditorial | None = None
    attempts: list[Attempt] = field(default_factory=list)
    blocked_until: str | None = None
    last_error: str = ""


@dataclass
class ArticleWork:
    article_id: str
    title: str
    url: str
    description: str
    source: str
    category: str
    published: str | None
    admitted_at: str
    body_sha256: str | None = None
    final_url: str | None = None
    fetched_at: str | None = None
    source_published: str | None = None
    extraction_status: str | None = None
    coverage_notes: tuple[str, ...] = ()
    chunks: tuple[Chunk, ...] = ()
    generations: dict[str, Generation] = field(default_factory=dict)
    acquisition_attempts: list[Attempt] = field(default_factory=list)
    acquisition_retry_at: str | None = None
    acquisition_error: str = ""
    delivery_state: Literal["pending", "reserved", "confirmed_failed", "unknown", "delivered"] = "pending"
    delivery_attempt_id: str | None = None

    def to_article(self) -> Article:
        published = datetime.fromisoformat(self.published) if self.published else None
        return Article(self.title, self.url, self.description, self.source, self.category, published)


@dataclass
class EditorialState:
    schema_version: int = STATE_VERSION
    articles: dict[str, ArticleWork] = field(default_factory=dict)
    order: list[str] = field(default_factory=list)
    cursor: int = 0
    provider_next_eligible: dict[str, str] = field(default_factory=dict)
    provider_unavailable_until: dict[str, str] = field(default_factory=dict)
    prefer_completion: bool = True


@dataclass(frozen=True)
class ReadyEditorialResult:
    article_id: str
    title: str
    url: str
    source: str
    category: str
    published: str | None
    provider: str
    model: str
    generation_id: str
    root_node_id: str
    body_sha256: str
    final_url: str
    fetched_at: str
    completed_chunks: int
    fact: EditorialField
    inference: EditorialField
    limitation: EditorialField
    why_read: EditorialField
    independent_complete: bool
    value_score: int
    value_rationale: str
    event_key: str
    admitted_at: str
    third_review_status: str = "awaiting_comparison"
    independent_disagreement: bool = False
    source_published: str | None = None

    def to_article_summary(self) -> ArticleSummary:
        peer = "независимый разбор завершён" if self.independent_complete else "независимый разбор не завершён"
        if self.independent_disagreement:
            third = {"pending": "дополнительный разбор ожидается", "complete": "дополнительный разбор завершён",
                     "not_configured": "дополнительная модель не настроена"}.get(self.third_review_status, "")
            peer += f"; решения об отборе различаются; {third}"
        publication = self.source_published or self.published
        date_label = publication.split("T", 1)[0] if publication else "дата не указана"
        text = (f"Факт из источника: {self.fact.text}\nОграничение: {self.limitation.text}\n"
                f"Вывод модели: {self.inference.text}\nЗачем читать: {self.why_read.text}\n"
                f"Опубликовано: {date_label}. Мнение {self.provider}/{self.model}; {peer}. "
                "Обработан весь сохранённый извлечённый текст; недоступные материалы не проверены.")
        return ArticleSummary(self.title, self.url, self.source, self.category, text)


def generation_id(body_sha256: str, provider: str, model: str, *,
                  chunking_version: str = CHUNKING_VERSION, prompt_version: str = PROMPT_VERSION) -> str:
    return content_hash([body_sha256, chunking_version, prompt_version, provider, model])


def make_chunks(body: str) -> tuple[Chunk, ...]:
    if not body:
        raise ValueError("Complete extracted body is empty.")
    body_sha = hashlib.sha256(body.encode()).hexdigest()
    result: list[Chunk] = []
    start = 0
    weight = 0
    for index, char in enumerate(body):
        unit = 1 if ord(char) < 128 else 3
        if weight + unit > CHUNK_WEIGHT and index > start:
            text_sha = hashlib.sha256(body[start:index].encode()).hexdigest()
            identity = content_hash([body_sha, CHUNKING_VERSION, len(result), start, index, text_sha])
            result.append(Chunk(identity, len(result), start, index, text_sha))
            start, weight = index, 0
        weight += unit
    text_sha = hashlib.sha256(body[start:].encode()).hexdigest()
    identity = content_hash([body_sha, CHUNKING_VERSION, len(result), start, len(body), text_sha])
    result.append(Chunk(identity, len(result), start, len(body), text_sha))
    return tuple(result)


def _safe_directory(state_dir: Path) -> Path:
    if state_dir.is_symlink():
        raise ValueError("Editorial state directory must not be a symlink.")
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir


def body_path(state_dir: Path, body_sha256: str) -> Path:
    if not _HEX.fullmatch(body_sha256):
        raise ValueError("Invalid editorial body identity.")
    directory = _safe_directory(state_dir) / "bodies"
    if directory.is_symlink():
        raise ValueError("Editorial body directory must not be a symlink.")
    directory.mkdir(exist_ok=True)
    path = directory / f"{body_sha256}.txt"
    if path.is_symlink():
        raise ValueError("Editorial body must not be a symlink.")
    return path


def save_body(state_dir: Path, body: str) -> str:
    data = body.encode()
    if not data or len(data) > MAX_BODY_BYTES:
        raise ValueError("Editorial body exceeds technical storage allowance.")
    digest = hashlib.sha256(data).hexdigest()
    path = body_path(state_dir, digest)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".body-", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.read_bytes() != data:
                raise ValueError("Immutable editorial body changed.") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return digest


def read_body(state_dir: Path, body_sha256: str) -> str:
    path = body_path(state_dir, body_sha256)
    if path.stat().st_size > MAX_BODY_BYTES:
        raise ValueError("Editorial body exceeds technical storage allowance.")
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != body_sha256:
        raise ValueError("Editorial body hash mismatch.")
    return data.decode("utf-8")


def admit_articles(state: EditorialState, articles: list[Article]) -> int:
    if state.order and any(article_hash(article.title, article.link) not in state.articles for article in articles):
        cursor = state.cursor % len(state.order)
        state.order = state.order[cursor:] + state.order[:cursor]
        state.cursor = 0
    admitted = 0
    for article in articles:
        identity = article_hash(article.title, article.link)
        if identity in state.articles:
            continue
        state.articles[identity] = ArticleWork(
            identity, article.title, article.link, article.description, article.source, article.category,
            article.pub_date.isoformat() if article.pub_date else None, utc_now(),
        )
        state.order.append(identity)
        admitted += 1
    return admitted


def _decode(value: Any, expected: Any, depth: int = 0) -> Any:
    """Strict bounded dataclass decoding; never execute serialized names or paths."""
    if depth > 30:
        raise ValueError("Editorial state nesting exceeds safety allowance.")
    origin, args = get_origin(expected), get_args(expected)
    if origin is types.UnionType:
        for option in args:
            try:
                return _decode(value, option, depth + 1)
            except ValueError:
                continue
        raise ValueError("Invalid optional editorial field.")
    if origin is Literal:
        if value not in args or type(value) not in {type(item) for item in args}:
            raise ValueError("Unknown editorial state value.")
        return value
    if origin in {list, tuple}:
        if not isinstance(value, list):
            raise ValueError("Expected editorial sequence.")
        decoded = [_decode(item, args[0], depth + 1) for item in value]
        return tuple(decoded) if origin is tuple else decoded
    if origin is dict:
        if not isinstance(value, dict):
            raise ValueError("Expected editorial mapping.")
        return {_decode(key, args[0], depth + 1): _decode(item, args[1], depth + 1) for key, item in value.items()}
    if is_dataclass(expected):
        if not isinstance(value, dict) or set(value) != {item.name for item in fields(expected)}:
            raise ValueError("Unknown editorial dataclass fields.")
        hints = get_type_hints(expected)
        values = {}
        for item in fields(expected):
            if item.name in value:
                values[item.name] = _decode(value[item.name], hints[item.name], depth + 1)
            elif item.default is MISSING and item.default_factory is MISSING:
                raise ValueError("Missing editorial state field.")
        constructor: Any = expected
        return constructor(**values)
    if type(value) is not expected:
        raise ValueError("Invalid editorial field type.")
    return value


def node_hash(node: AnalysisNode) -> str:
    payload = asdict(node)
    payload.pop("node_id")
    return content_hash(payload)


def _validate_task_keys(generation: Generation) -> None:
    for node in generation.nodes.values():
        source: str | list[str] = (node.chunk_ids[0] if node.stage == "chunk" else
                                    list(node.chunk_ids) if node.stage == "source" else list(node.input_node_ids))
        expected = content_hash([generation.generation_id, node.stage, source])
        if node.task_key != expected:
            raise ValueError("Editorial task is not bound to its analysis generation.")


def _validate_generation(article: ArticleWork, generation: Generation, body: str) -> None:
    expected = generation_id(generation.body_sha256, generation.provider, generation.model,
                             chunking_version=generation.chunking_version, prompt_version=generation.prompt_version)
    if generation.generation_id != expected:
        raise ValueError("Editorial generation identity mismatch.")
    generation_chunks = make_chunks(body)
    chunks = {chunk.chunk_id: chunk for chunk in generation_chunks}
    nodes = {node.node_id: node for node in generation.nodes.values()}
    for task_key, node in generation.nodes.items():
        if (task_key != node.task_key or node_hash(node) != node.node_id or not node.chunk_ids
                or len(set(node.chunk_ids)) != len(node.chunk_ids)):
            raise ValueError("Editorial node identity mismatch.")
        if any(identity not in chunks for identity in node.chunk_ids):
            raise ValueError("Editorial node has unknown source coverage.")
        if node.stage in {"chunk", "source"}:
            if node.input_node_ids or (node.stage == "chunk" and len(node.chunk_ids) != 1):
                raise ValueError("Invalid chunk node lineage.")
        else:
            if ((len(node.input_node_ids) != 2 if node.stage == "reduce" else len(node.input_node_ids) < 2)
                    or len(set(node.input_node_ids)) != len(node.input_node_ids)
                    or any(identity not in nodes for identity in node.input_node_ids)):
                raise ValueError("Invalid reduction node lineage.")
            children = [nodes[identity] for identity in node.input_node_ids]
            if tuple(identity for child in children for identity in child.chunk_ids) != node.chunk_ids:
                raise ValueError("Reduction omitted or reordered source coverage.")
        if any((claim.kind == "source") != (node.stage == "source") for claim in node.claims):
            raise ValueError("Raw source spans cannot masquerade as classified model findings.")
        if len({claim.claim_id for claim in node.claims}) != len(node.claims):
            raise ValueError("Duplicate editorial claim identities.")
        for claim in node.claims:
            if not claim.claim_id.startswith(task_key + ":") or (not claim.text.strip() and node.stage != "source"):
                raise ValueError("Invalid editorial claim identity.")
            for span in claim.spans:
                chunk = chunks.get(span.chunk_id)
                if (chunk is None or span.chunk_id not in node.chunk_ids or not span.quote
                        or not chunk.start <= span.start < span.end <= chunk.end
                        or body[span.start:span.end] != span.quote):
                    raise ValueError("Editorial claim does not match its exact source span.")
            if node.stage in {"chunk", "source"} and (not claim.spans or claim.supports):
                raise ValueError("Chunk claims require source spans only.")
        if node.stage in {"reduce", "collect"}:
            child_claims = {claim.claim_id: claim for identity in node.input_node_ids
                            for claim in nodes[identity].claims}
            covered = [identity for claim in node.claims for identity in claim.supports]
            if set(covered) != set(child_claims) or len(covered) != len(set(covered)):
                raise ValueError("Reduction omitted or duplicated child claims.")
            for claim in node.claims:
                if claim.spans or any(identity not in child_claims for identity in claim.supports):
                    raise ValueError("Invalid reduction claim support.")
                if any(child_claims[identity].kind == "qualification" for identity in claim.supports):
                    if claim.kind != "qualification":
                        raise ValueError("Reduction converted qualification into a fact.")
    _validate_final(generation.final, nodes, generation_chunks)
    _validate_task_keys(generation)
    from digest.editorial_worker import validate_cached_prompts

    validate_cached_prompts(article, generation, body)


def _validate_final(
    final: FinalEditorial | None, nodes: dict[str, AnalysisNode], generation_chunks: tuple[Chunk, ...],
) -> None:
    if final is not None:
        root = nodes.get(final.root_node_id)
        if root is None or root.chunk_ids != tuple(chunk.chunk_id for chunk in generation_chunks):
            raise ValueError("Final editorial decision lacks complete source coverage.")
        claims = {claim.claim_id: claim for claim in root.claims}
        if not 0 <= final.value_score <= 10:
            raise ValueError("Invalid final editorial value.")
        if final.decision == "ready":
            for item in (final.fact, final.inference, final.limitation, final.why_read):
                if item is None or not item.claim_ids or any(identity not in claims for identity in item.claim_ids):
                    raise ValueError("Final editorial field lacks grounded claim references.")
            assert final.fact and final.limitation
            if any(claims[key].kind not in {"fact", "source"} for key in final.fact.claim_ids):
                raise ValueError("Final source fact improperly cites a qualification.")
            qualifications = {key for key, claim in claims.items() if claim.kind == "qualification"}
            if not qualifications <= set(final.limitation.claim_ids):
                raise ValueError("Final limitation lost a source qualification.")
        elif not final.reason.strip():
            raise ValueError("Editorial rejection requires a reason.")


def validate_state(state: EditorialState, state_dir: Path) -> None:
    if (state.schema_version != STATE_VERSION or len(set(state.order)) != len(state.order)
            or set(state.order) != set(state.articles) or state.cursor < 0):
        raise ValueError("Invalid editorial state index.")
    for identity, article in state.articles.items():
        if identity != article.article_id or article_hash(article.title, article.url) != identity:
            raise ValueError("Editorial article identity mismatch.")
        if article.body_sha256 is None:
            if article.chunks or article.generations:
                raise ValueError("Unacquired article contains analysis.")
            continue
        if (article.final_url is None or urlparse(article.final_url).scheme not in {"http", "https"}
                or not urlparse(article.final_url).netloc or article.fetched_at is None
                or article.extraction_status not in {"article", "main", "body"}):
            raise ValueError("Acquired article lacks complete-source provenance.")
        try:
            if datetime.fromisoformat(article.fetched_at).tzinfo is None:
                raise ValueError("Acquisition timestamp requires timezone.")
        except ValueError as exc:
            raise ValueError("Invalid acquisition timestamp.") from exc
        body = read_body(state_dir, article.body_sha256)
        if make_chunks(body) != article.chunks:
            raise ValueError("Editorial chunk manifest is incomplete or changed.")
        for key, generation in article.generations.items():
            if key != generation.generation_id:
                raise ValueError("Editorial generation mapping mismatch.")
            generation_body = body if generation.body_sha256 == article.body_sha256 else read_body(
                state_dir, generation.body_sha256,
            )
            _validate_generation(article, generation, generation_body)


def load_state(state_dir: Path) -> EditorialState:
    path = _safe_directory(state_dir) / "state.json"
    if path.is_symlink():
        raise ValueError("Editorial state file must not be a symlink.")
    if not path.exists():
        return EditorialState()
    if path.stat().st_size > MAX_STATE_BYTES:
        raise ValueError("Editorial state exceeds technical storage allowance; no work was discarded.")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or not {"schema_version", "articles", "order", "cursor"} <= set(raw):
            raise ValueError("Incomplete persisted editorial state envelope.")
        state: EditorialState = _decode(raw, EditorialState)
        validate_state(state, state_dir)
        return state
    except (TypeError, KeyError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("Invalid editorial state document.") from exc


def store_state(state: EditorialState, state_dir: Path) -> None:
    directory = _safe_directory(state_dir)
    validate_state(state, state_dir)
    payload = asdict(state)
    if len(json.dumps(payload, indent=2).encode()) > MAX_STATE_BYTES:
        raise ValueError("Editorial state exceeds technical storage allowance; no work was discarded.")
    path = directory / "state.json"
    if path.is_symlink() or path.with_suffix(".json.tmp").is_symlink():
        raise ValueError("Editorial state output must not be a symlink.")
    atomic_json_write(path, payload)


def current_generation(article: ArticleWork, provider: str, model: str) -> Generation | None:
    if article.body_sha256 is None:
        return None
    return article.generations.get(generation_id(article.body_sha256, provider, model))


def chosen_generation(article: ArticleWork, config: Config) -> Generation | None:
    primary = current_generation(article, config.review.primary.provider, config.review.primary.model)
    secondary = current_generation(article, config.review.secondary.provider, config.review.secondary.model)
    if primary is not None and primary.final is not None:
        return primary
    if primary is not None and primary.last_error and secondary is not None and secondary.final is not None:
        return secondary
    return None


def independent_status(article: ArticleWork, config: Config) -> tuple[bool, str, bool]:
    primary = current_generation(article, config.review.primary.provider, config.review.primary.model)
    secondary = current_generation(article, config.review.secondary.provider, config.review.secondary.model)
    if primary is None or secondary is None or primary.final is None or secondary.final is None:
        return False, "awaiting_comparison", False
    disagreement = primary.final.decision != secondary.final.decision
    if not disagreement:
        return True, "not_required", False
    if config.review.tie_breaker is None:
        return True, "not_configured", True
    model = config.review.tie_breaker
    third = current_generation(article, model.provider, model.model)
    complete = third is not None and third.final is not None
    return complete, "complete" if complete else "pending", True


def ready_results(state: EditorialState, config: Config) -> list[ReadyEditorialResult]:
    result = []
    for identity in state.order:
        article = state.articles[identity]
        if article.delivery_state not in {"pending", "confirmed_failed"}:
            continue
        generation = chosen_generation(article, config)
        if generation is None or generation.final is None or generation.final.decision != "ready":
            continue
        _validate_task_keys(generation)
        _validate_final(generation.final, {node.node_id: node for node in generation.nodes.values()}, article.chunks)
        from digest.editorial_worker import validate_cached_final

        validate_cached_final(article, generation)
        final = generation.final
        if not all((final.fact, final.inference, final.limitation, final.why_read)):
            continue
        assert final.fact and final.inference and final.limitation and final.why_read
        assert article.body_sha256 and article.final_url and article.fetched_at
        independent_complete, third_status, disagreement = independent_status(article, config)
        result.append(ReadyEditorialResult(
            identity, article.title, article.url, article.source, article.category, article.published,
            generation.provider, generation.model, generation.generation_id, final.root_node_id,
            article.body_sha256, article.final_url, article.fetched_at, len(article.chunks),
            final.fact, final.inference, final.limitation, final.why_read,
            independent_complete,
            final.value_score, final.value_rationale, final.event_key, article.admitted_at,
            third_status, disagreement, article.source_published,
        ))
    return result


def resolve_claim_spans(generation: Generation, claim_id: str) -> tuple[Span, ...]:
    claims = {claim.claim_id: claim for node in generation.nodes.values() for claim in node.claims}
    pending, seen = [claim_id], set()
    spans: list[Span] = []
    while pending:
        identity = pending.pop()
        if identity in seen:
            continue
        seen.add(identity)
        if identity not in claims:
            raise ValueError("Unknown editorial claim reference.")
        claim = claims[identity]
        spans.extend(claim.spans)
        pending.extend(reversed(claim.supports))
    return tuple(dict.fromkeys(spans))
