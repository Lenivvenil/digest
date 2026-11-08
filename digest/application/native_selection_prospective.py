"""Versioned opt-in collection and native-judgment reconciliation, without models.

Provider candidate statuses remain owned by their original reports. Native
judgments are a separate exact-evidence policy, never provider dispositions or
proof of delivery. No truncation, ranking, source promotion or sending occurs.
"""
from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from digest._sanitize import sanitize_article
from digest._serialization import restore_dataclass
from digest.adapters.storage import native as storage
from digest.application import native, native_selection, prepared_delivery
from digest.application.candidate_lifecycle import checkpoint_candidates, index_candidate
from digest.application.candidate_review import merge_candidates
from digest.application.native_selection_lifecycle import build_lifecycle_record
from digest.application.preparation import CandidatePool, _candidate_setup, _save_prepared_fetch_stats
from digest.config import Config
from digest.domain.catalog.occurrences import occurrence_sha256
from digest.domain.editorial.candidates import CandidateArticle
from digest.domain.editorial.native import encode
from digest.presentation import publication_language

ROUTE = "native-selection-prospective/v1"
PLAN_SCHEMA = "native-selection-prospective-plan/v1"
FINGERPRINT = "sanitized-source-content/v1"
MAX_HISTORY_BYTES = 64 * 1024 * 1024
MAX_HISTORY_RECORDS = 512


def content_fingerprint(article: CandidateArticle) -> str:
    """Same canonical sanitizer, full bounded text, no timestamp/category refresh.

    Source binding is included; category/publication metadata is not. This is
    deterministic changed bounded evidence, not a semantic novelty detector.
    Sanitation/whitespace-only changes
    and feed publication-date refreshes cannot reopen a withheld occurrence.
    """
    title, description, source = sanitize_article(article.title, article.description, article.source)
    title, description, source = (" ".join(value.split()) for value in (title, description, source))
    return hashlib.sha256(encode({
        "schema": FINGERPRINT, "title": title, "link": article.link,
        "description": description, "source": source, "source_url": article.source_url,
    })).hexdigest()


def editorial_policy(config: Config) -> str:
    return hashlib.sha256(encode({
        "language": publication_language(config), "editorial_context": config.review.editorial_context,
    })).hexdigest()


@dataclass(frozen=True)
class Judgment:
    record: storage.Reference
    packet: storage.Reference
    planned_at: str
    day: str
    policy: str
    article: CandidateArticle
    status: str
    source_accepted: bool


HISTORY_FILE = "native_selection_history.json"


def history_references(cache: Path) -> list[storage.Reference]:
    """Read the durable complete history index; v4 migration needs a ready anchor.

    A missing/deleted lifecycle file is never equivalent to no prior judgment.
    Unindexed v4 history may be adopted only from the retained ready checkpoints,
    not by trusting orphan files discovered in a directory.
    """
    cache = cache.resolve().relative_to(Path.cwd().resolve())
    path = cache / HISTORY_FILE
    result: list[storage.Reference] = []
    if path.exists():
        value = storage.decode(storage.read_input(path))
        if set(value) != {"schema", "records"} or value["schema"] != "native-selection-history/v1":
            raise ValueError("Invalid native history index")
        result = [restore_dataclass(row, storage.Reference) for row in value["records"]]
    from digest.adapters.storage.edition import READY_FILE

    ready_path = cache / READY_FILE
    if ready_path.exists():
        ready = storage.decode(storage.read_input(ready_path))
        if ready.get("schema") == 4:
            storage.validate_native_ready(ready)
        anchored = [storage.Reference(name, sha) for name, sha in ready.get("checkpoint_refs", {}).items()
                    if name.endswith(".selection-lifecycle.json")]
        # Prior accepted history is bound into every prospective source packet.
        pending = list(anchored)
        while pending:
            ref = pending.pop()
            record = storage.decode(storage.read_reference(ref))
            packet_ref = restore_dataclass(record["references"]["packet"], storage.Reference)
            packet, _ = native_selection.source_packet(storage.read_reference(packet_ref))
            for raw_ref in packet["prior_packet_refs"]:
                prior = restore_dataclass(raw_ref, storage.Reference)
                if prior.path.endswith(".selection-lifecycle.json") and prior not in anchored:
                    anchored.append(prior)
                    pending.append(prior)
                    if len(anchored) > MAX_HISTORY_RECORDS:
                        raise ValueError("Native history ancestry exceeds bound")
        if path.exists() and not set(anchored) <= set(result):
            raise ValueError("Native history index omits retained ready evidence")
        if not path.exists():
            result = anchored
    if len(set(result)) != len(result) or len(result) > MAX_HISTORY_RECORDS:
        raise ValueError("Duplicate or oversized native history index")
    for ref in result:
        if Path(ref.path).parent != cache / "native" or Path(ref.path).name != ref.sha256 + ".selection-lifecycle.json":
            raise ValueError("Native history reference outside its exact namespace")
        storage.read_reference(ref)
    return result


