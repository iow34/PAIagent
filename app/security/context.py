from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class AccessContext:
    """The owner of the data accessed by the current request."""

    tenant_id: UUID
    user_id: str
