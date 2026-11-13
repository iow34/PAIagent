from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

import msgspec


class Period(msgspec.Struct, forbid_unknown_fields=True):
    start: datetime
    end: datetime


Key = Annotated[str, msgspec.Meta(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")]
Version = Annotated[int, msgspec.Meta(ge=0, le=2147483647)]


class Definition(msgspec.Struct, forbid_unknown_fields=True):
    key: Key
    value_schema: dict[str, Any]
    message_prefixes: Annotated[
        list[Annotated[str, msgspec.Meta(min_length=1, max_length=100)]],
        msgspec.Meta(max_length=5),
    ] = []


class FieldRule(msgspec.Struct, forbid_unknown_fields=True):
    collection_id: UUID
    schema_version: Annotated[int, msgspec.Meta(ge=1, le=2147483647)]
    field: Annotated[str, msgspec.Meta(min_length=2, max_length=300)]
    key: Key
    kind: Literal["preference", "observation"] = "preference"
    source_kind: Literal["record", "event"] = "record"


class SettingsInput(msgspec.Struct, forbid_unknown_fields=True):
    expected_version: Version
    enabled: bool = False
    collections: Annotated[list[UUID], msgspec.Meta(max_length=10)] = []
    definitions: Annotated[list[Definition], msgspec.Meta(max_length=30)] = []
    field_rules: Annotated[list[FieldRule], msgspec.Meta(max_length=30)] = []


class PreferenceWrite(msgspec.Struct, forbid_unknown_fields=True):
    collection_id: UUID
    key: Key
    value: Any
    expected_version: Version = 0
    restore: bool = False


class MemoryAction(msgspec.Struct, forbid_unknown_fields=True):
    expected_version: Annotated[int, msgspec.Meta(ge=1, le=2147483647)]


class MessageInput(msgspec.Struct, forbid_unknown_fields=True):
    collection_id: UUID
    external_id: Annotated[str, msgspec.Meta(min_length=1, max_length=100)]
    text: Annotated[str, msgspec.Meta(min_length=1, max_length=8000)]
    author: Literal["user"] = "user"


class MessageUpdate(msgspec.Struct, forbid_unknown_fields=True):
    expected_version: Annotated[int, msgspec.Meta(ge=1, le=2147483647)]
    text: Annotated[str, msgspec.Meta(min_length=1, max_length=8000)]


class ExtractionInput(msgspec.Struct, forbid_unknown_fields=True):
    delivery_id: UUID
    period: Period | None = None


class FeedbackInput(msgspec.Struct, forbid_unknown_fields=True):
    request_id: Annotated[str, msgspec.Meta(min_length=1, max_length=100)]
    rating: Literal["helpful", "unhelpful"] | None = None
    decision: Literal["accepted", "rejected", "outdated"] | None = None
    claim_index: Annotated[int, msgspec.Meta(ge=0, le=19)] | None = None
    comment: Annotated[str, msgspec.Meta(max_length=1000)] = ""
    preference: PreferenceWrite | None = None


class Personalization(msgspec.Struct, forbid_unknown_fields=True):
    enabled: bool = False
    collections: Annotated[list[UUID], msgspec.Meta(max_length=10)] = []
    keys: Annotated[list[Key], msgspec.Meta(max_length=30)] = []
    observations: bool = False
    use_feedback: bool = True
    max_items: Annotated[int, msgspec.Meta(ge=1, le=20)] = 10
    max_chars: Annotated[int, msgspec.Meta(ge=500, le=6000)] = 4000


class MemoryView(msgspec.Struct, forbid_unknown_fields=True):
    id: UUID
    collection_id: UUID
    source_id: UUID
    key: str
    kind: Literal["preference", "observation"]
    state: Literal[
        "candidate", "confirmed", "superseded", "invalidated", "deleted", "rejected"
    ]
    version: int
    origin: str
    value: Any
    provenance: dict[str, Any]
    period: Period | None
    created_at: datetime
    updated_at: datetime


class MemoryPage(msgspec.Struct, forbid_unknown_fields=True):
    items: list[MemoryView]
    next_cursor: UUID | None


class SettingsView(msgspec.Struct, forbid_unknown_fields=True):
    version: int
    enabled: bool
    config: dict[str, Any]


class MessageView(msgspec.Struct, forbid_unknown_fields=True):
    id: UUID
    collection_id: UUID
    external_id: str
    version: int
    author: Literal["user"]
    text: str
    created_at: datetime


class MessageCandidates(msgspec.Struct, forbid_unknown_fields=True):
    message_id: UUID
    version: int
    items: list[MemoryView]
    ambiguous_keys: list[str]


class ExtractionView(msgspec.Struct, forbid_unknown_fields=True):
    items: list[MemoryView]
    automatic_confirmation: bool


class FeedbackView(msgspec.Struct, forbid_unknown_fields=True):
    id: UUID
    result_id: UUID
    profile_id: UUID
    request_id: str
    rating: Literal["helpful", "unhelpful"] | None
    decision: Literal["accepted", "rejected", "outdated"] | None
    claim_index: int | None
    comment: str
    revision: int
    created_at: datetime
    effect: str


class FeedbackPage(msgspec.Struct, forbid_unknown_fields=True):
    items: list[FeedbackView]
    next_cursor: UUID | None
