import json
import unicodedata

import msgspec
from jsonschema import Draft202012Validator

from app.integration.validation import (
    FORMATS,
    ContractError,
    base_type,
    bounded_json,
    validate_schema,
)
from app.services.data_service import digest


def definition(config, key):
    value = next((d for d in config["definitions"] if d["key"] == key), None)
    if value is None:
        raise ContractError("Memory key is not declared in owner settings.")
    return value


def validate_value(config, key, value):
    schema = definition(config, key)["value_schema"]
    bounded_json(value, 2000)
    if (
        value is None
        or isinstance(value, (dict, list))
        or not Draft202012Validator(schema, format_checker=FORMATS).is_valid(value)
    ):
        raise ContractError("Memory value does not satisfy its declared scalar schema.")
    if isinstance(value, str) and not value.strip():
        raise ContractError("Memory strings must not be blank.")


def validate_settings(data):
    bounded_json(msgspec.to_builtins(data), 65536)
    if len(set(data.collections)) != len(data.collections) or len(
        {d.key for d in data.definitions}
    ) != len(data.definitions):
        raise ContractError("Memory collections and definitions must be unique.")
    prefixes = []
    for item in data.definitions:
        schema = item.value_schema
        validate_schema(
            {
                "type": "object",
                "properties": {"value": schema},
                "required": ["value"],
                "additionalProperties": False,
            },
            [],
        )
        if base_type(schema) in {"object", "array"} or (
            base_type(schema) == "string" and schema.get("maxLength", 0) > 500
        ):
            raise ContractError(
                "Memory supports scalar values; strings at most 500 characters."
            )
        for prefix in item.message_prefixes:
            if not prefix.strip() or "\n" in prefix or "\r" in prefix:
                raise ContractError(
                    "Message prefixes must be non-blank, single-line literals."
                )
            if any(
                prefix.startswith(old) or old.startswith(prefix) for old in prefixes
            ):
                raise ContractError("Message prefixes must not overlap.")
            prefixes.append(prefix)
    rules = set()
    for rule in data.field_rules:
        if rule.collection_id not in data.collections or rule.key not in {
            d.key for d in data.definitions
        }:
            raise ContractError(
                "Field rule must use an allowed collection and defined key."
            )
        if (rule.kind == "observation") != (rule.source_kind == "event"):
            raise ContractError(
                "Event fields remain period-bound observations; preferences use user records/messages or explicit API writes."
            )
        identity = (
            rule.collection_id,
            rule.source_kind,
            rule.schema_version,
            rule.field,
            rule.key,
        )
        if identity in rules:
            raise ContractError("Duplicate memory field rule.")
        rules.add(identity)


def message_values(config, text):
    for start, line in _lines(text):
        for item in config["definitions"]:
            for prefix in item["message_prefixes"]:
                if line.startswith(prefix):
                    raw = line[len(prefix) :].strip()
                    if base_type(item["value_schema"]) == "string":
                        value = raw
                    else:
                        try:
                            value = json.loads(raw)
                        except ValueError:
                            continue
                    try:
                        validate_value(config, item["key"], value)
                    except ContractError:
                        continue
                    yield (
                        item["key"],
                        value,
                        {"start": start, "end": start + len(line), "prefix": prefix},
                    )


def _lines(text):
    offset = 0
    for line in text.splitlines(keepends=True):
        yield offset, line.rstrip("\r\n")
        offset += len(line)


def proposal_fingerprint(text):
    return digest(" ".join(unicodedata.normalize("NFKC", text).casefold().split()))
