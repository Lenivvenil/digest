"""Compatibility exports for the candidate object-store adapter.

New production callers import digest.adapters.storage.candidate_objects directly.
"""

from digest.adapters.storage.candidate_objects import (
    MAX_OBJECT_BYTES,
    decode_active_candidate,
    digest,
    encode_active_candidate,
    freeze_packet,
    list_excluded,
    load_candidate,
    load_candidate_packets,
    packet_key,
    put_article,
    read_article,
    read_candidate_header,
    read_packet,
    read_policy,
    read_report_record,
    save_candidate,
    write_policy,
)

__all__ = [
    "MAX_OBJECT_BYTES",
    "digest",
    "put_article",
    "read_article",
    "packet_key",
    "freeze_packet",
    "read_report_record",
    "read_packet",
    "load_candidate_packets",
    "encode_active_candidate",
    "decode_active_candidate",
    "read_candidate_header",
    "load_candidate",
    "save_candidate",
    "list_excluded",
    "read_policy",
    "write_policy",
]
