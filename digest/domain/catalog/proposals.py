"""Pending source proposal values, exact identity, and decision eligibility."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone


def source_hash(url: str) -> str:
    """Return an 8-character hex hash of the URL for source decisions."""
    return hashlib.md5(url.encode(), usedforsecurity=False).hexdigest()[:8]


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
    pending: list[PendingSource], hash8: str, *, now: datetime,
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
        age = now - discovered
    except (TypeError, ValueError):
        return None
    if not timedelta(0) <= age <= timedelta(days=30):
        return None
    return source


def proposal_binding(source: PendingSource) -> str:
    """Bind a decision to the exact proposal, including its discovery timestamp."""
    identity = json.dumps(asdict(source), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()
