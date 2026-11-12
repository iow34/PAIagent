"""Public v1 DTOs. Unknown envelope/configuration fields are rejected."""

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

import msgspec

Name = Annotated[
    str, msgspec.Meta(min_length=1, max_length=100, pattern=r"^[a-z][a-z0-9_-]*$")
]
ExternalID = Annotated[str, msgspec.Meta(min_length=1, max_length=255)]
Version = Annotated[int, msgspec.Meta(ge=1, le=2147483647)]
Kind = Literal["record", "event"]


class NamedInput(msgspec.Struct, forbid_unknown_fields=True):
    name: Name
    description: Annotated[
        str, msgspec.Meta(max_length=1000, pattern=r"^[^\x00]*$")
    ] = ""


class FieldMapping(msgspec.Struct, forbid_unknown_fields=True):
    path: Annotated[str, msgspec.Meta(min_length=1, max_length=512)]
    role: Literal[
        "search_text",
        "display_name",
        "event_time",
        "quantity",
        "category",
        "external_reference",
    ]
    unit: Annotated[str, msgspec.Meta(min_length=1, max_length=64)] | None = None


class SchemaInput(msgspec.Struct, forbid_unknown_fields=True):
    kind: Kind
    version: Version
    json_schema: dict[str, Any]
    field_mappings: Annotated[list[FieldMapping], msgspec.Meta(max_length=100)] = (
        msgspec.field(default_factory=list)
    )


class SourceView(msgspec.Struct):
    id: UUID
    name: str
    description: str
    created_at: datetime


class CollectionView(msgspec.Struct):
    id: UUID
    source_id: UUID
    name: str
    description: str
    created_at: datetime


class SchemaView(msgspec.Struct):
    id: UUID
    collection_id: UUID
    kind: Kind
    version: int
    json_schema: dict[str, Any]
    field_mappings: list[FieldMapping]
    created_at: datetime


class SourcePage(msgspec.Struct):
    items: list[SourceView]
    next_cursor: UUID | None


class CollectionPage(msgspec.Struct):
    items: list[CollectionView]
    next_cursor: UUID | None


class SchemaPage(msgspec.Struct):
    items: list[SchemaView]
    next_cursor: UUID | None


class Envelope(msgspec.Struct, forbid_unknown_fields=True):
    api_version: Literal["1"]
    source_id: UUID
    collection_id: UUID
    schema_version: Version
    operation: Literal["snapshot", "upsert", "delete", "observe"]
    occurred_at: datetime
    payload: dict[str, Any] | None
    external_id: ExternalID | None = None
    object_version: Version | None = None
    event_id: ExternalID | None = None
    metadata: dict[str, str] = msgspec.field(default_factory=dict)


class ValidationResult(msgspec.Struct):
    valid: bool
    schema_kind: Kind
    schema_version: int
    # Validation does not ingest data or assign received_at.
    persisted: bool = False
