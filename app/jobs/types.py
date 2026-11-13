from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

import msgspec

from app.capabilities.types import Budget, RunInput

Status = Literal["pending", "running", "succeeded", "failed", "cancelled"]


class CreateRun(msgspec.Struct, forbid_unknown_fields=True):
    profile_id: UUID
    input: RunInput
    idempotency_key: Annotated[str, msgspec.Meta(min_length=1, max_length=100)]
    mode: Literal["async", "sync"] = "async"


class TriggerInput(msgspec.Struct, forbid_unknown_fields=True):
    version: Annotated[int, msgspec.Meta(ge=1, le=2147483647)]
    enabled: bool = True
    on_change: bool = False
    interval_seconds: Annotated[int, msgspec.Meta(ge=60, le=2592000)] | None = None
    start_at: datetime | None = None
    debounce_seconds: Annotated[int, msgspec.Meta(ge=1, le=300)] = 10
    max_wait_seconds: Annotated[int, msgspec.Meta(ge=1, le=600)] = 120
    query: Annotated[str, msgspec.Meta(max_length=2000)] = ""
    limit: Annotated[int, msgspec.Meta(ge=1, le=20)] = 5


class TriggerView(msgspec.Struct, forbid_unknown_fields=True):
    profile_id: UUID
    config: TriggerInput
    enabled: bool
    revision: UUID
    next_due_at: datetime | None
    last_checked_at: datetime | None
    error_code: str | None


class RunView(msgspec.Struct, forbid_unknown_fields=True):
    id: UUID
    profile_id: UUID
    profile_version: int
    status: Status
    reason: Literal["manual", "sync", "change", "schedule"]
    attempts: int
    max_attempts: int
    budget: Budget
    lease_expires_at: datetime | None
    created_at: datetime
    available_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    input_boundary: str | None
    data_as_of: datetime | None
    result_id: UUID | None
    error_code: str | None
    ephemeral: bool


class RunPage(msgspec.Struct, forbid_unknown_fields=True):
    items: list[RunView]
    next_cursor: UUID | None
