"""Lossless readable UTC second date table, with literal fallbacks."""

from datetime import datetime
from typing import Any


def pack_dates(values: list[str | None]) -> tuple[list[str], list[Any]]:
    compact: dict[str, tuple[str, int]] = {}
    for value in values:
        if value is None:
            continue
        try:
            parsed = datetime.fromisoformat(value)
            if value.endswith("+00:00") and parsed.microsecond == 0 and parsed.isoformat(timespec="seconds") == value:
                compact[value] = (value[:10], int(value[11:13] + value[14:16] + value[17:19]))
        except (ValueError, TypeError):
            pass
    days = sorted({day for day, _ in compact.values()})
    entries: list[Any] = []
    for value in values:
        if value is not None and value in compact:
            day, time = compact[value]
            entries.append([days.index(day), time])
        else:
            entries.append(value)
    return days, entries


def restore_date(table: dict[str, Any], index: int) -> str | None:
    if type(index) is not int or not 0 <= index < len(table["dates"]):
        raise ValueError("Invalid date table index")
    value = table["dates"][index]
    if value is None or isinstance(value, str):
        return value
    if (
        not isinstance(value, list)
        or len(value) != 2
        or any(type(v) is not int for v in value)
        or not 0 <= value[0] < len(table["date_days"])
        or not 0 <= value[1] <= 235959
    ):
        raise ValueError("Invalid date reference")
    time = str(value[1]).zfill(6)
    result = f"{table['date_days'][value[0]]}T{time[:2]}:{time[2:4]}:{time[4:]}+00:00"
    if datetime.fromisoformat(result).isoformat(timespec="seconds") != result:
        raise ValueError("Noncanonical date")
    return result
