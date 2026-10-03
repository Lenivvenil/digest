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
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

import httpx
import yaml

from digest._util import atomic_json_write

logger = logging.getLogger(__name__)

PENDING_FILE = "pending_sources.json"
DELIVERY_FILE = "discovery_delivery.json"


def source_hash(url: str) -> str:
    """Return an 8-character hex hash of the URL for source decisions."""
    return hashlib.md5(url.encode()).hexdigest()[:8]


@dataclass
class PendingSource:
    name: str
    url: str
    category: str
    discovered_at: str
    source_hash: str = field(default="")

    def __post_init__(self) -> None:
        if not self.source_hash:
            self.source_hash = source_hash(self.url)


def resolve_pending_proposal(
    pending: list[PendingSource], hash8: str,
) -> PendingSource | None:
    """Resolve exactly one unexpired proposal whose hash matches its URL."""
    if not isinstance(hash8, str) or not re.fullmatch(r"[0-9a-f]{8}", hash8):
        return None
    matches = [source for source in pending if source.source_hash == hash8]
    if len(matches) != 1:
        return None
    source = matches[0]
    if any(not isinstance(value, str) or not value.strip() for value in asdict(source).values()):
        return None
    if source_hash(source.url) != hash8:
        return None
    try:
        discovered = datetime.fromisoformat(source.discovered_at)
        if discovered.tzinfo is None:
            discovered = discovered.replace(tzinfo=timezone.utc)
        age = datetime.now(tz=timezone.utc) - discovered
    except (TypeError, ValueError):
        return None
    if not timedelta(0) <= age <= timedelta(days=30):
        return None
    return source


def proposal_binding(source: PendingSource) -> str:
    """Bind a decision to the exact proposal, including its discovery timestamp."""
    identity = json.dumps(asdict(source), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def load_pending(cache_dir: str, *, strict: bool = False) -> list[PendingSource]:
    """Load proposals; strict mode raises on unreadable or malformed cache data."""
    path = Path(cache_dir) / PENDING_FILE
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict) or not isinstance(data.get("pending"), list):
            raise ValueError("Pending source cache must contain a pending list.")
        sources: list[PendingSource] = []
        for item in data["pending"]:
            try:
                if not isinstance(item, dict) or any(
                    not isinstance(item.get(key), str) or not item[key].strip()
                    for key in ("name", "url", "category", "discovered_at")
                ):
                    raise ValueError("Pending source entries must contain nonempty string identity fields.")
                datetime.fromisoformat(item["discovered_at"])
                hash8 = item.get("source_hash", source_hash(item["url"]))
                if not isinstance(hash8, str) or not re.fullmatch(r"[0-9a-f]{8}", hash8):
                    raise ValueError("Pending source hashes must contain eight lowercase hexadecimal characters.")
                sources.append(
                    PendingSource(
                        name=item["name"],
                        url=item["url"],
                        category=item["category"],
                        discovered_at=item["discovered_at"],
                        source_hash=hash8,
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                if strict:
                    raise
                logger.warning("Skipping malformed pending source entry: %s", exc)
        return sources
    except FileNotFoundError:
        return []
    except Exception as exc:
        logger.warning("Failed to load pending sources: %s", exc)
        if strict:
            raise
        return []


def save_pending(
    sources: list[PendingSource], cache_dir: str, *, strict: bool = False,
) -> None:
    """Save pending sources, pruning old entries; optionally propagate I/O errors."""
    path = Path(cache_dir) / PENDING_FILE
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=30)
    pruned: list[PendingSource] = []
    for s in sources:
        try:
            ts = datetime.fromisoformat(s.discovered_at)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts >= cutoff:
                pruned.append(s)
        except ValueError:
            pruned.append(s)
    data = {"pending": [asdict(s) for s in pruned]}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json_write(path, data)
    except Exception as exc:
        logger.warning("Failed to save pending sources: %s", exc)
        if strict:
            raise


@dataclass(frozen=True)
class ProposalDelivery:
    status: Literal["confirmed", "rejected", "unknown"]
    message_id: int | None = None


def load_delivery(cache_dir: str) -> dict[str, Any]:
    path = Path(cache_dir) / DELIVERY_FILE
    if not path.exists():
        return {"schema_version": 1, "deliveries": {}, "history": [], "batch": None}
    if path.stat().st_size > 256000:
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
    return data


def save_delivery(data: dict[str, Any], cache_dir: str) -> None:
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
    cache_dir: str, now: datetime,
) -> tuple[list[PendingSource], dict[str, Any], int]:
    """Preserve expiry audit before removing it from the compatible pending list."""
    pending = load_pending(cache_dir, strict=True)
    data = load_delivery(cache_dir)
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
        elif resolve_pending_proposal(pending, source.source_hash) is None:
            raise ValueError("Ambiguous or invalid pending source identity.")
        else:
            kept.append(source)
    data["batch"] = None
    save_delivery(data, cache_dir)
    save_pending(kept, cache_dir, strict=True)
    return kept, data, expired


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
        if (source is None or resolve_pending_proposal(pending, source.source_hash) != source
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
