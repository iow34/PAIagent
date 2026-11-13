"""Owner-scoped storage and transaction locks for ingestion and erasure."""

import hashlib
from typing import TypeVar
from uuid import UUID

from litestar.exceptions import HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.db.models import (
    Base,
    ConversationSession,
    DataDelivery,
    DataFence,
    DataImport,
    DataObject,
    DataOwner,
    Document,
)
from app.security.context import AccessContext

RowT = TypeVar("RowT", bound=Base)
OwnedT = TypeVar("OwnedT", DataObject, DataDelivery, DataImport)


def owner(
    model: type[DataOwner] | type[Document] | type[ConversationSession],
    context: AccessContext,
) -> tuple[ColumnElement[bool], ColumnElement[bool]]:
    return (model.tenant_id == context.tenant_id, model.user_id == context.user_id)


async def lock_owner(session: AsyncSession, context: AccessContext) -> None:
    # A stable lock key across processes. Collisions only serialize unrelated owners.
    if session.get_bind().dialect.name == "postgresql":
        key = int.from_bytes(
            hashlib.sha256(f"{context.tenant_id}:{context.user_id}".encode()).digest()[
                :8
            ],
            signed=True,
        )
        await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


async def check_fence(
    session: AsyncSession,
    context: AccessContext,
    *,
    source_id: UUID | None = None,
    collection_id: UUID | None = None,
) -> None:
    # Legacy SQLite unit fixtures don't implement the PostgreSQL lifecycle contract.
    if session.get_bind().dialect.name != "postgresql":
        return
    scopes = ["user"]
    if source_id is not None:
        scopes.append(f"source:{source_id}")
    if collection_id is not None:
        scopes.append(f"collection:{collection_id}")
    if await session.scalar(
        select(DataFence.id)
        .where(*owner(DataFence, context), DataFence.scope.in_(scopes))
        .limit(1)
    ):
        raise HTTPException(
            status_code=410,
            detail="This data identity has been erased; reconnect with a new identity.",
            extra={"code": "data_erased"},
        )


async def guard_user_write(session: AsyncSession, context: AccessContext) -> None:
    await lock_owner(session, context)
    await check_fence(session, context)


class DataRepository:
    def __init__(self, session: AsyncSession, context: AccessContext) -> None:
        self.session = session
        self.context = context

    async def add(self, row: RowT) -> RowT:
        self.session.add(row)
        await self.session.flush()
        return row

    async def object(self, collection_id: UUID, external_id: str) -> DataObject | None:
        return await self.session.scalar(
            select(DataObject).where(
                *owner(DataObject, self.context),
                DataObject.collection_id == collection_id,
                DataObject.external_id == external_id,
            )
        )

    async def by_id(self, model: type[OwnedT], item_id: UUID) -> OwnedT | None:
        return await self.session.scalar(
            select(model).where(*owner(model, self.context), model.id == item_id)
        )

    async def event(self, collection_id: UUID, event_id: str) -> DataDelivery | None:
        return await self.session.scalar(
            select(DataDelivery).where(
                *owner(DataDelivery, self.context),
                DataDelivery.collection_id == collection_id,
                DataDelivery.event_id == event_id,
            )
        )

    async def version(self, object_id: UUID, version: int) -> DataDelivery | None:
        return await self.session.scalar(
            select(DataDelivery).where(
                *owner(DataDelivery, self.context),
                DataDelivery.object_id == object_id,
                DataDelivery.object_version == version,
            )
        )
