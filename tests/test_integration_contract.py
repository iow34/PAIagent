"""Useful contract boundaries: no persistence or model/network calls."""

import json
import unittest
from pathlib import Path
from uuid import uuid4

import msgspec

from app.integration.types import Envelope, FieldMapping, SchemaInput
from app.integration.validation import (
    ContractError,
    validate_envelope,
    validate_payload,
    validate_schema,
)

EXAMPLES = Path(__file__).resolve().parents[1] / "app/integration/examples"


class IntegrationContractTests(unittest.TestCase):
    def setUp(self):
        self.example = json.loads((EXAMPLES / "tasks.json").read_text())
        self.schema = self.example["schema"]["json_schema"]

    def test_three_independent_domains(self):
        for file in EXAMPLES.glob("*.json"):
            example = json.loads(file.read_text())
            definition = msgspec.convert(example["schema"], type=SchemaInput)
            validate_schema(definition.json_schema, definition.field_mappings)
            validate_payload(definition.json_schema, example["payload"])

    def test_unknown_ref_and_executable_constructs_rejected(self):
        for key, value in {
            "$ref": "http://127.0.0.1/private",
            "$defs": {},
            "allOf": [],
            "patternProperties": {},
            "default": {},
            "examples": [],
            "if": {},
            "pattern": "a+",
        }.items():
            with self.subTest(key=key), self.assertRaises(ContractError):
                validate_schema({**self.schema, key: value}, [])

    def test_invalid_nested_schema_and_unbounded_collections(self):
        for node in [
            True,
            {"type": "array", "items": {"type": "integer"}},
            {"type": "string"},
            {"type": "object", "properties": {}},
            {"type": "string", "maxLength": 10, "format": "email"},
            {"type": "string", "maxLength": 10, "enum": [1]},
            {"type": "number", "minimum": 10**1000},
        ]:
            with self.subTest(node=node), self.assertRaises(ContractError):
                validate_schema(
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {"x": node},
                    },
                    [],
                )

    def test_mapping_must_exist_and_match_semantics(self):
        for mapping in [
            FieldMapping("/missing", "search_text"),
            FieldMapping("/caption", "quantity", "hour"),
            FieldMapping("/estimate", "quantity"),
            FieldMapping("/caption", "event_time"),
            FieldMapping("/caption", "category", "unit"),
        ]:
            with self.subTest(mapping=mapping), self.assertRaises(ContractError):
                validate_schema(self.schema, [mapping])
        mapping = FieldMapping("/caption", "display_name")
        with self.assertRaises(ContractError):
            validate_schema(self.schema, [mapping, mapping])

    def test_payload_null_required_and_unknown_fields(self):
        validate_payload(
            self.schema, {"caption": "ok", "phase": "todo", "estimate": None}
        )
        for data in [
            {"caption": "ok"},
            {"caption": None, "phase": "todo"},
            {"caption": "ok", "phase": "todo", "other": 1},
            {"caption": "ok", "phase": "PRIVATE"},
            {"caption": "ok", "phase": "todo", "estimate": float("nan")},
        ]:
            with self.subTest(data=data), self.assertRaises(ContractError) as raised:
                validate_payload(self.schema, data)
            self.assertNotIn("PRIVATE", str(raised.exception.issues))

    def test_timestamp_formats_and_array_limits(self):
        example = json.loads((EXAMPLES / "learning_sessions.json").read_text())
        for value in [
            "2026-11-12",
            "2026-11-12T10:00:00",
            "2026-02-30T10:00:00Z",
            "2026-11-12 10:00:00Z",
            "2026-11-12T10:00:00+01:99",
        ]:
            with self.assertRaises(ContractError):
                validate_payload(
                    example["schema"]["json_schema"],
                    {**example["payload"], "started": value},
                )
        example = json.loads((EXAMPLES / "books.json").read_text())
        with self.assertRaises(ContractError):
            validate_payload(
                example["schema"]["json_schema"],
                {**example["payload"], "labels": ["x"] * 11},
            )

    def envelope(self, **values):
        data = {
            "api_version": "1",
            "source_id": str(uuid4()),
            "collection_id": str(uuid4()),
            "schema_version": 1,
            "operation": "snapshot",
            "occurred_at": "2026-11-12T10:00:00Z",
            "external_id": "a",
            "object_version": 1,
            "payload": {},
        }
        data.update(values)
        return msgspec.convert(data, type=Envelope)

    def test_operation_semantics(self):
        self.assertEqual(validate_envelope(self.envelope()), "record")
        self.assertEqual(
            validate_envelope(
                self.envelope(operation="delete", event_id="e", payload=None)
            ),
            "record",
        )
        self.assertEqual(
            validate_envelope(
                self.envelope(
                    operation="observe",
                    event_id="e",
                    object_version=None,
                    external_id=None,
                )
            ),
            "event",
        )
        for values in [
            {"event_id": "e"},
            {"operation": "observe", "event_id": "e"},
            {"operation": "upsert"},
            {"operation": "delete", "event_id": "e"},
            {"payload": None},
            {"occurred_at": "2026-11-12T10:00:00"},
            {"metadata": {"tenant_id": "evil"}},
            {"external_id": " a"},
        ]:
            with self.subTest(values=values), self.assertRaises(ContractError):
                validate_envelope(self.envelope(**values))

    def test_envelope_server_fields_cannot_be_injected(self):
        for values in [
            {"received_at": "2026-11-12T10:00:00Z"},
            {"tenant_id": str(uuid4())},
            {"user_id": "other"},
            {"api_version": "2"},
            {"object_version": 0},
        ]:
            with self.assertRaises(msgspec.ValidationError):
                self.envelope(**values)

    def test_schema_size_depth_and_property_limits(self):
        for change in [
            {"description": "x" * 40000},
            {"properties": {f"f{i}": {"type": "boolean"} for i in range(101)}},
        ]:
            with self.assertRaises(ContractError):
                validate_schema({**self.schema, **change}, [])
        nested = {"type": "string", "maxLength": 10}
        for _ in range(7):
            nested = {
                "type": "object",
                "additionalProperties": False,
                "properties": {"a": nested},
            }
        with self.assertRaises(ContractError):
            validate_schema(nested, [])

    def test_contract_envelope_definition_matches_dto(self):
        contract = json.loads((EXAMPLES.parent / "contract.json").read_text())
        self.assertEqual(
            contract["envelope_json_schema"], msgspec.json.schema(Envelope)
        )

    def test_storage_strings_and_raw_depth_rejected_before_metaschema(self):
        for field in ("description", "title"):
            with self.assertRaises(ContractError):
                validate_schema({**self.schema, field: "unsafe\x00text"}, [])
        schema = {"type": "object", "additionalProperties": False, "properties": {}}
        for _ in range(100):
            schema = {
                "type": "object",
                "properties": {"nested": schema},
                "additionalProperties": False,
            }
        with self.assertRaises(ContractError):
            validate_schema(schema, [])
