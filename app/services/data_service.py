"""Durable ingestion, immutable history, bounded imports and privacy erasure.

All mutations use the same short owner transaction lock. No provider calls occur
inside transactions. Returning from a public method means commit succeeded.
"""

import hashlib
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, Literal, NoReturn, cast
from uuid import UUID, uuid4

import msgspec
from litestar.exceptions import HTTPException, NotFoundException
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.db.models import (
    AnalysisProfile,
    AnalysisProfileVersion,
    AnalysisResult,
    ConversationSession,
    DataDelivery,
    DataFence,
    DataImport,
    DataImportMember,
    DataImportPart,
    DataObject,
    DataOutbox,
    Document,
    EvidenceFragment,
    FileProvenance,
    IntegrationCollection,
)
from app.integration.data_types import (
    BatchAck,
    BatchInput,
    EventPage,
    EventView,
    ImportInput,
    IngestEnvelope,
    ItemAck,
    ObjectReference,
    RecordPage,
    RecordView,
)
from app.integration.types import Kind
from app.integration.validation import (
    ContractError,
    fail,
    validate_envelope,
    validate_payload,
)
from app.repositories.data_lifecycle import (
    DataRepository,
    check_fence,
    lock_owner,
    owner,
)
from app.repositories.integration import IntegrationRepository
from app.security.permissions import require_scope, require_source
from app.services.integration_service import IntegrationService, found

logger = logging.getLogger(__name__)

Channel = Literal["records", "events", "imports"]


def conflict(
    code: str = "delivery_conflict",
    detail: str = "An immutable identifier was reused with different content.",
) -> NoReturn:
    raise HTTPException(status_code=409, detail=detail, extra={"code": code})


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            msgspec.to_builtins(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
    ).hexdigest()


def record_view(row: DataObject) -> RecordView:
    assert row.payload is not None
    return RecordView(
        row.id,
        row.source_id,
        row.collection_id,
        row.external_id,
        row.object_version,
        row.schema_version,
        row.payload,
        row.data_metadata,
        [UUID(x) for x in row.references],
        row.occurred_at,
        row.received_at,
    )


def event_view(row: DataDelivery) -> EventView:
    return EventView(
        row.id,
        row.source_id,
        row.collection_id,
        row.object_id,
        row.object_version,
        row.event_id,
        row.source_sequence,
        row.schema_version,
        row.operation,
        row.payload,
        row.data_metadata,
        [UUID(x) for x in row.references],
        row.applied,
        row.corrected_by_id,
        row.occurred_at,
        row.received_at,
    )


