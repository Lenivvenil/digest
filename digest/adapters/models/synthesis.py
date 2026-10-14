"""Supported Groq strict wire shape; local validation owns structural constraints."""

from __future__ import annotations

from typing import Any


def groq_synthesis_response_format() -> dict[str, Any]:
    def closed(properties: dict[str, Any]) -> dict[str, Any]:
        return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}

    text = {"type": "string"}
    nullable_text = {"anyOf": [text, {"type": "null"}]}
    prose = closed({"interpretation": text, "qualification": nullable_text})
    support = closed({
        "evidence_id": text,
        "occurrence_sha256": text,
        "canonical_observation": text,
        "presentation_observation": nullable_text,
    })
    comparison = closed({
        "supports": {"type": "array", "items": support},
        "premise_ids": {"type": "array", "items": text},
        "canonical": prose,
        "presentation": {"anyOf": [prose, {"type": "null"}]},
    })
    schema = closed({
        "schema_version": {"type": "integer", "enum": [2]},
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
        "name": "selected_source_comparison_v2", "strict": True, "schema": schema,
    }}
