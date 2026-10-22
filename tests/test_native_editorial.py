"""Synthetic public native handoff fixtures; all outbound/model execution is denied."""

from __future__ import annotations

import json
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from digest.adapters.storage import edition as editions
from digest.adapters.storage import native as storage
from digest.adapters.storage.candidate_progress import load_candidate_progress
from digest.adapters.storage.sources import save_source_state
from digest.application import candidate_review, native, prepared_delivery
from digest.config import (
    Config,
    FiltersConfig,
    IrritatorConfig,
    LLMConfig,
    ObsidianConfig,
    RadarConfig,
    TelegramConfig,
)
from digest.domain.catalog.articles import article_hash
from digest.domain.catalog.sources import SourceConfig, SourceStateStore
from digest.domain.editorial.candidates import Candidate, CandidateArticle, CandidatePacket, CandidateProgress
from digest.domain.editorial.native import encode, export_packet, read_packet, sha256
from digest.presentation.source_attribution import SOURCE_CREDITS

NOW = datetime(2026, 10, 21, 12, tzinfo=timezone.utc)
CACHE = Path(".cache")


def occurrences() -> list[CandidateArticle]:
    return [
        CandidateArticle(
            f"Synthetic headline {i}",
            f"https://example.invalid/article-{i}",
            f"Synthetic excerpt {i}.",
            f"Synthetic source {i}",
            "Synthetic category",
            (NOW + timedelta(days=i == 1)).isoformat(),
            feed,
        )
        for i, feed in enumerate(["https://example.invalid/feed", *SOURCE_CREDITS])
    ]


def config() -> Config:
    result = Config(
        LLMConfig([]),
        RadarConfig(),
        IrritatorConfig(),
        [SourceConfig(a.source, a.source_url, a.category, True, recency_hours=48) for a in occurrences()],
        FiltersConfig(),
        TelegramConfig(delivery_mode="compact", bot_username="synthetic_bot"),
        ObsidianConfig(enabled=False),
    )
    result.review.max_selections = 2
    return result


def planned() -> CandidatePacket:
    progress = CandidateProgress(
        candidates={
            article_hash(a.title, a.link): Candidate(article_hash(a.title, a.link), a, NOW.isoformat(), 3)
            for a in occurrences()
        }
    )
    packet = candidate_review.plan_packet(progress, config(), now=NOW)
    assert packet is not None
    return packet


def seed(*, day: Any = None) -> storage.PendingNative:
    packet = planned()
    intended = day or NOW.date()
    raw = export_packet(
        packet,
        publication_date=intended,
        language="ru",
        max_excerpt_chars=500,
        owner_sha256=sha256(b"123"),
        editorial_context="Synthetic priorities",
    )
    pending = storage.PendingNative(1, sha256(b"123"), intended.isoformat(), storage.put_bytes(raw, CACHE, "packet"))
    storage.save_pending(CACHE, pending)
    return pending


def response_for(pending: storage.PendingNative) -> bytes:
    _, packet = read_packet(storage.read_reference(pending.packet))
    return encode(
        {
            "schema": "native-editorial-response/v1",
            "packet_sha256": pending.packet.sha256,
            "execution_ref": "synthetic-writer",
            "language": "ru",
            "items": [
                {
                    "evidence_id": item.evidence_id,
                    "status": "withheld" if i == 0 else "card",
                    "text": "" if i == 0 else f"Синтетическая карточка {i}.",
                }
                for i, item in enumerate(packet.evidence.items)
            ],
            "closing": {"text": "WITHHELD CLOSING"},
            "comparison": {
                "text": "WITHHELD COMPARISON",
                "evidence_ids": [item.evidence_id for item in packet.evidence.items[1:]],
            },
        }
    )


def review_for(pending: storage.PendingNative, response: bytes, *, rejected: int = 0) -> bytes:
    return encode(
        {
            "schema": "external-source-review/v2",
            "packet_sha256": pending.packet.sha256,
            "response_sha256": sha256(response),
            "writer_execution_ref": "synthetic-writer",
            "reviewer_execution_ref": "synthetic-reviewer",
            "verdict": "fail" if rejected else "accept",
            "notes": "Synthetic fixture, not a semantic review.",
            "card_verdicts": [
                {
                    "evidence_id": item["evidence_id"],
                    "text_sha256": sha256(item["text"].encode()),
                    "verdict": "reject" if i < rejected else "accept",
                    "notes": "Synthetic fixture verdict.",
                }
                for i, item in enumerate(item for item in json.loads(response)["items"] if item["status"] == "card")
            ],
        }
    )


