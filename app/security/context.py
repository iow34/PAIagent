from dataclasses import dataclass
from uuid import UUID

# DEV_TENANT_ID = UUID("00000000-0000-4000-8000-000000000001")


@dataclass(frozen=True)
class AccessContext:
    """The owner of the data accessed by the current request."""

    tenant_id: UUID
    user_id: str
