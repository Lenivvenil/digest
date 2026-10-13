"""Supported Groq strict wire shape; local validation owns substantive constraints."""

from __future__ import annotations

from typing import Any


def groq_synthesis_response_format() -> dict[str, Any]:
    def closed(properties: dict[str, Any]) -> dict[str, Any]:
        return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}

    text = {"type": "string"}
    prose = closed({"relationship": text, "qualification": text})
    support = closed({
        "evidence_id": text,
        "occurrence_sha256": text,
        "field": {"type": "string", "enum": ["excerpt"]},
        "quote": text,
    })
    comparison = closed({
        "relationship_type": {"type": "string", "enum": ["shared_mechanism", "contrast", "complement"]},
        "supports": {"type": "array", "items": support},
        "canonical": prose,
        "presentation": {"anyOf": [prose, {"type": "null"}]},
    })
    schema = closed({
        "schema_version": {"type": "integer", "enum": [1]},
        "publication_language": {"type": "string", "enum": ["en", "ru"]},
        "status": {"type": "string", "enum": ["comparison", "abstained"]},
        "abstention_reason": {"anyOf": [
            {"type": "string", "enum": [
                "insufficient_evidence", "no_supported_relationship", "material_conflict_unresolved",
            ]},
            {"type": "null"},
        ]},
        "comparison": {"anyOf": [comparison, {"type": "null"}]},
    })
    return {"type": "json_schema", "json_schema": {
        "name": "selected_source_comparison_v1", "strict": True, "schema": schema,
    }}