def register_history(cache: Path, reference: storage.Reference) -> None:
    """Import owns this append, after accepted immutable proof and before ready.

    Exclusive lifecycle bytes and pending recovery survive an interrupted index
    write. The previous complete index/ready anchor is never inferred from mtime.
    """
    from digest.adapters.storage.edition import write_record

    cache = cache.resolve().relative_to(Path.cwd().resolve())
    previous = history_references(cache)
    expected = set(previous) | {reference}
    actual = {storage.Reference(path.as_posix(), path.name.removesuffix(".selection-lifecycle.json"))
              for path in (cache / "native").glob("*.selection-lifecycle.json")}
    if actual != expected:
        raise ValueError("Unindexed or missing native lifecycle evidence; HOLD")
    write_record(cache / HISTORY_FILE, {
        "schema": "native-selection-history/v1",
        "records": [asdict(ref) for ref in sorted(expected, key=lambda ref: ref.path)],
    })


def verified_history(cache: Path, *, now: datetime) -> list[Judgment]:
    """Rebuild each full historical accepted chain using its captured config.

    No current eligibility/seen/recency checks are applied to historical proofs.
    Invalid, foreign, future or conflicting evidence fails closed before collect.
    Filesystem mtime and lexicographic hashes never decide judgment precedence.
    """
    cache = cache.resolve().relative_to(Path.cwd().resolve())
    owner = prepared_delivery._owner()[1]
    result: list[Judgment] = []
    total = 0
    paths = sorted((cache / "native").glob("*.selection-lifecycle*"))
    references = history_references(cache)
    if {item.path for item in references} != {path.as_posix() for path in paths}:
        raise ValueError("Native history index differs from retained lifecycle objects; HOLD")
    if len(paths) > MAX_HISTORY_RECORDS:
        raise ValueError("Native history capacity exceeded; no history was truncated")
    dated: dict[tuple[str, str], str] = {}
    for path in paths:
        suffix = ".selection-lifecycle.json"
        if not path.name.endswith(suffix):
            raise ValueError("Partial native lifecycle checkpoint; HOLD")
        ref = storage.Reference(path.as_posix(), path.name.removesuffix(suffix))
        if ref not in references:
            raise ValueError("Native history reference differs from durable history index")
        raw = storage.read_reference(ref)
        total += len(raw)
        if total > MAX_HISTORY_BYTES:
            raise ValueError("Native history byte capacity exceeded; HOLD")
        record = storage.decode(raw)
        refs = {k: restore_dataclass(v, storage.Reference) for k, v in record["references"].items()}
        if set(refs) != {"packet", "response", "review", "acceptance"}:
            raise ValueError("Native history requires the complete accepted chain")
        packet, _ = native_selection.source_packet(storage.read_reference(refs["packet"]))
        planned = datetime.fromisoformat(packet["planned_at"])
        if (planned.tzinfo is None or planned.utcoffset() is None or planned >= now
                or planned.date().isoformat() != packet["publication_date"]
                or packet["publication_date"] >= now.date().isoformat()):
            raise ValueError("Native history must precede the new UTC edition boundary")
        if packet["owner_sha256"] != owner:
            raise ValueError("Foreign native history owner; HOLD")
        final_raw = storage.read_reference(refs["acceptance"])
        final = storage.decode(final_raw)
        projection_ref = storage.Reference(
            str(Path(refs["packet"].path).parent / (final["projection_sha256"] + ".composition.json")),
            final["projection_sha256"],
        )
        projected = storage.decode(storage.read_reference(projection_ref))
        pending = storage.PendingNative(
            2, owner, packet["publication_date"], refs["packet"], refs["response"], refs["review"],
            projection_ref, refs["acceptance"],
        )
        config: Config = restore_dataclass(packet["effective_configuration"], Config)
        rebuilt = native_selection.projection(
            pending, storage.read_reference(refs["response"]), storage.read_reference(refs["review"]), config,
        )
        if encode(rebuilt) != encode(projected):
            raise ValueError("Historical native projection differs from exact accepted sources")
        native_selection.validate_acceptance(final_raw, projection_ref, projected)
        expected = build_lifecycle_record(**native_selection.lifecycle_arguments(pending, projected))
        if encode(expected) != raw:
            raise ValueError("Historical native lifecycle differs from its accepted chain")
        for row in record["candidates"]:
            key = (packet["publication_date"], row["identity"])
            if key in dated and dated[key] != ref.sha256:
                raise ValueError("Conflicting native judgments within one UTC edition; HOLD")
            dated[key] = ref.sha256
            result.append(Judgment(
                ref, refs["packet"], packet["planned_at"], packet["publication_date"], editorial_policy(config),
                CandidateArticle(**row["original"]), row["decision"]["status"], row["source_accepted"],
            ))
    return sorted(result, key=lambda item: (item.day, item.planned_at))


