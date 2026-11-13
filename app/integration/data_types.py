"""Step 7 wire types. All write bodies reject unknown fields."""

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

import msgspec

from app.integration.types import Envelope, ExternalID, Version


class ObjectReference(msgspec.Struct, forbid_unknown_fields=True):
    collection_id: UUID
    external_id: ExternalID


class IngestEnvelope(Envelope, forbid_unknown_fields=True):
    source_sequence: Version | None = None
    references: Annotated[list[ObjectReference], msgspec.Meta(max_length=20)] = (
        msgspec.field(default_factory=list)
    )


class BatchInput(msgspec.Struct, forbid_unknown_fields=True):
    items: Annotated[list[IngestEnvelope], msgspec.Meta(min_length=1, max_length=100)]


class ImportInput(msgspec.Struct, forbid_unknown_fields=True):
    snapshot_id: ExternalID
    boundary: ExternalID
    part: Annotated[int, msgspec.Meta(ge=1, le=10000)]
    final: bool
    completeness: Literal["partial", "complete"]
    items: Annotated[list[IngestEnvelope], msgspec.Meta(max_length=100)]
    mode: Literal["snapshot"] = "snapshot"


class ItemAck(msgspec.Struct):
    status: Literal["accepted", "duplicate", "stale", "rejected"]
    delivery_id: UUID | None = None
    object_id: UUID | None = None
    received_at: datetime | None = None
    error: dict[str, Any] | None = None
    index: int = 0


class BatchAck(msgspec.Struct):
    batch_id: UUID
    items: list[ItemAck]


class RecordView(msgspec.Struct):
    id: UUID
    source_id: UUID
    collection_id: UUID
    external_id: str
    object_version: int
    schema_version: int
    payload: dict[str, Any]
    metadata: dict[str, str]
    references: list[UUID]
    occurred_at: datetime
    received_at: datetime


class EventView(msgspec.Struct):
    id: UUID
    source_id: UUID
    collection_id: UUID
    object_id: UUID | None
    object_version: int | None
    event_id: str | None
    source_sequence: int | None
    schema_version: int
    operation: str
    payload: dict[str, Any] | None
    metadata: dict[str, str]
    references: list[UUID]
    applied: bool
    corrected_by_id: UUID | None
    occurred_at: datetime
    received_at: datetime


class RecordPage(msgspec.Struct):
    items: list[RecordView]
    next_cursor: UUID | None


class EventPage(msgspec.Struct):
    items: list[EventView]
    next_cursor: UUID | None