def project(*, rejected: int = 0, cfg: Config | None = None) -> storage.PendingNative:
    pending = seed()
    response = response_for(pending)
    return native.project_response(
        cfg or config(),
        response,
        review_for(pending, response, rejected=rejected),
        expected_packet_sha256=pending.packet.sha256,
        now=NOW,
    )


def acceptance_for(pending: storage.PendingNative) -> bytes:
    assert pending.projection and pending.response and pending.review
    return encode(
        {
            "schema": "native-final-acceptance/v1",
            "projection_sha256": pending.projection.sha256,
            "packet_sha256": pending.packet.sha256,
            "response_sha256": pending.response.sha256,
            "review_sha256": pending.review.sha256,
            "reviewer_execution_ref": "synthetic-final-reviewer",
            "verdict": "accept",
            "notes": "Synthetic final-composition acceptance fixture only.",
        }
    )


def freeze(pending: storage.PendingNative, cfg: Config | None = None) -> tuple[Path, str]:
    assert pending.projection
    return native.import_accepted(
        cfg or config(), acceptance_for(pending), expected_projection_sha256=pending.projection.sha256, now=NOW
    )


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "SYNTHETIC_INVALID_TOKEN")
    for key in ("ALL_PROXY", "all_proxy", "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        monkeypatch.delenv(key, raising=False)

    def denied(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("Outbound, model dispatch or begin_packet is forbidden in native tests")

    for target in (
        "socket.socket.connect",
        "socket.socket.connect_ex",
        "socket.getaddrinfo",
        "httpx.Client.send",
        "httpx.AsyncClient.send",
        "digest.llm.complete",
        "digest.application.candidate_review.begin_packet",
        "digest.application.prepared_delivery.send_prepared_chunk",
    ):
        monkeypatch.setattr(target, denied)

    class Clock(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:
            return NOW if tz else NOW.replace(tzinfo=None)

    for target in (native, prepared_delivery, candidate_review):
        monkeypatch.setattr(target, "datetime", Clock)
    monkeypatch.setattr("digest.radar.collector.datetime", Clock)
    monkeypatch.setattr("digest.edition_runtime.datetime", Clock)


async def test_real_export_precedes_begin_and_resumes_without_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    from digest.radar.collector import SourceCollectionOutcome, _capture_candidates

    async def collect(cfg: Config, **kwargs: Any) -> Any:
        articles = [a.article() for a in occurrences()]
        kwargs["inventory"].sources = [
            SourceCollectionOutcome(s.name, s.url, s.category, s.priority) for s in cfg.sources
        ]
        _capture_candidates(kwargs["inventory"], cfg.sources, [[a] for a in articles], {}, NOW, [], {})
        return {"Synthetic category": articles}, {}

    fetch = AsyncMock(side_effect=collect)
    monkeypatch.setattr("digest.radar.collect", fetch)
    pending = await native.export_sources(config(), now=NOW)
    assert pending is not None and pending.projection is None
    saved = load_candidate_progress(str(CACHE))
    assert len(saved.candidates) == 3 and not saved.packets
    assert all(c.status == "not_presented" and c.disposition is None for c in saved.candidates.values())
    assert await native.export_sources(config(), now=NOW) == pending
    assert fetch.await_count == 1
    with pytest.raises(ValueError, match="pending"):
        storage.block_pending()


def test_mixed_projection_freeze_keeps_original_failed_review_and_exact_card() -> None:
    pending = project(rejected=1)
    assert pending.review and pending.projection
    assert json.loads(storage.read_reference(pending.review))["verdict"] == "fail"
    projection = json.loads(storage.read_reference(pending.projection))
    assert not (CACHE / editions.READY_FILE).exists()
    path, digest = freeze(pending)
    value, loaded = editions.load_edition(path, "123", NOW, digest)
    assert value.schema == 4 and loaded == digest and len(value.articles) == 1
    original = json.loads(storage.read_reference(pending.response))["items"][-1]["text"]
    assert value.canonical_metadata["cards"][0]["summary"] == original
    assert projection["publication"] == storage.publication_fields(asdict(value))
    assert "WITHHELD" not in str(value.payloads) and value.presentation_metadata["translation"] is None
    assert value.canonical_metadata["native_editorial"]["provider"] is None
    assert storage.load_pending(CACHE) is None and not (CACHE / "candidate_progress.json").exists()
    assert len(value.checkpoint_refs) == 5


def test_all_rejected_retains_no_ready_hold() -> None:
    pending = project(rejected=2)
    assert storage.load_pending(CACHE) == pending
    assert pending.projection
    assert not json.loads(storage.read_reference(pending.projection))["publication"]["articles"]
    with pytest.raises(ValueError, match="no main cards"):
        freeze(pending)
    assert not (CACHE / editions.READY_FILE).exists()


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "unknown", "text", "response", "packet", "blank"])
def test_incomplete_or_changed_source_review_fails_before_sidecars(mutation: str) -> None:
    pending = seed()
    response = response_for(pending)
    review = json.loads(review_for(pending, response))
    if mutation == "missing":
        review["card_verdicts"].pop()
    elif mutation == "duplicate":
        review["card_verdicts"][1] = review["card_verdicts"][0]
    elif mutation == "unknown":
        review["card_verdicts"][0]["evidence_id"] = "f" * 32
    elif mutation == "text":
        review["card_verdicts"][0]["text_sha256"] = "0" * 64
    elif mutation in {"response", "packet"}:
        review[mutation + "_sha256"] = "0" * 64
    else:
        review["card_verdicts"][0]["verdict"] = ""
    before = set(CACHE.rglob("*"))
    with pytest.raises(ValueError):
        native.project_response(
            config(), response, encode(review), expected_packet_sha256=pending.packet.sha256, now=NOW
        )
    assert set(CACHE.rglob("*")) == before and storage.load_pending(CACHE) == pending


@pytest.mark.parametrize("change", ["disabled", "demoted", "url", "category", "age", "blocklist", "seen"])
@pytest.mark.parametrize("phase", ["project", "import"])
def test_current_effective_eligibility_rechecked(change: str, phase: str) -> None:
    cfg = config()
    pending = project(cfg=cfg) if phase == "import" else seed()
    if change == "disabled":
        cfg.sources[0].enabled = False
    elif change == "demoted":
        state = SourceStateStore()
        state.mark_demoted(cfg.sources[0].name)
        save_source_state(state, str(CACHE))
    elif change in {"url", "category"}:
        setattr(cfg.sources[0], change, "https://example.invalid/changed" if change == "url" else "Other")
    elif change == "age":
        cfg.sources[0].recency_hours = -1
    elif change == "blocklist":
        cfg.filters.blocklist_keywords = ["Synthetic"]
    else:
        a = occurrences()[0]
        (CACHE / "seen_articles.json").write_text(json.dumps({article_hash(a.title, a.link): NOW.isoformat()}))
    with pytest.raises(ValueError, match="no-longer-eligible"):
        if phase == "import":
            freeze(pending, cfg)
        else:
            response = response_for(pending)
            native.project_response(
                cfg, response, review_for(pending, response), expected_packet_sha256=pending.packet.sha256, now=NOW
            )
    assert not (CACHE / editions.READY_FILE).exists()


@pytest.mark.parametrize("field", ["verdict", "projection_sha256", "packet_sha256", "response_sha256", "review_sha256"])
def test_final_acceptance_required_before_ready(field: str) -> None:
    pending = project()
    acceptance = json.loads(acceptance_for(pending))
    acceptance[field] = "reject" if field == "verdict" else "f" * 64
    with pytest.raises(ValueError):
        native.import_accepted(
            config(), encode(acceptance), expected_projection_sha256=pending.projection.sha256, now=NOW
        )
    assert not (CACHE / editions.READY_FILE).exists() and storage.load_pending(CACHE).acceptance is None


@pytest.mark.parametrize(
    "target", ["canonical", "presented", "order", "payload", "coverage", "acceptance", "projection"]
)
async def test_rehashed_ready_cannot_bypass_claim_or_sender(target: str) -> None:
    pending = project()
    path, ready_sha = freeze(pending)
    _, claim_sha = prepared_delivery.claim_edition(ready_sha, now=NOW)
    data = json.loads(path.read_bytes())
    if target == "canonical":
        data["canonical_metadata"]["cards"][0]["summary"] += " Changed"
    elif target == "presented":
        data["articles"][0]["card"]["summary"] += " Changed"
    elif target == "order":
        data["articles"].reverse()
    elif target == "payload":
        data["payloads"][0]["text"] += " Changed"
    elif target == "coverage":
        data["articles"][0]["source"] = "Changed"
    else:
        reference = data["native_" + target]
        Path(reference["path"]).write_bytes(b"{}")
    data["canonical_sha256"] = sha256(editions.canonical_bytes(data["canonical_metadata"]))
    data["presentation_sha256"] = sha256(editions.canonical_bytes(data["presentation_metadata"]))
    data["content_sha256"] = sha256(editions.canonical_bytes({k: v for k, v in data.items() if k != "content_sha256"}))
    changed_sha = editions.write_record(path, data)
    # Both direct boundaries fail before any new claim/receipt/transport effect.
    with pytest.raises(ValueError):
        await prepared_delivery.send_prepared_edition(changed_sha, claim_sha, config=config(), now=NOW)
    (CACHE / editions.CLAIM_FILE).unlink()
    with pytest.raises(ValueError):
        prepared_delivery.claim_edition(changed_sha, now=NOW)
    assert not (CACHE / editions.CLAIM_FILE).exists() and not (CACHE / editions.RECEIPTS_FILE).exists()


async def test_existing_sender_accounts_real_card_identity_with_mock_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    pending = project()
    path, digest = freeze(pending)
    data = json.loads(path.read_bytes())
    calls = AsyncMock(side_effect=[("confirmed", 101 + i) for i in range(len(data["payloads"]))])
    monkeypatch.setattr(prepared_delivery, "send_prepared_chunk", calls)
    _, claim = prepared_delivery.claim_edition(digest, now=NOW)
    result = await prepared_delivery.send_prepared_edition(digest, claim, config=config(), now=NOW)
    assert result.complete and calls.await_count == len(data["payloads"])
    seen = json.loads((CACHE / "seen_articles.json").read_bytes())
    assert set(seen) == {item["full_hash"] for item in data["articles"]}
    assert all(item["card"]["title"].startswith("Synthetic headline") for item in data["articles"])
    assert not (CACHE / "candidate_progress.json").exists()


def test_archive_failure_and_freeze_interruption_resume_exactly(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = config()
    cfg.obsidian.enabled = True
    pending = project(cfg=cfg)
    with monkeypatch.context() as patch:
        patch.setattr("digest.delivery.markdown.write_digest", lambda *a, **k: None)
        with pytest.raises(ValueError, match="archive failed"):
            freeze(pending, cfg)
    retained = storage.load_pending(CACHE)
    assert retained.acceptance is not None and retained.archive is None
    with monkeypatch.context() as patch:
        patch.setattr(prepared_delivery, "prepare_edition", lambda *a, **k: (_ for _ in ()).throw(OSError("fixture")))
        with pytest.raises(OSError):
            freeze(retained, cfg)
    retained = storage.load_pending(CACHE)
    assert retained.archive is not None
    archive_bytes = storage.read_reference(retained.archive)
    path, digest = freeze(retained, cfg)
    assert len(list(Path("digests").glob("*.md"))) == 1
    value, _ = editions.load_edition(path, "123", NOW, digest)
    assert value.checkpoint_refs[retained.archive.path] == retained.archive.sha256
    for card in value.articles:
        assert card.card.summary.encode() in archive_bytes.replace(b"\n> ", b"\n")


def test_final_ready_recovery_without_recomposition(monkeypatch: pytest.MonkeyPatch) -> None:
    pending = project()
    with monkeypatch.context() as patch:
        patch.setattr(storage, "clear_pending", lambda cache: (_ for _ in ()).throw(OSError("fixture")))
        with pytest.raises(OSError):
            freeze(pending)
    retained = storage.load_pending(CACHE)
    ready = (CACHE / editions.READY_FILE).read_bytes()
    cfg = config()
    cfg.sources = []  # Frozen recovery is immutable, not a new policy/admission pass.
    path, digest = freeze(retained, cfg)
    assert path.read_bytes() == ready and digest == sha256(ready) and storage.load_pending(CACHE) is None


def test_same_writer_cannot_accept_final_composition_or_restore_ready() -> None:
    pending = project()
    invalid = json.loads(acceptance_for(pending))
    invalid["reviewer_execution_ref"] = "synthetic-writer"
    with pytest.raises(ValueError, match="acceptance"):
        native.import_accepted(config(), encode(invalid), expected_projection_sha256=pending.projection.sha256, now=NOW)
    path, _ = freeze(pending)
    data = json.loads(path.read_bytes())
    false_acceptance = storage.put_bytes(encode(invalid), CACHE, "final-acceptance")
    data["native_acceptance"] = asdict(false_acceptance)
    data["checkpoint_refs"][false_acceptance.path] = false_acceptance.sha256
    data["content_sha256"] = sha256(editions.canonical_bytes({k: v for k, v in data.items() if k != "content_sha256"}))
    digest = editions.write_record(path, data)
    with pytest.raises(ValueError):
        prepared_delivery.claim_edition(digest, now=NOW)
    assert not (CACHE / editions.CLAIM_FILE).exists()


@pytest.mark.parametrize("rejected", [0, 1, 2])
def test_project_repeat_is_immutable_and_replacement_fails(rejected: int) -> None:
    pending = project(rejected=rejected)
    raw = storage.read_reference(pending.response)
    review = storage.read_reference(pending.review)
    assert (
        native.project_response(config(), raw, review, expected_packet_sha256=pending.packet.sha256, now=NOW) == pending
    )
    changed = json.loads(raw)
    changed["items"][-1]["text"] += " Another version."
    changed_raw = encode(changed)
    with pytest.raises(ValueError, match="replacement"):
        native.project_response(
            config(),
            changed_raw,
            review_for(pending, changed_raw),
            expected_packet_sha256=pending.packet.sha256,
            now=NOW,
        )


@pytest.mark.parametrize("kind", ["ready", "claim", "receipt", "preparation"])
def test_conflicting_work_blocks_before_any_sidecars(kind: str) -> None:
    from digest.domain.editorial.summaries import ArticleSummary
    from digest.preparation import PreparationSnapshot, save_preparation

    pending = seed()
    card = ArticleSummary("Other", "https://example.invalid/other", "Other", "Other", "Other card")
    if kind == "preparation":
        save_preparation(PreparationSnapshot([card], [], "", None, 1, 1, ["Other"]), now=NOW)
    elif kind == "receipt":
        (CACHE / editions.RECEIPTS_FILE).write_bytes(b"{}")
    else:
        _, digest = prepared_delivery.prepare_edition([card], config(), now=NOW)
        if kind == "claim":
            prepared_delivery.claim_edition(digest, now=NOW)
    before = {p: p.read_bytes() for p in CACHE.rglob("*") if p.is_file()}
    raw = response_for(pending)
    with pytest.raises(ValueError):
        native.project_response(
            config(), raw, review_for(pending, raw), expected_packet_sha256=pending.packet.sha256, now=NOW
        )
    assert {p: p.read_bytes() for p in CACHE.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize("mutation", ["schema", "duplicate_key", "owner", "date", "phase", "archive", "source"])
def test_corrupt_pending_never_resets(mutation: str) -> None:
    pending = seed()
    path = CACHE / storage.PENDING_FILE
    data = json.loads(path.read_bytes())
    if mutation == "duplicate_key":
        path.write_text('{"schema":1,' + path.read_text()[1:])
    elif mutation == "source":
        Path(pending.packet.path).write_bytes(b"{}")
    else:
        if mutation == "schema":
            data["schema"] = 99
        elif mutation == "owner":
            data["owner_sha256"] = "0" * 64
        elif mutation == "date":
            data["publication_date"] = "2026-10-22"
        elif mutation == "phase":
            data["response"] = asdict(pending.packet)
        else:
            data["archive"] = asdict(pending.packet)
        path.write_bytes(encode(data))
    before = path.read_bytes()
    with pytest.raises(ValueError):
        storage.load_pending(CACHE)
    assert path.read_bytes() == before


def test_partial_checkpoint_write_does_not_poison_immutable_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = b'{"synthetic":"complete bytes must survive retry"}'
    original = Path.open

    class BrokenWrite:
        def __init__(self, handle: Any) -> None:
            self.handle = handle

        def __enter__(self) -> Any:
            self.handle.__enter__()
            return self

        def __exit__(self, *args: Any) -> Any:
            return self.handle.__exit__(*args)

        def write(self, value: bytes) -> None:
            self.handle.write(value[:10])
            raise OSError("Synthetic partial write")

    def partial(path: Path, *args: Any, **kwargs: Any) -> Any:
        handle = original(path, *args, **kwargs)
        return BrokenWrite(handle) if path.name.endswith(".tmp") else handle

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", partial)
        with pytest.raises(OSError):
            storage.put_bytes(raw, CACHE, "response")
    assert not list((CACHE / "native").glob("*.json"))
    reference = storage.put_bytes(raw, CACHE, "response")
    assert storage.read_reference(reference) == raw
    assert storage.put_bytes(raw, CACHE, "response") == reference
    Path(reference.path).write_bytes(b"{}")
    with pytest.raises(ValueError, match="mismatch"):
        storage.put_bytes(raw, CACHE, "response")


@pytest.mark.parametrize("expired", [False, True])
def test_release_retains_evidence_unresolved_and_is_repeatable(expired: bool) -> None:
    pending = seed(day=NOW.date() - timedelta(days=1)) if expired else project(rejected=2)
    before = {item.path: storage.read_reference(item) for item in storage.references(pending)}
    path, digest = native.release_pending(config(), expected_packet_sha256=pending.packet.sha256, now=NOW)
    assert storage.load_pending(CACHE) is None and digest == sha256(path.read_bytes())
    record = json.loads(path.read_bytes())
    assert record["reason"] == ("expired" if expired else "no_retained_main")
    assert record["pending"] == asdict(pending)
    assert {name: Path(name).read_bytes() for name in before} == before
    assert native.release_pending(config(), expected_packet_sha256=pending.packet.sha256, now=NOW) == (path, digest)
    assert not (CACHE / editions.READY_FILE).exists() and not (CACHE / "candidate_progress.json").exists()
    storage.block_pending()


def test_interrupted_release_resumes_exact_record(monkeypatch: pytest.MonkeyPatch) -> None:
    pending = project(rejected=2)
    with monkeypatch.context() as patch:
        patch.setattr(storage, "clear_pending", lambda cache: (_ for _ in ()).throw(OSError("Synthetic interruption")))
        with pytest.raises(OSError):
            native.release_pending(config(), expected_packet_sha256=pending.packet.sha256, now=NOW)
    path = CACHE / "native" / f"{pending.packet.sha256}.release.json"
    retained = path.read_bytes()
    native.release_pending(config(), expected_packet_sha256=pending.packet.sha256, now=NOW + timedelta(minutes=1))
    assert path.read_bytes() == retained and storage.load_pending(CACHE) is None


@pytest.mark.parametrize("state", ["current", "wrong_hash", "ready", "claim", "receipt"])
def test_release_refuses_valid_work_or_delivery_conflict(state: str) -> None:
    from digest.domain.editorial.summaries import ArticleSummary

    pending = project(rejected=0 if state == "current" else 2)
    if state in {"ready", "claim"}:
        card = ArticleSummary("Other", "https://example.invalid/other", "Other", "Other", "Other")
        _, digest = prepared_delivery.prepare_edition([card], config(), now=NOW)
        if state == "claim":
            prepared_delivery.claim_edition(digest, now=NOW)
    elif state == "receipt":
        (CACHE / editions.RECEIPTS_FILE).write_bytes(b"{}")
    before = {p: p.read_bytes() for p in CACHE.rglob("*") if p.is_file()}
    with pytest.raises(ValueError):
        native.release_pending(
            config(), expected_packet_sha256="0" * 64 if state == "wrong_hash" else pending.packet.sha256, now=NOW
        )
    assert {p: p.read_bytes() for p in CACHE.rglob("*") if p.is_file()} == before


async def test_cli_all_phases_console_and_future_window(monkeypatch: pytest.MonkeyPatch, caplog: Any) -> None:
    from digest.main import main
    from digest.radar.collector import SourceCollectionOutcome, _capture_candidates

    async def collect(cfg: Config, **kwargs: Any) -> Any:
        articles = [a.article() for a in occurrences()]
        kwargs["inventory"].sources = [
            SourceCollectionOutcome(s.name, s.url, s.category, s.priority) for s in cfg.sources
        ]
        _capture_candidates(kwargs["inventory"], cfg.sources, [[a] for a in articles], {}, NOW, [], {})
        return {"Synthetic category": articles}, {}

    monkeypatch.setattr("digest.config.load_config", lambda path: config())
    monkeypatch.setattr("digest.radar.collect", collect)
    caplog.set_level("INFO")
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    assert await main(["--native-phase", "export", "--edition-date", "2026-10-22"]) == 0
    pending = storage.load_pending(CACHE)
    assert pending.packet.sha256 in caplog.text and pending.packet.path in caplog.text
    response = response_for(pending)
    Path("response.json").write_bytes(response)
    Path("review.json").write_bytes(review_for(pending, response))
    assert (
        await main(
            [
                "--native-phase",
                "project",
                "--native-response",
                "response.json",
                "--native-review",
                "review.json",
                "--native-packet-sha",
                pending.packet.sha256,
            ]
        )
        == 0
    )
    pending = storage.load_pending(CACHE)
    Path("acceptance.json").write_bytes(acceptance_for(pending))
    assert (
        await main(
            [
                "--native-phase",
                "import",
                "--native-acceptance",
                "acceptance.json",
                "--native-projection-sha",
                pending.projection.sha256,
            ]
        )
        == 0
    )
    assert '"edition_status": "pending_window"' in caplog.text
    assert not (CACHE / editions.CLAIM_FILE).exists()


@pytest.mark.parametrize(
    "args",
    [
        ["--native-phase", "export", "--dry-run"],
        ["--native-phase", "export", "--prepare-edition"],
        ["--native-phase", "project"],
        ["--native-phase", "import"],
        ["--native-phase", "release"],
        ["--native-response", "response.json"],
        ["--native-phase", "project", "--edition-date", "2026-10-22"],
    ],
)
async def test_cli_rejects_ambiguous_or_missing_phase_inputs(args: list[str]) -> None:
    from digest.main import main

    assert await main(args) == 1
    assert not CACHE.exists()


async def test_cli_release_and_ordinary_pending_precedence(monkeypatch: pytest.MonkeyPatch, caplog: Any) -> None:
    from digest.main import main

    monkeypatch.setattr("digest.config.load_config", lambda path: config())
    pending = project(rejected=2)
    caplog.set_level("INFO")
    assert await main([]) == 1
    assert "handoff is pending" in caplog.text
    assert await main(["--native-phase", "release", "--native-packet-sha", pending.packet.sha256]) == 0
    assert '"native_status": "released"' in caplog.text


async def test_export_preserves_active_trial_planning(monkeypatch: pytest.MonkeyPatch) -> None:
    from digest.application.preparation import EmptyWork

    cfg = config()
    for source in cfg.sources:
        source.trial = True
    state = SourceStateStore()
    state.mark_graduated(cfg.sources[1].name)
    state.mark_demoted(cfg.sources[2].name)
    save_source_state(state, str(CACHE))
    seen: dict[str, Any] = {}

    def plan(*args: Any, **kwargs: Any) -> Any:
        seen.update(kwargs)
        return EmptyWork.NO_CANDIDATES, {}, set()

    monkeypatch.setattr("digest.radar.collect", AsyncMock(return_value=({}, {})))
    monkeypatch.setattr("digest.application.preparation.plan_candidate_inputs", plan)
    assert await native.export_sources(cfg, now=NOW) is None
    source = cfg.sources[0]
    assert seen["trial_sources"] == frozenset({(source.name, source.url, source.category)})


def test_archive_counts_and_native_evidence_link_are_packet_scoped() -> None:
    cfg = config()
    cfg.obsidian.enabled = True
    pending = project(cfg=cfg, rejected=1)
    path, digest = freeze(pending, cfg)
    edition, _ = editions.load_edition(path, "123", NOW, digest)
    archive_path = next(Path(name) for name in edition.checkpoint_refs if name.endswith(".md"))
    archive = archive_path.read_text()
    assert "sources_count: 1" in archive and "articles_count: 3" in archive
    assert "группам категорий" in archive and "[Материалы внешней редакторской проверки](" in archive
    manifest_path = next(Path(name) for name in edition.checkpoint_refs if name.endswith(".archive-evidence.json"))
    manifest = json.loads(manifest_path.read_bytes())
    assert manifest["schema"] == "native-archive-evidence/v1" and len(manifest["references"]) == 5
    assert all(edition.checkpoint_refs[r["path"]] == r["sha256"] for r in manifest["references"])
    assert edition.canonical_metadata["distinct_packet_publishers"] == 3
    assert len(edition.articles) == 1


@pytest.mark.parametrize("mutation", ["handed", "feed", "evidence", "cap", "excerpt", "language", "schema"])
def test_source_packet_is_exact_and_never_relabels_provider_work(mutation: str) -> None:
    packet = planned()
    arguments: dict[str, Any] = {
        "publication_date": NOW.date(),
        "language": "ru",
        "max_excerpt_chars": 500,
        "owner_sha256": sha256(b"123"),
        "editorial_context": "Synthetic priorities",
    }
    if mutation == "handed":
        packet.handed_to_preparation = True
    elif mutation == "feed":
        packet.articles = (replace(packet.articles[0], source_url=""), *packet.articles[1:])
    elif mutation == "evidence":
        packet.articles = (replace(packet.articles[0], description="Different excerpt"), *packet.articles[1:])
    elif mutation == "cap":
        packet.max_selections = 0
    elif mutation == "excerpt":
        arguments["max_excerpt_chars"] = 0
    elif mutation == "language":
        arguments["language"] = "unknown"
    else:
        value = json.loads(export_packet(packet, **arguments))
        value["schema"] = "unknown"
        with pytest.raises(ValueError):
            read_packet(encode(value))
        return
    with pytest.raises(ValueError):
        export_packet(packet, **arguments)


@pytest.mark.parametrize("mutation", ["items", "duplicate", "unknown", "text", "abstention", "extra", "cap"])
def test_native_response_invariants(mutation: str) -> None:
    pending = seed()
    value = json.loads(response_for(pending))
    if mutation == "items":
        value["items"].pop()
    elif mutation == "duplicate":
        value["items"][1] = value["items"][0]
    elif mutation == "unknown":
        value["items"][1]["evidence_id"] = "0" * 32
    elif mutation == "text":
        value["items"][1]["text"] = "x" * 601
    elif mutation == "abstention":
        value["items"][0]["text"] = "Cannot invent abstention prose"
    elif mutation == "extra":
        value["items"][0]["unexpected"] = True
    else:
        value["items"][0].update(status="card", text="Third card exceeds planned cap")
    raw = encode(value)
    with pytest.raises(ValueError):
        native.project_response(
            config(), raw, review_for(pending, raw), expected_packet_sha256=pending.packet.sha256, now=NOW
        )


@pytest.mark.parametrize(
    "dependencies,expected",
    [
        (None, "ineligible_unvalidated_dependencies"),
        ([], "ineligible_unvalidated_dependencies"),
        (["unknown", "unknown"], "ineligible_unvalidated_dependencies"),
        (["unknown", "other"], "ineligible_nonpublished_dependency"),
    ],
)
def test_optional_comparison_dependencies_never_activate_publication(dependencies: Any, expected: str) -> None:
    pending = seed()
    value = json.loads(response_for(pending))
    value["comparison"]["evidence_ids"] = dependencies
    raw = encode(value)
    pending = native.project_response(
        config(), raw, review_for(pending, raw), expected_packet_sha256=pending.packet.sha256, now=NOW
    )
    _, digest = freeze(pending)
    edition, _ = editions.load_edition(CACHE / editions.READY_FILE, "123", NOW, digest)
    assert edition.presentation_metadata["comparison"] == expected
    assert "WITHHELD" not in str(edition.payloads)


@pytest.mark.parametrize("change", ["language", "archive", "bot"])
def test_accepted_composition_cannot_be_recomposed_after_config_change(change: str) -> None:
    pending = project()
    cfg = config()
    if change == "language":
        cfg.radar.language = "en"
    elif change == "archive":
        cfg.obsidian.enabled = True
    else:
        cfg.telegram.bot_username = "different_synthetic_bot"
    with pytest.raises(ValueError):
        freeze(pending, cfg)
    assert storage.load_pending(CACHE).acceptance is None and not (CACHE / editions.READY_FILE).exists()


def test_native_markers_cannot_be_frozen_under_legacy_ready_schema() -> None:
    from digest.domain.editorial.summaries import ArticleSummary

    with pytest.raises(ValueError):
        prepared_delivery.prepare_edition(
            [ArticleSummary("Synthetic", "https://example.invalid/x", "Synthetic", "Synthetic", "Synthetic")],
            config(),
            canonical_metadata={"external_editorial": {"publication_authorized": False}},
            now=NOW,
        )
    assert not (CACHE / editions.READY_FILE).exists()


@pytest.mark.parametrize("reason,future", [("no_retained_main", False), ("expired", False), ("expired", True)])
def test_release_replay_cannot_lie_about_reason_or_time(reason: str, future: bool) -> None:
    pending = project()
    record = {
        "schema": 1,
        "pending": asdict(pending),
        "reason": reason,
        "released_at": (NOW + timedelta(days=1) if future else NOW).isoformat(),
    }
    path = CACHE / "native" / f"{pending.packet.sha256}.release.json"
    editions.write_record(path, record)
    with pytest.raises(ValueError, match="reason/time"):
        native.release_pending(config(), expected_packet_sha256=pending.packet.sha256, now=NOW)
    assert storage.load_pending(CACHE) == pending


def test_checkpoint_retry_finishes_post_link_directory_durability(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import Mock

    raw = b'{"synthetic":"post-link interruption"}'
    with monkeypatch.context() as patch:
        patch.setattr(storage, "_sync", lambda path: (_ for _ in ()).throw(OSError("Synthetic post-link failure")))
        with pytest.raises(OSError):
            storage.put_bytes(raw, CACHE, "response")
    sync = Mock(wraps=storage._sync)
    monkeypatch.setattr(storage, "_sync", sync)
    reference = storage.put_bytes(raw, CACHE, "response")
    assert storage.read_reference(reference) == raw
    sync.assert_called_once_with((CACHE / "native").resolve())
