"""Validate only declared JSON paths, typed filters and supported operations."""

from datetime import UTC
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import msgspec
from jsonschema import Draft202012Validator

from app.capabilities.types import ProfileInput
from app.integration.validation import (
    FORMATS,
    ContractError,
    base_type,
    bounded_json,
    validate_schema,
)


def node_at(schema, path):
    node = schema
    if not path.startswith("/"):
        raise ContractError("Field paths must be JSON Pointers through objects.")
    for part in path[1:].split("/"):
        if base_type(node) != "object" or part not in node.get("properties", {}):
            raise ContractError("Unknown field path: " + path)
        node = node["properties"][part]
    if base_type(node) in {"object", "array"}:
        raise ContractError("Selections support scalar fields only: " + path)
    return node


def value_at(payload, path):
    value = payload
    for part in path[1:].split("/"):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def validate_profile(config: ProfileInput):
    bounded_json(msgspec.to_builtins(config), 65536)
    if not config.name.strip() or not config.goal.strip():
        raise ContractError("Profile name and goal must not be blank.")
    try:
        ZoneInfo(config.timezone)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ContractError("Unknown IANA timezone.") from error
    for period in (config.period, config.comparison_period):
        if period is not None:
            try:
                if period.start.utcoffset() is None or period.end.utcoffset() is None:
                    raise ValueError()
                if period.start.astimezone(UTC) >= period.end.astimezone(UTC):
                    raise ValueError()
            except (ValueError, OverflowError) as error:
                raise ContractError(
                    "Periods require aware timestamps and start < end."
                ) from error
    if not config.selections and config.period is not None:
        raise ContractError(
            "Inline text has no validated time field; periods require collection selections."
        )
    if config.comparison_period and not config.period:
        raise ContractError("A comparison requires a primary period.")
    if config.comparison_period and config.period:
        a, b = config.period, config.comparison_period
        if max(a.start, b.start) < min(a.end, b.end):
            raise ContractError("Comparison periods must not overlap.")
    if config.metrics and config.capability != "analyze":
        raise ContractError("Exact metrics are supported by analyze only.")
    if config.comparison_period and not config.metrics:
        raise ContractError("Comparison periods require exact metrics.")
    if len({(s.collection_id, s.kind) for s in config.selections}) != len(
        config.selections
    ):
        raise ContractError("Duplicate collection/kind selections are not allowed.")
    if len({m.name for m in config.metrics}) != len(config.metrics):
        raise ContractError("Metric names must be unique.")
    for metric in config.metrics:
        if metric.selection >= len(config.selections):
            raise ContractError("Metric selection index is unavailable.")
        if (metric.operation == "count") != (metric.field is None):
            raise ContractError(
                "count counts rows; other operations require a quantity field."
            )
    if config.capability == "extract":
        if config.output_schema is None:
            raise ContractError("extract requires output_schema.")
        validate_schema(config.output_schema, [])

        # Strict provider outputs need every declared property required.
        def required(node):
            if base_type(node) == "object":
                if set(node.get("required", [])) != set(node["properties"]):
                    raise ContractError(
                        "Output objects require all properties; use nullable fields for missing values."
                    )
                for child in node["properties"].values():
                    required(child)
            elif base_type(node) == "array":
                required(node["items"])

        required(config.output_schema)
    elif config.output_schema is not None:
        raise ContractError("Custom output_schema is supported by extract only.")


def validate_selection(selection, schema, mappings, metrics):
    if selection.kind == "event" and selection.mode != "history":
        raise ContractError("Event selections require mode=history.")
    if len(set(selection.fields)) != len(selection.fields):
        raise ContractError("Selected fields must be unique.")
    roles = {(m["path"], m["role"]): m for m in mappings}
    for path in selection.fields:
        node_at(schema, path)

    def allowed(path):
        if path not in selection.fields:
            raise ContractError(
                "Filter, time and metric paths must be selected fields: " + path
            )
        return node_at(schema, path)

    if (
        selection.time_field != "occurred_at"
        and allowed(selection.time_field).get("format") != "date-time"
    ):
        raise ContractError("time_field requires a date-time field.")
    for f in selection.filters:
        node = allowed(f.field)
        if f.op in {"is_null", "not_null"}:
            if f.value is not None:
                raise ContractError("Null operators do not take a value.")
            continue
        values = f.value if f.op == "in" else [f.value]
        if not isinstance(values, list) or not 1 <= len(values) <= 100:
            raise ContractError("in requires 1..100 typed scalar values.")
        for v in values:
            if v is None or not Draft202012Validator(
                node, format_checker=FORMATS
            ).is_valid(v):
                raise ContractError(
                    "Filter value must satisfy its declared non-null field schema."
                )
        if f.op in {"gt", "gte", "lt", "lte"} and base_type(node) not in {
            "integer",
            "number",
        }:
            raise ContractError("Ordering filters support numeric fields only.")
    for metric in metrics:
        if metric.field:
            allowed(metric.field)
            if (metric.field, "quantity") not in roles:
                raise ContractError(
                    "Numeric metrics require a registered quantity with an explicit unit."
                )
        if metric.group_by:
            allowed(metric.group_by)
            if (metric.group_by, "category") not in roles:
                raise ContractError("Grouping requires a registered category field.")
