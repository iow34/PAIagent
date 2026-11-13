from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

import msgspec

from app.personalization.types import Period, Personalization

Capability = Literal["search", "query", "summarize", "extract", "analyze", "recommend"]
Short = Annotated[str, msgspec.Meta(min_length=1, max_length=2000)]
Path = Annotated[str, msgspec.Meta(min_length=2, max_length=300)]


class Filter(msgspec.Struct, forbid_unknown_fields=True):
    field: Path
    op: Literal["eq", "ne", "in", "gt", "gte", "lt", "lte", "is_null", "not_null"]
    value: Any = None


class Selection(msgspec.Struct, forbid_unknown_fields=True):
    collection_id: UUID
    schema_version: Annotated[int, msgspec.Meta(ge=1, le=2147483647)]
    fields: Annotated[list[Path], msgspec.Meta(min_length=1, max_length=30)]
    kind: Literal["record", "event"] = "record"
    mode: Literal["current", "history"] = "current"
    filters: Annotated[list[Filter], msgspec.Meta(max_length=20)] = []
    time_field: str = "occurred_at"
    include_text: bool = False


class Metric(msgspec.Struct, forbid_unknown_fields=True):
    name: Annotated[str, msgspec.Meta(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,63}$")]
    selection: Annotated[int, msgspec.Meta(ge=0, le=9)]
    operation: Literal["count", "sum", "average", "min", "max"]
    field: Path | None = None
    group_by: Path | None = None
    interval: Literal["day", "week", "month"] | None = None


class Budget(msgspec.Struct, forbid_unknown_fields=True):
    max_attempts: Annotated[int, msgspec.Meta(ge=1, le=3)] = 3
    retry_seconds: Annotated[int, msgspec.Meta(ge=1, le=60)] = 5
    tool_calls: Literal[0] = 0
    max_records: Annotated[int, msgspec.Meta(ge=1, le=5000)] = 1000
    max_fragments: Annotated[int, msgspec.Meta(ge=1, le=1000)] = 1000
    context_chars: Annotated[int, msgspec.Meta(ge=2000, le=30000)] = 24000
    model_calls: Annotated[int, msgspec.Meta(ge=1, le=32)] = 16
    input_tokens: Annotated[int, msgspec.Meta(ge=1000, le=256000)] = 64000
    output_tokens: Annotated[int, msgspec.Meta(ge=256, le=32000)] = 16000
    per_call_output_tokens: Annotated[int, msgspec.Meta(ge=256, le=4000)] = 2000
    seconds: Annotated[int, msgspec.Meta(ge=1, le=50)] = 50


class ProfileInput(msgspec.Struct, forbid_unknown_fields=True):
    name: Annotated[str, msgspec.Meta(min_length=1, max_length=100)]
    capability: Capability
    goal: Short
    selections: Annotated[list[Selection], msgspec.Meta(max_length=10)] = []
    period: Period | None = None
    comparison_period: Period | None = None
    timezone: Annotated[str, msgspec.Meta(min_length=1, max_length=100)] = "UTC"
    constraints: Annotated[list[Short], msgspec.Meta(max_length=10)] = []
    language: Annotated[str, msgspec.Meta(min_length=1, max_length=50)] = "uk"
    output_schema: dict[str, Any] | None = None
    metrics: Annotated[list[Metric], msgspec.Meta(max_length=20)] = []
    personalization: Personalization = msgspec.field(default_factory=Personalization)
    minimum_samples: Annotated[int, msgspec.Meta(ge=1, le=5000)] = 1
    budget: Budget = msgspec.field(default_factory=Budget)


class InlineInput(msgspec.Struct, forbid_unknown_fields=True):
    text: Annotated[str, msgspec.Meta(min_length=1, max_length=500000)]
    label: Annotated[str, msgspec.Meta(min_length=1, max_length=100)] = "inline"


class HistoryMessage(msgspec.Struct, forbid_unknown_fields=True):
    role: Literal["user", "assistant"]
    content: Annotated[str, msgspec.Meta(min_length=1, max_length=2000)]


class RunInput(msgspec.Struct, forbid_unknown_fields=True):
    version: Annotated[int, msgspec.Meta(ge=1, le=2147483647)]
    query: Annotated[str, msgspec.Meta(max_length=2000)] = ""
    inline: Annotated[list[InlineInput], msgspec.Meta(max_length=10)] = []
    history: Annotated[list[HistoryMessage], msgspec.Meta(max_length=10)] = []
    limit: Annotated[int, msgspec.Meta(ge=1, le=20)] = 5


class ProfileView(msgspec.Struct, forbid_unknown_fields=True):
    profile_id: UUID
    version: int
    config: ProfileInput
    created_at: datetime


class ProfilePage(msgspec.Struct, forbid_unknown_fields=True):
    items: list[ProfileView]
    next_cursor: UUID | None


class CapabilityResult(msgspec.Struct, forbid_unknown_fields=True):
    run_id: UUID
    result_id: UUID
    capability: Capability
    profile_id: UUID
    profile_version: int
    execution_status: Literal["succeeded"]
    data_state: Literal["sufficient", "partial", "insufficient_data"]
    payload: dict[str, Any]
    sources: list[dict[str, Any]]
    period: Period | None
    comparison_period: Period | None
    timezone: str
    coverage: dict[str, Any]
    limitations: list[str]
    generated_at: datetime
    data_as_of: datetime
    freshness: Literal["current", "stale"]
    persisted: bool
    usage: dict[str, int]
    personalization: dict[str, Any] = {}


class ResultSummary(msgspec.Struct, forbid_unknown_fields=True):
    result_id: UUID
    profile_id: UUID
    profile_version: int
    capability: Capability
    execution_status: Literal["succeeded"]
    data_state: Literal["sufficient", "partial", "insufficient_data"]
    generated_at: datetime
    freshness: Literal["current", "stale"]


class ResultPage(msgspec.Struct, forbid_unknown_fields=True):
    items: list[ResultSummary]
    next_cursor: UUID | None
