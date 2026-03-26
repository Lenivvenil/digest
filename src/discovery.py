"""Source discovery approval workflow.

Handles pending source storage, Telegram approval messages, and writing
approved sources to config.yaml as trial entries.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from src._util import atomic_json_write

logger = logging.getLogger(__name__)

PENDING_FILE = "pending_sources.json"


def source_hash(url: str) -> str:
    """Return an 8-character hex hash of the URL (for callback_data)."""
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


def load_pending(cache_dir: str) -> list[PendingSource]:
    """Load pending sources from cache. Return empty list if missing."""
    path = Path(cache_dir) / PENDING_FILE
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            return []
        sources: list[PendingSource] = []
        for item in data.get("pending", []):
            try:
                sources.append(
                    PendingSource(
                        name=item["name"],
                        url=item["url"],
                        category=item["category"],
                        discovered_at=item["discovered_at"],
                        source_hash=item.get("source_hash", source_hash(item["url"])),
                    )
                )
            except (KeyError, TypeError) as exc:
                logger.warning("Skipping malformed pending source entry: %s", exc)
        return sources
    except Exception as exc:
        logger.warning("Failed to load pending sources: %s", exc)
        return []


def save_pending(sources: list[PendingSource], cache_dir: str) -> None:
    """Save pending sources to cache. Prunes entries older than 30 days."""
    path = Path(cache_dir) / PENDING_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
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
        atomic_json_write(path, data)
    except Exception as exc:
        logger.warning("Failed to save pending sources: %s", exc)


async def send_source_approval_message(
    source: PendingSource, bot_token: str, chat_id: str
) -> bool:
    """Send a Telegram message for a single discovered source with approve/reject buttons.

    callback_data format: src:ok:<hash> / src:no:<hash>  (max 15 bytes, well within 64)
    """
    text = (
        f"New RSS source suggested:\n\n"
        f"*{source.name}*\n"
        f"Category: {source.category}\n"
        f"URL: `{source.url}`\n\n"
        f"Add to config as a trial source?"
    )
    keyboard = {
        "inline_keyboard": [
            [
                {"text": "Add", "callback_data": f"src:ok:{source.source_hash}"},
                {"text": "Reject", "callback_data": f"src:no:{source.source_hash}"},
            ]
        ]
    }
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
        "reply_markup": keyboard,
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json=payload,
            )
            resp.raise_for_status()
            logger.info("Sent approval message for source '%s'", source.name)
            return True
    except Exception as exc:
        logger.warning("Failed to send approval message for '%s': %s", source.name, exc)
        return False


def add_source_to_config(config_path: str, source: PendingSource) -> None:
    """Append a trial source block to the sources list in config.yaml.

    Uses line-by-line text editing to preserve comments and formatting.
    Skips if a source with the same URL already exists.
    """
    path = Path(config_path)
    with path.open("r", encoding="utf-8") as fh:
        content = fh.read()
        lines = content.splitlines()

    # Check for duplicate URL
    for line in lines:
        if re.match(r"^\s+url:\s*[\"']?" + re.escape(source.url) + r"[\"']?\s*$", line):
            logger.info(
                "Source '%s' already exists in config (URL match), skipping", source.name
            )
            return

    # Build YAML block for the new trial source
    block = (
        f"\n  - name: \"{source.name}\"\n"
        f"    url: \"{source.url}\"\n"
        f"    category: \"{source.category}\"\n"
        f"    enabled: true\n"
        f"    priority: 3\n"
        f"    trial: true\n"
        f"    trial_days: 14\n"
    )

    bak_path = path.with_suffix(".yaml.bak")
    tmp_path = path.with_suffix(".yaml.tmp")
    try:
        import shutil as _shutil
        _shutil.copy2(path, bak_path)
    except OSError as exc:
        logger.warning("Could not create config backup '%s': %s", bak_path, exc)
        bak_path = None  # type: ignore[assignment]

    try:
        new_content = content.rstrip("\n") + block
        with tmp_path.open("w", encoding="utf-8") as fh:
            fh.write(new_content)
        tmp_path.replace(path)
        if bak_path is not None and bak_path.exists():
            bak_path.unlink(missing_ok=True)
        logger.info("Added trial source '%s' to config", source.name)
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        if bak_path is not None and bak_path.exists():
            logger.error(
                "config.yaml write failed — backup preserved at '%s' for manual recovery.",
                bak_path,
            )
        raise
