"""Registry lifecycle, access checks and validation without ingesting data."""

import logging
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import TypeVar, cast
from uuid import UUID

import msgspec
from litestar.exceptions import (
    HTTPException,
    NotFoundException,
    PermissionDeniedException,
)
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import IntegrationCollection, IntegrationSchema, IntegrationSource
from app.integration.types import (
    CollectionPage,
    CollectionView,
    Envelope,
    Kind,
    NamedInput,
    SchemaInput,
    SchemaPage,
    SchemaView,
    SourcePage,
    SourceView,
    ValidationResult,
)
from app.integration.validation import (
    validate_envelope,
    validate_payload,
    validate_schema,
)
from app.repositories.integration import IntegrationRepository, RegistryRow
from app.security.context import AccessContext
from app.security.permissions import require_scope, require_source

logger = logging.getLogger(__name__)


ViewT = TypeVar("ViewT", bound=msgspec.Struct)
PageT = TypeVar("PageT", bound=msgspec.Struct)
ValueT = TypeVar("ValueT")


def view(row: RegistryRow, dto: type[ViewT]) -> ViewT:
    values = {field: getattr(row, field) for field in dto.__struct_fields__}
    return msgspec.convert(values, type=dto)


def page(
    rows: Sequence[RegistryRow], limit: int, dto: type[ViewT], page_type: type[PageT]
) -> PageT:
    return msgspec.convert(
        {
            "items": [view(row, dto) for row in rows[:limit]],
            "next_cursor": rows[limit - 1].id if len(rows) > limit else None,
        },
        type=page_type,
    )


def found(row: ValueT | None) -> ValueT:
    if row is None:
        raise NotFoundException(
            detail="Registry resource not found.", extra={"code": "not_found"}
        )
    return row


class IntegrationService:
    def __init__(
        self, factory: async_sessionmaker[AsyncSession], context: AccessContext
    ):
        self.factory = factory
        self.context = context

    @asynccontextmanager
    async def repository(self) -> AsyncIterator[IntegrationRepository]:
        try:
            async with self.factory.begin() as session:
                yield IntegrationRepository(session, self.context)
        except IntegrityError as error:
            if getattr(error.orig, "sqlstate", None) == "23505":
                raise HTTPException(
                    status_code=409,
                    detail="Name or schema version already exists; versions are immutable.",
                    extra={"code": "registry_conflict"},
                ) from error
            logger.error("Registry constraint failed (%s).", type(error).__name__)
            raise HTTPException(
                status_code=500,
                detail="Registry storage failed.",
                extra={"code": "storage_error"},
            ) from error
        except SQLAlchemyError as error:
            logger.error("Registry storage failed (%s).", type(error).__name__)
            raise HTTPException(
                status_code=500,
                detail="Registry storage failed.",
                extra={"code": "storage_error"},
            ) from error

    async def accessible_source(
        self, repo: IntegrationRepository, source_id: UUID
    ) -> IntegrationSource:
        row = found(await repo.source(source_id))
        require_source(self.context, row.id)
        return row

    async def accessible_collection(
        self, repo: IntegrationRepository, collection_id: UUID
    ) -> IntegrationCollection:
        row = found(await repo.collection(collection_id))
        require_source(self.context, row.source_id)
        return row

    async def create_source(self, data: NamedInput) -> SourceView:
        require_scope(self.context, "sources:write")
        if self.context.source_ids is not None:
            raise PermissionDeniedException(
                detail="Creating sources requires an unrestricted source grant.",
                extra={"code": "source_forbidden"},
            )
        async with self.repository() as repo:
            row = await repo.add(
                IntegrationSource(
                    tenant_id=self.context.tenant_id,
                    name=data.name,
                    description=data.description,
                )
            )
            return view(row, SourceView)

    async def get_source(self, source_id: UUID) -> SourceView:
        require_scope(self.context, "sources:read")
        async with self.repository() as repo:
            return view(await self.accessible_source(repo, source_id), SourceView)

    async def sources(self, cursor: UUID | None, limit: int) -> SourcePage:
        require_scope(self.context, "sources:read")
        async with self.repository() as repo:
            return page(
                await repo.page(IntegrationSource, cursor=cursor, limit=limit),
                limit,
                SourceView,
                SourcePage,
            )

    async def create_collection(
        self, source_id: UUID, data: NamedInput
    ) -> CollectionView:
        require_scope(self.context, "collections:write")
        async with self.repository() as repo:
            await self.accessible_source(repo, source_id)
            row = await repo.add(
                IntegrationCollection(
                    tenant_id=self.context.tenant_id,
                    source_id=source_id,
                    name=data.name,
                    description=data.description,
                )
            )
            return view(row, CollectionView)

    async def get_collection(self, collection_id: UUID) -> CollectionView:
        require_scope(self.context, "collections:read")
        async with self.repository() as repo:
            return view(
                await self.accessible_collection(repo, collection_id), CollectionView
            )

    async def collections(
        self, source_id: UUID, cursor: UUID | None, limit: int
    ) -> CollectionPage:
        require_scope(self.context, "collections:read")
        async with self.repository() as repo:
            await self.accessible_source(repo, source_id)
            rows = await repo.page(
                IntegrationCollection,
                cursor=cursor,
                limit=limit,
                filters=(IntegrationCollection.source_id == source_id,),
            )
            return page(rows, limit, CollectionView, CollectionPage)

    async def create_schema(self, collection_id: UUID, data: SchemaInput) -> SchemaView:
        require_scope(self.context, "schemas:write")
        validate_schema(data.json_schema, data.field_mappings)
        async with self.repository() as repo:
            await self.accessible_collection(repo, collection_id)
            row = await repo.add(
                IntegrationSchema(
                    tenant_id=self.context.tenant_id,
                    collection_id=collection_id,
                    kind=data.kind,
                    version=data.version,
                    json_schema=data.json_schema,
                    field_mappings=msgspec.to_builtins(data.field_mappings),
                )
            )
            return view(row, SchemaView)

    async def get_schema(
        self, collection_id: UUID, kind: Kind, version: int
    ) -> SchemaView:
        require_scope(self.context, "schemas:read")
        async with self.repository() as repo:
            await self.accessible_collection(repo, collection_id)
            return view(
                found(await repo.schema(collection_id, kind, version)), SchemaView
            )

    async def schemas(
        self, collection_id: UUID, kind: Kind | None, cursor: UUID | None, limit: int
    ) -> SchemaPage:
        require_scope(self.context, "schemas:read")
        async with self.repository() as repo:
            await self.accessible_collection(repo, collection_id)
            filters = [IntegrationSchema.collection_id == collection_id]
            if kind is not None:
                filters.append(IntegrationSchema.kind == kind)
            rows = await repo.page(
                IntegrationSchema, cursor=cursor, limit=limit, filters=filters
            )
            return page(rows, limit, SchemaView, SchemaPage)

    async def validate(self, collection_id: UUID, data: Envelope) -> ValidationResult:
        require_scope(self.context, "data:validate")
        kind = cast(Kind, validate_envelope(data))
        async with self.repository() as repo:
            collection = await self.accessible_collection(repo, collection_id)
            if (
                data.collection_id != collection.id
                or data.source_id != collection.source_id
            ):
                raise HTTPException(
                    status_code=422,
                    detail="Envelope source/collection does not match the URL resource.",
                    extra={"code": "envelope_mismatch"},
                )
            row = found(await repo.schema(collection_id, kind, data.schema_version))
            schema = row.json_schema
        if data.payload is not None:
            validate_payload(schema, data.payload)
        return ValidationResult(
            valid=True, schema_kind=kind, schema_version=data.schema_version
        )
