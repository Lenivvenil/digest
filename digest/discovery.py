"""Compatibility exports for source discovery; production callers use the owners."""
from __future__ import annotations

from datetime import datetime, timezone

from digest.adapters.storage.discovery import (
    DELIVERY_FILE as DELIVERY_FILE,
)
from digest.adapters.storage.discovery import (
    METADATA_MAX_BYTES as METADATA_MAX_BYTES,
)
from digest.adapters.storage.discovery import (
    load_delivery as load_delivery,
)
from digest.adapters.storage.discovery import (
    save_delivery as save_delivery,
)
from digest.adapters.storage.pending_sources import (
    PENDING_FILE as PENDING_FILE,
)
from digest.adapters.storage.pending_sources import (
    load_pending as load_pending,
)
from digest.adapters.storage.pending_sources import (
    save_pending as save_pending,
)
from digest.adapters.storage.source_config import add_source_to_config as add_source_to_config
from digest.adapters.telegram.discovery import send_source_approval_message as send_source_approval_message
from digest.application.discovery import (
    prepare_pending_offers as prepare_pending_offers,
)
from digest.application.discovery import (
    prune_discovery_state as prune_discovery_state,
)
from digest.application.discovery import (
    record_source_history as record_source_history,
)
from digest.application.discovery import (
    send_reserved_proposals as send_reserved_proposals,
)
from digest.domain.catalog.exploration import (
    ProposalDelivery as ProposalDelivery,
)
from digest.domain.catalog.exploration import (
    select_exploration_area as select_exploration_area,
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


def resolve_pending_proposal(pending: list[PendingSource], hash8: str) -> PendingSource | None:
    """Compatibility entrypoint; callers with a decision time use the catalog rule."""
    return _resolve_pending_proposal(pending, hash8, now=datetime.now(tz=timezone.utc))
