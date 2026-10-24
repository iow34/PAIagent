from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ServiceAPIKey


class APIKeysRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_active_tenant_id(
        self,
        *,
        key_hash: str,
    ) -> UUID | None:
        statement = select(ServiceAPIKey.tenant_id).where(
            ServiceAPIKey.key_hash == key_hash,
            ServiceAPIKey.is_active.is_(True),
        )

        return await self._session.scalar(statement)