@dataclass
class ProspectivePlan:
    pool: CandidatePool
    originals: dict[str, dict[str, Any]]
    audit: dict[str, Any]


def reconcile_pool(pool: CandidatePool, config: Config, history: list[Judgment], *, now: datetime) -> ProspectivePlan:
    """Consume already canonically reconciled work, never manufacture dispositions."""
    from digest.domain.catalog.articles import article_hash

    by_id: dict[str, list[Judgment]] = {}
    for item in history:
        by_id.setdefault(article_hash(item.article.title, item.article.link), []).append(item)
    originals: dict[str, dict[str, Any]] = {}
    rows: list[dict[str, Any]] = []
    blockers = []
    policy = editorial_policy(config)
    for identity, candidate in sorted(pool.progress.candidates.items()):
        fingerprint = content_fingerprint(candidate.article)
        judgments = by_id.get(identity, [])
        same_content = [item for item in judgments if content_fingerprint(item.article) == fingerprint]
        matching = [item for item in same_content if item.policy == policy]
        prior = matching[-1] if matching else None
        if same_content and same_content[-1].status in {"needs_enrichment", "card"}:
            # Policy wording cannot provide missing source evidence.
            prior = same_content[-1]
        action, reason = "admit", "eligible_unfinished"
        if any(item.source_accepted for item in judgments) and candidate.delivery_cache_observed_at is None:
            action, reason = "recovery_required", "previous_source_accepted_without_delivery_evidence"
            blockers.append(identity)
        elif not candidate.eligible:
            action, reason = "excluded", candidate.eligibility_reason
        elif candidate.status != "not_presented":
            action, reason = "original_owner", "provider_" + candidate.status
        elif any(item.source_accepted for item in judgments):
            # Canonical retained delivery observation is not a new receipt. This
            # matches provider recovery's protection after the pruned seen horizon.
            action, reason = "excluded", "retained_delivery_observation"
        elif prior is not None:
            transition = (prior.status in {"withheld", "duplicate"}
                          and prior.article.published is not None
                          and datetime.fromisoformat(prior.article.published) > datetime.fromisoformat(prior.planned_at)
                          and now >= datetime.fromisoformat(prior.article.published))
            if transition:
                reason = "previously_future_source_timestamp_now_current"
            elif prior.status in {"withheld", "duplicate"}:
                action, reason = "native_suppressed", "unchanged_" + prior.status
            elif prior.status == "needs_enrichment":
                action, reason = "needs_enrichment", "unchanged_evidence_requires_enrichment"
            elif prior.status == "capacity_deferred":
                reason = "retained_capacity_deferred"
            else:
                action, reason = "needs_enrichment", "proposed_card_not_source_accepted"
        elif judgments:
            reason = "changed_content_or_editorial_policy"
        if action == "admit":
            originals[identity] = asdict(candidate.article)
        rows.append({
            "identity": identity, "occurrence_sha256": occurrence_sha256(candidate.article),
            "content_sha256": fingerprint, "provider_status": candidate.status,
            "action": action, "reason": reason,
            "prior_judgment": asdict(prior.record) if prior else None,
        })
    audit = {
        "schema": PLAN_SCHEMA, "route": ROUTE, "planned_at": now.isoformat(),
        "content_fingerprint": FINGERPRINT, "editorial_policy_sha256": policy,
        "configuration_sha256": hashlib.sha256(encode(asdict(config))).hexdigest(),
        "history": [asdict(ref) for ref in sorted({item.record for item in history}, key=lambda ref: ref.path)],
        "candidates": rows, "admitted_count": len(originals),
        "enrichment_count": sum(row["action"] == "needs_enrichment" for row in rows),
        "recovery_required": blockers, "collection": pool.progress.latest_collection_json,
        "limits": "RSS evidence only; no source extraction, new ranking, provider decision or delivery proof",
    }
    if len(encode(audit)) > storage.MAX_BYTES:
        raise ValueError("Prospective full-pool audit exceeds bound; no truncation")
    return ProspectivePlan(pool, originals, audit)


