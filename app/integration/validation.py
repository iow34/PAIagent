"""A bounded, explicit subset of JSON Schema; no reference resolution or code."""

import json
import math
import re
from datetime import datetime
from itertools import islice
from typing import Any, NoReturn

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError

from app.integration.types import Envelope, FieldMapping

MAX_SCHEMA_BYTES = 32768
MAX_PAYLOAD_BYTES = 65536
MAX_DEPTH = 5
MAX_PROPERTIES = 100
MAX_ITEMS = 100
MAX_STRING = 16384
DIALECT = "https://json-schema.org/draft/2020-12/schema"
COMMON = {"type", "title", "description", "enum"}
KEYWORDS = {
    "object": {"properties", "required", "additionalProperties"},
    "array": {"items", "minItems", "maxItems"},
    "string": {"minLength", "maxLength", "format"},
    "number": {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"},
    "integer": {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"},
    "boolean": set(),
}
FORMATS = FormatChecker(formats=["date", "uuid"])


@FORMATS.checks("date-time", raises=(ValueError, OverflowError))
def timestamp(value: Any) -> bool:
    if not isinstance(value, str):
        return True
    # RFC3339: offset required, no naive/local datetimes.
    if not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[Zz]|[+-](?:[01]\d|2[0-3]):[0-5]\d)",
        value,
    ):
        return False
    return datetime.fromisoformat(value.upper()).utcoffset() is not None


class ContractError(ValueError):
    def __init__(
        self,
        detail: str,
        *,
        code: str = "invalid_contract",
        issues: list[dict[str, Any]] | None = None,
    ):
        super().__init__(detail)
        self.code = code
        self.issues = issues or []


def fail(detail: str, path: str = "") -> NoReturn:
    raise ContractError(detail, issues=[{"path": path, "message": detail}])


def bounded_json(value: Any, limit: int) -> None:
    try:
        encoded = json.dumps(
            value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode()
    except (ValueError, TypeError, RecursionError, UnicodeError) as error:
        raise ContractError("Expected finite, UTF-8 JSON values.") from error
    if len(encoded) > limit:
        raise ContractError(f"JSON exceeds {limit} bytes.", code="size_limit")
    # Bound raw nesting before the metaschema validator recurses, and reject
    # NUL characters that PostgreSQL JSONB cannot represent.
    pending = [(value, 0)]
    while pending:
        item, depth = pending.pop()
        if depth > 16:
            raise ContractError("JSON nesting exceeds the contract limit.")
        if isinstance(item, str) and "\x00" in item:
            raise ContractError("NUL characters are not supported in JSON strings.")
        if isinstance(item, dict):
            pending.extend((key, depth + 1) for key in item)
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)


def base_type(node: dict[str, Any]) -> str:
    value = node.get("type")
    if isinstance(value, str) and value in KEYWORDS:
        return value
    if isinstance(value, list) and len(value) == 2 and "null" in value:
        other = next((item for item in value if item != "null"), None)
        if isinstance(other, str) and other in KEYWORDS:
            return other
    fail('type must be a supported type or [type, "null"].')


def validate_schema(schema: dict[str, Any], mappings: list[FieldMapping]) -> None:
    bounded_json(schema, MAX_SCHEMA_BYTES)
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as error:
        raise ContractError(
            "Invalid JSON Schema.",
            issues=[
                {
                    "path": "/".join(map(str, error.absolute_path)),
                    "message": error.message,
                }
            ],
        ) from error
    count = 0

    def visit(node: dict[str, Any], depth: int, path: str) -> None:
        nonlocal count
        if not isinstance(node, dict) or depth > MAX_DEPTH:
            fail(
                f"Schema nodes must be objects, at most {MAX_DEPTH} levels deep.", path
            )
        kind = base_type(node)
        allowed = COMMON | KEYWORDS[kind] | ({"$schema"} if depth == 0 else set())
        unknown = set(node) - allowed
        if unknown:
            fail(f"Unsupported keywords: {', '.join(sorted(unknown))}.", path)
        if "$schema" in node and node["$schema"] != DIALECT:
            fail("Only JSON Schema 2020-12 is supported.", path)
        for annotation in ("title", "description"):
            if annotation in node and len(node[annotation]) > 1000:
                fail("Schema annotations must not exceed 1000 characters.", path)
        if "enum" in node:
            if kind in {"object", "array"} or not 1 <= len(node["enum"]) <= 100:
                fail("enum supports 1..100 scalar values only.", path)
            enum_validator = Draft202012Validator(
                {k: v for k, v in node.items() if k != "enum"}, format_checker=FORMATS
            )
            if any(not enum_validator.is_valid(value) for value in node["enum"]):
                fail("enum values must satisfy the field type and constraints.", path)
        if kind == "object":
            if (
                node.get("additionalProperties") is not False
                or "properties" not in node
            ):
                fail(
                    "Objects require properties and additionalProperties: false.", path
                )
            props = node["properties"]
            count += len(props)
            if count > MAX_PROPERTIES:
                fail(f"At most {MAX_PROPERTIES} properties per schema.", path)
            if set(node.get("required", [])) - set(props):
                fail("required must reference declared properties.", path)
            for name, child in props.items():
                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,63}", name):
                    fail(
                        "Property names use letters, digits, underscore or hyphen; max 64.",
                        path,
                    )
                visit(child, depth + 1, path + "/properties/" + name)
        elif kind == "array":
            if "items" not in node or not 1 <= node.get("maxItems", 0) <= MAX_ITEMS:
                fail(f"Arrays require items and maxItems in 1..{MAX_ITEMS}.", path)
            if node.get("minItems", 0) > node["maxItems"]:
                fail("minItems exceeds maxItems.", path)
            visit(node["items"], depth + 1, path + "/items")
        elif kind == "string":
            if not 1 <= node.get("maxLength", 0) <= MAX_STRING:
                fail(f"Strings require maxLength in 1..{MAX_STRING}.", path)
            if node.get("minLength", 0) > node["maxLength"]:
                fail("minLength exceeds maxLength.", path)
            if "format" in node and node["format"] not in {"date-time", "date", "uuid"}:
                fail("Supported formats: date-time, date, uuid.", path)
        elif kind in {"number", "integer"}:
            for key in KEYWORDS[kind] & node.keys():
                if abs(node[key]) > 1e308 or not math.isfinite(node[key]):
                    fail("Numeric bounds must be finite.", path)
            low = node.get("minimum", node.get("exclusiveMinimum"))
            high = node.get("maximum", node.get("exclusiveMaximum"))
            if low is not None and high is not None and low > high:
                fail("Lower bound exceeds upper bound.", path)

    visit(schema, 0, "")
    if schema.get("type") != "object":
        fail("The payload root must be a non-nullable object.")
    validate_mappings(schema, mappings)


