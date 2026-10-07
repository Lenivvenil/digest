"""Exploration, pruning, retry and reservation policy over the existing metadata.

State shapes and mutations remain compatible; decision times are supplied by the
application independently at each existing observation point.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from digest.domain.catalog.proposals import PendingSource, proposal_binding, resolve_pending_proposal


@dataclass(frozen=True)
class ProposalDelivery:
    status: Literal["confirmed", "rejected", "unknown"]
    message_id: int | None = None


def metadata_stamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("Invalid discovery metadata timestamp.")
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        raise ValueError("Discovery exploration timestamps must include a timezone.")
    return stamp


def retain_area_offers(data: dict[str, Any], areas: list[str]) -> None:
    """Keep one actual/possible offer per configured area before pruning receipts."""
    offers = {area: value for area, value in data["area_offers"].items() if area in areas}
    for binding, receipt in data["deliveries"].items():
        area = data["proposal_areas"].get(binding)
        if area not in areas or receipt["status"] not in {"confirmed", "unknown"}:
            continue
        current = offers.get(area)
        if current is None or metadata_stamp(receipt["updated_at"]) > metadata_stamp(current["updated_at"]):
            offers[area] = {"updated_at": receipt["updated_at"], "status": receipt["status"]}
    data["area_offers"] = offers
    data["attempted_areas"] = [area for area in data["attempted_areas"] if area in areas]


def select_exploration_area(data: dict[str, Any], areas: list[str]) -> str:
    """Prefer least-recent offers within a fair pass; failed attempts are not offers."""
    eligible = [area for area in areas if area not in data["attempted_areas"]]
    if not eligible:
        data["attempted_areas"] = []
        eligible = areas
    oldest = datetime.min.replace(tzinfo=timezone.utc)
    area = min(eligible, key=lambda item: metadata_stamp(data["area_offers"][item]["updated_at"])
               if item in data["area_offers"] else oldest)
    data["attempted_areas"].append(area)
    return area


def has_source_history(data: dict[str, Any], source: PendingSource, decision: str) -> bool:
    binding = proposal_binding(source)
    return any(item.get("binding") == binding and item.get("decision") == decision for item in data["history"])


def append_source_history(data: dict[str, Any], source: PendingSource, decision: str, *, now: datetime) -> None:
    data["history"].append({"binding": proposal_binding(source), "url": source.url, "name": source.name,
                            "category": source.category, "decision": decision, "recorded_at": now.isoformat()})


def prune_recent_metadata(data: dict[str, Any], areas: list[str], *, now: datetime) -> None:
    """Fold actual/possible offers before the original 30-day receipt/history pruning."""
    retain_area_offers(data, areas)
    cutoff = now - timedelta(days=30)
    data["history"] = [item for item in data["history"]
                       if datetime.fromisoformat(item["recorded_at"]) >= cutoff]
    data["deliveries"] = {key: value for key, value in data["deliveries"].items()
                          if datetime.fromisoformat(value["updated_at"]) >= cutoff}


def expire_pending_proposal(data: dict[str, Any], source: PendingSource, *, now: datetime) -> bool:
    stamp = datetime.fromisoformat(source.discovered_at)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    if stamp >= now - timedelta(days=30):
        return False
    binding = proposal_binding(source)
    if not any(item["binding"] == binding for item in data["history"]):
        append_source_history(data, source, "expired", now=now)
    return True


def finish_pruning(data: dict[str, Any], kept: list[PendingSource]) -> None:
    kept_bindings = {proposal_binding(source) for source in kept}
    retained_bindings = kept_bindings | set(data["deliveries"]) | {item["binding"] for item in data["history"]}
    data["proposal_areas"] = {key: area for key, area in data["proposal_areas"].items() if key in retained_bindings}
    data["validation_failures"] = {key: value for key, value in data["validation_failures"].items()
                                   if key in kept_bindings}
    data["batch"] = None


def pending_offer_eligible(
    source: PendingSource, data: dict[str, Any], configured: set[str], cycle: str,
    validations: int, counts: dict[str, int],
) -> bool:
    """Hold every existing receipt; defer failed validation for one distinct cycle."""
    binding = proposal_binding(source)
    receipt = data["deliveries"].get(binding)
    if receipt is not None:
        counts["held"] += int(receipt["status"] in {"reserved", "unknown", "rejected"})
        return False
    if source.url in configured or validations == 3:
        return False
    failure = data["validation_failures"].get(binding)
    if failure is not None and (
        cycle == failure["failed_cycle"] or failure["skipped_cycle"] is None or cycle == failure["skipped_cycle"]
    ):
        if cycle != failure["failed_cycle"] and failure["skipped_cycle"] is None:
            failure["skipped_cycle"] = cycle
        counts["validation_deferred"] += 1
        return False
    return True


def record_validation_failure(data: dict[str, Any], binding: str, cycle: str, *, now: datetime) -> None:
    data["validation_failures"][binding] = {
        "failed_at": now.isoformat(), "failed_cycle": cycle, "skipped_cycle": None,
    }


def reserve_offers(data: dict[str, Any], offers: list[PendingSource], owner: str, *, now: datetime) -> None:
    for source in offers:
        data["deliveries"][proposal_binding(source)] = {
            "status": "reserved", "updated_at": now.isoformat(), "owner": owner,
        }


def bind_batch(
    data: dict[str, Any], offers: list[PendingSource], owner: str, target: str,
    pending_sha: str, counts: dict[str, int],
) -> None:
    data["batch"] = {"owner": owner, "pending_sha256": pending_sha, "target": target,
                     "bindings": [proposal_binding(source) for source in offers],
                     "prepare_counts": counts.copy()}
    counts["prepared"] = len(offers)
    data["batch"]["prepare_counts"] = counts.copy()


def validate_batch(batch: dict[str, Any], owner: str, target: str, pending_sha: str | None) -> None:
    if (batch.get("owner") != owner or batch.get("pending_sha256") != pending_sha
            or batch.get("target") != target or not isinstance(batch.get("bindings"), list)
            or len(batch["bindings"]) > 3 or len(set(batch["bindings"])) != len(batch["bindings"])):
        raise ValueError("Discovery batch ownership or binding mismatch.")


def validate_reserved_proposal(
    pending: list[PendingSource], source: PendingSource, receipt: dict[str, Any], owner: str, *, now: datetime,
) -> None:
    if (resolve_pending_proposal(pending, source.source_hash, now=now) != source
            or receipt.get("status") != "reserved" or receipt.get("owner") != owner):
        raise ValueError("Discovery offer no longer matches its reservation.")
