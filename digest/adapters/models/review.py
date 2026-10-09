"""Concrete structured-output wire shape for the existing Groq RSS review route."""

from __future__ import annotations

from typing import Any


def groq_review_response_format(*, allow_closing: bool = False) -> dict[str, Any]:
    """Closed wire shape only; local validation still owns counts, IDs and exact quotes."""

    def closed(properties: dict[str, Any]) -> dict[str, Any]:
        return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}

    text = {"type": "string"}
    schema = closed(
        {
            "selections": {
                "type": "array",
                "items": closed(
                    {
                        "evidence_id": text,
                        "reason": text,
                        "quote": text,
                        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                    }
                ),
            },
            "limitations": {"type": "array", "items": text},
            "dispositions": {
                "type": "array",
                "items": {
                    "anyOf": [
                        closed({"evidence_id": text, "status": {"type": "string", "enum": ["selected"]}}),
                        closed(
                            {
                                "evidence_id": text,
                                "status": {"type": "string", "enum": ["not_selected", "deferred"]},
                                "reason": text,
                            }
                        ),
                        closed(
                            {
                                "evidence_id": text,
                                "status": {"type": "string", "enum": ["duplicate"]},
                                "reason": text,
                                "retained_id": text,
                            }
                        ),
                    ]
                },
            },
        }
    )
    if allow_closing:
        schema["properties"]["dispositions"]["items"]["anyOf"].pop(0)
        schema["properties"]["closing"] = closed(
            {
                "schema_version": {"type": "integer", "enum": [2]},
                "selection": {"anyOf": [schema["properties"]["selections"]["items"], {"type": "null"}]},
            }
        )
        schema["required"].append("closing")
    name = "rss_selection_closing_v2" if allow_closing else "rss_selection_v1"
    return {"type": "json_schema", "json_schema": {"name": name, "strict": True, "schema": schema}}
