"""Pure JSON encoding and restoration shared across digest boundaries."""

from __future__ import annotations

import json
import math
import re
import types
from dataclasses import fields, is_dataclass
from typing import Any, Literal, get_args, get_origin, get_type_hints


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def restore_dataclass(value: Any, expected: Any) -> Any:
    """Decode only the exact declared dataclasses and JSON primitives."""
    origin, args = get_origin(expected), get_args(expected)
    if origin is types.UnionType:
        for candidate in args:
            try:
                return restore_dataclass(value, candidate)
            except ValueError:
                continue
        raise ValueError("Invalid optional checkpoint field.")
    if origin is Literal:
        if value not in args or not isinstance(value, str):
            raise ValueError("Invalid checkpoint enum.")
        return value
    if origin in (list, tuple):
        if not isinstance(value, list):
            raise ValueError("Invalid checkpoint collection.")
        items = [restore_dataclass(item, args[0]) for item in value]
        return tuple(items) if origin is tuple else items
    if origin is dict:
        if not isinstance(value, dict):
            raise ValueError("Invalid checkpoint mapping.")
        return {restore_dataclass(key, args[0]): restore_dataclass(item, args[1]) for key, item in value.items()}
    if isinstance(expected, type) and is_dataclass(expected):
        if not isinstance(value, dict) or set(value) != {field.name for field in fields(expected)}:
            raise ValueError("Invalid checkpoint dataclass fields.")
        hints = get_type_hints(expected)
        return expected(**{key: restore_dataclass(item, hints[key]) for key, item in value.items()})
    if expected is float:
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError("Invalid checkpoint numeric value.")
        return value
    if type(value) is not expected:
        raise ValueError("Invalid checkpoint primitive field.")
    return value


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate checkpoint JSON key.")
        result[key] = value
    return result


def extract_json(text: str) -> Any:
    """Extract JSON from LLM response text.

    Handles: raw JSON, JSON in markdown code fences, JSON embedded in text.
    Raises ValueError if no valid JSON is found.
    """
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if fence_match:
        try:
            return json.loads(fence_match.group(1).strip())
        except json.JSONDecodeError:
            pass
    for pattern in (r"\{[\s\S]*\}", r"\[[\s\S]*\]"):
        match = re.search(pattern, text)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass
    raise ValueError(f"No valid JSON found in LLM response: {text[:200]!r}")
