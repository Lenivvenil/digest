"""Direct immutable candidate evidence and independently addressable latest state.

No object contains earlier progress or report history. Callers write and verify
these objects before removing anything from the active checkpoint.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from digest._util import atomic_json_write
from digest.preparation import _canonical, _restore, _safe, _unique_object
from digest.radar.collector import article_hash

if TYPE_CHECKING:
    from digest.candidate_review import Candidate, CandidateArticle, CandidatePacket

MAX_OBJECT_BYTES = 32_000_000


def digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _key(value: str, length: int = 64) -> str:
    if not isinstance(value, str) or len(value) != length or any(c not in "0123456789abcdef" for c in value):
        raise ValueError("Invalid candidate storage reference.")
    return value


def _path(cache_dir: str | Path, directory: str, key: str, length: int = 64) -> Path:
    return _safe(Path(cache_dir) / directory / f"{_key(key, length)}.json")


def _read(path: Path) -> dict[str, Any]:
    path = _safe(path)
    if not path.exists():
        raise ValueError(f"Missing candidate evidence: {path.name}; restore the retained object.")
    if path.stat().st_size > MAX_OBJECT_BYTES:
        raise ValueError("Candidate evidence exceeds its byte budget.")
    record = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
    if not isinstance(record, dict):
        raise ValueError("Invalid candidate storage envelope.")
    body = {key: value for key, value in record.items() if key != "sha256"}
    if record.get("sha256") != digest(body):
        raise ValueError("Candidate storage hash mismatch.")
    return record


def _write(path: Path, body: dict[str, Any], *, immutable: bool = True) -> Path:
    record = {**body, "sha256": digest(body)}
    if len(json.dumps(record, indent=2).encode()) > MAX_OBJECT_BYTES:
        raise ValueError("Candidate evidence exceeds its byte budget; no evidence was truncated.")
    if immutable and path.exists():
        if _canonical(_read(path)) != _canonical(record):
            raise ValueError("Immutable candidate evidence differs from the retained object.")
        return path
    _safe(path.with_suffix(path.suffix + ".tmp"))
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json_write(path, record)
    if _canonical(_read(path)) != _canonical(record):
        raise ValueError("Candidate evidence write verification failed.")
    return path


def put_article(article: CandidateArticle, cache_dir: str | Path) -> str:
    """Store one canonical occurrence once, shared by all packets and states."""
    sha = digest(asdict(article))
    _write(_path(cache_dir, "candidate_sources", sha), {"schema_version": 1, "article": asdict(article)})
    if read_article(sha, cache_dir) != article:
        raise ValueError("Candidate source write verification failed.")
    return sha


def read_article(sha: str, cache_dir: str | Path) -> CandidateArticle:
    from digest.candidate_review import CandidateArticle

    record = _read(_path(cache_dir, "candidate_sources", sha))
    if set(record) != {"schema_version", "article", "sha256"} or type(record["schema_version"]) is not int:
        raise ValueError("Invalid candidate source envelope.")
    if record["schema_version"] != 1 or digest(record["article"]) != sha:
        raise ValueError("Candidate source binding mismatch.")
    article: CandidateArticle = _restore(record["article"], CandidateArticle)
    parsed = article.article()
    if parsed.pub_date is not None and parsed.pub_date.tzinfo is None:
        raise ValueError("Candidate publication time requires a timezone.")
    return article


def _validate_packet(packet: CandidatePacket) -> None:
    from digest.candidate_review import Candidate, CandidateProgress, _validate

    candidates = {article_hash(item.title, item.link): Candidate(
        article_hash(item.title, item.link), item, packet.planned_at, packet.priorities.get(item.source, 0))
        for item in packet.articles}
    _validate(CandidateProgress(candidates=candidates, packets=[packet]))


def packet_key(packet: CandidatePacket) -> str:
    """Address completed reports by report hash and unfinished attempts by packet hash."""
    return digest(asdict(packet.report)) if packet.report else digest(asdict(replace(
        packet, handed_to_preparation=False)))


def freeze_packet(packet: CandidatePacket, summary: dict[str, Any], cache_dir: str | Path) -> Path:
    """Freeze exactly one attempt, its captures and source references."""
    _validate_packet(packet)
    report_sha = packet_key(packet)
    path = _path(cache_dir, "candidate_reports", report_sha)
    if path.exists():
        saved = read_packet(report_sha, cache_dir)
        if saved != replace(packet, handed_to_preparation=False):
            raise ValueError("Frozen candidate packet differs from the saved report.")
        return path
    article_refs = [put_article(article, cache_dir) for article in packet.articles]
    raw = asdict(replace(packet, handed_to_preparation=False))
    del raw["articles"]
    raw["article_refs"] = article_refs
    _write(path, {"schema_version": 2,
                  "report_sha256": digest(asdict(packet.report)) if packet.report else None,
                  "packet_sha256": digest(asdict(replace(packet, handed_to_preparation=False))),
                  "report_bundle_id": packet.evidence.bundle_id, "summary": summary, "packet": raw})
    read_packet(report_sha, cache_dir)
    return path


def _report_record(report_sha: str, cache_dir: str | Path) -> dict[str, Any]:
    record = _read(_path(cache_dir, "candidate_reports", report_sha))
    if (set(record) != {"schema_version", "report_sha256", "packet_sha256", "report_bundle_id",
                       "summary", "packet", "sha256"}
            or type(record["schema_version"]) is not int or record["schema_version"] != 2
            or (record["report_sha256"] or record["packet_sha256"]) != report_sha
            or not isinstance(record["summary"], dict)):
        raise ValueError("Unsupported candidate report schema or invalid report binding; regenerate draft accounting.")
    return record


def read_report_record(report_sha: str, cache_dir: str | Path) -> dict[str, Any]:
    record = _report_record(report_sha, cache_dir)
    _decode_packet(record, cache_dir)
    return record


def _decode_packet(record: dict[str, Any], cache_dir: str | Path) -> CandidatePacket:
    from digest.candidate_review import CandidatePacket

    raw = record.get("packet")
    if not isinstance(raw, dict) or not isinstance(raw.get("article_refs"), list) or "articles" in raw:
        raise ValueError("Invalid frozen candidate source references.")
    body = {key: value for key, value in raw.items() if key != "article_refs"}
    body["articles"] = [asdict(read_article(sha, cache_dir)) for sha in raw["article_refs"]]
    packet: CandidatePacket = _restore(body, CandidatePacket)
    if ((digest(asdict(packet.report)) if packet.report else None) != record["report_sha256"]
            or digest(asdict(packet)) != record["packet_sha256"]
            or packet.evidence.bundle_id != record["report_bundle_id"] or packet.handed_to_preparation):
        raise ValueError("Frozen candidate report binding mismatch.")
    _validate_packet(packet)
    return packet


def read_packet(report_sha: str, cache_dir: str | Path) -> CandidatePacket:
    return _decode_packet(_report_record(report_sha, cache_dir), cache_dir)


def _validate_handoffs(record: dict[str, Any]) -> None:
    handoffs = record.get("report_handoffs", {})
    if (not isinstance(handoffs, dict) or not set(handoffs) <= set(record["report_refs"])
            or any(type(value) is not bool for value in handoffs.values())):
        raise ValueError("Invalid candidate preparation handoff provenance.")


def _state_record(identity: str, cache_dir: str | Path) -> dict[str, Any] | None:
    path = _path(cache_dir, "candidate_index", identity, 32)
    if not path.exists():
        return None
    record = _read(path)
    fields_without_handoffs = {"schema_version", "candidate", "article_ref", "occurrence_refs",
                               "report_refs", "policy_fields", "sha256"}
    if (set(record) not in (fields_without_handoffs, fields_without_handoffs | {"report_handoffs"})
            or type(record["schema_version"]) is not int or record["schema_version"] != 1
            or not isinstance(record["candidate"], dict) or record["candidate"].get("identity") != identity
            or not isinstance(record["occurrence_refs"], list) or not isinstance(record["report_refs"], list)):
        raise ValueError("Invalid candidate latest-state record.")
    _validate_handoffs(record)
    fields = record["policy_fields"]
    if (not isinstance(fields, list) or len(fields) != 1 + len(record["occurrence_refs"])
            or any(not isinstance(item, dict) or set(item) != {"source", "source_url", "category", "published"}
                   or not all(isinstance(item[key], str) for key in ("source", "source_url", "category"))
                   or item["published"] is not None and not isinstance(item["published"], str) for item in fields)):
        raise ValueError("Invalid candidate policy fields.")
    return record


def load_candidate_packets(identity: str, cache_dir: str | Path) -> tuple[CandidatePacket, ...]:
    record = _state_record(identity, cache_dir)
    if record is None:
        return ()
    return tuple(replace(read_packet(sha, cache_dir),
                         handed_to_preparation=record.get("report_handoffs", {}).get(sha, False))
                 for sha in record["report_refs"])


def encode_active_candidate(candidate: Candidate, cache_dir: str | Path) -> dict[str, Any]:
    """Encode active metadata with source refs; never write an index or retire work."""
    raw = asdict(candidate)
    del raw["article"], raw["occurrences"]
    return {"candidate": raw, "article_ref": put_article(candidate.article, cache_dir),
            "occurrence_refs": [put_article(article, cache_dir) for article in candidate.occurrences]}


def decode_active_candidate(raw: dict[str, Any], cache_dir: str | Path) -> Candidate:
    """Restore sources; active-progress validation separately binds decision proof."""
    from digest.candidate_review import Candidate

    if (not isinstance(raw, dict) or set(raw) != {"candidate", "article_ref", "occurrence_refs"}
            or not isinstance(raw["candidate"], dict) or not isinstance(raw["occurrence_refs"], list)):
        raise ValueError("Invalid active candidate source-reference fields.")
    body = dict(raw["candidate"])
    if "article" in body or "occurrences" in body:
        raise ValueError("Candidate state must use direct source references.")
    body["article"] = asdict(read_article(raw["article_ref"], cache_dir))
    body["occurrences"] = [asdict(read_article(sha, cache_dir)) for sha in raw["occurrence_refs"]]
    candidate: Candidate = _restore(body, Candidate)
    if (any(article_hash(article.title, article.link) != candidate.identity
            for article in (candidate.article, *candidate.occurrences))
            or candidate.article in candidate.occurrences):
        raise ValueError("Active candidate source identity or occurrence mismatch.")
    if datetime.fromisoformat(candidate.first_observed_at).tzinfo is None:
        raise ValueError("Candidate observation requires a timezone.")
    if (candidate.delivery_cache_observed_at is not None
            and datetime.fromisoformat(candidate.delivery_cache_observed_at).tzinfo is None):
        raise ValueError("Candidate delivery-cache evidence requires a timezone.")
    return candidate


def read_candidate_header(identity: str, cache_dir: str | Path) -> dict[str, Any] | None:
    """Read latest status, eligibility and decision metadata without expanding proof."""
    record = _state_record(identity, cache_dir)
    return {**record["candidate"], "policy_fields": record["policy_fields"]} if record is not None else None


def _policy_fields(candidate: Candidate) -> list[dict[str, str | None]]:
    return [{"source": article.source, "source_url": article.source_url,
             "category": article.category, "published": article.published}
            for article in (candidate.article, *candidate.occurrences)]


def _decode_candidate(record: dict[str, Any], cache_dir: str | Path) -> Candidate:
    from digest.candidate_review import Candidate, CandidateProgress, _validate

    _validate_handoffs(record)
    candidate = decode_active_candidate({key: record[key] for key in (
        "candidate", "article_ref", "occurrence_refs")}, cache_dir)
    if record["policy_fields"] != _policy_fields(candidate):
        raise ValueError("Candidate policy fields differ from the retained source evidence.")
    packets = [read_packet(sha, cache_dir) for sha in record["report_refs"]]
    candidates: dict[str, Candidate] = {}
    for packet in packets:
        for article in packet.articles:
            identity = article_hash(article.title, article.link)
            if identity not in candidates:
                candidates[identity] = Candidate(identity, article, packet.planned_at, 0)
            elif article != candidates[identity].article and article not in candidates[identity].occurrences:
                candidates[identity].occurrences += (article,)
    candidates[candidate.identity] = candidate
    _validate(CandidateProgress(candidates=candidates, packets=packets))
    return candidate


def load_candidate(identity: str, cache_dir: str | Path) -> Candidate | None:
    record = _state_record(identity, cache_dir)
    return _decode_candidate(record, cache_dir) if record is not None else None


def save_candidate(
    candidate: Candidate, packet_report_shas: tuple[str, ...], cache_dir: str | Path, *,
    report_handoffs: dict[str, bool] | None = None,
) -> Path:
    """Verify direct proofs, archive this state, then atomically publish latest state.

    The caller removes the active record only after this succeeds. An old active
    record must take precedence after interruption at any earlier boundary.
    """
    body = {"schema_version": 1, **encode_active_candidate(candidate, cache_dir),
            "policy_fields": _policy_fields(candidate), "report_refs": list(dict.fromkeys(packet_report_shas)),
            "report_handoffs": report_handoffs or {}}
    if _decode_candidate(body, cache_dir) != candidate:
        raise ValueError("Candidate latest-state verification failed.")
    _write(_path(cache_dir, "candidate_history", digest(body)), body)
    marker = _path(cache_dir, "candidate_excluded", candidate.identity, 32)
    excluded = not candidate.eligible and candidate.status not in {"not_selected", "duplicate"}
    # A marker can safely precede the index: missing/stale markers are ignored.
    if excluded:
        _write(marker, {"schema_version": 1, "identity": candidate.identity}, immutable=False)
    path = _write(_path(cache_dir, "candidate_index", candidate.identity, 32), body, immutable=False)
    if not excluded and marker.exists():
        marker.unlink()
    return path


def list_excluded(cache_dir: str | Path) -> tuple[str, ...]:
    """Enumerate reversible exclusions without opening resolved candidate history."""
    directory = _safe(Path(cache_dir) / "candidate_excluded")
    identities = []
    for path in sorted(directory.glob("*.json")):
        identity = _key(path.stem, 32)
        marker = _read(path)
        if marker != {"schema_version": 1, "identity": identity,
                      "sha256": digest({"schema_version": 1, "identity": identity})}:
            raise ValueError("Invalid candidate exclusion marker.")
        record = _state_record(identity, cache_dir)
        if record is not None and not record["candidate"].get("eligible", True) and record["candidate"].get(
                "status") not in {"not_selected", "duplicate"}:
            identities.append(identity)
    return tuple(identities)


def read_policy(cache_dir: str | Path) -> str | None:
    path = _safe(Path(cache_dir) / "candidate_policy.json")
    if not path.exists():
        return None
    record = _read(path)
    if (set(record) != {"schema_version", "policy_sha256", "sha256"}
            or type(record["schema_version"]) is not int or record["schema_version"] != 1):
        raise ValueError("Invalid candidate policy header.")
    return _key(record["policy_sha256"])


def write_policy(policy_sha: str, cache_dir: str | Path) -> Path:
    return _write(_safe(Path(cache_dir) / "candidate_policy.json"),
                  {"schema_version": 1, "policy_sha256": _key(policy_sha)}, immutable=False)