class DataService(IntegrationService):
    @asynccontextmanager
    async def transaction(
        self, *, write: bool = False
    ) -> AsyncIterator[DataRepository]:
        try:
            async with self.factory.begin() as session:
                if write:
                    await lock_owner(session, self.context)
                yield DataRepository(session, self.context)
        except IntegrityError as error:
            # Do not log SQL/parameters: they may contain private payloads.
            if getattr(error.orig, "sqlstate", None) == "23505":
                conflict()
            logger.error("Data constraint failed (%s).", type(error).__name__)
            raise HTTPException(
                status_code=500,
                detail="Data storage failed.",
                extra={"code": "storage_error"},
            ) from error
        except SQLAlchemyError as error:
            logger.error("Data storage failed (%s).", type(error).__name__)
            raise HTTPException(
                status_code=503,
                detail="Data storage unavailable; retry identical delivery.",
                extra={"code": "storage_unavailable"},
            ) from error

    def ownership(self, collection: IntegrationCollection) -> dict[str, Any]:
        return {
            "tenant_id": self.context.tenant_id,
            "user_id": self.context.user_id,
            "source_id": collection.source_id,
            "collection_id": collection.id,
        }

    async def collection(
        self, repo: DataRepository, collection_id: UUID, *, writable: bool = False
    ) -> IntegrationCollection:
        row = await self.accessible_collection(
            IntegrationRepository(repo.session, self.context), collection_id
        )
        if writable:
            await check_fence(
                repo.session,
                self.context,
                source_id=row.source_id,
                collection_id=row.id,
            )
        return row

    async def references(self, repo: DataRepository, data: IngestEnvelope) -> list[str]:
        references = list(data.references)
        if data.operation == "observe" and data.external_id is not None:
            references.append(ObjectReference(data.collection_id, data.external_id))
        result = set()
        for reference in references:
            if (
                not reference.external_id.strip()
                or reference.external_id != reference.external_id.strip()
                or not reference.external_id.isprintable()
            ):
                fail(
                    "Reference identifiers must be printable without surrounding whitespace.",
                    "/references",
                )
            await self.collection(repo, reference.collection_id, writable=True)
            target = await repo.object(reference.collection_id, reference.external_id)
            if target is None or target.deleted:
                raise NotFoundException(
                    detail="Referenced object is unavailable.",
                    extra={"code": "reference_not_found"},
                )
            result.add(str(target.id))
        return sorted(result)

    async def replace_record(self, object_id: UUID, data: IngestEnvelope) -> ItemAck:
        require_scope(self.context, "data:write")
        async with self.transaction(write=True) as repo:
            row = found(await repo.by_id(DataObject, object_id))
            require_source(self.context, row.source_id)
            if (
                data.external_id != row.external_id
                or data.collection_id != row.collection_id
                or data.operation != "upsert"
            ):
                fail("PUT requires an upsert for the addressed object.")
            result = await self._ingest(repo, row.collection_id, data, "records")
        return result

    async def correct_event(self, delivery_id: UUID, data: IngestEnvelope) -> ItemAck:
        """Correct an observation with a new immutable event; preserve the audit trail."""
        require_scope(self.context, "data:write")
        async with self.transaction(write=True) as repo:
            row = found(await repo.by_id(DataDelivery, delivery_id))
            require_source(self.context, row.source_id)
            if row.erased:
                raise HTTPException(
                    status_code=410,
                    detail="Event identity has been erased.",
                    extra={"code": "data_erased"},
                )
            if (
                row.operation != "observe"
                or data.operation != "observe"
                or data.collection_id != row.collection_id
                or data.event_id == row.event_id
            ):
                fail(
                    "Corrections require an observation in the same collection with a new event_id; state changes use the next object_version."
                )
            if data.event_id is None:
                fail("Corrections require event_id.")
            assert data.event_id is not None
            existing = await repo.event(row.collection_id, data.event_id)
            if row.corrected_by_id is not None:
                if existing is None or existing.id != row.corrected_by_id:
                    conflict(
                        "correction_conflict",
                        "Correct the most recent observation in this chain.",
                    )
            elif existing is not None:
                conflict(
                    "correction_conflict", "A correction must use a new event identity."
                )
            result = await self._ingest(repo, row.collection_id, data, "events")
            row.corrected_by_id = result.delivery_id
            from app.personalization.lifecycle import invalidate_deliveries

            await invalidate_deliveries(repo.session, self.context, [row.id])
        return result

    async def ingest_one(
        self, collection_id: UUID, data: IngestEnvelope, channel: Channel = "records"
    ) -> ItemAck:
        require_scope(self.context, "data:write")
        if data.operation == "delete":
            require_scope(self.context, "data:delete")
        async with self.transaction(write=True) as repo:
            result = await self._ingest(repo, collection_id, data, channel)
        return result

    async def _ingest(
        self,
        repo: DataRepository,
        collection_id: UUID,
        data: IngestEnvelope,
        channel: Channel,
    ) -> ItemAck:
        kind = cast(Kind, validate_envelope(data))
        if kind == "record":
            assert data.external_id is not None and data.object_version is not None
        allowed = {
            "records": {"snapshot", "upsert", "delete"},
            "events": {"upsert", "delete", "observe"},
            "imports": {"snapshot"},
        }[channel]
        if data.operation not in allowed:
            fail("Operation is not accepted by this endpoint.", "/operation")
        if data.operation == "delete":
            require_scope(self.context, "data:delete")
        if data.operation == "snapshot" and data.source_sequence is not None:
            fail(
                "Snapshot versions are not change-stream sequence entries.",
                "/source_sequence",
            )
        collection = await self.collection(repo, collection_id, writable=True)
        if (
            data.source_id != collection.source_id
            or data.collection_id != collection_id
        ):
            fail("Envelope source/collection does not match the URL resource.")
        schema = found(
            await IntegrationRepository(repo.session, self.context).schema(
                collection_id, kind, data.schema_version
            )
        )
        if data.payload is not None:
            validate_payload(schema.json_schema, data.payload)
        try:
            data = msgspec.structs.replace(
                data, occurred_at=data.occurred_at.astimezone(UTC)
            )
        except (ValueError, OverflowError) as error:
            raise ContractError(
                "occurred_at is outside the supported UTC range."
            ) from error
        fingerprint = digest(data)
        state_fingerprint = digest(
            [
                data.schema_version,
                data.payload,
                data.metadata,
                data.occurred_at,
                data.references,
            ]
        )
        if data.event_id is not None:
            prior = await repo.event(collection_id, data.event_id)
            if prior is not None:
                if prior.erased:
                    raise HTTPException(
                        status_code=410,
                        detail="Event identity has been erased.",
                        extra={"code": "data_erased"},
                    )
                if prior.fingerprint != fingerprint:
                    conflict()
                return ItemAck(
                    "duplicate", prior.id, prior.object_id, prior.received_at
                )
        obj = None
        if kind == "record":
            assert data.external_id is not None and data.object_version is not None
            obj = await repo.object(collection_id, data.external_id)
            if obj is not None and obj.deleted:
                raise HTTPException(
                    status_code=410,
                    detail="Object identity has been erased.",
                    extra={"code": "data_erased"},
                )
            if obj is not None:
                prior = await repo.version(obj.id, data.object_version)
                if prior is not None:
                    # A snapshot may describe the same already-delivered state.
                    # An upsert arriving after that snapshot promotes the receipt
                    # to the actual change event without duplicating the version.
                    if prior.state_fingerprint != state_fingerprint:
                        conflict("version_conflict")
                    if data.operation == "snapshot":
                        return ItemAck("duplicate", prior.id, obj.id, prior.received_at)
                    if prior.operation != "snapshot" or data.operation != "upsert":
                        conflict("version_conflict")
                    # Promotion is a new history write: current source grants
                    # must still authorize every referenced object.
                    await self.references(repo, data)
                    prior.operation = "upsert"
                    prior.event_id = data.event_id
                    prior.source_sequence = data.source_sequence
                    prior.fingerprint = fingerprint
                    await repo.add(
                        DataOutbox(
                            **self.ownership(collection),
                            delivery_id=prior.id,
                            object_id=obj.id,
                            reason="history_changed",
                        )
                    )
                    return ItemAck("accepted", prior.id, obj.id, prior.received_at)
        refs = await self.references(repo, data)
        now = datetime.now(UTC)
        if kind == "record" and obj is None:
            obj = await repo.add(
                DataObject(
                    **self.ownership(collection),
                    external_id=data.external_id,
                    object_version=data.object_version,
                    schema_version=data.schema_version,
                    payload=None,
                    occurred_at=data.occurred_at,
                    received_at=now,
                )
            )
            is_new = True
        else:
            is_new = False
        applied = (
            obj is not None
            and data.object_version is not None
            and (is_new or data.object_version > obj.object_version)
        )
        delivery = await repo.add(
            DataDelivery(
                **self.ownership(collection),
                object_id=obj.id if obj else None,
                object_version=data.object_version,
                event_id=data.event_id,
                source_sequence=data.source_sequence,
                schema_version=data.schema_version,
                operation=data.operation,
                fingerprint=fingerprint,
                state_fingerprint=state_fingerprint,
                payload=data.payload,
                data_metadata=data.metadata,
                references=refs,
                occurred_at=data.occurred_at,
                received_at=now,
                applied=applied,
            )
        )
        if data.payload is not None:
            from app.normalization.common import structured

            for ordinal, (content, position) in enumerate(
                structured(data.payload, schema.field_mappings)
            ):
                await repo.add(
                    EvidenceFragment(
                        delivery_id=delivery.id,
                        ordinal=ordinal,
                        content=content,
                        position=position,
                    )
                )
        if applied:
            assert obj is not None and data.object_version is not None
            obj.object_version = data.object_version
            obj.schema_version = data.schema_version
            obj.payload = data.payload
            obj.data_metadata = data.metadata
            obj.references = refs
            obj.occurred_at = data.occurred_at
            obj.received_at = now
        if applied:
            assert obj is not None
            from app.personalization.lifecycle import invalidate_deliveries

            previous = list(
                await repo.session.scalars(
                    select(DataDelivery.id).where(
                        *owner(DataDelivery, self.context),
                        DataDelivery.object_id == obj.id,
                        DataDelivery.id != delivery.id,
                    )
                )
            )
            await invalidate_deliveries(repo.session, self.context, previous)
        if applied and data.operation == "delete":
            assert obj is not None
            await self._erase_objects(repo, [obj.id])
        else:
            await repo.add(
                DataOutbox(
                    **self.ownership(collection),
                    delivery_id=delivery.id,
                    object_id=obj.id if obj else None,
                    reason="state_changed" if applied else "history_changed",
                )
            )
        return ItemAck(
            "accepted" if applied or kind == "event" else "stale",
            delivery.id,
            obj.id if obj else None,
            now,
        )

    async def batch(
        self, collection_id: UUID, data: BatchInput, channel: Channel
    ) -> BatchAck:
        require_scope(self.context, "data:write")
        # Reject inaccessible collections before processing individual items.
        async with self.transaction() as repo:
            await self.collection(repo, collection_id, writable=True)
        items = []
        for index, item in enumerate(data.items):
            try:
                ack = await self.ingest_one(collection_id, item, channel)
            except ContractError as error:
                ack = ItemAck(
                    "rejected",
                    error={
                        "status_code": 422,
                        "code": error.code,
                        "issues": error.issues,
                    },
                )
            except HTTPException as error:
                ack = ItemAck(
                    "rejected",
                    error={
                        "status_code": error.status_code,
                        "code": (
                            error.extra.get("code", "request_failed")
                            if isinstance(error.extra, dict)
                            else "request_failed"
                        ),
                        "detail": error.detail,
                    },
                )
            ack.index = index
            items.append(ack)
        return BatchAck(uuid4(), items)

    async def records(
        self,
        collection_id: UUID,
        cursor: UUID | None = None,
        limit: int = 50,
        external_id: str | None = None,
    ) -> RecordPage:
        require_scope(self.context, "data:read")
        async with self.transaction() as repo:
            await self.collection(repo, collection_id)
            stmt = select(DataObject).where(
                *owner(DataObject, self.context),
                DataObject.collection_id == collection_id,
                DataObject.deleted.is_(False),
            )
            if cursor is not None:
                stmt = stmt.where(DataObject.id > cursor)
            if external_id is not None:
                stmt = stmt.where(DataObject.external_id == external_id)
            rows = list(
                await repo.session.scalars(
                    stmt.order_by(DataObject.id).limit(limit + 1)
                )
            )
            return RecordPage(
                [record_view(x) for x in rows[:limit]],
                rows[limit - 1].id if len(rows) > limit else None,
            )

    async def record(self, object_id: UUID) -> RecordView:
        require_scope(self.context, "data:read")
        async with self.transaction() as repo:
            row = found(await repo.by_id(DataObject, object_id))
            require_source(self.context, row.source_id)
            if row.deleted:
                raise NotFoundException(detail="Record is unavailable.")
            return record_view(row)

    async def events(
        self,
        collection_id: UUID,
        cursor: UUID | None = None,
        limit: int = 50,
        external_id: str | None = None,
        occurred_from: datetime | None = None,
        occurred_before: datetime | None = None,
        object_id: UUID | None = None,
    ) -> EventPage:
        require_scope(self.context, "data:read")
        for value in (occurred_from, occurred_before):
            if value is not None and value.utcoffset() is None:
                fail("Time filters require timezone offsets.")
        if occurred_from and occurred_before and occurred_from >= occurred_before:
            fail("Time interval must be nonempty.")
        async with self.transaction() as repo:
            await self.collection(repo, collection_id)
            stmt = select(DataDelivery).where(
                *owner(DataDelivery, self.context),
                DataDelivery.collection_id == collection_id,
                DataDelivery.erased.is_(False),
            )
            if cursor is not None:
                stmt = stmt.where(DataDelivery.id > cursor)
            if external_id is not None:
                obj = await repo.object(collection_id, external_id)
                if obj is None or obj.deleted:
                    return EventPage([], None)
                stmt = stmt.where(
                    (DataDelivery.object_id == obj.id)
                    | DataDelivery.references.contains([str(obj.id)])
                )
            if object_id is not None:
                stmt = stmt.where(DataDelivery.object_id == object_id)
            if occurred_from is not None:
                stmt = stmt.where(DataDelivery.occurred_at >= occurred_from)
            if occurred_before is not None:
                stmt = stmt.where(DataDelivery.occurred_at < occurred_before)
            rows = list(
                await repo.session.scalars(
                    stmt.order_by(DataDelivery.id).limit(limit + 1)
                )
            )
            return EventPage(
                [event_view(x) for x in rows[:limit]],
                rows[limit - 1].id if len(rows) > limit else None,
            )

    async def event(self, delivery_id: UUID) -> EventView:
        require_scope(self.context, "data:read")
        async with self.transaction() as repo:
            row = found(await repo.by_id(DataDelivery, delivery_id))
            require_source(self.context, row.source_id)
            if row.erased:
                raise NotFoundException(detail="Event is unavailable.")
            return event_view(row)

    async def import_part(
        self, collection_id: UUID, data: ImportInput
    ) -> dict[str, Any]:
        require_scope(self.context, "data:write")
        for value in (data.snapshot_id, data.boundary):
            if not value.strip() or value != value.strip() or not value.isprintable():
                fail(
                    "Import identifiers must be printable without surrounding whitespace."
                )
        async with self.transaction(write=True) as repo:
            collection = await self.collection(repo, collection_id, writable=True)
            row = await repo.session.scalar(
                select(DataImport).where(
                    *owner(DataImport, self.context),
                    DataImport.collection_id == collection_id,
                    DataImport.snapshot_id == data.snapshot_id,
                )
            )
            if row is None:
                row = await repo.add(
                    DataImport(
                        **self.ownership(collection),
                        snapshot_id=data.snapshot_id,
                        boundary=data.boundary,
                        completeness=data.completeness,
                    )
                )
            elif row.boundary != data.boundary or row.completeness != data.completeness:
                conflict("import_conflict")
            part = await repo.session.get(DataImportPart, (row.id, data.part))
            if part is not None:
                if part.fingerprint != digest(data):
                    conflict("import_conflict")
                return {
                    "import_id": str(row.id),
                    "duplicate": True,
                    "items": part.acknowledgements,
                }
            if row.final_part is not None and (
                data.part > row.final_part
                or (data.final and data.part != row.final_part)
            ):
                conflict(
                    "import_closed", "Part is outside the declared final boundary."
                )
            if data.final:
                maximum = await repo.session.scalar(
                    select(func.max(DataImportPart.part)).where(
                        DataImportPart.import_id == row.id
                    )
                )
                if maximum is not None and maximum > data.part:
                    conflict(
                        "import_conflict",
                        "Final part precedes an already accepted part.",
                    )
                row.final_part = data.part
            acks = []
            for index, item in enumerate(data.items):
                ack = await self._ingest(repo, collection_id, item, "imports")
                ack.index = index
                acks.append(msgspec.to_builtins(ack))
                member = await repo.session.get(
                    DataImportMember, (row.id, ack.object_id)
                )
                if member is None:
                    await repo.add(
                        DataImportMember(
                            import_id=row.id,
                            object_id=ack.object_id,
                            object_version=item.object_version,
                        )
                    )
                elif member.object_version != item.object_version:
                    conflict(
                        "import_conflict",
                        "A snapshot cannot contain multiple versions of the same object.",
                    )
            await repo.add(
                DataImportPart(
                    import_id=row.id,
                    part=data.part,
                    fingerprint=digest(data),
                    acknowledgements=acks,
                )
            )
            result = {"import_id": str(row.id), "duplicate": False, "items": acks}
        return result

    async def _import_status(
        self, repo: DataRepository, row: DataImport
    ) -> dict[str, Any]:
        parts = list(
            await repo.session.scalars(
                select(DataImportPart.part)
                .where(DataImportPart.import_id == row.id)
                .order_by(DataImportPart.part)
            )
        )
        missing = sorted(
            set(range(1, (row.final_part or max(parts, default=0)) + 1)) - set(parts)
        )
        return {
            "id": str(row.id),
            "snapshot_id": row.snapshot_id,
            "boundary": row.boundary,
            "declared_completeness": row.completeness,
            "final_part": row.final_part,
            "received_parts": parts,
            "missing_parts": missing,
            "snapshot_complete": row.completeness == "complete"
            and row.final_part is not None
            and not missing,
            "history_complete": False,
            "created_at": row.created_at.isoformat(),
        }

    async def import_status(self, import_id: UUID) -> dict[str, Any]:
        require_scope(self.context, "data:read")
        async with self.transaction() as repo:
            row = found(await repo.by_id(DataImport, import_id))
            require_source(self.context, row.source_id)
            return await self._import_status(repo, row)

    async def reconcile(
        self, import_id: UUID, cursor: UUID | None = None, limit: int = 50
    ) -> dict[str, Any]:
        require_scope(self.context, "data:read")
        # A consistent read relative to ingestion, not an automatic deletion command.
        async with self.transaction(write=True) as repo:
            row = found(await repo.by_id(DataImport, import_id))
            require_source(self.context, row.source_id)
            stmt = (
                select(DataObject, DataImportMember.object_version)
                .outerjoin(
                    DataImportMember,
                    (DataImportMember.object_id == DataObject.id)
                    & (DataImportMember.import_id == row.id),
                )
                .where(
                    *owner(DataObject, self.context),
                    DataObject.collection_id == row.collection_id,
                    DataObject.deleted.is_(False),
                )
            )
            if cursor:
                stmt = stmt.where(DataObject.id > cursor)
            rows = (
                await repo.session.execute(
                    stmt.order_by(DataObject.id).limit(limit + 1)
                )
            ).all()
            return {
                "import": await self._import_status(repo, row),
                "items": [
                    {
                        "object_id": str(obj.id),
                        "external_id": obj.external_id,
                        "current_version": obj.object_version,
                        "snapshot_version": version,
                        "status": "absent_from_snapshot"
                        if version is None
                        else "newer_than_snapshot"
                        if obj.object_version > version
                        else "matched",
                    }
                    for obj, version in rows[:limit]
                ],
                "next_cursor": str(rows[limit - 1][0].id)
                if len(rows) > limit
                else None,
                "deletes_applied": False,
            }

    async def coverage(self, collection_id: UUID) -> dict[str, Any]:
        require_scope(self.context, "data:read")
        async with self.transaction(write=True) as repo:
            await self.collection(repo, collection_id)
            filters = (
                *owner(DataDelivery, self.context),
                DataDelivery.collection_id == collection_id,
            )
            sequence = (
                select(
                    DataDelivery.source_sequence.label("n"),
                    func.lag(DataDelivery.source_sequence, 1, 0)
                    .over(order_by=DataDelivery.source_sequence)
                    .label("previous"),
                )
                .where(*filters, DataDelivery.source_sequence.is_not(None))
                .subquery()
            )
            gaps = (
                await repo.session.execute(
                    select(
                        (sequence.c.previous + 1).label("start"),
                        (sequence.c.n - 1).label("end"),
                    )
                    .where(sequence.c.n > sequence.c.previous + 1)
                    .limit(101)
                )
            ).all()
            maximum = await repo.session.scalar(
                select(func.max(DataDelivery.source_sequence)).where(*filters)
            )
            unsequenced = await repo.session.scalar(
                select(func.count())
                .select_from(DataDelivery)
                .where(
                    *filters,
                    DataDelivery.source_sequence.is_(None),
                    DataDelivery.operation != "snapshot",
                )
            )
            erased = await repo.session.scalar(
                select(func.count())
                .select_from(DataDelivery)
                .where(*filters, DataDelivery.erased.is_(True))
            )
            snapshots = await repo.session.scalar(
                select(func.count())
                .select_from(DataDelivery)
                .where(*filters, DataDelivery.operation == "snapshot")
            )
            return {
                "source_sequence_high_watermark": maximum,
                "missing_sequence_ranges": [
                    {"start": a, "end": b} for a, b in gaps[:100]
                ],
                "gaps_truncated": len(gaps) > 100,
                "unsequenced_events": unsequenced,
                "snapshot_versions": snapshots,
                "erased_deliveries": erased,
                "history_complete": False,
                "limitations": [
                    "Sequence starts at 1 per owner and collection; only the received prefix can be checked.",
                    "Snapshots establish state, not past actions; no source end watermark or historical completeness is inferred.",
                    "Erasure removes private history; retained sequence markers do not restore that history.",
                ],
            }

    async def _erase_deliveries(
        self, repo: DataRepository, delivery_ids: list[UUID]
    ) -> None:
        if not delivery_ids:
            return
        # A corrected observation remains connected to its original evidence.
        # Erase the entire chain in either direction, including changed references.
        chain = set(delivery_ids)
        pending = set(delivery_ids)
        while pending:
            batch = set(list(pending)[:500])
            pending.difference_update(batch)
            linked = list(
                await repo.session.scalars(
                    select(DataDelivery).where(
                        *owner(DataDelivery, self.context),
                        (DataDelivery.id.in_(batch))
                        | (DataDelivery.corrected_by_id.in_(batch)),
                    )
                )
            )
            neighbors = {row.id for row in linked}
            neighbors.update(
                row.corrected_by_id for row in linked if row.corrected_by_id
            )
            pending.update(neighbors - chain)
            chain.update(neighbors)
        delivery_ids = list(chain)
        # Any erased member invalidates the entire derived collection result,
        # including conclusions based on absence, counts and sampled evidence.
        impacted_collections = set()
        for offset in range(0, len(delivery_ids), 1000):
            impacted_collections.update(
                await repo.session.scalars(
                    select(DataDelivery.collection_id).where(
                        *owner(DataDelivery, self.context),
                        DataDelivery.id.in_(delivery_ids[offset : offset + 1000]),
                    )
                )
            )
        from app.jobs.lifecycle import erase_runs
        from app.personalization.lifecycle import invalidate_deliveries

        await invalidate_deliveries(
            repo.session, self.context, delivery_ids, erased=True
        )
        await erase_runs(repo.session, self.context, impacted_collections)
        for collection_id in impacted_collections:
            await repo.session.execute(
                delete(AnalysisResult).where(
                    *owner(AnalysisResult, self.context),
                    AnalysisResult.collection_ids.contains([str(collection_id)]),
                )
            )
        # Keep PostgreSQL bind counts bounded for accumulated user histories.
        for offset in range(0, len(delivery_ids), 1000):
            batch_ids = delivery_ids[offset : offset + 1000]
            await repo.session.execute(
                delete(EvidenceFragment).where(
                    EvidenceFragment.delivery_id.in_(batch_ids)
                )
            )
            await repo.session.execute(
                delete(FileProvenance).where(FileProvenance.delivery_id.in_(batch_ids))
            )
            await repo.session.execute(
                delete(DataOutbox).where(
                    *owner(DataOutbox, self.context),
                    DataOutbox.delivery_id.in_(batch_ids),
                )
            )
            await repo.session.execute(
                update(DataDelivery)
                .where(
                    *owner(DataDelivery, self.context), DataDelivery.id.in_(batch_ids)
                )
                .values(
                    payload=None,
                    data_metadata={},
                    references=[],
                    fingerprint=None,
                    state_fingerprint=None,
                    erased=True,
                )
            )

    async def _erase_objects(self, repo: DataRepository, object_ids: list[UUID]) -> int:
        pending = set(object_ids)
        visited: set[UUID] = set()
        impacted: set[tuple[UUID, UUID]] = set()
        while pending:
            object_id = pending.pop()
            if object_id in visited:
                continue
            visited.add(object_id)
            obj = found(await repo.by_id(DataObject, object_id))
            impacted.add((obj.source_id, obj.collection_id))
            # Erase referring private records as well: their historical payloads
            # may embed content derived from the erased object.
            dependents = list(
                await repo.session.scalars(
                    select(DataDelivery).where(
                        *owner(DataDelivery, self.context),
                        DataDelivery.erased.is_(False),
                        (DataDelivery.object_id == object_id)
                        | DataDelivery.references.contains([str(object_id)]),
                    )
                )
            )
            for delivery in dependents:
                impacted.add((delivery.source_id, delivery.collection_id))
                if delivery.object_id and delivery.object_id != object_id:
                    pending.add(delivery.object_id)
            await self._erase_deliveries(repo, [x.id for x in dependents])
            await repo.session.execute(
                delete(DataOutbox).where(
                    *owner(DataOutbox, self.context), DataOutbox.object_id == object_id
                )
            )
            # Remove manifests/acknowledgements that could retain import metadata.
            imports = select(DataImportMember.import_id).where(
                DataImportMember.object_id == object_id
            )
            await repo.session.execute(
                delete(DataImport).where(
                    *owner(DataImport, self.context), DataImport.id.in_(imports)
                )
            )
            obj.deleted = True
            obj.payload = None
            obj.data_metadata = {}
            obj.references = []
        for source_id, collection_id in impacted:
            await repo.add(
                DataOutbox(
                    tenant_id=self.context.tenant_id,
                    user_id=self.context.user_id,
                    source_id=source_id,
                    collection_id=collection_id,
                    reason="erasure",
                )
            )
        await repo.session.flush()
        return len(visited)

    async def erase_object(self, object_id: UUID) -> dict[str, Any]:
        require_scope(self.context, "data:delete")
        async with self.transaction(write=True) as repo:
            row = found(await repo.by_id(DataObject, object_id))
            require_source(self.context, row.source_id)
            count = await self._erase_objects(repo, [row.id])
        return {"erased": True, "objects_erased": count, "identity_reusable": False}

    async def erase_event(self, delivery_id: UUID) -> dict[str, Any]:
        require_scope(self.context, "data:delete")
        async with self.transaction(write=True) as repo:
            row = found(await repo.by_id(DataDelivery, delivery_id))
            require_source(self.context, row.source_id)
            if row.object_id is not None:
                await self._erase_objects(repo, [row.object_id])
            else:
                await self._erase_deliveries(repo, [row.id])
                await repo.add(
                    DataOutbox(
                        tenant_id=self.context.tenant_id,
                        user_id=self.context.user_id,
                        source_id=row.source_id,
                        collection_id=row.collection_id,
                        reason="erasure",
                    )
                )
        return {"erased": True, "identity_reusable": False}

    async def erase_scope(
        self, *, source_id: UUID | None = None, collection_id: UUID | None = None
    ) -> dict[str, Any]:
        require_scope(self.context, "data:delete")
        if (
            source_id is None
            and collection_id is None
            and self.context.source_ids is not None
        ):
            from litestar.exceptions import PermissionDeniedException

            raise PermissionDeniedException(
                detail="Erasing all user data requires an unrestricted source grant."
            )
        async with self.transaction(write=True) as repo:
            scope = "user"
            if collection_id is not None:
                await self.collection(repo, collection_id)
                scope = f"collection:{collection_id}"
            elif source_id is not None:
                await self.accessible_source(
                    IntegrationRepository(repo.session, self.context), source_id
                )
                scope = f"source:{source_id}"
            exists = await repo.session.scalar(
                select(DataFence.id).where(
                    *owner(DataFence, self.context), DataFence.scope == scope
                )
            )
            if not exists:
                await repo.add(
                    DataFence(
                        tenant_id=self.context.tenant_id,
                        user_id=self.context.user_id,
                        scope=scope,
                    )
                )

            def filters(
                model: type[DataObject]
                | type[DataDelivery]
                | type[DataOutbox]
                | type[DataImport],
            ):
                values = list(owner(model, self.context))
                if collection_id is not None:
                    values.append(model.collection_id == collection_id)
                elif source_id is not None:
                    values.append(model.source_id == source_id)
                return values

            ids = list(
                await repo.session.scalars(
                    select(DataObject.id).where(
                        *filters(DataObject), DataObject.deleted.is_(False)
                    )
                )
            )
            await self._erase_objects(repo, ids)
            # Include independent observations and imports with empty manifests.
            delivery_ids = list(
                await repo.session.scalars(
                    select(DataDelivery.id).where(*filters(DataDelivery))
                )
            )
            await self._erase_deliveries(repo, delivery_ids)
            await repo.session.execute(delete(DataImport).where(*filters(DataImport)))
            await repo.session.execute(delete(DataOutbox).where(*filters(DataOutbox)))
            # Scope erasure also removes private profile goals/configurations.
            profile_stmt = (
                select(AnalysisProfileVersion.profile_id)
                .join(
                    AnalysisProfile,
                    AnalysisProfile.id == AnalysisProfileVersion.profile_id,
                )
                .where(*owner(AnalysisProfile, self.context))
            )
            if scope != "user":
                collection_ids = (
                    [collection_id]
                    if collection_id
                    else list(
                        await repo.session.scalars(
                            select(IntegrationCollection.id).where(
                                IntegrationCollection.source_id == source_id,
                                IntegrationCollection.tenant_id
                                == self.context.tenant_id,
                            )
                        )
                    )
                )
                from app.jobs.lifecycle import erase_runs
                from app.personalization.lifecycle import (
                    erase_scope as erase_memory_scope,
                )

                await erase_memory_scope(repo.session, self.context, collection_ids)
                await erase_runs(repo.session, self.context, collection_ids)
                for cid in collection_ids:
                    await repo.session.execute(
                        delete(AnalysisResult).where(
                            *owner(AnalysisResult, self.context),
                            AnalysisResult.collection_ids.contains([str(cid)]),
                        )
                    )
                    affected = profile_stmt.where(
                        (
                            AnalysisProfileVersion.config["selections"].contains(
                                [{"collection_id": str(cid)}]
                            )
                        )
                        | (
                            AnalysisProfileVersion.config["personalization"][
                                "collections"
                            ].contains([str(cid)])
                        )
                    )
                    await repo.session.execute(
                        delete(AnalysisProfile).where(AnalysisProfile.id.in_(affected))
                    )
            else:
                from app.personalization.lifecycle import (
                    erase_scope as erase_memory_scope,
                )

                await erase_memory_scope(repo.session, self.context)
                await repo.session.execute(
                    delete(AnalysisProfile).where(*owner(AnalysisProfile, self.context))
                )
            if scope == "user":
                # Legacy documents are not source-addressable. FK cascades remove
                # chunks, vectors, knowledge and conversation messages.
                await repo.session.execute(
                    delete(ConversationSession).where(
                        *owner(ConversationSession, self.context)
                    )
                )
                await repo.session.execute(
                    delete(Document).where(*owner(Document, self.context))
                )
        return {"erased": True, "scope": scope, "identity_reusable": False}
