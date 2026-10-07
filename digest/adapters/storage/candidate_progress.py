"""Active candidate checkpoint codec and archive-reference persistence.

Retirement is an explicit application operation; these functions never select
which candidates are complete or ineligible.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from digest._serialization import canonical_json_bytes as _canonical
from digest._serialization import restore_dataclass as _restore
from digest._serialization import unique_object as _unique_object
from digest._util import atomic_json_write
from digest.adapters.storage.checkpoints import safe_checkpoint_path as _safe
from digest.domain.editorial.candidates import Candidate, CandidateArticle, CandidatePacket, CandidateProgress
from digest.domain.editorial.candidates import validate_progress as _validate
from digest.domain.editorial.reviews import BlindReviewReport

CANDIDATE_FILE = "candidate_progress.json"
MAX_BYTES = 32_000_000

def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _active_candidate_body(candidate: Candidate) -> dict[str, Any]:
    raw = asdict(candidate)
    del raw["article"], raw["occurrences"]
    return {"candidate": raw, "article_ref": _digest(asdict(candidate.article)),
            "occurrence_refs": [_digest(asdict(article)) for article in candidate.occurrences]}


def _active_packet_body(packet: CandidatePacket, cache_dir: str | Path | None) -> dict[str, Any]:
    from digest.adapters.storage.candidate_objects import packet_key, read_packet

    key = packet_key(packet)
    path = _safe(Path(cache_dir) / "candidate_reports" / f"{key}.json") if cache_dir is not None else None
    if path is not None and cache_dir is not None and path.exists():
        if read_packet(key, cache_dir) != replace(packet, handed_to_preparation=False):
            raise ValueError("Active candidate packet differs from its frozen evidence.")
        return {"packet_ref": key, "handed_to_preparation": packet.handed_to_preparation}
    raw = asdict(packet)
    del raw["articles"]
    raw["article_refs"] = [_digest(asdict(article)) for article in packet.articles]
    return {"packet": raw}


def _working_set(progress: CandidateProgress, cache_dir: str | Path | None) -> dict[str, Any]:
    _validate(progress)
    return {"kind": "candidate_working_set", "schema_version": 1,
            "candidates": {identity: _active_candidate_body(candidate)
                           for identity, candidate in progress.candidates.items()},
            "packets": [_active_packet_body(packet, cache_dir) for packet in progress.packets],
            "latest_collection_json": progress.latest_collection_json, "policy_sha256": progress.policy_sha256}


def _working_record(progress: CandidateProgress, cache_dir: str | Path | None) -> dict[str, Any]:
    body = _working_set(progress, cache_dir)
    return {"candidate_accounting": body, "sha256": _digest(body)}


def progress_size(progress: CandidateProgress, cache_dir: str | Path | None = None) -> int:
    """Measure only the current working checkpoint, never indexed historical bodies."""
    return len(json.dumps(_working_record(progress, cache_dir), indent=2).encode("utf-8"))


def _materialize_collection(value: str, cache_dir: str | Path) -> str:
    from digest.adapters.storage.candidate_objects import put_article

    raw = json.loads(value)
    sources = {item["source"]: item["url"] for item in raw.get("sources", [])}
    for observation in raw.get("observations", []):
        article = observation.pop("article", None)
        if article is not None:
            saved = CandidateArticle(article["title"], article["link"], article["description"], article["source"],
                                     article["category"], article["pub_date"],
                                     observation.get("source_url") or sources[article["source"]])
            observation["article_reference"] = {"identity": observation["identity"],
                                                "occurrence_sha256": put_article(saved, cache_dir)}
        reference = observation["article_reference"]
        if (reference["identity"] != observation["identity"]
                or not re.fullmatch(r"[0-9a-f]{64}", reference["occurrence_sha256"])):
            raise ValueError("Collection accounting source identity mismatch.")
    return json.dumps(raw, ensure_ascii=False, sort_keys=True)


def _restore_active_packet(raw: Any, cache_dir: str | Path) -> CandidatePacket:
    from digest.adapters.storage.candidate_objects import read_article, read_packet

    if not isinstance(raw, dict):
        raise ValueError("Invalid active candidate packet.")
    if set(raw) == {"packet_ref", "handed_to_preparation"}:
        if type(raw["handed_to_preparation"]) is not bool:
            raise ValueError("Invalid candidate preparation handoff flag.")
        return replace(read_packet(raw["packet_ref"], cache_dir),
                       handed_to_preparation=raw["handed_to_preparation"])
    if set(raw) != {"packet"} or not isinstance(raw["packet"], dict):
        raise ValueError("Invalid active candidate packet fields.")
    body = dict(raw["packet"])
    refs = body.pop("article_refs", None)
    if not isinstance(refs, list) or "articles" in body:
        raise ValueError("Invalid active candidate source references.")
    body["articles"] = [asdict(read_article(sha, cache_dir)) for sha in refs]
    packet: CandidatePacket = _restore(body, CandidatePacket)
    return packet


def load_candidate_progress(cache_dir: str | Path = ".cache") -> CandidateProgress:
    from digest.adapters.storage.candidate_objects import decode_active_candidate

    path = _safe(Path(cache_dir) / CANDIDATE_FILE)
    if not path.exists():
        return CandidateProgress()
    if path.stat().st_size > MAX_BYTES:
        raise ValueError(f"Candidate progress exceeds {MAX_BYTES}-byte budget.")
    record = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
    if (not isinstance(record, dict) or set(record) != {"candidate_accounting", "sha256"}
            or record["sha256"] != _digest(record["candidate_accounting"])):
        raise ValueError("Candidate progress hash or envelope mismatch.")
    body = record["candidate_accounting"]
    if (not isinstance(body, dict) or set(body) != {"kind", "schema_version", "candidates", "packets",
                                                  "latest_collection_json", "policy_sha256"}
            or body["kind"] != "candidate_working_set" or type(body["schema_version"]) is not int
            or body["schema_version"] != 1 or not isinstance(body["candidates"], dict)
            or not isinstance(body["packets"], list) or not isinstance(body["latest_collection_json"], str)
            or not isinstance(body["policy_sha256"], str)):
        raise ValueError("Unsupported candidate prototype checkpoint; regenerate draft candidate accounting.")
    policy_sha = body["policy_sha256"]
    if policy_sha and (len(policy_sha) != 64 or any(char not in "0123456789abcdef" for char in policy_sha)):
        raise ValueError("Invalid active candidate policy hash.")
    progress = CandidateProgress(
        candidates={identity: decode_active_candidate(raw, cache_dir) for identity, raw in body["candidates"].items()},
        packets=[_restore_active_packet(raw, cache_dir) for raw in body["packets"]],
        latest_collection_json=body["latest_collection_json"], policy_sha256=policy_sha)
    _validate(progress)
    return progress


def _report_accounting_path(report: BlindReviewReport, cache_dir: str | Path) -> Path:
    return _safe(Path(cache_dir) / "candidate_reports" / f"{_digest(asdict(report))}.json")


def candidate_accounting_sources(
    report: BlindReviewReport, cache_dir: str | Path = ".cache",
) -> list[Path]:
    """Verify and enumerate exact source objects for accepted checkpoint hash refs."""
    from digest.adapters.storage.candidate_objects import read_report_record

    if not _report_accounting_path(report, cache_dir).exists():
        return []
    record = read_report_record(_digest(asdict(report)), cache_dir)
    collection = json.loads(record["packet"]["collection_json"])
    keys = [*record["packet"]["article_refs"], *(item["article_reference"]["occurrence_sha256"]
             for item in collection.get("observations", []))]
    from digest.adapters.storage.candidate_objects import read_article

    for key in keys:
        read_article(key, cache_dir)
    return [_safe(Path(cache_dir) / "candidate_sources" / f"{sha}.json")
            for sha in dict.fromkeys(keys)]


def archive_candidate_accounting(
    report: BlindReviewReport, archive: Path, cache_dir: str | Path = ".cache",
) -> Path | None:
    """Copy exact immutable packet accounting; legacy accepted reports need none."""
    from digest.adapters.storage.candidate_objects import read_report_record

    frozen = _report_accounting_path(report, cache_dir)
    if not frozen.exists():
        return None
    payload = read_report_record(_digest(asdict(report)), cache_dir)
    path = _safe(Path(str(archive) + ".candidates.json"))
    _safe(path.with_suffix(path.suffix + ".tmp"))
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.stat().st_size > MAX_BYTES:
            raise ValueError("Existing candidate accounting archive exceeds its byte budget.")
        saved = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
        if saved != payload:
            raise ValueError("Existing candidate accounting archive differs from frozen report accounting.")
        return path
    atomic_json_write(path, payload)
    return path


def materialize_progress(progress: CandidateProgress, cache_dir: str | Path) -> None:
    """Write and verify referenced sources before active state can retire anything."""
    from digest.adapters.storage.candidate_objects import encode_active_candidate, put_article

    for candidate in progress.candidates.values():
        encode_active_candidate(candidate, cache_dir)
    for packet in progress.packets:
        for article in packet.articles:
            put_article(article, cache_dir)
    progress.latest_collection_json = _materialize_collection(progress.latest_collection_json, cache_dir)
    for packet in progress.packets:
        packet.collection_json = _materialize_collection(packet.collection_json, cache_dir)


def write_progress(progress: CandidateProgress, cache_dir: str | Path = ".cache") -> Path:
    """Write the prepared active record and acknowledge policy only after persistence."""
    from digest.adapters.storage.candidate_objects import write_policy

    record = _working_record(progress, cache_dir)
    if len(json.dumps(record, indent=2).encode("utf-8")) > MAX_BYTES:
        raise ValueError(f"Candidate progress exceeds {MAX_BYTES}-byte budget; no manifest was truncated.")
    path = _safe(Path(cache_dir) / CANDIDATE_FILE)
    _safe(path.with_suffix(path.suffix + ".tmp"))
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json_write(path, record)
    # The header cannot acknowledge a policy until reactivated active work is durable.
    if progress.policy_sha256:
        write_policy(progress.policy_sha256, cache_dir)
    return path
