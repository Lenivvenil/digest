"""Compatible prepared-edition entrypoints; concrete owners preserve persisted contracts.

The caller must durably publish both returned hashes outside this process before
sending. Local files are not a substitute for that external serialization barrier.
"""

from digest.adapters.storage.edition import CLAIM_FILE as CLAIM_FILE
from digest.adapters.storage.edition import READY_FILE as READY_FILE
from digest.adapters.storage.edition import RECEIPTS_FILE as RECEIPTS_FILE
from digest.adapters.storage.edition import (
    canonical_bytes,
    content_sha256,
    decode_claim,
    load_claim,
    load_edition,
    read_record,
    validate_manifest_record,
    verify_checkpoints,
    write_record,
)
from digest.adapters.telegram.prepared import accepted_message_id
from digest.application.prepared_delivery import _DISPATCH_SECONDS as _DISPATCH_SECONDS
from digest.application.prepared_delivery import _instant as _instant
from digest.application.prepared_delivery import _legacy_guard as _legacy_guard
from digest.application.prepared_delivery import _owner as _owner
from digest.application.prepared_delivery import claim_edition as claim_edition
from digest.application.prepared_delivery import inspect_edition as inspect_edition
from digest.application.prepared_delivery import mark_applied as mark_applied
from digest.application.prepared_delivery import prepare_edition as prepare_edition
from digest.application.prepared_delivery import send_prepared_edition as send_prepared_edition
from digest.domain.delivery.edition import SCHEMA_VERSION as SCHEMA_VERSION
from digest.domain.delivery.edition import (
    ChunkReceipt,
    Claim,
    Edition,
    PreparedArticle,
    Receipts,
    parse_instant,
    project_result,
)

# Private compatibility names remain aliases; new callers use the owning modules.
_sha = content_sha256
_canonical = canonical_bytes
_read = read_record
_write = write_record
_validate_manifest = validate_manifest_record
_load_edition = load_edition
_validate_claim = decode_claim
_load_claim = load_claim
_checkpoints = verify_checkpoints
_time = parse_instant
_result = project_result
_PreparedArticle = PreparedArticle
_Edition = Edition
_Claim = Claim
_ChunkReceipt = ChunkReceipt
_Receipts = Receipts
_accepted = accepted_message_id
