"""Source discovery approval workflow.

Handles pending source storage, Telegram approval messages, and writing
approved sources to config.yaml as trial entries.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import textwrap
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

import httpx
import yaml

from digest._util import atomic_json_write
from digest.adapters.storage.pending_sources import (
    PENDING_FILE as PENDING_FILE,
)
from digest.adapters.storage.pending_sources import (
    load_pending as load_pending,
)
from digest.adapters.storage.pending_sources import (
    save_pending as save_pending,
)
from digest.domain.catalog.proposals import (
    PendingSource as PendingSource,
)
from digest.domain.catalog.proposals import (
    proposal_binding as proposal_binding,
)
from digest.domain.catalog.proposals import (
    resolve_pending_proposal as _resolve_pending_proposal,
)
from digest.domain.catalog.proposals import (
    source_hash as source_hash,
)

logger = logging.getLogger(__name__)

DELIVERY_FILE = "discovery_delivery.json"
METADATA_MAX_BYTES = 256000


def resolve_pending_proposal(pending: list[PendingSource], hash8: str) -> PendingSource | None:
    """Compatibility entrypoint; callers with a decision time use the catalog rule."""
    return _resolve_pending_proposal(pending, hash8, now=datetime.now(tz=timezone.utc))


@dataclass(frozen=True)
class ProposalDelivery:
    status: Literal["confirmed", "rejected", "unknown"]
    message_id: int | None = None


def load_delivery(cache_dir: str) -> dict[str, Any]:
    path = Path(cache_dir) / DELIVERY_FILE
    if not path.exists():
        data: dict[str, Any] = {"schema_version": 1, "deliveries": {}, "history": [], "batch": None}
        _load_exploration_metadata(data)
        return data
    if path.stat().st_size > METADATA_MAX_BYTES:
        raise ValueError("Discovery metadata exceeds its storage bound.")
    data = json.loads(path.read_text())
    if (not isinstance(data, dict) or data.get("schema_version") != 1
            or not isinstance(data.get("deliveries"), dict) or not isinstance(data.get("history"), list)
            or data.get("batch") is not None and not isinstance(data["batch"], dict)):
        raise ValueError("Invalid discovery delivery metadata.")
    for key, value in data["deliveries"].items():
        if (not isinstance(key, str) or not re.fullmatch(r"[a-f0-9]{64}", key)
                or not isinstance(value, dict)
                or value.get("status") not in {"reserved", "confirmed", "rejected", "unknown"}
                or not isinstance(value.get("updated_at"), str)):
            raise ValueError("Invalid discovery delivery receipt.")
        if value["status"] == "confirmed" and (type(value.get("message_id")) is not int or value["message_id"] <= 0):
            raise ValueError("Confirmed proposal delivery requires a message receipt.")
        datetime.fromisoformat(value["updated_at"])
    for item in data["history"]:
        if (not isinstance(item, dict) or item.get("decision") not in {"approved", "rejected", "expired"}
                or any(not isinstance(item.get(key), str) for key in
                       ("binding", "url", "name", "category", "recorded_at"))):
            raise ValueError("Invalid discovery history.")
        datetime.fromisoformat(item["recorded_at"])
    _load_exploration_metadata(data)
    return data


def _metadata_stamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("Invalid discovery metadata timestamp.")
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        raise ValueError("Discovery exploration timestamps must include a timezone.")
    return stamp


def _area_name(value: Any) -> bool:
    return isinstance(value, str) and 1 <= len(value) <= 80 and value == value.strip()


def _binding(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None


def _cycle(value: Any) -> bool:
    return isinstance(value, str) and 1 <= len(value) <= 160


def _load_exploration_metadata(data: dict[str, Any]) -> None:
    """Optional schema-1 additions never change legacy proposal identities."""
    for key in ("proposal_areas", "area_offers", "validation_failures"):
        if not isinstance(data.setdefault(key, {}), dict):
            raise ValueError(f"Invalid discovery {key} metadata.")
    for binding, area in data["proposal_areas"].items():
        if not _binding(binding) or not _area_name(area):
            raise ValueError("Invalid discovery proposal area.")
    if len(data["area_offers"]) > 16:
        raise ValueError("Discovery area offers exceed the configured area bound.")
    for area, offer in data["area_offers"].items():
        if (not _area_name(area) or not isinstance(offer, dict)
                or offer.get("status") not in ("confirmed", "unknown")):
            raise ValueError("Invalid discovery area offer.")
        _metadata_stamp(offer.get("updated_at"))
    attempted = data.setdefault("attempted_areas", [])
    if (not isinstance(attempted, list) or len(attempted) > 16
            or any(not _area_name(area) for area in attempted) or len(set(attempted)) != len(attempted)):
        raise ValueError("Invalid discovery attempted areas.")
    for binding, failure in data["validation_failures"].items():
        if (not _binding(binding) or not isinstance(failure, dict)
                or not _cycle(failure.get("failed_cycle"))
                or "skipped_cycle" not in failure
                or failure["skipped_cycle"] is not None and (
                    not _cycle(failure["skipped_cycle"]) or failure["skipped_cycle"] == failure["failed_cycle"]
                )):
            raise ValueError("Invalid discovery validation failure.")
        _metadata_stamp(failure.get("failed_at"))
    generation = data.setdefault("generation", None)
    if generation is not None:
        if (not isinstance(generation, dict) or not _area_name(generation.get("requested_area"))
                or not _cycle(generation.get("cycle"))
                or generation.get("outcome") not in ("started", "failed", "empty", "no_valid_proposals", "proposed")
                or not isinstance(generation.get("bindings"), list) or len(generation["bindings"]) > 3
                or any(not _binding(binding) for binding in generation["bindings"])
                or len(set(generation["bindings"])) != len(generation["bindings"])):
            raise ValueError("Invalid discovery generation record.")
        _metadata_stamp(generation.get("requested_at"))


def save_delivery(data: dict[str, Any], cache_dir: str) -> None:
    if len(json.dumps(data, indent=2).encode("utf-8")) > METADATA_MAX_BYTES:
        raise ValueError("Discovery metadata exceeds its storage bound.")
    path = Path(cache_dir) / DELIVERY_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json_write(path, data)


def record_source_history(source: PendingSource, decision: str, cache_dir: str) -> None:
    """Preserve public proposal identity/decision before removing a pending item."""
    data = load_delivery(cache_dir)
    binding = proposal_binding(source)
    if not any(item.get("binding") == binding and item.get("decision") == decision for item in data["history"]):
        data["history"].append({"binding": binding, "url": source.url, "name": source.name,
                                "category": source.category, "decision": decision,
                                "recorded_at": datetime.now(tz=timezone.utc).isoformat()})
    save_delivery(data, cache_dir)


def prune_discovery_state(
    cache_dir: str, now: datetime, exploration_areas: list[str],
) -> tuple[list[PendingSource], dict[str, Any], int]:
    """Preserve expiry audit before removing it from the compatible pending list."""
    pending = load_pending(cache_dir, strict=True)
    data = load_delivery(cache_dir)
    _load_exploration_metadata(data)
    _retain_area_offers(data, exploration_areas)
    cutoff = now - timedelta(days=30)
    data["history"] = [item for item in data["history"]
                       if datetime.fromisoformat(item["recorded_at"]) >= cutoff]
    data["deliveries"] = {key: value for key, value in data["deliveries"].items()
                          if datetime.fromisoformat(value["updated_at"]) >= cutoff}
    kept = []
    expired = 0
    for source in pending:
        stamp = datetime.fromisoformat(source.discovered_at)
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        if stamp < cutoff:
            expired += 1
            binding = proposal_binding(source)
            if not any(item["binding"] == binding for item in data["history"]):
                data["history"].append({"binding": binding, "url": source.url,
                                        "name": source.name, "category": source.category,
                                        "decision": "expired", "recorded_at": now.isoformat()})
        elif _resolve_pending_proposal(pending, source.source_hash, now=datetime.now(tz=timezone.utc)) is None:
            raise ValueError("Ambiguous or invalid pending source identity.")
        else:
            kept.append(source)
    kept_bindings = {proposal_binding(source) for source in kept}
    retained_bindings = kept_bindings | set(data["deliveries"]) | {item["binding"] for item in data["history"]}
    data["proposal_areas"] = {key: area for key, area in data["proposal_areas"].items() if key in retained_bindings}
    data["validation_failures"] = {key: value for key, value in data["validation_failures"].items()
                                   if key in kept_bindings}
    data["batch"] = None
    save_delivery(data, cache_dir)
    save_pending(kept, cache_dir, strict=True)
    return kept, data, expired


def _retain_area_offers(data: dict[str, Any], areas: list[str]) -> None:
    """Keep one actual/possible offer per configured area before pruning receipts."""
    offers = {area: value for area, value in data["area_offers"].items() if area in areas}
    for binding, receipt in data["deliveries"].items():
        area = data["proposal_areas"].get(binding)
        if area not in areas or receipt["status"] not in {"confirmed", "unknown"}:
            continue
        current = offers.get(area)
        if current is None or _metadata_stamp(receipt["updated_at"]) > _metadata_stamp(current["updated_at"]):
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
    area = min(eligible, key=lambda item: _metadata_stamp(data["area_offers"][item]["updated_at"])
               if item in data["area_offers"] else oldest)
    data["attempted_areas"].append(area)
    return area


async def prepare_pending_offers(
    pending: list[PendingSource], data: dict[str, Any], configured: set[str],
    cycle: str, now: datetime, counts: dict[str, int],
) -> tuple[list[PendingSource], int]:
    """Retry failed exact proposals after one distinct prepare cycle, within three checks."""
    from digest.discovery_feed import validate_feed_url

    offers: list[PendingSource] = []
    validations = 0
    for source in pending:
        binding = proposal_binding(source)
        receipt = data["deliveries"].get(binding)
        if receipt is not None:
            counts["held"] += int(receipt["status"] in {"reserved", "unknown", "rejected"})
            continue
        if source.url in configured or validations == 3:
            continue
        failure = data["validation_failures"].get(binding)
        if failure is not None and (
            cycle == failure["failed_cycle"] or failure["skipped_cycle"] is None or cycle == failure["skipped_cycle"]
        ):
            if cycle != failure["failed_cycle"] and failure["skipped_cycle"] is None:
                failure["skipped_cycle"] = cycle
            counts["validation_deferred"] += 1
            continue
        validations += 1
        try:
            await validate_feed_url(source.url)
        except Exception as exc:
            counts["invalid_feed"] += 1
            data["validation_failures"][binding] = {
                "failed_at": now.isoformat(), "failed_cycle": cycle, "skipped_cycle": None,
            }
            logger.warning("Pending feed validation unavailable (%s)", type(exc).__name__)
            continue
        data["validation_failures"].pop(binding, None)
        offers.append(source)
    return offers, validations


async def send_source_approval_message(
    source: PendingSource, bot_token: str, chat_id: str, bot_username: str = "",
) -> ProposalDelivery:
    """Send source decision links, or commands when no valid bot username is set."""
    username_valid = isinstance(bot_username, str) and re.fullmatch(r"[A-Za-z0-9_]{5,32}", bot_username)
    text = (
        f"New RSS source suggested:\n\n"
        f"{source.name}\n"
        f"Category: {source.category}\n"
        f"URL: {source.url}\n\n"
        "Add to config as a trial source?\n\n"
    )
    if username_valid:
        text += "Tap Add or Reject, then tap Start.\n"
    text += "Or send " if username_valid else "Send "
    text += (
        f"/source ok {source.source_hash} to add it or "
        f"/source no {source.source_hash} to reject it.\n"
        "Your decision is collected on the next run. "
        "Telegram keeps uncollected decision messages for at most 24 hours."
    )
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
    }
    if username_valid:
        payload["reply_markup"] = {
            "inline_keyboard": [[
                {"text": "Add", "url": f"https://t.me/{bot_username}?start=source_ok_{source.source_hash}"},
                {"text": "Reject", "url": f"https://t.me/{bot_username}?start=source_no_{source.source_hash}"},
            ]],
        }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json=payload,
            )
            resp.raise_for_status()
            result = resp.json()
            if result.get("ok") is False:
                return ProposalDelivery("rejected")
            message_id = result.get("result", {}).get("message_id")
            if result.get("ok") is not True or type(message_id) is not int or message_id <= 0:
                return ProposalDelivery("unknown")
            return ProposalDelivery("confirmed", message_id)
    except httpx.HTTPStatusError as exc:
        # An explicit Bot API rejection is distinguishable from an uncertain send.
        try:
            rejected = exc.response.json().get("ok") is False
        except (ValueError, AttributeError):
            rejected = False
        return ProposalDelivery("rejected" if rejected else "unknown")
    except Exception as exc:
        logger.warning("Source proposal delivery uncertain (%s)", type(exc).__name__)
        return ProposalDelivery("unknown")


async def send_reserved_proposals(
    cache_dir: str, owner: str, target: str, token: str, chat: str, username: str,
    pending_sha: str | None, delivery_sha: str | None, counts: dict[str, int],
) -> dict[str, int]:
    """Send exactly one owned, externally persisted pair; any uncertainty holds replay."""
    pending_path, delivery_path = Path(cache_dir) / PENDING_FILE, Path(cache_dir) / DELIVERY_FILE
    if (not pending_sha or not delivery_sha
            or hashlib.sha256(pending_path.read_bytes()).hexdigest() != pending_sha
            or hashlib.sha256(delivery_path.read_bytes()).hexdigest() != delivery_sha):
        raise ValueError("Discovery persisted pair hash mismatch.")
    data = load_delivery(cache_dir)
    batch = data["batch"]
    if batch is None:
        counts.update(data.get("prepare_counts", {}))
        return counts
    if (batch.get("owner") != owner or batch.get("pending_sha256") != pending_sha
            or batch.get("target") != target or not isinstance(batch.get("bindings"), list)
            or len(batch["bindings"]) > 3 or len(set(batch["bindings"])) != len(batch["bindings"])):
        raise ValueError("Discovery batch ownership or binding mismatch.")
    counts.update(batch.get("prepare_counts", {}))
    pending = load_pending(cache_dir, strict=True)
    by_binding = {proposal_binding(source): source for source in pending}
    for binding in batch["bindings"]:
        source = by_binding.get(binding)
        receipt = data["deliveries"].get(binding, {})
        if (source is None
                or _resolve_pending_proposal(pending, source.source_hash, now=datetime.now(tz=timezone.utc)) != source
                or receipt.get("status") != "reserved" or receipt.get("owner") != owner):
            raise ValueError("Discovery offer no longer matches its reservation.")
    for binding in batch["bindings"]:
        receipt = data["deliveries"][binding]
        receipt.update(status="unknown", updated_at=datetime.now(tz=timezone.utc).isoformat())
        save_delivery(data, cache_dir)
        outcome = await send_source_approval_message(by_binding[binding], token, chat, username)
        receipt.update(status=outcome.status, message_id=outcome.message_id)
        save_delivery(data, cache_dir)
        counts[outcome.status] += 1
    return counts


def add_source_to_config(config_path: str, source: PendingSource) -> None:
    """Atomically add an idempotent trial source to the YAML sources list."""
    path = Path(config_path)
    with path.open("r", encoding="utf-8") as fh:
        content = fh.read()
    config = yaml.safe_load(content)
    if not isinstance(config, dict):
        raise ValueError("Cannot add a source: config must be a YAML mapping.")
    sources = config.setdefault("sources", [])
    if not isinstance(sources, list) or any(not isinstance(item, dict) for item in sources):
        raise ValueError("Cannot add a source: config.sources must be a list of mappings.")
    if any(item.get("url") == source.url for item in sources):
        logger.info("Source '%s' already exists in config (URL match), skipping", source.name)
        return
    entry = {
        "name": source.name,
        "url": source.url,
        "category": source.category,
        "enabled": True,
        "priority": 3,
        "trial": True,
        "trial_days": 14,
    }
    new_content = _insert_source_entry(content, entry)
    # Check the text edit before touching the file, including unusual YAML styles.
    updated = yaml.safe_load(new_content)
    expected = {**config, "sources": [*sources, entry]}
    if updated != expected:
        raise ValueError("Cannot safely insert a source into this YAML config layout.")

    bak_path = path.with_suffix(".yaml.bak")
    tmp_path = path.with_suffix(".yaml.tmp")
    try:
        shutil.copy2(path, bak_path)
    except OSError as exc:
        logger.error("Cannot create config backup, aborting source addition: %s", exc)
        raise

    try:
        with tmp_path.open("w", encoding="utf-8") as fh:
            fh.write(new_content)
        tmp_path.replace(path)
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        if bak_path.exists():
            logger.error(
                "config.yaml write failed — backup preserved at '%s' for manual recovery.",
                bak_path,
            )
        raise
    try:
        bak_path.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("Source added, but config backup cleanup failed: %s", exc)
    logger.info("Added trial source '%s' to config", source.name)


def _insert_source_entry(content: str, entry: dict[str, Any]) -> str:
    """Locate the sources sequence with YAML nodes and preserve existing text."""
    root = yaml.compose(content)
    if not isinstance(root, yaml.MappingNode):
        raise ValueError("Cannot add a source: config must be a YAML mapping.")
    matches = [value for key, value in root.value if key.value == "sources"]
    block = yaml.safe_dump([entry], sort_keys=False, allow_unicode=True)
    if not matches:
        return content.rstrip("\n") + "\nsources:\n" + textwrap.indent(block, "  ")
    if len(matches) != 1 or not isinstance(matches[0], yaml.SequenceNode):
        raise ValueError("Cannot add a source: config must have one sources list.")
    node = matches[0]
    if node.flow_style:
        # An inline sources list can be extended without rewriting its comments.
        index = node.end_mark.index - 1
        if content[index:index + 1] != "]":
            raise ValueError("Cannot safely insert a source into this YAML config layout.")
        serialized = yaml.safe_dump(entry, default_flow_style=True, sort_keys=False, allow_unicode=True).strip()
        separator = ", " if node.value else ""
        return content[:index] + separator + serialized + content[index:]
    if content[node.start_mark.index:node.start_mark.index + 1] != "-":
        raise ValueError("Cannot safely insert a source into an aliased or anchored sources list.")
    index = node.end_mark.index
    # The end mark may include indentation preceding the next top-level key.
    line_start = index - node.end_mark.column
    if not content[line_start:index].strip():
        index = line_start
    prefix = content[:index]
    if prefix and not prefix.endswith("\n"):
        prefix += "\n"
    return prefix + textwrap.indent(block, " " * node.start_mark.column) + content[index:]