def validate_mappings(schema: dict[str, Any], mappings: list[FieldMapping]) -> None:
    if len(mappings) > 100:
        fail("At most 100 mappings are supported.")
    seen = set()
    singleton = set()
    role_types = {
        "search_text": {"string"},
        "display_name": {"string"},
        "event_time": {"string"},
        "quantity": {"number", "integer"},
        "category": {"string", "integer", "boolean"},
        "external_reference": {"string"},
    }
    for mapping in mappings:
        if not mapping.path.startswith("/"):
            fail("Mapping paths must be JSON Pointers starting with /.", mapping.path)
        node = schema
        for part in mapping.path[1:].split("/"):
            if base_type(node) != "object" or part not in node.get("properties", {}):
                fail(
                    "Mapping must reference a declared field through objects, not arrays.",
                    mapping.path,
                )
            node = node["properties"][part]
        if base_type(node) not in role_types[mapping.role]:
            fail("Mapping role is incompatible with field type.", mapping.path)
        if mapping.role == "event_time" and node.get("format") != "date-time":
            fail("event_time requires format: date-time.", mapping.path)
        if mapping.role == "quantity":
            if (
                mapping.unit is None
                or not mapping.unit.strip()
                or mapping.unit != mapping.unit.strip()
                or not mapping.unit.isprintable()
            ):
                fail(
                    "quantity requires an explicit unit (use '1' for dimensionless).",
                    mapping.path,
                )
        elif mapping.unit is not None:
            fail("unit is only supported for quantity.", mapping.path)
        pair = (mapping.path, mapping.role)
        if pair in seen or (
            mapping.role in {"display_name", "event_time", "external_reference"}
            and mapping.role in singleton
        ):
            fail("Duplicate or ambiguous mapping.", mapping.path)
        seen.add(pair)
        singleton.add(mapping.role)


def validate_payload(schema: dict[str, Any], payload: dict[str, Any]) -> None:
    bounded_json(payload, MAX_PAYLOAD_BYTES)
    errors = list(
        islice(
            Draft202012Validator(schema, format_checker=FORMATS).iter_errors(payload),
            20,
        )
    )
    if errors:
        # Do not echo private payload values in errors.
        raise ContractError(
            "Payload does not match the registered schema.",
            code="payload_validation_failed",
            issues=[
                {
                    "path": "/" + "/".join(map(str, e.absolute_path)),
                    "keyword": str(e.validator),
                    "message": f"Constraint {e.validator} failed.",
                }
                for e in errors
            ],
        )


def validate_envelope(envelope: Envelope) -> str:
    if envelope.occurred_at.utcoffset() is None:
        fail("occurred_at requires a timezone offset.", "/occurred_at")
    for name in ("external_id", "event_id"):
        value = getattr(envelope, name)
        if value is not None and (
            not value.strip()
            or value != value.strip()
            or any(ord(c) < 32 or ord(c) == 127 for c in value)
        ):
            fail(
                "IDs cannot contain surrounding whitespace or control characters.",
                "/" + name,
            )
    if set(envelope.metadata) - {"trace_id", "origin", "correlation_id"}:
        fail("Allowed metadata keys: trace_id, origin, correlation_id.", "/metadata")
    if any(
        not 1 <= len(value) <= 255 or not value.isprintable()
        for value in envelope.metadata.values()
    ):
        fail("Metadata values must contain 1..255 printable characters.", "/metadata")
    if envelope.operation == "observe":
        if envelope.event_id is None or envelope.object_version is not None:
            fail("Observations require event_id and prohibit object_version.")
        kind = "event"
    else:
        kind = "record"
        if envelope.external_id is None or envelope.object_version is None:
            fail("State operations require external_id and object_version.")
        if envelope.operation in {"upsert", "delete"} and envelope.event_id is None:
            fail("State changes require event_id.")
        if envelope.operation == "snapshot" and envelope.event_id is not None:
            fail("Snapshots do not accept event_id.")
    if (envelope.operation == "delete") != (envelope.payload is None):
        fail("Only delete uses null payload; all other operations require an object.")
    return kind
