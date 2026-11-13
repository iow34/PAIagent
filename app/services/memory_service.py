"""Owner-consented, source-scoped preferences and confirmable candidates."""

from datetime import UTC, datetime
from uuid import UUID

import msgspec
from litestar.exceptions import HTTPException, PermissionDeniedException
from sqlalchemy import func, select, update

from app.capabilities.validation import node_at, value_at
from app.db.models import (
    DataDelivery,
    DataObject,
    FileProvenance,
    MemoryBlock,
    MemoryItem,
    MemoryMessage,
)
from app.integration.validation import ContractError
from app.personalization.lifecycle import invalidate_items, settings_row, touch
from app.personalization.types import (
    ExtractionInput,
    MemoryAction,
    MessageInput,
    MessageUpdate,
    PreferenceWrite,
    SettingsInput,
)
from app.personalization.validation import (
    definition,
    message_values,
    validate_settings,
    validate_value,
)
from app.repositories.data_lifecycle import check_fence, owner
from app.repositories.integration import IntegrationRepository
from app.security.permissions import require_scope, require_source
from app.services.data_service import DataService, conflict, digest
from app.services.integration_service import found

MAX_ITEMS = 2000
MAX_MESSAGES = 1000


def item_view(row):
    return {
        "id": str(row.id),
        "collection_id": str(row.collection_id),
        "source_id": str(row.source_id),
        "key": row.key,
        "kind": row.kind,
        "state": row.state,
        "version": row.version,
        "origin": row.origin,
        "value": row.value,
        "provenance": row.provenance,
        "period": row.period,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def settings_view(row):
    return {
        "version": row.version if row else 0,
        "enabled": row.enabled if row else False,
        "config": row.config
        if row
        else {"collections": [], "definitions": [], "field_rules": []},
    }


class MemoryService(DataService):
    def unrestricted(self):
        if self.context.source_ids is not None:
            raise PermissionDeniedException(
                detail="Owner-wide memory settings require an unrestricted source grant."
            )

    async def get_settings(self):
        require_scope(self.context, "memory:read")
        self.unrestricted()
        async with self.transaction() as repo:
            return settings_view(await settings_row(repo.session, self.context))

    async def configure(self, data: SettingsInput):
        require_scope(self.context, "memory:write")
        self.unrestricted()
        data = msgspec.convert(msgspec.to_builtins(data), type=SettingsInput)
        validate_settings(data)
        async with self.transaction(write=True) as repo:
            await check_fence(repo.session, self.context)
            row = await settings_row(repo.session, self.context, create=True)
            assert row is not None
            if row.version != data.expected_version:
                conflict(
                    "memory_version_conflict",
                    "Memory settings changed; read their current version.",
                )
            for cid in data.collections:
                await self.collection(repo, cid, writable=True)
            for rule in data.field_rules:
                schema = found(
                    await IntegrationRepository(repo.session, self.context).schema(
                        rule.collection_id, rule.source_kind, rule.schema_version
                    )
                )
                node_at(schema.json_schema, rule.field)
            config = msgspec.to_builtins(data)
            config.pop("expected_version")
            config.pop("enabled")
            rows = list(
                await repo.session.scalars(
                    select(MemoryItem).where(
                        *owner(MemoryItem, self.context),
                        MemoryItem.state.in_(["candidate", "confirmed"]),
                    )
                )
            )
            invalid = []
            for item in rows:
                try:
                    if str(item.collection_id) not in config["collections"]:
                        raise ContractError("Collection removed.")
                    validate_value(config, item.key, item.value)
                    if item.origin == "field" and not any(
                        r["collection_id"] == str(item.collection_id)
                        and r["key"] == item.key
                        and r["field"] == item.provenance.get("field")
                        and r["schema_version"] == item.provenance.get("schema_version")
                        and r["kind"] == item.kind
                        for r in config["field_rules"]
                    ):
                        raise ContractError("Field consent removed.")
                    if (
                        item.origin == "message"
                        and item.provenance.get("prefix")
                        not in definition(config, item.key)["message_prefixes"]
                    ):
                        raise ContractError("Message prefix removed.")
                except ContractError:
                    invalid.append(item.id)
            await invalidate_items(
                repo.session, self.context, invalid, reason="consent_changed"
            )
            row.config, row.enabled = config, data.enabled
            await touch(repo.session, self.context)
            return settings_view(row)

    async def policy(self, repo, collection_id):
        collection = await self.collection(repo, collection_id, writable=True)
        row = await settings_row(repo.session, self.context)
        if row is None or not row.enabled:
            raise ContractError("Managed memory is disabled.", code="memory_disabled")
        if str(collection_id) not in row.config["collections"]:
            raise ContractError("Collection is not allowed for managed memory.")
        return collection, row.config

    async def blocked(self, repo, collection_id, key):
        return await repo.session.scalar(
            select(MemoryBlock.id).where(
                *owner(MemoryBlock, self.context),
                MemoryBlock.collection_id == collection_id,
                MemoryBlock.key == key,
            )
        )

    async def active(self, repo, collection_id, key):
        return await repo.session.scalar(
            select(MemoryItem).where(
                *owner(MemoryItem, self.context),
                MemoryItem.collection_id == collection_id,
                MemoryItem.key == key,
                MemoryItem.kind == "preference",
                MemoryItem.state == "confirmed",
            )
        )

    async def item(self, repo, item_id):
        row = found(
            await repo.session.scalar(
                select(MemoryItem).where(
                    *owner(MemoryItem, self.context), MemoryItem.id == item_id
                )
            )
        )
        require_source(self.context, row.source_id)
        return row

    async def _capacity(self, repo, model, maximum):
        count = await repo.session.scalar(
            select(func.count()).select_from(model).where(*owner(model, self.context))
        )
        if count >= maximum:
            raise ContractError(
                "Managed memory storage limit reached.", code="memory_limit"
            )

    async def put_preference(self, data: PreferenceWrite):
        require_scope(self.context, "memory:write")
        data = msgspec.convert(msgspec.to_builtins(data), type=PreferenceWrite)
        async with self.transaction(write=True) as repo:
            row = await self._preference(repo, data)
            return item_view(row)

    async def _preference(self, repo, data):
        collection, policy = await self.policy(repo, data.collection_id)
        validate_value(policy, data.key, data.value)
        if await self.blocked(repo, data.collection_id, data.key) and not data.restore:
            raise HTTPException(
                status_code=410,
                detail="Memory key was deleted; an explicit restore is required.",
                extra={"code": "memory_deleted"},
            )
        current = await self.active(repo, data.collection_id, data.key)
        if data.expected_version != (current.version if current else 0):
            conflict(
                "memory_version_conflict",
                "Read the current confirmed preference version before correction.",
            )
        identity = digest(["explicit", str(data.collection_id), data.key])
        row = await repo.session.scalar(
            select(MemoryItem).where(
                *owner(MemoryItem, self.context), MemoryItem.identity == identity
            )
        )
        if current and current != row:
            await invalidate_items(
                repo.session, self.context, [current.id], reason="explicit_correction"
            )
            current.state = "superseded"
            await repo.session.flush()
        if row is None:
            await self._capacity(repo, MemoryItem, MAX_ITEMS)
            row = MemoryItem(
                **self.ownership(collection),
                identity=identity,
                key=data.key,
                kind="preference",
                state="confirmed",
                origin="explicit",
                value=data.value,
                provenance={"asserted_by": "user_api"},
                version=1,
            )
            await repo.add(row)
        else:
            await invalidate_items(
                repo.session, self.context, [row.id], reason="explicit_correction"
            )
            row.state, row.origin, row.value = "confirmed", "explicit", data.value
            row.provenance, row.period = {"asserted_by": "user_api"}, None
            row.delivery_id = row.message_id = None
            row.updated_at = datetime.now(UTC)
        await touch(repo.session, self.context)
        await repo.session.flush()
        return row

    async def source_current(self, repo, row):
        from app.personalization.lifecycle import source_current

        return await source_current(repo.session, self.context, row)

    async def confirm(self, item_id, data: MemoryAction):
        data = msgspec.convert(msgspec.to_builtins(data), type=MemoryAction)
        require_scope(self.context, "memory:write")
        async with self.transaction(write=True) as repo:
            row = await self.item(repo, item_id)
            if row.version != data.expected_version:
                conflict("memory_version_conflict")
            await self.policy(repo, row.collection_id)
            if row.state == "confirmed":
                return item_view(row)
            if row.state != "candidate" or await self.blocked(
                repo, row.collection_id, row.key
            ):
                raise ContractError(
                    "Only current, non-deleted candidates can be confirmed."
                )
            if not await self.source_current(repo, row):
                conflict(
                    "memory_source_changed",
                    "Candidate source changed; extract it again.",
                )
            if row.kind == "preference":
                current = await self.active(repo, row.collection_id, row.key)
                if current:
                    if current.value != row.value:
                        conflict(
                            "memory_conflict",
                            "A confirmed preference differs; use explicit versioned correction.",
                        )
                    row.state = "superseded"
                    row.version += 1
                    await touch(repo.session, self.context)
                    return item_view(current)
            row.state = "confirmed"
            row.version += 1
            row.updated_at = datetime.now(UTC)
            await touch(repo.session, self.context)
            return item_view(row)

    async def delete_item(self, item_id):
        require_scope(self.context, "memory:write")
        async with self.transaction(write=True) as repo:
            row = await self.item(repo, item_id)
            if row.state != "deleted":
                await invalidate_items(
                    repo.session,
                    self.context,
                    [row.id],
                    erased=True,
                    reason="user_deleted",
                )
            return item_view(row)

    async def get_item(self, item_id):
        require_scope(self.context, "memory:read")
        async with self.transaction() as repo:
            return item_view(await self.item(repo, item_id))

    async def items(self, cursor=None, limit=50, collection_id=None, state=None):
        require_scope(self.context, "memory:read")
        async with self.transaction() as repo:
            stmt = select(MemoryItem).where(*owner(MemoryItem, self.context))
            if collection_id:
                await self.collection(repo, collection_id)
                stmt = stmt.where(MemoryItem.collection_id == collection_id)
            if self.context.source_ids is not None:
                stmt = stmt.where(MemoryItem.source_id.in_(self.context.source_ids))
            if cursor:
                stmt = stmt.where(MemoryItem.id > cursor)
            if state:
                stmt = stmt.where(MemoryItem.state == state)
            rows = list(
                await repo.session.scalars(
                    stmt.order_by(MemoryItem.id).limit(limit + 1)
                )
            )
            return {
                "items": [item_view(r) for r in rows[:limit]],
                "next_cursor": str(rows[limit - 1].id) if len(rows) > limit else None,
            }

    async def _candidate(
        self,
        repo,
        collection,
        key,
        value,
        identity,
        origin,
        provenance,
        *,
        kind="preference",
        delivery_id=None,
        message_id=None,
        period=None,
    ):
        if await self.blocked(repo, collection.id, key):
            return None
        row = await repo.session.scalar(
            select(MemoryItem).where(
                *owner(MemoryItem, self.context), MemoryItem.identity == identity
            )
        )
        if row and row.state in {"deleted", "rejected"}:
            return None
        if (
            row
            and row.value == value
            and row.provenance == provenance
            and row.period == period
            and row.kind == kind
            and row.state in {"candidate", "confirmed"}
        ):
            return row
        if row:
            await invalidate_items(
                repo.session, self.context, [row.id], reason="source_changed"
            )
            row.value, row.provenance, row.state, row.kind = (
                value,
                provenance,
                "candidate",
                kind,
            )
            row.delivery_id, row.message_id, row.period = (
                delivery_id,
                message_id,
                period,
            )
        else:
            await self._capacity(repo, MemoryItem, MAX_ITEMS)
            row = await repo.add(
                MemoryItem(
                    **self.ownership(collection),
                    identity=identity,
                    key=key,
                    kind=kind,
                    state="candidate",
                    origin=origin,
                    value=value,
                    provenance=provenance,
                    delivery_id=delivery_id,
                    message_id=message_id,
                    period=period,
                    version=1,
                )
            )
        await touch(repo.session, self.context)
        await repo.session.flush()
        return row

    async def _message_candidates(self, repo, message):
        collection, policy = await self.policy(repo, message.collection_id)
        values = {}
        ambiguous = set()
        for key, value, span in message_values(policy, message.content):
            if key in values and values[key][0] != value:
                ambiguous.add(key)
            values[key] = (value, span)
        items = []
        for key, (value, span) in values.items():
            if key in ambiguous:
                continue
            row = await self._candidate(
                repo,
                collection,
                key,
                value,
                digest(["message", str(message.id), key]),
                "message",
                {
                    **span,
                    "message_id": str(message.id),
                    "message_version": message.version,
                    "author": "user",
                },
                message_id=message.id,
            )
            if row:
                items.append(item_view(row))
        return {
            "message_id": str(message.id),
            "version": message.version,
            "items": items,
            "ambiguous_keys": sorted(ambiguous),
        }

    async def message(self, data: MessageInput):
        require_scope(self.context, "memory:write")
        data = msgspec.convert(msgspec.to_builtins(data), type=MessageInput)
        from app.integration.validation import bounded_json

        bounded_json(msgspec.to_builtins(data), 40000)
        if (
            not data.external_id.strip()
            or not data.external_id.isprintable()
            or not data.text.strip()
        ):
            raise ContractError(
                "User message ID/text must not be blank; ID must be printable."
            )
        async with self.transaction(write=True) as repo:
            collection, _ = await self.policy(repo, data.collection_id)
            row = await repo.session.scalar(
                select(MemoryMessage).where(
                    *owner(MemoryMessage, self.context),
                    MemoryMessage.collection_id == collection.id,
                    MemoryMessage.external_id == data.external_id,
                )
            )
            fingerprint = digest(data)
            if row:
                if row.erased:
                    raise HTTPException(
                        status_code=410, detail="User message identity was erased."
                    )
                if row.fingerprint != fingerprint:
                    conflict(
                        "memory_message_conflict",
                        "Message ID is immutable; use versioned PUT to edit it.",
                    )
            else:
                await self._capacity(repo, MemoryMessage, MAX_MESSAGES)
                row = await repo.add(
                    MemoryMessage(
                        **self.ownership(collection),
                        external_id=data.external_id,
                        content=data.text,
                        fingerprint=fingerprint,
                        version=1,
                        erased=False,
                    )
                )
            return await self._message_candidates(repo, row)

    async def message_row(self, repo, message_id):
        row = found(
            await repo.session.scalar(
                select(MemoryMessage).where(
                    *owner(MemoryMessage, self.context), MemoryMessage.id == message_id
                )
            )
        )
        require_source(self.context, row.source_id)
        if row.erased:
            raise HTTPException(status_code=410, detail="User message was erased.")
        return row

    async def get_message(self, message_id):
        require_scope(self.context, "memory:read")
        async with self.transaction() as repo:
            row = await self.message_row(repo, message_id)
            return {
                "id": str(row.id),
                "collection_id": str(row.collection_id),
                "external_id": row.external_id,
                "version": row.version,
                "author": "user",
                "text": row.content,
                "created_at": row.created_at,
            }

    async def edit_message(self, message_id, data: MessageUpdate):
        data = msgspec.convert(msgspec.to_builtins(data), type=MessageUpdate)
        require_scope(self.context, "memory:write")
        from app.integration.validation import bounded_json

        bounded_json(msgspec.to_builtins(data), 40000)
        if not data.text.strip():
            raise ContractError("User message must not be blank.")
        async with self.transaction(write=True) as repo:
            row = await self.message_row(repo, message_id)
            if row.version != data.expected_version:
                conflict("memory_version_conflict")
            await self.policy(repo, row.collection_id)
            affected = list(
                await repo.session.scalars(
                    select(MemoryItem.id).where(
                        *owner(MemoryItem, self.context),
                        MemoryItem.message_id == row.id,
                    )
                )
            )
            await invalidate_items(
                repo.session, self.context, affected, reason="message_changed"
            )
            row.content, row.version = data.text, row.version + 1
            row.fingerprint = digest(
                MessageInput(row.collection_id, row.external_id, data.text)
            )
            return await self._message_candidates(repo, row)

    async def delete_message(self, message_id):
        require_scope(self.context, "memory:write")
        async with self.transaction(write=True) as repo:
            row = await self.message_row(repo, message_id)
            affected = list(
                await repo.session.scalars(
                    select(MemoryItem.id).where(
                        *owner(MemoryItem, self.context),
                        MemoryItem.message_id == row.id,
                    )
                )
            )
            await invalidate_items(
                repo.session,
                self.context,
                affected,
                erased=True,
                reason="message_erased",
            )
            row.content = row.fingerprint = None
            row.erased = True
            row.version += 1
            await touch(repo.session, self.context)
        return {"erased": True}

    async def extract(self, data: ExtractionInput):
        data = msgspec.convert(msgspec.to_builtins(data), type=ExtractionInput)
        require_scope(self.context, "memory:write")
        require_scope(self.context, "data:read")
        async with self.transaction(write=True) as repo:
            delivery = found(
                await repo.session.scalar(
                    select(DataDelivery).where(
                        *owner(DataDelivery, self.context),
                        DataDelivery.id == data.delivery_id,
                    )
                )
            )
            collection, policy = await self.policy(repo, delivery.collection_id)
            if delivery.erased or delivery.corrected_by_id or delivery.payload is None:
                raise ContractError("Extraction requires a current non-erased source.")
            if await repo.session.get(FileProvenance, delivery.id):
                raise ContractError(
                    "Attached document contents/metadata are not personal-memory sources."
                )
            if delivery.object_id:
                obj = found(await repo.session.get(DataObject, delivery.object_id))
                if obj.deleted or obj.object_version != delivery.object_version:
                    conflict(
                        "memory_source_changed",
                        "Only the current record version can create candidates.",
                    )
            kind = "event" if delivery.operation == "observe" else "record"
            items = []
            for rule in policy["field_rules"]:
                if (
                    rule["collection_id"] != str(collection.id)
                    or rule["schema_version"] != delivery.schema_version
                    or rule["source_kind"] != kind
                ):
                    continue
                value = value_at(delivery.payload, rule["field"])
                if value is None:
                    continue
                validate_value(policy, rule["key"], value)
                period = None
                if rule["kind"] == "observation":
                    p = data.period
                    if (
                        p is None
                        or p.start.utcoffset() is None
                        or p.end.utcoffset() is None
                        or not p.start <= delivery.occurred_at < p.end
                    ):
                        raise ContractError(
                            "Observation requires an aware period covering the event; start <= time < end."
                        )
                    period = msgspec.to_builtins(p)
                schema = found(
                    await IntegrationRepository(repo.session, self.context).schema(
                        collection.id, kind, delivery.schema_version
                    )
                )
                unit = next(
                    (
                        m.get("unit")
                        for m in schema.field_mappings
                        if m["path"] == rule["field"] and m["role"] == "quantity"
                    ),
                    None,
                )
                row = await self._candidate(
                    repo,
                    collection,
                    rule["key"],
                    value,
                    digest(
                        [
                            "field",
                            str(delivery.object_id or delivery.id),
                            rule["field"],
                            rule["key"],
                        ]
                    ),
                    "field",
                    {
                        "delivery_id": str(delivery.id),
                        "object_version": delivery.object_version,
                        "schema_version": delivery.schema_version,
                        "field": rule["field"],
                        "source_kind": kind,
                        "sample_count": 1,
                        "unit": unit,
                        "applicability": "single declared user field; not an immutable personal trait",
                    },
                    kind=rule["kind"],
                    delivery_id=delivery.id,
                    period=period,
                )
                if row:
                    items.append(item_view(row))
            return {"items": items, "automatic_confirmation": False}

    async def forget_all(self):
        require_scope(self.context, "memory:write")
        self.unrestricted()
        async with self.transaction(write=True) as repo:
            row = await settings_row(repo.session, self.context)
            ids = list(
                await repo.session.scalars(
                    select(MemoryItem.id).where(*owner(MemoryItem, self.context))
                )
            )
            await invalidate_items(
                repo.session,
                self.context,
                ids,
                erased=True,
                reason="user_forgot_memory",
            )
            if row:
                for cid in row.config["collections"]:
                    collection = await self.collection(repo, UUID(cid))
                    for entry in row.config["definitions"]:
                        if not await self.blocked(repo, collection.id, entry["key"]):
                            await repo.add(
                                MemoryBlock(
                                    **self.ownership(collection), key=entry["key"]
                                )
                            )
                row.enabled = False
                await touch(repo.session, self.context)
            await repo.session.execute(
                update(MemoryMessage)
                .where(*owner(MemoryMessage, self.context))
                .values(
                    content=None,
                    fingerprint=None,
                    erased=True,
                    version=MemoryMessage.version + 1,
                )
            )
        return {
            "erased": True,
            "enabled": False,
            "feedback": "managed separately per result",
        }
