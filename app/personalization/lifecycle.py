"""Privacy and revision hooks shared with ingestion; no service/provider imports."""

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import delete, func, select, tuple_, update

from app.db.models import (
    AnalysisProfileVersion,
    AnalysisResult,
    AnalysisRun,
    MemoryBlock,
    MemoryItem,
    MemoryMessage,
    MemorySettings,
    PersonalizationUse,
)
from app.repositories.data_lifecycle import owner


async def settings_row(session, context, *, create=False):
    row = await session.scalar(
        select(MemorySettings).where(*owner(MemorySettings, context))
    )
    if row is None and create:
        row = MemorySettings(
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            version=0,
            enabled=False,
            config={"collections": [], "definitions": [], "field_rules": []},
            revision=uuid4(),
        )
        session.add(row)
        await session.flush()
    return row


async def touch(session, context) -> MemorySettings:
    row = await settings_row(session, context, create=True)
    assert row is not None
    row.version += 1
    row.revision = uuid4()
    row.updated_at = datetime.now(UTC)
    return row


async def invalidate_items(
    session, context, item_ids, *, erased=False, reason="source_changed"
):
    ids = list(set(item_ids))
    if not ids:
        return
    for offset in range(0, len(ids), 500):
        batch = ids[offset : offset + 500]
        rows = list(
            await session.scalars(
                select(MemoryItem).where(
                    *owner(MemoryItem, context), MemoryItem.id.in_(batch)
                )
            )
        )
        if not erased:
            rows = [row for row in rows if row.state != "deleted"]
        dependent = select(PersonalizationUse.result_id).where(
            PersonalizationUse.memory_id.in_(batch)
        )
        if erased:
            await session.execute(
                delete(AnalysisResult).where(
                    *owner(AnalysisResult, context), AnalysisResult.id.in_(dependent)
                )
            )
        if erased:
            for row in rows:
                exists = await session.scalar(
                    select(MemoryBlock.id).where(
                        *owner(MemoryBlock, context),
                        MemoryBlock.collection_id == row.collection_id,
                        MemoryBlock.key == row.key,
                    )
                )
                if exists is None:
                    session.add(
                        MemoryBlock(
                            tenant_id=row.tenant_id,
                            user_id=row.user_id,
                            source_id=row.source_id,
                            collection_id=row.collection_id,
                            key=row.key,
                        )
                    )
        if erased:
            for cid in {row.collection_id for row in rows}:
                profiles = select(
                    AnalysisProfileVersion.profile_id, AnalysisProfileVersion.version
                ).where(
                    AnalysisProfileVersion.config["personalization"]["enabled"]
                    .as_boolean()
                    .is_(True),
                    AnalysisProfileVersion.config["personalization"][
                        "collections"
                    ].contains([str(cid)]),
                )
                await session.execute(
                    update(AnalysisRun)
                    .where(
                        *owner(AnalysisRun, context),
                        AnalysisRun.status.in_(["pending", "running"]),
                        tuple_(AnalysisRun.profile_id, AnalysisRun.profile_version).in_(
                            profiles
                        ),
                    )
                    .values(
                        status="cancelled",
                        input={},
                        lease_token=None,
                        lease_expires_at=None,
                        error_code="memory_erased",
                        finished_at=func.clock_timestamp(),
                    )
                )
        for row in rows:
            row.value = None
            row.provenance = {"reason": reason}
            row.period = None
            row.state = "deleted" if erased else "invalidated"
            row.version += 1
            row.updated_at = datetime.now(UTC)
        await session.flush()
    await touch(session, context)


async def invalidate_deliveries(session, context, delivery_ids, *, erased=False):
    ids = list(delivery_ids)
    for offset in range(0, len(ids), 500):
        affected = list(
            await session.scalars(
                select(MemoryItem.id).where(
                    *owner(MemoryItem, context),
                    MemoryItem.delivery_id.in_(ids[offset : offset + 500]),
                    MemoryItem.state != "deleted",
                )
            )
        )
        await invalidate_items(
            session,
            context,
            affected,
            erased=erased,
            reason="source_erased" if erased else "source_changed",
        )


async def erase_scope(session, context, collection_ids=None):
    statement = select(MemoryItem.id).where(*owner(MemoryItem, context))
    if collection_ids is not None:
        statement = statement.where(MemoryItem.collection_id.in_(collection_ids))
    await invalidate_items(
        session,
        context,
        list(await session.scalars(statement)),
        erased=True,
        reason="scope_erased",
    )
    for model in (MemoryItem, MemoryMessage, MemoryBlock):
        stmt = delete(model).where(*owner(model, context))
        if collection_ids is not None:
            stmt = stmt.where(model.collection_id.in_(collection_ids))
        await session.execute(stmt)
    if collection_ids is None:
        await session.execute(
            delete(MemorySettings).where(*owner(MemorySettings, context))
        )
    else:
        row = await settings_row(session, context)
        if row:
            removed = {str(cid) for cid in collection_ids}
            row.config = {
                **row.config,
                "collections": [
                    cid for cid in row.config["collections"] if cid not in removed
                ],
                "field_rules": [
                    rule
                    for rule in row.config["field_rules"]
                    if rule["collection_id"] not in removed
                ],
            }
            await touch(session, context)


async def source_current(session, context, row):
    from app.db.models import DataDelivery, DataObject

    if row.message_id:
        message = await session.scalar(
            select(MemoryMessage).where(
                *owner(MemoryMessage, context), MemoryMessage.id == row.message_id
            )
        )
        return (
            message is not None
            and not message.erased
            and message.version == row.provenance.get("message_version")
        )
    if row.delivery_id:
        delivery = await session.scalar(
            select(DataDelivery).where(
                *owner(DataDelivery, context), DataDelivery.id == row.delivery_id
            )
        )
        if delivery is None or delivery.erased or delivery.corrected_by_id:
            return False
        if delivery.object_id:
            obj = await session.scalar(
                select(DataObject).where(
                    *owner(DataObject, context), DataObject.id == delivery.object_id
                )
            )
            return (
                obj is not None
                and not obj.deleted
                and obj.object_version == delivery.object_version
            )
    return True