async def collect_plan(config: Config, *, route: str, cache: Path = Path(".cache"),
                       now: datetime | None = None) -> ProspectivePlan:
    """Collect every eligible observed identity plus canonical unfinished history.

    Retains inventory before source allocation slots; no plan_packet truncation.
    Only immutable observed source objects/fetch stats may be written before the
    caller's all-role capacity admission. Working-set checkpoint is separate.
    """
    from digest.adapters.storage.sources import load_stats, save_source_category_map
    from digest.radar import AllFeedsFailedError, collect
    from digest.radar.collector import _prune_cache

    if route != ROUTE:
        raise ValueError("Explicit versioned prospective selection route required")
    instant = prepared_delivery._instant(now)
    native._identity(config, instant.date(), instant)
    native._guard(cache, instant.date(), instant)
    storage.block_pending(cache)
    history = verified_history(cache, now=instant)
    effective, priorities = native._portfolio(config, cache)
    pool = _candidate_setup(str(cache))
    # Native retirement does not forge provider eligibility or disposition.
    # Restore its independently indexed originals before canonical reconciliation.
    from digest.adapters.storage.candidate_objects import load_candidate, load_candidate_packets, packet_key
    from digest.domain.catalog.articles import article_hash

    known_packets = {packet_key(packet) for packet in pool.progress.packets}
    for identity in sorted({article_hash(item.article.title, item.article.link) for item in history}):
        if identity not in pool.progress.candidates:
            candidate = load_candidate(identity, cache)
            if candidate is None:
                raise ValueError("Native history lost its canonical retained candidate; HOLD")
            pool.progress.candidates[identity] = candidate
            for packet in load_candidate_packets(identity, cache):
                if packet_key(packet) not in known_packets:
                    pool.progress.packets.append(packet)
                    known_packets.add(packet_key(packet))
    metrics: dict[str, Any] = {}
    failed = False
    try:
        articles, _ = await collect(
            effective, effective_priorities=priorities, fetch_metrics=metrics, inventory=pool.inventory,
        )
    except AllFeedsFailedError:
        failed, articles = True, {}
    merge_candidates(
        pool.progress, pool.inventory.eligible_articles(), effective, _prune_cache(pool.delivered), priorities,
        now=instant, inventory=pool.inventory, delivery_history=pool.delivered, cache_dir=cache,
    )
    plan = reconcile_pool(pool, config, history, now=instant)
    plan.audit["collection_failed"] = failed
    if failed and not plan.originals:
        raise AllFeedsFailedError("All feeds failed and no selectable retained native work is available")
    _save_prepared_fetch_stats(load_stats(str(cache), strict=True), metrics, articles, config, str(cache), False)
    save_source_category_map(effective.enabled_sources, str(cache))
    return plan


def checkpoint_plan(plan: ProspectivePlan, *, cache: Path = Path(".cache")) -> storage.Reference:
    """After complete-role admission: verified history retirement, then audit."""
    # Index originals and their original provider proofs before active removal.
    # Native judgment proof remains in the independent accepted-chain index.
    for row in plan.audit["candidates"]:
        if row["action"] in {"native_suppressed", "needs_enrichment"}:
            candidate = plan.pool.progress.candidates[row["identity"]]
            index_candidate(candidate, plan.pool.progress, cache)
            del plan.pool.progress.candidates[row["identity"]]
    checkpoint_candidates(plan.pool.progress, cache)
    return storage.put_bytes(encode(plan.audit), cache, "selection-plan")
