"""One whole-issue reservation; the caller must durably push it before sending."""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timezone
from pathlib import Path

from digest._util import atomic_json_write

ISSUE_FILE = "compact_issue.json"
_TERMINAL = {"confirmed", "not_sent", "failed_no_delivery"}
_STATES = _TERMINAL | {"reserved", "sending", "partial", "unknown"}


@dataclass(frozen=True)
class _Record:
    schema: int
    generation: str
    date: str
    config_sha256: str
    owner_sha256: str
    state: str
    accepted_count: int | None = 0
    attempted_count: int | None = 0


def _safe(path: Path) -> Path:
    path = Path(os.path.abspath(path))
    if any(item.is_symlink() for item in (path, *path.parents)):
        raise ValueError("Compact issue paths must not contain symlinks.")
    return path


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _bindings(config_path: str | Path) -> tuple[str, str]:
    owner = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not re.fullmatch(r"[1-9][0-9]*", owner):
        raise ValueError("Compact issue delivery requires a positive private TELEGRAM_CHAT_ID.")
    return _sha(_safe(Path(config_path)).read_bytes()), _sha(owner.encode())


def _valid_counts(state: str, accepted: int | None, attempted: int | None) -> bool:
    if state == "unknown" and accepted is None and attempted is None:
        return True
    if type(accepted) is not int or type(attempted) is not int or accepted < 0 or attempted < accepted:
        return False
    return ((state not in {"reserved", "sending", "not_sent"} or attempted == 0)
            and (state not in {"confirmed", "partial"} or accepted > 0)
            and (state != "confirmed" or accepted == attempted)
            and (state != "failed_no_delivery" or accepted == 0))


def _read(path: Path) -> tuple[_Record, str]:
    raw = _safe(path).read_bytes()
    try:
        if len(raw) > 4096:
            raise ValueError
        record = _Record(**json.loads(raw))
        if (type(record.schema) is not int or record.schema != 1 or record.state not in _STATES
                or not re.fullmatch(r"[a-f0-9]{32}", record.generation)
                or not all(re.fullmatch(r"[a-f0-9]{64}", value)
                           for value in (record.config_sha256, record.owner_sha256))
                or date.fromisoformat(record.date).isoformat() != record.date
                or not _valid_counts(record.state, record.accepted_count, record.attempted_count)):
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid compact issue marker; inspect it before publishing.") from exc
    return record, _sha(raw)


def _write(path: Path, record: _Record) -> str:
    _safe(path)
    _safe(path.with_suffix(path.suffix + ".tmp"))
    atomic_json_write(path, asdict(record))
    return _sha(path.read_bytes())


def reserve(
    config_path: str | Path, cache_dir: str | Path = ".cache", now: datetime | None = None,
) -> tuple[Path, str]:
    """Reserve today's issue; callers serialize this with their runtime Git barrier."""
    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        raise ValueError("Compact issue reservation requires a timezone-aware time.")
    today = instant.astimezone(timezone.utc).date().isoformat()
    config_sha, owner_sha = _bindings(config_path)
    path = _safe(Path(cache_dir) / ISSUE_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        previous, _ = _read(path)
        if previous.state not in _TERMINAL or previous.date >= today:
            raise ValueError("Compact issue is already reserved or held; inspect it before publishing.")
    record = _Record(1, uuid.uuid4().hex, today, config_sha, owner_sha, "reserved")
    return path, _write(path, record)


@dataclass
class IssueGuard:
    """A validated reservation whose transitions fail closed on marker changes."""

    _path: Path
    _record: _Record
    _sha256: str

    @property
    def state(self) -> str:
        return self._record.state

    def _transition(self, record: _Record) -> None:
        if _read(self._path)[1] != self._sha256:
            raise ValueError("Compact issue marker changed; inspect it before publishing.")
        digest = _write(self._path, record)
        self._record, self._sha256 = record, digest

    def mark_sending(self) -> None:
        """Call synchronously immediately before the first POST; propagate failures."""
        if self.state != "reserved":
            raise ValueError("Compact issue sending requires an unused reservation.")
        self._transition(replace(self._record, state="sending"))

    def finish(self, outcome: str, *, accepted_count: int | None = None, attempted_count: int | None = None) -> None:
        """Record only known transport results; partial/unknown always remain held."""
        if outcome == "not_sent" and accepted_count is None and attempted_count is None:
            accepted_count = attempted_count = 0
        if (not _valid_counts(outcome, accepted_count, attempted_count)
                or outcome not in _STATES - {"reserved", "sending"}
                or self.state not in {"reserved", "sending"}
                or self.state == "reserved" and outcome != "not_sent"
                or outcome == "not_sent" and self.state != "reserved"):
            raise ValueError("Invalid compact issue delivery transition or counts.")
        self._transition(replace(self._record, state=outcome,
                                 accepted_count=accepted_count, attempted_count=attempted_count))


def load_guard(
    config_path: str | Path, expected_sha256: str, cache_dir: str | Path = ".cache",
) -> IssueGuard:
    """Validate the exact successfully pushed reservation before any delivery call."""
    path = _safe(Path(cache_dir) / ISSUE_FILE)
    record, digest = _read(path)
    if (not re.fullmatch(r"[a-f0-9]{64}", expected_sha256) or digest != expected_sha256
            or (record.config_sha256, record.owner_sha256) != _bindings(config_path)
            or record.state != "reserved"):
        raise ValueError("Compact issue reservation hash, binding or state mismatch; publishing blocked.")
    return IssueGuard(path, record, digest)
