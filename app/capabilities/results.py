"""Public payload schemas and validation before any result is persisted."""

from typing import Any

from jsonschema import Draft202012Validator

from app.capabilities.generation import CLAIM, GenerationError, object_schema

SOURCE = {
    "type": "object",
    "required": ["id", "content", "position"],
    "additionalProperties": False,
    "properties": {
        "id": {"type": "string"},
        "content": {"type": "string"},
        "position": {"type": "object"},
        **{
            key: {"type": "string"}
            for key in [
                "delivery_id",
                "evidence_path",
                "source_id",
                "collection_id",
                "occurred_at",
                "label",
                "extraction_coverage",
            ]
        },
        **{key: {"type": ["string", "null"]} for key in ["object_id", "event_id"]},
        "object_version": {"type": ["integer", "null"]},
        "schema_version": {"type": "integer"},
        "selection": {"type": "integer", "minimum": 0},
        "extraction_limitations": {"type": "array", "items": {"type": "string"}},
    },
}
DECIMAL = {"type": ["string", "null"], "pattern": r"^-?\d+(\.\d+)?$"}
METRIC = object_schema(
    {
        "name": {"type": "string"},
        "operation": {"enum": ["count", "sum", "average", "min", "max"]},
        "selection": {"type": "integer", "minimum": 0},
        "period": {"enum": ["primary", "comparison"]},
        "group": {"type": ["string", "integer", "boolean", "null"]},
        "bucket": {"type": ["string", "null"]},
        "timezone": {"type": "string"},
        "unit": {"type": "string"},
        "value": DECIMAL,
        **{
            key: {"type": "integer", "minimum": 0}
            for key in ["sample_count", "value_count", "null_count"]
        },
        "decimal_precision": {"const": 650},
        "rounding": {"const": "ROUND_HALF_EVEN"},
        "denominator": {"type": ["integer", "null"], "minimum": 0},
        "source_ids": {
            "type": "array",
            "maxItems": 5000,
            "items": {"type": "string"},
            "uniqueItems": True,
        },
    }
)
DIFFERENCE = object_schema(
    {
        "name": {"type": "string"},
        "operation": {"const": "difference"},
        "selection": {"type": "integer", "minimum": 0},
        "group": {"type": ["string", "integer", "boolean", "null"]},
        "bucket": {"type": ["string", "null"]},
        "unit": {"type": "string"},
        "primary": DECIMAL,
        "comparison": DECIMAL,
        "value": DECIMAL,
        "meaning": {"type": "string"},
    }
)


def payload_schema(capability, output_schema=None):
    if capability == "search":
        return object_schema(
            {"items": {"type": "array", "maxItems": 20, "items": SOURCE}}
        )
    props: dict[str, Any] = {
        "claims": {"type": "array", "maxItems": 20, "items": CLAIM},
        "limitations": {"type": "array", "items": {"type": "string"}},
    }
    if capability == "analyze":
        props["metrics"] = {"type": "array", "items": {"anyOf": [METRIC, DIFFERENCE]}}
    elif capability == "extract":
        props["data"] = {
            "anyOf": [output_schema or {"type": "object"}, {"type": "null"}]
        }
        props["source_ids"] = {
            "type": "array",
            "items": {"type": "string"},
            "uniqueItems": True,
        }
    return object_schema(props)


def validate_result(config, result):
    payload = result["payload"]
    if not Draft202012Validator(
        payload_schema(config.capability, config.output_schema)
    ).is_valid(payload):
        raise GenerationError("Final payload failed the public schema.")
    allowed = {source["id"] for source in result["sources"]}
    source_validator = Draft202012Validator(SOURCE)
    if len(allowed) != len(result["sources"]) or any(
        not source_validator.is_valid(source) for source in result["sources"]
    ):
        raise GenerationError("Final evidence failed the public schema.")
    refs = [claim["source_ids"] for claim in payload.get("claims", [])]
    refs.extend(metric.get("source_ids", []) for metric in payload.get("metrics", []))
    if "source_ids" in payload:
        refs.append(payload["source_ids"])
    if any(not set(ids) <= allowed for ids in refs):
        raise GenerationError("Final references are not present in returned evidence.")
    if (
        config.capability == "extract"
        and result["data_state"] != "insufficient_data"
        and payload["data"] is None
    ):
        raise GenerationError("Extraction requires typed data when sufficient.")
