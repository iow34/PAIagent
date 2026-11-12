"""Tenant-scoped registry queries. Schema versions are insert-only."""

from collections.abc import Sequence
from typing import TypeVar
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.db.models import IntegrationCollection, IntegrationSchema, IntegrationSource
from app.security.context import AccessContext

RegistryRow = IntegrationSource | IntegrationCollection | IntegrationSchema
RowT = TypeVar("RowT", bound=RegistryRow)


class IntegrationRepository:
    def __init__(self, session: AsyncSession, context: AccessContext) -> None:
        self.session = session
        self.context = context

    async def add(self, row: RowT) -> RowT:
        self.session.add(row)
        await self.session.flush()
        return row

    async def source(self, source_id: UUID) -> IntegrationSource | None:
        return await self.session.scalar(
            select(IntegrationSource).where(
                IntegrationSource.tenant_id == self.context.tenant_id,
                IntegrationSource.id == source_id,
            )
        )

    async def collection(self, collection_id: UUID) -> IntegrationCollection | None:
        return await self.session.scalar(
            select(IntegrationCollection).where(
                IntegrationCollection.tenant_id == self.context.tenant_id,
                IntegrationCollection.id == collection_id,
            )
        )

    async def schema(
        self, collection_id: UUID, kind: str, version: int
    ) -> IntegrationSchema | None:
        return await self.session.scalar(
            select(IntegrationSchema).where(
                IntegrationSchema.tenant_id == self.context.tenant_id,
                IntegrationSchema.collection_id == collection_id,
                IntegrationSchema.kind == kind,
                IntegrationSchema.version == version,
            )
        )

    async def page(
        self,
        model: type[RowT],
        *,
        cursor: UUID | None,
        limit: int,
        filters: Sequence[ColumnElement[bool]] = (),
    ) -> list[RowT]:
        statement = select(model).where(
            model.tenant_id == self.context.tenant_id, *filters
        )
        if cursor is not None:
            statement = statement.where(model.id > cursor)
        if model is IntegrationSource and self.context.source_ids is not None:
            statement = statement.where(model.id.in_(self.context.source_ids))
        return list(
            await self.session.scalars(statement.order_by(model.id).limit(limit + 1))
        )
