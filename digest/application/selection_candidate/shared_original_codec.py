"""Isolated proposal codec experiment; not integrated or authorized for live use."""

from copy import deepcopy
from typing import Any, Iterator

from digest.application.selection_candidate import bounded_selection_transport as core


def original_slots(payload: dict[str, Any]) -> Iterator[dict[str, Any]]:
    yield from payload["proposals"]
    for proposal in payload.get("optional_proposals", []):
        yield from (s for s in proposal["sources"] if "original" in s or "original_ref" in s)


def original_reference(triage: dict[str, Any], index: int, original: dict[str, Any]) -> dict[str, Any]:
    """Lossless raw-source overrides against the explicitly sanitized writer view."""
    row = triage["rows"][index]
    inherited = {
        "title": row[2],
        "description": row[3],
        "source": triage["sources"][row[0]][0],
        "category": triage["categories"][triage["sources"][row[0]][1]],
        "published": restore_date(triage, row[1]),
    }
    suffix = ""
    if original["description"].startswith(row[3]):
        suffix = original["description"][len(row[3]) :]
        inherited["description"] += suffix
    overrides = {key: value for key, value in original.items() if inherited.get(key) != value}
    return {"triage_row_index": index, "description_suffix": suffix, "remaining_fields": overrides}


def pack(payload: dict[str, Any], *, writer: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(payload)
    triage = result["all_pool_triage"]
    if not isinstance(triage.get("categories"), list) or not all(isinstance(c, str) for c in triage["categories"]):
        raise ValueError("Invalid category table")
    for source in triage["sources"]:
        if (
            not isinstance(source, (list, tuple))
            or len(source) != 2
            or not isinstance(source[0], str)
            or type(source[1]) is not int
            or not 0 <= source[1] < len(triage["categories"])
        ):
            raise ValueError("Invalid source category reference")
    for slot in original_slots(result):
        original = slot.pop("original")
        identity = slot["decision"]["id"] if "decision" in slot else slot["id"]
        indexes = {r[0]: i for i, r in enumerate(writer["rows"])}
        if identity not in indexes:
            raise ValueError("Unknown original identity")
        index = indexes[identity]
        if "triage_row_index" in slot and slot["triage_row_index"] != index:
            raise ValueError("Original identity/index mismatch")
        slot["original_ref"] = original_reference(triage, index, original)
    decisions = result["all_decisions"]
    decisions["rows"] = sorted(decisions["rows"], key=lambda r: r[0])
    if [r[0] for r in decisions["rows"]] != list(range(len(triage["rows"]))):
        raise ValueError("Incomplete indexed decision table")
    decisions["rows"] = [r[1:] for r in decisions["rows"]]
    decisions["columns"] = ["status_index", "duplicate_triage_row_index"]
    if not all(row[4] == len(row[3]) for row in triage["rows"]):
        raise ValueError("Unexpected excerpt length")
    triage["rows"] = [r[:4] for r in triage["rows"]]
    triage["columns"] = triage["columns"][:4]
    result["original_codec"] = CODEC_CONTRACT
    return result


def restore_date(triage: dict[str, Any], index: int) -> str | None:
    return core.restored_date(triage, index)


def unpack(payload: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(payload)
    if result.pop("original_codec") != CODEC_CONTRACT:
        raise ValueError("Unexpected original codec")
    triage = result["all_pool_triage"]
    if not isinstance(triage.get("categories"), list) or not all(isinstance(c, str) for c in triage["categories"]):
        raise ValueError("Invalid category table")
    for source in triage["sources"]:
        if (
            not isinstance(source, (list, tuple))
            or len(source) != 2
            or not isinstance(source[0], str)
            or type(source[1]) is not int
            or not 0 <= source[1] < len(triage["categories"])
        ):
            raise ValueError("Invalid source category reference")
    if triage["columns"] != ["source_index", "date_index", "title", "complete_writer_excerpt"]:
        raise ValueError("Unexpected triage columns")
    for row in triage["rows"]:
        if (
            not isinstance(row, list)
            or len(row) != 4
            or any(type(v) is not int for v in row[:2])
            or not all(isinstance(v, str) for v in row[2:])
            or not 0 <= row[0] < len(triage["sources"])
            or not 0 <= row[1] < len(triage["dates"])
        ):
            raise ValueError("Bad triage row")
    decisions = result["all_decisions"]
    if decisions["columns"] != ["status_index", "duplicate_triage_row_index"]:
        raise ValueError("Unexpected decision columns")
    for row in decisions["rows"]:
        if (
            not isinstance(row, list)
            or len(row) != 2
            or type(row[0]) is not int
            or not 0 <= row[0] < len(decisions["statuses"])
            or (row[1] is not None and (type(row[1]) is not int or not 0 <= row[1] < len(triage["rows"])))
        ):
            raise ValueError("Bad decision row")
    for slot in original_slots(result):
        ref = slot.pop("original_ref")
        if not isinstance(ref, dict) or set(ref) != {"triage_row_index", "description_suffix", "remaining_fields"}:
            raise ValueError("Bad reference fields")
        index = ref["triage_row_index"]
        if type(index) is not int or not 0 <= index < len(triage["rows"]):
            raise ValueError("Bad original index")
        row = triage["rows"][index]
        remaining = ref["remaining_fields"]
        if not isinstance(remaining, dict) or not isinstance(ref["description_suffix"], str):
            raise ValueError("Bad reference values")
        original = {
            "title": row[2],
            "description": row[3] + ref["description_suffix"],
            "source": triage["sources"][row[0]][0],
            "category": triage["categories"][triage["sources"][row[0]][1]],
            "published": restore_date(triage, row[1]),
            **remaining,
        }
        if core.sha(original) != slot["occurrence_sha256"]:
            raise ValueError("Original occurrence mismatch")
        slot["original"] = original
    decisions = result["all_decisions"]
    if len(decisions["rows"]) != len(triage["rows"]):
        raise ValueError("Incomplete decision table")
    decisions["rows"] = [[i, *row] for i, row in enumerate(decisions["rows"])]
    decisions["columns"] = ["triage_row_index", "status_index", "duplicate_triage_row_index"]
    triage["rows"] = [[*row, len(row[3])] for row in triage["rows"]]
    triage["columns"] = [*triage["columns"], "full_excerpt_chars"]
    return result


CODEC_CONTRACT = (
    "v2: all_decisions positions bind identical all_pool_triage positions across the complete pool. "
    "Excerpts are complete configured writer evidence, up to 500 characters, not full articles. "
    "original_ref joins its row: title/source/category/date via row/tables; description=excerpt+description_suffix; "
    "then remaining_fields overrides/adds exact raw fields, including unsanitized values. "
    "Review joined fields as the complete original; writer evidence is sanitized, not raw slices."
)


def compact_ref_reserve(
    writer: dict[str, Any],
    articles: dict[str, dict[str, Any]],
    chosen_main_ids: list[str],
    closing_id: str | None,
    closing_ids: list[str],
) -> int:
    indexes = {core.full_id(r[0]): i for i, r in enumerate(writer["rows"])}
    costs = {}
    for identity, original in articles.items():
        index = indexes[identity]
        triage = {
            **{key: writer[key] for key in ("sources", "categories", "dates", "date_days")},
            "rows": [[r[2], r[3], r[4], r[5]] for r in writer["rows"]],
        }
        ref = original_reference(triage, index, original)
        costs[identity] = len(core.encode(core.encode(ref).decode()))
    count = len(chosen_main_ids)
    reserve = max(0, sum(sorted(costs.values(), reverse=True)[:count]) - sum(costs[i] for i in chosen_main_ids))
    if closing_id is not None:
        reserve += max(costs[i] for i in closing_ids) - costs[closing_id]
    return reserve + count * len(str(max(0, len(articles) - 1)))
