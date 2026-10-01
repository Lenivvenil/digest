"""Offline contracts for immutable source evidence and fail-closed editorial state."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import FrozenInstanceError, asdict, replace
from pathlib import Path
from typing import Any

import pytest

from digest import editorial_state as editorial
from digest.editorial_state import (
    AnalysisNode,
    ArticleWork,
    Attempt,
    EditorialField,
    EditorialState,
    Generation,
)
from digest.radar.collector import Article, article_hash
from scripts.review_fixture import fixture_config
from tests.factories import make_article

NOW = "2026-10-01T03:00:00+00:00"
BODY = "Opening claim. " + "evidence " * 1000 + "Final footnote limits the initial claim."


@pytest.fixture(autouse=True)
def no_live_http(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("Editorial state tests must not make live HTTP calls")

    monkeypatch.setattr("httpx.AsyncClient", forbidden)
    monkeypatch.setattr("httpx.Client", forbidden)


def _write_payload(state_dir: Path, payload: Any) -> Path:
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / "state.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _acquired_state(state_dir: Path, body: str = BODY) -> tuple[EditorialState, ArticleWork]:
    state = EditorialState()
    article = make_article(title="Оригинальный заголовок", description="RSS is not source evidence.")
    editorial.admit_articles(state, [article])
    work = state.articles[state.order[0]]
    work.body_sha256 = editorial.save_body(state_dir, body)
    work.chunks = editorial.make_chunks(body)
    work.final_url = "https://example.com/canonical"
    work.fetched_at = NOW
    work.source_published = "2026-09-30T12:00:00+00:00"
    work.extraction_status = "article"
    work.coverage_notes = ("Complete normalized extracted HTML text.",)
    return state, work


def _hashed_node(node: AnalysisNode) -> AnalysisNode:
    return replace(node, node_id=editorial.node_hash(node))


def _ready_state(state_dir: Path, *, prompt_version: str = editorial.PROMPT_VERSION
                 ) -> tuple[EditorialState, ArticleWork, Generation]:
    from digest.editorial_worker import next_task, parse_final, parse_node

    state, work = _acquired_state(state_dir)
    assert work.body_sha256 is not None
    config = fixture_config()
    provider, model = config.review.primary.provider, config.review.primary.model
    identity = editorial.generation_id(work.body_sha256, provider, model, prompt_version=prompt_version)
    generation = Generation(identity, provider, model, work.body_sha256, prompt_version=prompt_version)
    while True:
        task = next_task(work, generation, BODY)
        assert task is not None
        if task.stage == "final":
            break
        if task.stage == "chunk":
            assert task.chunk is not None
            first = task.chunk.index == 0
            raw = {"claims": [{
                "kind": "fact" if first else "qualification",
                "text": "Источник сообщает начальное утверждение о результатах испытания" if first else
                        "Заключительная оговорка ограничивает применимость исходного утверждения",
                "quote": "Opening claim." if first else "Final footnote limits the initial claim.",
            }], "empty_reason": ""}
        else:
            raw = {"claims": [{"kind": child.claims[0].kind, "text": child.claims[0].text,
                               "supports": [child.claims[0].claim_id]} for child in task.children],
                   "empty_reason": ""}
        node = parse_node(task, json.dumps(raw), BODY, {"prompt_tokens": 17, "completion_tokens": 11})
        generation.nodes[task.task_key] = node
    root = task.children[0]
    fact_id, limit_id = (claim.claim_id for claim in root.claims)
    raw_final = {
        "decision": "ready", "reason": "", "value_score": 8,
        "value_rationale": "Источник содержит проверяемое изменение",
        "event_key": "Изменение результатов испытания",
        "fact": {"text": "Источник сообщает о конкретном изменении результатов испытания",
                 "claim_ids": [fact_id]},
        "inference": {"text": "Это может изменить подход к следующей проверке технологии",
                      "claim_ids": [fact_id]},
        "limitation": {"text": "Последняя сноска ограничивает область применимости исходного утверждения",
                       "claim_ids": [limit_id]},
        "why_read": {"text": "В оригинале полезно изучить условия испытания и заключительную оговорку",
                     "claim_ids": [fact_id, limit_id]},
    }
    generation.final = parse_final(task, json.dumps(raw_final), {"prompt_tokens": 23, "completion_tokens": 19})
    work.generations[identity] = generation
    return state, work, generation


def _first_leaf(generation: Generation) -> AnalysisNode:
    return next(node for node in generation.nodes.values() if node.stage == "chunk")


def _root_node(generation: Generation) -> AnalysisNode:
    return next(node for node in generation.nodes.values() if node.stage == "reduce")


def test_missing_state_starts_empty_without_fabricating_checkpoint(tmp_path: Path) -> None:
    state_dir = tmp_path / "new-state"
    assert editorial.load_state(state_dir) == EditorialState()
    assert not (state_dir / "state.json").exists()


def test_empty_state_roundtrips(tmp_path: Path) -> None:
    editorial.store_state(EditorialState(), tmp_path)
    assert editorial.load_state(tmp_path) == EditorialState()


def test_admission_preserves_original_metadata_and_has_no_slot_quota(tmp_path: Path) -> None:
    articles = [
        make_article(title=f"Материал {index}", link=f"https://example.com/{index}",
                     description="Original <p>RSS text</p> " * 100, source="Исходная лента", category="Банки")
        for index in range(45)
    ]
    articles.append(Article("Undated", "https://example.com/undated", "Original", "Feed", "Tech", None))
    state = EditorialState()
    assert editorial.admit_articles(state, articles) == 46
    state.cursor = 17
    state.provider_next_eligible = {"gemini": NOW}
    editorial.store_state(state, tmp_path)
    loaded = editorial.load_state(tmp_path)
    assert loaded == state
    assert [loaded.articles[identity].to_article() for identity in loaded.order] == articles
    assert all(loaded.articles[identity].admitted_at for identity in loaded.order)
    assert len(loaded.order) == len(set(loaded.order)) == 46


def test_rediscovery_does_not_overwrite_original_or_reset_delivery(tmp_path: Path) -> None:
    state = EditorialState()
    article = make_article()
    assert editorial.admit_articles(state, [article, article]) == 1
    work = state.articles[article_hash(article.title, article.link)]
    work.delivery_state = "unknown"
    work.delivery_attempt_id = "attempt-original"
    original = deepcopy(work)
    changed_feed = replace(article, description="Later changed description", source="Different feed")
    assert editorial.admit_articles(state, [changed_feed]) == 0
    assert state.articles[work.article_id] == original
    editorial.store_state(state, tmp_path)
    assert editorial.load_state(tmp_path).articles[work.article_id] == original


@pytest.mark.parametrize("payload", [
    None, [], {}, {"schema_version": 1},
    {"schema_version": 999, "articles": {}, "order": [], "cursor": 0, "provider_next_eligible": {}},
    {"schema_version": True, "articles": {}, "order": [], "cursor": 0, "provider_next_eligible": {}},
    {"schema_version": 1, "articles": [], "order": [], "cursor": 0, "provider_next_eligible": {}},
    {"schema_version": 1, "articles": {}, "order": [], "cursor": True, "provider_next_eligible": {}},
    {"schema_version": 1, "articles": {}, "order": [], "cursor": -1, "provider_next_eligible": {}},
    {"schema_version": 1, "articles": {}, "order": ["missing"], "cursor": 0, "provider_next_eligible": {}},
    {"schema_version": 1, "articles": {}, "order": [], "cursor": 0, "provider_next_eligible": {}, "extra": "field"},
])
def test_invalid_state_fails_closed_without_rewriting(tmp_path: Path, payload: Any) -> None:
    path = _write_payload(tmp_path, payload)
    before = path.read_bytes()
    with pytest.raises(ValueError):
        editorial.load_state(tmp_path)
    assert path.read_bytes() == before


@pytest.mark.parametrize("data", [b'{"schema_version":', b"\xff\xfe"])
def test_truncated_or_invalid_utf8_state_fails_closed(tmp_path: Path, data: bytes) -> None:
    path = tmp_path / "state.json"
    path.write_bytes(data)
    with pytest.raises(ValueError):
        editorial.load_state(tmp_path)
    assert path.read_bytes() == data


def test_oversize_state_fails_without_discarding_work(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _write_payload(tmp_path, asdict(EditorialState()))
    before = path.read_bytes()
    monkeypatch.setattr(editorial, "MAX_STATE_BYTES", len(before) - 1)
    with pytest.raises(ValueError, match="allowance"):
        editorial.load_state(tmp_path)
    assert path.read_bytes() == before


@pytest.mark.parametrize("field,value", [
    ("delivery_state", "retry_unknown"), ("published", 123), ("article_id", "forged"),
    ("title", "Changed identity"), ("url", "https://example.com/changed"),
])
def test_invalid_article_fields_rejected(tmp_path: Path, field: str, value: Any) -> None:
    state = EditorialState()
    editorial.admit_articles(state, [make_article()])
    payload = asdict(state)
    payload["articles"][state.order[0]][field] = value
    _write_payload(tmp_path, payload)
    with pytest.raises(ValueError):
        editorial.load_state(tmp_path)


@pytest.mark.parametrize("location", ["directory", "state", "temporary", "bodies", "body"])
def test_symlink_storage_targets_rejected(tmp_path: Path, location: str) -> None:
    state_dir = tmp_path / "state"
    target = tmp_path / "untouched"
    target.mkdir()
    if location == "directory":
        state_dir.symlink_to(target, target_is_directory=True)
        with pytest.raises(ValueError, match="symlink"):
            editorial.load_state(state_dir)
        return
    state_dir.mkdir()
    if location in {"state", "temporary"}:
        victim = target / "checkpoint.json"
        victim.write_text("Do not change", encoding="utf-8")
        name = "state.json" if location == "state" else "state.json.tmp"
        (state_dir / name).symlink_to(victim)
        with pytest.raises(ValueError, match="symlink"):
            editorial.store_state(EditorialState(), state_dir)
        if location == "state":
            with pytest.raises(ValueError, match="symlink"):
                editorial.load_state(state_dir)
        assert victim.read_text() == "Do not change"
    elif location == "bodies":
        (state_dir / "bodies").symlink_to(target, target_is_directory=True)
        with pytest.raises(ValueError, match="symlink"):
            editorial.save_body(state_dir, BODY)
    else:
        digest = hashlib.sha256(BODY.encode()).hexdigest()
        (state_dir / "bodies").mkdir()
        victim = target / "body.txt"
        victim.write_text(BODY, encoding="utf-8")
        (state_dir / "bodies" / f"{digest}.txt").symlink_to(victim)
        with pytest.raises(ValueError, match="symlink"):
            editorial.read_body(state_dir, digest)


@pytest.mark.parametrize("body", ["a", "x" * 7500, "x" * 7501, "аб" * 5001, "🙂\n\t" * 4000,
                                  "  Header\n\n" + "detail " * 20000 + "\nFootnote limits all claims.  "])
def test_chunks_cover_every_character_in_order_without_normalizing(body: str) -> None:
    chunks = editorial.make_chunks(body)
    assert isinstance(chunks, tuple)
    assert chunks == editorial.make_chunks(body)
    assert chunks[0].start == 0 and chunks[-1].end == len(body)
    assert [chunk.index for chunk in chunks] == list(range(len(chunks)))
    assert len({chunk.chunk_id for chunk in chunks}) == len(chunks)
    assert "".join(body[chunk.start:chunk.end] for chunk in chunks) == body
    assert all(left.end == right.start for left, right in zip(chunks, chunks[1:], strict=False))
    for chunk in chunks:
        text = body[chunk.start:chunk.end]
        assert text and chunk.text_sha256 == hashlib.sha256(text.encode()).hexdigest()
        assert sum(1 if ord(char) < 128 else 3 for char in text) <= editorial.CHUNK_WEIGHT
    with pytest.raises(FrozenInstanceError):
        chunks[0].end = 0  # type: ignore[misc]


def test_empty_source_cannot_make_a_chunk_manifest() -> None:
    with pytest.raises(ValueError):
        editorial.make_chunks("")


def test_changed_body_rekeys_even_unchanged_chunk_text() -> None:
    first = editorial.make_chunks("x" * 7500 + "tail")
    changed = editorial.make_chunks("x" * 7500 + "changed tail")
    assert first[0].text_sha256 == changed[0].text_sha256
    assert first[0].chunk_id != changed[0].chunk_id


def test_body_is_exact_content_addressed_and_never_overwritten(tmp_path: Path) -> None:
    body = "  Оригинальный текст.\n\n| Таблица | Значение |\n[1] Финальная оговорка.  "
    identity = editorial.save_body(tmp_path, body)
    path = editorial.body_path(tmp_path, identity)
    before = path.stat().st_mtime_ns
    assert editorial.save_body(tmp_path, body) == identity
    assert path.stat().st_mtime_ns == before
    assert editorial.read_body(tmp_path, identity) == body
    assert identity == hashlib.sha256(body.encode()).hexdigest()
    assert list((tmp_path / "bodies").iterdir()) == [path]
    path.write_text("corrupted", encoding="utf-8")
    with pytest.raises(ValueError, match="Immutable"):
        editorial.save_body(tmp_path, body)
    with pytest.raises(ValueError, match="hash mismatch"):
        editorial.read_body(tmp_path, identity)
    assert path.read_text() == "corrupted"


@pytest.mark.parametrize("identity", ["../outside", "A" * 64, "f" * 63, "f" * 65, "", "/tmp/source"])
def test_body_identity_cannot_select_an_arbitrary_path(tmp_path: Path, identity: str) -> None:
    with pytest.raises(ValueError, match="identity"):
        editorial.read_body(tmp_path, identity)


def test_body_storage_limit_counts_utf8_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(editorial, "MAX_BODY_BYTES", 5)
    for body in ("", "я" * 3):
        with pytest.raises(ValueError, match="allowance"):
            editorial.save_body(tmp_path, body)
    identity = editorial.save_body(tmp_path, "я" * 2)
    assert editorial.read_body(tmp_path, identity) == "я" * 2


def test_complete_analysis_roundtrips_without_duplicating_body(tmp_path: Path) -> None:
    state, work, generation = _ready_state(tmp_path)
    editorial.store_state(state, tmp_path)
    before = (tmp_path / "state.json").read_bytes()
    loaded = editorial.load_state(tmp_path)
    assert loaded == state
    assert (tmp_path / "state.json").read_bytes() == before
    assert BODY not in (tmp_path / "state.json").read_text()
    assert len(list((tmp_path / "bodies").iterdir())) == 1
    assert work.body_sha256 is not None
    assert editorial.read_body(tmp_path, work.body_sha256) == BODY
    limitation = generation.final.limitation if generation.final else None
    assert limitation is not None
    spans = editorial.resolve_claim_spans(generation, limitation.claim_ids[0])
    assert [span.quote for span in spans] == ["Final footnote limits the initial claim."]
    assert spans[0].chunk_id == work.chunks[-1].chunk_id


@pytest.mark.parametrize("damage", ["missing", "reordered", "gap", "hash", "body"])
def test_incomplete_or_changed_chunk_coverage_is_rejected(tmp_path: Path, damage: str) -> None:
    state, work = _acquired_state(tmp_path)
    payload = asdict(state)
    raw = payload["articles"][work.article_id]
    raw["chunks"] = list(raw["chunks"])
    if damage == "missing":
        raw["chunks"].pop()
    elif damage == "reordered":
        raw["chunks"].reverse()
    elif damage == "gap":
        raw["chunks"][0]["end"] -= 1
    elif damage == "hash":
        raw["chunks"][0]["text_sha256"] = "0" * 64
    else:
        assert work.body_sha256 is not None
        editorial.body_path(tmp_path, work.body_sha256).write_text(BODY + " changed", encoding="utf-8")
    _write_payload(tmp_path, payload)
    with pytest.raises(ValueError):
        editorial.load_state(tmp_path)


@pytest.mark.parametrize("changed", ["body", "provider", "model", "chunking", "prompt"])
def test_generation_identity_changes_for_every_evidence_or_model_key(changed: str) -> None:
    args = {"body_sha256": "a" * 64, "provider": "gemini", "model": "model-a",
            "chunking_version": editorial.CHUNKING_VERSION, "prompt_version": editorial.PROMPT_VERSION}
    original = editorial.generation_id(**args)
    field = {"body": "body_sha256", "chunking": "chunking_version", "prompt": "prompt_version"}.get(changed, changed)
    args[field] = "changed-value"
    assert editorial.generation_id(**args) != original


def test_stage_is_part_of_node_identity(tmp_path: Path) -> None:
    _, _, generation = _ready_state(tmp_path)
    leaf = _first_leaf(generation)
    assert editorial.node_hash(replace(leaf, stage="reduce")) != leaf.node_id


@pytest.mark.parametrize("field", ["generation_id", "body_sha256", "provider", "model", "prompt_version",
                                  "chunking_version"])
def test_stale_generation_identity_is_not_accepted(tmp_path: Path, field: str) -> None:
    state, work, generation = _ready_state(tmp_path)
    payload = asdict(state)
    raw = payload["articles"][work.article_id]["generations"][generation.generation_id]
    raw[field] = "0" * 64 if field.endswith("id") or field == "body_sha256" else "changed"
    _write_payload(tmp_path, payload)
    with pytest.raises((ValueError, FileNotFoundError)):
        editorial.load_state(tmp_path)


@pytest.mark.parametrize("changed", ["body", "provider", "model", "prompt", "chunking"])
def test_archived_generation_is_not_reused_for_current_work(tmp_path: Path, changed: str) -> None:
    state, work, original = _ready_state(tmp_path)
    config = fixture_config()
    if changed == "body":
        new_body = BODY + " New source correction."
        work.body_sha256 = editorial.save_body(tmp_path, new_body)
        work.chunks = editorial.make_chunks(new_body)
    else:
        work.generations.clear()
        old = deepcopy(original)
        field = {"prompt": "prompt_version", "chunking": "chunking_version"}.get(changed, changed)
        setattr(old, field, "old-setting")
        old.generation_id = editorial.generation_id(
            old.body_sha256, old.provider, old.model, chunking_version=old.chunking_version,
            prompt_version=old.prompt_version,
        )
        work.generations[old.generation_id] = old
    assert editorial.current_generation(work, config.review.primary.provider, config.review.primary.model) is None
    assert editorial.ready_results(state, config) == []


@pytest.mark.parametrize("status,eligible", [
    ("pending", True), ("confirmed_failed", True), ("reserved", False), ("unknown", False), ("delivered", False),
])
def test_only_safe_delivery_states_are_ready(tmp_path: Path, status: Any, eligible: bool) -> None:
    state, work, generation = _ready_state(tmp_path)
    work.delivery_state = status
    work.delivery_attempt_id = None if status == "pending" else "telegram-attempt-1"
    editorial.store_state(state, tmp_path)
    loaded = editorial.load_state(tmp_path)
    results = editorial.ready_results(loaded, fixture_config())
    assert bool(results) is eligible
    assert loaded.articles[work.article_id].delivery_state == status
    assert loaded.articles[work.article_id].delivery_attempt_id == work.delivery_attempt_id
    if eligible:
        result = results[0]
        assert (result.title, result.url, result.source, result.category, result.published) == (
            work.title, work.url, work.source, work.category, work.published,
        )
        assert result.completed_chunks == len(work.chunks)
        assert result.generation_id == generation.generation_id
        assert not result.independent_complete
        assert "независимый разбор не завершён" in result.to_article_summary().summary


def test_unfinished_analysis_never_becomes_ready(tmp_path: Path) -> None:
    state, _, generation = _ready_state(tmp_path)
    generation.final = None
    assert editorial.ready_results(state, fixture_config()) == []
    editorial.store_state(state, tmp_path)
    assert editorial.load_state(tmp_path) == state


def test_interrupted_attempt_and_retry_progress_survives_restart(tmp_path: Path) -> None:
    state, work, generation = _ready_state(tmp_path)
    generation.final = None
    generation.last_error = "provider rate limited"
    generation.blocked_until = "2026-10-01T04:00:00+00:00"
    generation.attempts = [Attempt(
        "model-attempt-1", "final", "final:root", "a" * 64, NOW, "unknown", "runner stopped",
        generation.blocked_until, {"prompt_tokens": 123},
    )]
    work.acquisition_attempts = [Attempt("fetch-attempt-1", "acquire", work.article_id, "", NOW, "success")]
    state.cursor = 1
    state.provider_next_eligible = {generation.provider: generation.blocked_until}
    editorial.store_state(state, tmp_path)
    loaded = editorial.load_state(tmp_path)
    assert loaded == state
    assert editorial.ready_results(loaded, fixture_config()) == []
    assert loaded.articles[work.article_id].generations[generation.generation_id].attempts[0].status == "unknown"


@pytest.mark.parametrize("decision", ["ready", "rejected"])
def test_final_decision_requires_complete_source_coverage(tmp_path: Path, decision: Any) -> None:
    state, _, generation = _ready_state(tmp_path)
    assert generation.final is not None
    generation.final = replace(generation.final, decision=decision, root_node_id=_first_leaf(generation).node_id,
                               reason="Not useful" if decision == "rejected" else "")
    with pytest.raises(ValueError, match="complete source coverage"):
        editorial.store_state(state, tmp_path)


def test_grounded_rejection_is_retained_but_never_ready(tmp_path: Path) -> None:
    state, _, generation = _ready_state(tmp_path)
    assert generation.final is not None
    generation.final = replace(generation.final, decision="rejected", reason="Нет полезного изменения",
                               fact=None, inference=None, limitation=None, why_read=None)
    editorial.store_state(state, tmp_path)
    loaded = editorial.load_state(tmp_path)
    assert loaded == state
    assert editorial.ready_results(loaded, fixture_config()) == []


def test_invented_final_claim_reference_is_rejected(tmp_path: Path) -> None:
    state, _, generation = _ready_state(tmp_path)
    assert generation.final is not None
    generation.final = replace(generation.final, limitation=EditorialField("Invented", ("missing-claim",)))
    with pytest.raises(ValueError, match="grounded claim"):
        editorial.store_state(state, tmp_path)


def test_unknown_claim_cannot_be_resolved(tmp_path: Path) -> None:
    _, _, generation = _ready_state(tmp_path)
    with pytest.raises(ValueError, match="Unknown editorial claim"):
        editorial.resolve_claim_spans(generation, "not-a-claim")


def _replace_node(generation: Generation, node: AnalysisNode) -> None:
    """Rehash a tampered node and its parent to exercise semantic validation."""
    old = generation.nodes[node.task_key]
    changed = _hashed_node(node)
    generation.nodes[node.task_key] = changed
    for parent in list(generation.nodes.values()):
        if old.node_id in parent.input_node_ids:
            _replace_node(generation, replace(parent, input_node_ids=tuple(
                changed.node_id if identity == old.node_id else identity for identity in parent.input_node_ids
            )))
    if generation.final is not None and generation.final.root_node_id == old.node_id:
        generation.final = replace(generation.final, root_node_id=changed.node_id)


@pytest.mark.parametrize("damage", ["quote", "outside_span", "unknown_chunk", "duplicate_claim"])
def test_rehashed_source_claim_tampering_is_rejected(tmp_path: Path, damage: str) -> None:
    state, _, generation = _ready_state(tmp_path)
    leaf = _first_leaf(generation)
    claim = leaf.claims[0]
    if damage == "duplicate_claim":
        changed = replace(leaf, claims=(claim, replace(claim, text="A competing meaning for the same claim ID")))
    else:
        span = claim.spans[0]
        if damage == "quote":
            span = replace(span, quote="Invented source quotation")
        elif damage == "outside_span":
            span = replace(span, start=span.start - 1)
        else:
            span = replace(span, chunk_id="unknown-source-chunk")
        changed = replace(leaf, claims=(replace(claim, spans=(span,)),))
    _replace_node(generation, changed)
    _write_payload(tmp_path, asdict(state))
    expected = "Duplicate editorial claim" if damage == "duplicate_claim" else "exact source span"
    with pytest.raises(ValueError, match=expected):
        editorial.load_state(tmp_path)


@pytest.mark.parametrize("damage", ["missing_input", "reorder", "drop_claim", "duplicate_support",
                                    "invented_support", "qualification_to_fact"])
def test_rehashed_reduction_cannot_lose_or_invent_lineage(tmp_path: Path, damage: str) -> None:
    state, _, generation = _ready_state(tmp_path)
    root = _root_node(generation)
    if damage == "missing_input":
        changed = replace(root, input_node_ids=root.input_node_ids[:1])
    elif damage == "reorder":
        changed = replace(root, input_node_ids=tuple(reversed(root.input_node_ids)))
    elif damage == "drop_claim":
        changed = replace(root, claims=root.claims[:1])
    elif damage == "duplicate_support":
        changed = replace(root, claims=tuple(replace(claim, supports=root.claims[0].supports) for claim in root.claims))
    elif damage == "invented_support":
        changed = replace(root, claims=(root.claims[0], replace(root.claims[1], supports=("invented",))))
    else:
        changed = replace(root, claims=(root.claims[0], replace(root.claims[1], kind="fact")))
    _replace_node(generation, changed)
    _write_payload(tmp_path, asdict(state))
    with pytest.raises(ValueError):
        editorial.load_state(tmp_path)


def test_ready_final_cannot_drop_known_qualification(tmp_path: Path) -> None:
    state, _, generation = _ready_state(tmp_path)
    assert generation.final is not None
    generation.final = replace(generation.final, limitation=EditorialField(
        "Источник якобы не содержит существенных ограничений исходного утверждения",
        (_root_node(generation).claims[0].claim_id,),
    ))
    _write_payload(tmp_path, asdict(state))
    with pytest.raises(ValueError, match="lost a source qualification"):
        editorial.load_state(tmp_path)


@pytest.mark.parametrize("field,value", [("extraction_status", "incomplete"), ("final_url", None),
                                         ("final_url", "file:///tmp/article"), ("fetched_at", None),
                                         ("fetched_at", "yesterday"), ("fetched_at", "2026-10-01T03:00:00")])
def test_ready_analysis_requires_complete_acquisition_provenance(tmp_path: Path, field: str, value: Any) -> None:
    state, work, _ = _ready_state(tmp_path)
    payload = asdict(state)
    payload["articles"][work.article_id][field] = value
    _write_payload(tmp_path, payload)
    with pytest.raises(ValueError):
        editorial.load_state(tmp_path)


def test_missing_delivery_status_cannot_reset_delivered_article_to_pending(tmp_path: Path) -> None:
    state, work, _ = _ready_state(tmp_path)
    work.delivery_state = "delivered"
    work.delivery_attempt_id = "accepted-by-telegram"
    payload = asdict(state)
    del payload["articles"][work.article_id]["delivery_state"]
    _write_payload(tmp_path, payload)
    with pytest.raises(ValueError):
        editorial.load_state(tmp_path)


def test_state_size_check_matches_written_encoding(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = EditorialState()
    editorial.admit_articles(state, [make_article(description="Русский текст " * 100)])
    compact_size = len(json.dumps(asdict(state), ensure_ascii=False).encode())
    actual_size = len(json.dumps(asdict(state), indent=2).encode())
    assert compact_size < actual_size
    cap = (compact_size + actual_size) // 2
    monkeypatch.setattr(editorial, "MAX_STATE_BYTES", cap)
    try:
        editorial.store_state(state, tmp_path)
    except ValueError:
        assert not (tmp_path / "state.json").exists()
    else:
        assert (tmp_path / "state.json").stat().st_size <= cap
        assert editorial.load_state(tmp_path) == state


def test_failed_validation_keeps_previous_durable_checkpoint(tmp_path: Path) -> None:
    state = EditorialState()
    editorial.admit_articles(state, [make_article()])
    editorial.store_state(state, tmp_path)
    before = (tmp_path / "state.json").read_bytes()
    state.cursor = -1
    with pytest.raises(ValueError, match="state index"):
        editorial.store_state(state, tmp_path)
    assert (tmp_path / "state.json").read_bytes() == before
    assert editorial.load_state(tmp_path).cursor == 0


@pytest.mark.parametrize("field", ["model", "provider"])
def test_relabelled_generation_cannot_reuse_another_models_tasks(tmp_path: Path, field: str) -> None:
    state, work, generation = _ready_state(tmp_path)
    old_id = generation.generation_id
    setattr(generation, field, "different-model" if field == "model" else "different-provider")
    generation.generation_id = editorial.generation_id(
        generation.body_sha256, generation.provider, generation.model,
    )
    work.generations = {generation.generation_id: generation}
    assert generation.generation_id != old_id
    _write_payload(tmp_path, asdict(state))
    with pytest.raises(ValueError, match="bound to its analysis generation"):
        editorial.load_state(tmp_path)


@pytest.mark.parametrize("damage", ["empty_fact", "english_fact", "filler", "repeat_fields",
                                    "duplicate_refs", "score_bool", "missing_rationale", "wrong_prompt"])
def test_cached_ready_final_is_revalidated_with_live_schema(tmp_path: Path, damage: str) -> None:
    state, _, generation = _ready_state(tmp_path)
    final = generation.final
    assert final and final.fact and final.inference
    if damage in {"empty_fact", "english_fact", "filler"}:
        text = {"empty_fact": "", "english_fact": "An unsupported English-only cached editorial statement",
                "filler": "Этот материал представляет интерес для читателя"}[damage]
        final = replace(final, fact=replace(final.fact, text=text))
    elif damage == "repeat_fields":
        final = replace(final, inference=replace(final.inference, text=final.fact.text))
    elif damage == "duplicate_refs":
        final = replace(final, fact=replace(final.fact, claim_ids=final.fact.claim_ids * 2))
    elif damage == "score_bool":
        final = replace(final, value_score=True)
    elif damage == "missing_rationale":
        final = replace(final, value_rationale="")
    else:
        final = replace(final, prompt_hash="wrong-final-prompt")
    generation.final = final
    _write_payload(tmp_path, asdict(state))
    with pytest.raises(ValueError):
        editorial.load_state(tmp_path)
    with pytest.raises(ValueError):
        editorial.ready_results(state, fixture_config())


@pytest.mark.parametrize("stage", ["chunk", "reduce"])
def test_current_cached_node_prompt_must_match_live_task(tmp_path: Path, stage: str) -> None:
    from digest.editorial_worker import validate_cached_prompts

    _, article, generation = _ready_state(tmp_path)
    node = next(node for node in generation.nodes.values() if node.stage == stage)
    generation.nodes[node.task_key] = replace(node, prompt_hash="wrong-current-prompt")
    with pytest.raises(ValueError, match="prompt changed"):
        validate_cached_prompts(article, generation, BODY)


def test_historical_prompt_generation_roundtrips_without_becoming_reusable(tmp_path: Path) -> None:
    state, article, generation = _ready_state(tmp_path, prompt_version="historical-prompt-v0")
    assert generation.final and generation.final.fact
    generation.final = replace(generation.final, prompt_hash="historical-final-prompt",
                               fact=replace(generation.final.fact, text="Historical contract text"))
    editorial.store_state(state, tmp_path)
    loaded = editorial.load_state(tmp_path)
    assert loaded == state
    config = fixture_config()
    assert editorial.current_generation(loaded.articles[article.article_id], config.review.primary.provider,
                                        config.review.primary.model) is None
    assert editorial.ready_results(loaded, config) == []
