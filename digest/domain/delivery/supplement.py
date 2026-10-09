"""One immutable later-edition fragment and its separate transport coverage."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from datetime import date, timedelta

from digest._serialization import canonical_json_bytes
from digest.domain.investigation.delivered import DeliveredInvestigationInput


@dataclass(frozen=True)
class SupplementFragment:
    fragment_id: str
    origin: DeliveredInvestigationInput
    checkpoint: str
    result: str
    result_sha256: str
    investigated_at: str
    text: str


@dataclass(frozen=True)
class SupplementCoverage:
    fragment_id: str
    covering_chunks: tuple[int, ...]


@dataclass(frozen=True)
class PendingSupplement:
    fragment: SupplementFragment
    projection: str
    projection_sha256: str
    attempt: str


@dataclass(frozen=True)
class PreparedSupplement(PendingSupplement):
    coverage: SupplementCoverage


def fragment_identity(fragment: SupplementFragment) -> str:
    body = asdict(fragment)
    del body["fragment_id"]
    return hashlib.sha256(canonical_json_bytes(body)).hexdigest()


def fragment_window(fragment: SupplementFragment, publication_day: date) -> str:
    origin = date.fromisoformat(fragment.origin.publication_day)
    if publication_day >= origin + timedelta(days=4):
        return "expired"
    return "eligible" if origin < publication_day else "not_yet"
