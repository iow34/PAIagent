"""Step 7: real PostgreSQL concurrency, history, imports, erasure and HTTP."""

import asyncio
import importlib
import json
import unittest
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import msgspec
import test_postgres_indexing as fixture
from alembic.migration import MigrationContext
from alembic.operations import Operations
from litestar import Litestar
from litestar.di import Provide
from litestar.exceptions import HTTPException
from sqlalchemy import func, select, text

from app.api.controllers.data import DataController
from app.api.controllers.evidence import EvidenceController
from app.api.controllers.integration import IntegrationController
from app.asgi import create_app
from app.db.models import (
    ConversationMessage,
    ConversationSession,
    DataDelivery,
    DataFence,
    DataImport,
    DataImportMember,
    DataImportPart,
    DataObject,
    DataOutbox,
    Document,
    DocumentChunk,
    DocumentKnowledgeRecord,
    ServiceAPIKey,
)
from app.integration.data_types import ImportInput, IngestEnvelope
from app.integration.types import NamedInput, SchemaInput
from app.integration.validation import ContractError
from app.repositories.conversations import ConversationRepository
from app.repositories.data_lifecycle import lock_owner
from app.repositories.documents import DocumentRepository
from app.security.api_keys import hash_api_key
from app.security.context import AccessContext
from app.security.permissions import SCOPES
from app.services.data_service import DataService

EXAMPLES = Path(__file__).resolve().parents[1] / "app/integration/examples"


@unittest.skipUnless(fixture.TEST_URL, "Use tests/run_postgres_tests.py")
class DataLifecycleTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.PostgreSQLIndexingTests.asyncSetUp
    drop_schema = fixture.PostgreSQLIndexingTests.drop_schema

    async def seed(self, example="tasks", context=None):
        context = context or AccessContext(
            self.context.tenant_id, "user", tuple(SCOPES)
        )
        service = DataService(self.factory, context)
        data = json.loads((EXAMPLES / (example + ".json")).read_text())
        source = await service.create_source(NamedInput("source-" + uuid4().hex))
        collection = await service.create_collection(
            source.id, msgspec.convert(data["collection"], type=NamedInput)
        )
        schema = msgspec.convert(data["schema"], type=SchemaInput)
        await service.create_schema(collection.id, schema)
        envelope = IngestEnvelope(
            "1",
            source.id,
            collection.id,
            1,
            data["operation"],
            datetime.now(UTC),
            data["payload"],
            external_id="item-1" if schema.kind == "record" else None,
            object_version=1 if schema.kind == "record" else None,
            event_id="event-1" if schema.kind == "event" else None,
        )
        return service, collection.id, envelope

    def change(self, envelope, version, **kwargs):
        values = {
            "operation": "upsert",
            "object_version": version,
            "event_id": f"e-{version}",
            "payload": {**envelope.payload, "phase": "done" if version % 2 else "todo"},
        }
        values.update(kwargs)
        return msgspec.structs.replace(envelope, **values)

    async def count(self, model):
        async with self.factory() as session:
            return await session.scalar(select(func.count()).select_from(model))

    async def test_three_independent_domains_and_schema_versions(self):
        for example in ("tasks", "books", "learning_sessions"):
            service, coll, env = await self.seed(example)
            channel = "events" if env.operation == "observe" else "records"
            ack = await service.ingest_one(coll, env, channel)
            self.assertEqual(ack.status, "accepted")
            duplicate = await service.ingest_one(coll, env, channel)
            self.assertEqual(duplicate.status, "duplicate")
            self.assertEqual(ack.received_at, duplicate.received_at)
            if channel == "records":
                self.assertEqual(
                    (await service.record(ack.object_id)).payload, env.payload
                )
            self.assertEqual(len((await service.events(coll)).items), 1)
        self.assertEqual(await self.count(DataOutbox), 3)

    async def test_duplicate_conflict_stale_and_late_history(self):
        service, coll, env = await self.seed()
        first = await service.ingest_one(coll, self.change(env, 1))
        third = await service.ingest_one(coll, self.change(env, 3))
        second = await service.ingest_one(coll, self.change(env, 2))
        self.assertEqual(second.status, "stale")
        self.assertEqual((await service.record(first.object_id)).object_version, 3)
        self.assertEqual(len((await service.events(coll)).items), 3)
        self.assertEqual(
            (await service.ingest_one(coll, self.change(env, 3))).status, "duplicate"
        )
        with self.assertRaises(HTTPException) as caught:
            await service.ingest_one(coll, self.change(env, 3, payload=env.payload))
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(await self.count(DataOutbox), 3)
        self.assertNotEqual(first.delivery_id, third.delivery_id)

    async def test_concurrent_duplicates_versions_and_conflicting_events(self):
        service, coll, env = await self.seed()
        acks = await asyncio.gather(
            *(service.ingest_one(coll, self.change(env, 1)) for _ in range(5))
        )
        self.assertEqual(
            sorted(x.status for x in acks), ["accepted"] + ["duplicate"] * 4
        )
        await asyncio.gather(
            *(service.ingest_one(coll, self.change(env, n)) for n in (5, 2, 4, 3))
        )
        self.assertEqual((await service.record(acks[0].object_id)).object_version, 5)
        results = await asyncio.gather(
            service.ingest_one(coll, self.change(env, 6, event_id="same")),
            service.ingest_one(coll, self.change(env, 7, event_id="same")),
            return_exceptions=True,
        )
        self.assertEqual(
            sum(isinstance(x, HTTPException) and x.status_code == 409 for x in results),
            1,
        )
        self.assertEqual(await self.count(DataDelivery), 6)

    async def test_sequence_gaps_late_arrivals_and_collisions(self):
        service, coll, env = await self.seed()
        await service.ingest_one(coll, self.change(env, 1, source_sequence=1))
        await service.ingest_one(coll, self.change(env, 3, source_sequence=3))
        self.assertEqual(
            (await service.coverage(coll))["missing_sequence_ranges"],
            [{"start": 2, "end": 2}],
        )
        await service.ingest_one(coll, self.change(env, 2, source_sequence=2))
        coverage = await service.coverage(coll)
        self.assertEqual(coverage["missing_sequence_ranges"], [])
        self.assertFalse(coverage["history_complete"])
        with self.assertRaises(HTTPException):
            await service.ingest_one(coll, self.change(env, 4, source_sequence=3))
        self.assertEqual((await service.records(coll)).items[0].object_version, 3)

    async def test_snapshot_and_change_same_version_deduplicate(self):
        service, coll, env = await self.seed()
        initial = await service.ingest_one(coll, env)
        change = msgspec.structs.replace(
            env, operation="upsert", event_id="e1", source_sequence=1
        )
        await service.ingest_one(coll, change)
        self.assertEqual((await service.ingest_one(coll, env)).status, "duplicate")
        self.assertEqual((await service.ingest_one(coll, change)).status, "duplicate")
        self.assertEqual(await self.count(DataDelivery), 1)
        self.assertEqual((await service.event(initial.delivery_id)).event_id, "e1")

    async def test_import_during_changes_reconciliation_and_missing_parts(self):
        service, coll, env = await self.seed()
        await service.ingest_one(coll, self.change(env, 3))
        part2 = ImportInput("snapshot-1", "cursor-123", 2, True, "complete", [env])
        accepted = await service.import_part(coll, part2)
        import_id = UUID(accepted["import_id"])
        self.assertEqual((await service.import_status(import_id))["missing_parts"], [1])
        absent = msgspec.structs.replace(env, external_id="not-in-snapshot")
        await service.ingest_one(coll, absent)
        await service.import_part(
            coll, msgspec.structs.replace(part2, part=1, final=False, items=[])
        )
        self.assertTrue((await service.import_status(import_id))["snapshot_complete"])
        self.assertFalse((await service.import_status(import_id))["history_complete"])
        rows = (await service.reconcile(import_id))["items"]
        self.assertEqual(
            {x["status"] for x in rows}, {"newer_than_snapshot", "absent_from_snapshot"}
        )
        self.assertEqual(len((await service.records(coll)).items), 2)
        self.assertTrue((await service.import_part(coll, part2))["duplicate"])
        with self.assertRaises(HTTPException):
            await service.import_part(
                coll, msgspec.structs.replace(part2, boundary="other")
            )
        with self.assertRaises(HTTPException):
            await service.import_part(
                coll, msgspec.structs.replace(part2, part=3, final=False)
            )

    async def test_import_part_rolls_back_on_bad_item(self):
        service, coll, env = await self.seed()
        invalid = msgspec.structs.replace(
            env, external_id="bad", payload={"phase": "todo"}
        )
        with self.assertRaises(ContractError):
            await service.import_part(
                coll, ImportInput("s", "b", 1, True, "complete", [env, invalid])
            )
        for model in (
            DataObject,
            DataDelivery,
            DataOutbox,
            DataImport,
            DataImportPart,
            DataImportMember,
        ):
            self.assertEqual(await self.count(model), 0)

    async def test_references_isolation_and_cascading_erasure(self):
        from app.integration.data_types import ObjectReference

        service, coll, env = await self.seed()
        first = await service.ingest_one(coll, env)
        referenced = msgspec.structs.replace(
            env, external_id="referring", references=[ObjectReference(coll, "item-1")]
        )
        second = await service.ingest_one(coll, referenced)
        foreign = DataService(
            self.factory, AccessContext(self.context.tenant_id, "other", tuple(SCOPES))
        )
        with self.assertRaises(HTTPException) as caught:
            await foreign.ingest_one(coll, referenced)
        self.assertEqual(caught.exception.status_code, 404)
        await service.erase_object(first.object_id)
        self.assertEqual((await service.records(coll)).items, [])
        self.assertEqual((await service.events(coll)).items, [])
        with self.assertRaises(HTTPException):
            await service.record(second.object_id)
        with self.assertRaises(HTTPException) as caught:
            await service.ingest_one(coll, self.change(env, 100))
        self.assertEqual(caught.exception.status_code, 410)
        async with self.factory() as session:
            rows = list(await session.scalars(select(DataDelivery)))
            self.assertTrue(
                all(
                    x.payload is None
                    and x.data_metadata == {}
                    and x.fingerprint is None
                    and x.references == []
                    for x in rows
                )
            )

    async def test_stale_delete_does_not_erase_newer_state(self):
        service, coll, env = await self.seed()
        ack = await service.ingest_one(coll, self.change(env, 3))
        stale = msgspec.structs.replace(
            env,
            object_version=2,
            operation="delete",
            payload=None,
            event_id="old-delete",
        )
        self.assertEqual((await service.ingest_one(coll, stale)).status, "stale")
        self.assertEqual((await service.record(ack.object_id)).object_version, 3)
        await service.ingest_one(
            coll,
            msgspec.structs.replace(stale, object_version=4, event_id="new-delete"),
        )
        self.assertEqual((await service.events(coll)).items, [])
        with self.assertRaises(HTTPException):
            await service.ingest_one(coll, stale)

    async def test_collection_source_and_owner_deletion_boundaries(self):
        service, coll, env = await self.seed()
        other, other_coll, other_env = await self.seed()
        await service.ingest_one(coll, env)
        await other.ingest_one(other_coll, other_env)
        await service.erase_scope(collection_id=coll)
        with self.assertRaises(HTTPException) as caught:
            await service.ingest_one(
                coll, msgspec.structs.replace(env, external_id="new")
            )
        self.assertEqual(caught.exception.status_code, 410)
        self.assertEqual(len((await other.records(other_coll)).items), 1)
        await other.erase_scope(source_id=other_env.source_id)
        self.assertEqual((await other.records(other_coll)).items, [])
        await service.erase_scope()
        self.assertEqual(await self.count(DataOutbox), 0)
        self.assertEqual(await self.count(DataFence), 3)
        with self.assertRaises(HTTPException):
            async with self.factory.begin() as session:
                await ConversationRepository(session).create_session(
                    context=service.context
                )
        with self.assertRaises(HTTPException):
            async with self.factory.begin() as session:
                await DocumentRepository(session).create(
                    context=service.context,
                    title="must not persist",
                    content_type="txt",
                    attempt_id=uuid4(),
                )

    async def test_delete_races_ingestion_and_legacy_creation(self):
        service, coll, env = await self.seed()
        async with self.factory() as session:
            async with session.begin():
                await lock_owner(session, service.context)
                task = asyncio.create_task(service.ingest_one(coll, env))
                await asyncio.sleep(0.03)
                self.assertFalse(task.done())
                session.add(
                    DataFence(
                        tenant_id=service.context.tenant_id,
                        user_id="user",
                        scope="user",
                    )
                )
            with self.assertRaises(HTTPException) as caught:
                await task
            self.assertEqual(caught.exception.status_code, 410)
        self.assertEqual(await self.count(DataObject), 0)

    async def test_user_erasure_cascades_legacy_private_content(self):
        service, _coll, _env = await self.seed()
        async with self.factory.begin() as session:
            doc = await DocumentRepository(session).create(
                context=service.context,
                title="private title",
                content_type="txt",
                attempt_id=uuid4(),
            )
            await DocumentRepository(session).add_chunks(
                document_id=doc.id, texts=["private content"]
            )
            conv = await ConversationRepository(session).create_session(
                context=service.context
            )
            session.add(
                ConversationMessage(
                    session_id=conv.id,
                    request_id=uuid4(),
                    position=0,
                    role="user",
                    content="secret",
                    sources=[],
                )
            )
        await service.erase_scope()
        for model in (
            Document,
            DocumentChunk,
            ConversationSession,
            ConversationMessage,
            DocumentKnowledgeRecord,
        ):
            self.assertEqual(await self.count(model), 0)
        with self.assertRaises(LookupError):
            async with self.factory.begin() as session:
                await DocumentRepository(session).save_embeddings(
                    context=service.context,
                    document_id=doc.id,
                    vectors=[[0.0] * 1536],
                    model="text-embedding-3-small",
                    dimensions=1536,
                    total_tokens=1,
                    attempt_id=doc.indexing_attempt_id,
                )

    @asynccontextmanager
    async def http(self, service):
        async with self.factory.begin() as session:
            for key, scopes, sources in [
                ("admin", sorted(SCOPES), None),
                ("write", ["data:write"], None),
                ("restricted", sorted(SCOPES), []),
                ("legacy", ["legacy:*"], None),
            ]:
                session.add(
                    ServiceAPIKey(
                        tenant_id=service.context.tenant_id,
                        name=key,
                        key_hash=hash_api_key(key),
                        scopes=scopes,
                        source_ids=sources,
                    )
                )

        async def factory():
            return self.factory

        app = Litestar(
            route_handlers=[IntegrationController, DataController, EvidenceController],
            dependencies={
                "db_session_factory": Provide(factory),
                "embedding_service": Provide(
                    lambda: fixture.FakeEmbeddings(), sync_to_thread=False
                ),
            },
            openapi_config=create_app().openapi_config,
        )
        async with (
            app.lifespan(),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
                headers={"Authorization": "Bearer admin", "X-User-ID": "user"},
            ) as client,
        ):
            yield client

    async def test_http_batches_auth_filters_limits_and_openapi(self):
        service, coll, env = await self.seed()
        async with self.http(service) as client:
            path = f"/v1/collections/{coll}"
            payload = msgspec.to_builtins(env)
            response = await client.post(
                path + "/records",
                json={
                    "items": [payload, {**payload, "external_id": "bad", "payload": {}}]
                },
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(
                [x["status"] for x in response.json()["items"]],
                ["accepted", "rejected"],
            )
            object_id = response.json()["items"][0]["object_id"]
            self.assertEqual(
                (
                    await client.get(
                        "/v1/records/" + object_id, headers={"X-User-ID": "other"}
                    )
                ).status_code,
                404,
            )
            self.assertEqual(
                (
                    await client.get(
                        path + "/records",
                        headers={"Authorization": "Bearer restricted"},
                    )
                ).status_code,
                403,
            )
            self.assertEqual(
                (
                    await client.post(
                        path + "/records/single",
                        json=payload,
                        headers={"Authorization": "Bearer legacy"},
                    )
                ).status_code,
                403,
            )
            change = msgspec.to_builtins(self.change(env, 2))
            self.assertEqual(
                (
                    await client.put(
                        "/v1/records/" + object_id,
                        json=change,
                        headers={"Authorization": "Bearer write"},
                    )
                ).status_code,
                200,
            )
            delete_body = {
                **change,
                "operation": "delete",
                "payload": None,
                "object_version": 3,
                "event_id": "delete",
            }
            self.assertEqual(
                (
                    await client.post(
                        path + "/records/single",
                        json=delete_body,
                        headers={"Authorization": "Bearer write"},
                    )
                ).status_code,
                403,
            )
            self.assertEqual(
                (await client.get(path + "/records?limit=0")).status_code, 400
            )
            self.assertEqual(
                (await client.get(path + "/records?wat=1")).status_code, 400
            )
            self.assertEqual(
                (
                    await client.get(path + "/events?occurred_from=2026-11-12T10:00:00")
                ).status_code,
                422,
            )
            self.assertEqual(
                (
                    await client.post(
                        path + "/records", json={"items": [payload] * 101}
                    )
                ).status_code,
                400,
            )
            self.assertEqual(
                (
                    await client.post(
                        path + "/records/single",
                        json={**payload, "tenant_id": str(uuid4())},
                    )
                ).status_code,
                400,
            )
            self.assertEqual(
                (
                    await client.post(
                        path + "/records",
                        content="x" * 1048577,
                        headers={"Content-Type": "application/json"},
                    )
                ).status_code,
                413,
            )
            spec = (await client.get("/schema/openapi.json")).json()
            self.assertIn("/v1/users/me/data", spec["paths"])
            self.assertEqual(
                (
                    await client.delete(
                        "/v1/users/me/data",
                        headers={"Authorization": "Bearer restricted"},
                    )
                ).status_code,
                403,
            )
            self.assertEqual(
                (await client.delete("/v1/records/" + object_id)).status_code, 200
            )
            self.assertEqual(
                (await client.get("/v1/records/" + object_id)).status_code, 404
            )

    async def test_migration_roundtrip_preserves_registry_and_matches_models(self):
        service, coll, env = await self.seed()
        module = importlib.import_module(
            "app.db.migrations.versions.a7d9e3f1b820_add_data_lifecycle"
        )
        async with self.engine.begin() as conn:
            await conn.execute(
                text(f'SET LOCAL search_path TO "{self.schema}", public')
            )

            def roundtrip(connection):
                from sqlalchemy import inspect

                from app.db.models import Base

                with Operations.context(MigrationContext.configure(connection)):
                    normalization = importlib.import_module(
                        "app.db.migrations.versions.b8e4f2a6c930_add_normalized_evidence"
                    )
                    importlib.import_module(
                        "app.db.migrations.versions.e1b7c5d9f263_managed_memory"
                    ).downgrade()
                    importlib.import_module(
                        "app.db.migrations.versions.d0a6b4c8e152_background_execution"
                    ).downgrade()
                    importlib.import_module(
                        "app.db.migrations.versions.c9f5a3b7d041_analysis_profiles"
                    ).downgrade()
                    normalization.downgrade()
                    module.downgrade()
                    module.upgrade()
                    normalization.upgrade()
                    importlib.import_module(
                        "app.db.migrations.versions.c9f5a3b7d041_analysis_profiles"
                    ).upgrade()
                    importlib.import_module(
                        "app.db.migrations.versions.d0a6b4c8e152_background_execution"
                    ).upgrade()
                    importlib.import_module(
                        "app.db.migrations.versions.e1b7c5d9f263_managed_memory"
                    ).upgrade()
                inspector = inspect(connection)
                for table in Base.metadata.tables.values():
                    if not table.name.startswith("data_"):
                        continue
                    reflected = inspector.get_columns(table.name, schema=self.schema)
                    actual = {
                        col["name"]: (
                            col["nullable"],
                            str(col["type"].compile(dialect=connection.dialect)),
                        )
                        for col in reflected
                    }
                    expected = {
                        col.name: (
                            col.nullable,
                            str(col.type.compile(dialect=connection.dialect)),
                        )
                        for col in table.columns
                    }
                    self.assertEqual(actual, expected, table.name)
                    actual_unique = {
                        tuple(row["column_names"])
                        for row in inspector.get_unique_constraints(
                            table.name, schema=self.schema
                        )
                    }
                    from sqlalchemy import UniqueConstraint

                    expected_unique = {
                        tuple(col.name for col in c.columns)
                        for c in table.constraints
                        if isinstance(c, UniqueConstraint)
                    }
                    self.assertEqual(actual_unique, expected_unique, table.name)

            await conn.run_sync(roundtrip)
        self.assertEqual((await service.ingest_one(coll, env)).status, "accepted")
        self.assertEqual(await self.count(DataOutbox), 1)

    async def test_observation_corrections_are_retryable_and_erased_together(self):
        service, coll, env = await self.seed("learning_sessions")
        first = await service.ingest_one(coll, env, "events")
        correction = msgspec.structs.replace(
            env, event_id="correction-1", payload={**env.payload, "elapsed": 1200}
        )
        second = await service.correct_event(first.delivery_id, correction)
        self.assertEqual(
            (await service.correct_event(first.delivery_id, correction)).status,
            "duplicate",
        )
        self.assertEqual(
            (await service.event(first.delivery_id)).corrected_by_id, second.delivery_id
        )
        with self.assertRaises(HTTPException):
            await service.correct_event(
                first.delivery_id,
                msgspec.structs.replace(correction, event_id="branch"),
            )
        third = await service.correct_event(
            second.delivery_id,
            msgspec.structs.replace(correction, event_id="correction-2"),
        )
        await service.erase_event(second.delivery_id)
        self.assertEqual((await service.events(coll)).items, [])
        for delivery_id in (first.delivery_id, second.delivery_id, third.delivery_id):
            with self.assertRaises(HTTPException):
                await service.event(delivery_id)
        with self.assertRaises(HTTPException) as caught:
            await service.ingest_one(coll, env, "events")
        self.assertEqual(caught.exception.status_code, 410)

    async def test_schema_upgrade_preserves_original_payload_and_version(self):
        service, coll, env = await self.seed()
        first = await service.ingest_one(coll, env)
        schema = SchemaInput(
            "record",
            2,
            {
                "type": "object",
                "properties": {"label": {"type": "string", "maxLength": 100}},
                "additionalProperties": False,
                "required": ["label"],
            },
        )
        await service.create_schema(coll, schema)
        updated = msgspec.structs.replace(
            env,
            schema_version=2,
            object_version=2,
            event_id="v2",
            operation="upsert",
            payload={"label": "New representation"},
        )
        await service.replace_record(first.object_id, updated)
        history = (await service.events(coll)).items
        self.assertEqual({x.schema_version for x in history}, {1, 2})
        self.assertEqual((await service.event(first.delivery_id)).payload, env.payload)
        self.assertEqual(
            (await service.record(first.object_id)).payload, updated.payload
        )
        with self.assertRaises(ContractError):
            await service.ingest_one(
                coll,
                msgspec.structs.replace(
                    updated, object_version=3, event_id="v3", payload=env.payload
                ),
            )

    async def test_cross_tenant_identity_isolation_and_database_foreign_keys(self):
        from sqlalchemy.exc import IntegrityError

        from app.db.models import Tenant

        service, coll, env = await self.seed()
        ack = await service.ingest_one(coll, env)
        tenant = uuid4()
        async with self.factory.begin() as session:
            session.add(Tenant(id=tenant, name="Other tenant"))
        other = DataService(self.factory, AccessContext(tenant, "user", tuple(SCOPES)))
        for operation in (
            lambda: other.record(ack.object_id),
            lambda: other.event(ack.delivery_id),
            lambda: other.erase_object(ack.object_id),
            lambda: other.ingest_one(coll, env),
        ):
            with self.assertRaises(HTTPException) as caught:
                await operation()
            self.assertEqual(caught.exception.status_code, 404)
        with self.assertRaises(IntegrityError):
            async with self.factory.begin() as session:
                session.add(
                    DataObject(
                        tenant_id=tenant,
                        user_id="user",
                        source_id=env.source_id,
                        collection_id=coll,
                        external_id="forged",
                        object_version=1,
                        schema_version=1,
                        payload={},
                        occurred_at=datetime.now(UTC),
                    )
                )

    async def test_outbox_failure_rolls_back_before_acknowledgement(self):
        from unittest.mock import patch

        from sqlalchemy.exc import SQLAlchemyError

        from app.repositories.data_lifecycle import DataRepository

        service, coll, env = await self.seed()
        original = DataRepository.add

        async def fail_outbox(repo, row):
            if isinstance(row, DataOutbox):
                raise SQLAlchemyError("Simulated outbox outage")
            return await original(repo, row)

        with (
            patch.object(DataRepository, "add", fail_outbox),
            self.assertRaises(HTTPException) as caught,
        ):
            await service.ingest_one(coll, env)
        self.assertEqual(caught.exception.status_code, 503)
        self.assertEqual(await self.count(DataObject), 0)
        self.assertEqual(await self.count(DataDelivery), 0)
        self.assertEqual((await service.ingest_one(coll, env)).status, "accepted")

    async def test_inflight_query_cannot_save_after_owner_erasure(self):
        from app.answer_types import AnswerResult
        from app.services.query_service import QueryService

        service, _coll, _env = await self.seed()
        started, resume = asyncio.Event(), asyncio.Event()

        class Workflow:
            async def run(self, **kwargs):
                started.set()
                await resume.wait()
                return AnswerResult(
                    "Old private answer", (), "insufficient_information"
                )

        query = QueryService(session_factory=self.factory, workflow=Workflow())
        task = asyncio.create_task(
            query.query(context=service.context, question="Private question")
        )
        try:
            await asyncio.wait_for(started.wait(), 3)
            await service.erase_scope()
            resume.set()
            with self.assertRaises(HTTPException) as caught:
                await asyncio.wait_for(task, 3)
            self.assertEqual(caught.exception.status_code, 410)
            self.assertEqual(await self.count(ConversationSession), 0)
            self.assertEqual(await self.count(ConversationMessage), 0)
        finally:
            resume.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_source_outbox_lost_ack_retry_over_real_tcp(self):
        import socket
        import sqlite3

        import uvicorn

        from app.integration.delivery_example import (
            commit_change,
            deliver_one,
            initialize,
        )

        service, coll, env = await self.seed()
        async with self.http(service) as client:
            app = client._transport.app
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
            server = uvicorn.Server(
                uvicorn.Config(app, lifespan="off", log_level="error", access_log=False)
            )
            task = asyncio.create_task(server.serve(sockets=[sock]))
            try:
                async with asyncio.timeout(5):
                    while not server.started:
                        if task.done():
                            await task
                        await asyncio.sleep(0.01)
                body = msgspec.to_builtins(self.change(env, 1))

                def send():
                    with (
                        sqlite3.connect(":memory:") as connection,
                        httpx.Client(
                            base_url=f"http://127.0.0.1:{port}",
                            headers={
                                "Authorization": "Bearer admin",
                                "X-User-ID": "user",
                            },
                            timeout=5,
                        ) as sender,
                    ):
                        initialize(connection)
                        commit_change(connection, body)
                        # Simulate a process dying after the remote commit but
                        # before removing the source-local outbox row.
                        persisted = connection.execute(
                            "SELECT body FROM source_outbox"
                        ).fetchone()[0]
                        first = sender.post(
                            f"/v1/collections/{coll}/records/single",
                            content=persisted.encode(),
                            headers={"Content-Type": "application/json"},
                        )
                        self.assertEqual(first.json()["status"], "accepted")
                        self.assertEqual(deliver_one(connection, sender), "delivered")
                        self.assertEqual(deliver_one(connection, sender), "empty")

                await asyncio.to_thread(send)
                self.assertEqual(await self.count(DataDelivery), 1)
                self.assertEqual(await self.count(DataOutbox), 1)
            finally:
                server.should_exit = True
                await asyncio.wait_for(task, 5)
                sock.close()

    async def test_full_migration_chain_preserves_existing_document(self):
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        from app.db.models import Base

        document_id = uuid4()
        chunk_id = uuid4()
        async with self.engine.begin() as conn:
            await conn.execute(
                text(f'SET LOCAL search_path TO "{self.schema}", public')
            )
            await conn.run_sync(Base.metadata.drop_all)

            def migrate(connection):
                revisions = list(
                    reversed(
                        list(
                            ScriptDirectory.from_config(
                                Config("alembic.ini")
                            ).walk_revisions()
                        )
                    )
                )
                with Operations.context(MigrationContext.configure(connection)):
                    for index, revision in enumerate(revisions):
                        revision.module.upgrade()
                        if index == 0:
                            connection.execute(
                                text(
                                    "INSERT INTO documents(id,user_id,title,content_type,status) VALUES (:id,'dev-user','preserved private title','txt','pending')"
                                ),
                                {"id": document_id},
                            )
                            connection.execute(
                                text(
                                    "INSERT INTO document_chunks(id,document_id,chunk_index,content) VALUES (:id,:doc,0,'preserved private text')"
                                ),
                                {"id": chunk_id, "doc": document_id},
                            )

            await conn.run_sync(migrate)
        async with self.factory() as session:
            doc = await session.get(Document, document_id)
            chunk = await session.get(DocumentChunk, chunk_id)
            self.assertEqual(doc.title, "preserved private title")
            self.assertEqual(chunk.content, "preserved private text")
            tenant_id = doc.tenant_id
        self.context = AccessContext(tenant_id, "dev-user")
        service, coll, env = await self.seed(
            context=AccessContext(tenant_id, "dev-user", tuple(SCOPES))
        )
        self.assertEqual((await service.ingest_one(coll, env)).status, "accepted")
        self.assertEqual(await self.count(DataOutbox), 1)

    async def test_http_import_correction_pagination_and_filters(self):
        service, coll, env = await self.seed("learning_sessions")
        async with self.http(service) as client:
            path = f"/v1/collections/{coll}"
            first = await client.post(
                path + "/events/single", json=msgspec.to_builtins(env)
            )
            self.assertEqual(first.status_code, 200, first.text)
            delivery_id = first.json()["delivery_id"]
            correction = msgspec.to_builtins(
                msgspec.structs.replace(
                    env, event_id="fix", payload={**env.payload, "elapsed": 100}
                )
            )
            corrected = await client.post(
                f"/v1/events/{delivery_id}/corrections", json=correction
            )
            self.assertEqual(corrected.status_code, 200, corrected.text)
            first_page = (await client.get(path + "/events?limit=1")).json()
            second_page = (
                await client.get(
                    path + "/events",
                    params={"limit": 1, "cursor": first_page["next_cursor"]},
                )
            ).json()
            self.assertNotEqual(
                first_page["items"][0]["id"], second_page["items"][0]["id"]
            )
            self.assertIsNone(second_page["next_cursor"])
            future = (
                await client.get(
                    path + "/events", params={"occurred_from": "9999-01-01T00:00:00Z"}
                )
            ).json()
            self.assertEqual(future["items"], [])
            self.assertEqual(
                (await client.delete(f"/v1/events/{delivery_id}")).status_code, 200
            )
            self.assertEqual((await client.get(path + "/events")).json()["items"], [])
            _service, record_coll, record_env = await self.seed()
            payload = {
                "snapshot_id": "http-snapshot",
                "boundary": "source-cursor",
                "part": 1,
                "final": True,
                "completeness": "complete",
                "items": [msgspec.to_builtins(record_env)],
            }
            response = await client.post(
                f"/v1/collections/{record_coll}/imports", json=payload
            )
            self.assertEqual(response.status_code, 200, response.text)
            import_id = response.json()["import_id"]
            self.assertTrue(
                (await client.get(f"/v1/imports/{import_id}")).json()[
                    "snapshot_complete"
                ]
            )
            self.assertEqual(
                (await client.get(f"/v1/imports/{import_id}/reconciliation")).json()[
                    "items"
                ][0]["status"],
                "matched",
            )
            self.assertFalse(
                (await client.get(f"/v1/collections/{record_coll}/coverage")).json()[
                    "history_complete"
                ]
            )
            # Bundled implementation contracts must describe real routes.
            contract = (await client.get("/v1/integration-contract")).json()
            spec = (await client.get("/schema/openapi.json")).json()
            for route in contract["implemented_data_routes"]:
                self.assertIn(route["method"].lower(), spec["paths"][route["path"]])

    async def test_http_deadline_rolls_back_waiting_ingestion(self):
        from unittest.mock import patch

        service, coll, env = await self.seed()
        async with self.http(service) as client, self.factory.begin() as holding:
            await lock_owner(holding, service.context)
            with patch("app.api.execution_timeout.REQUEST_TIMEOUT_SECONDS", 0.05):
                response = await client.post(
                    f"/v1/collections/{coll}/records/single",
                    json=msgspec.to_builtins(env),
                )
            self.assertEqual(response.status_code, 504, response.text)
            self.assertEqual(response.json()["extra"]["code"], "data_timeout")
        self.assertEqual(await self.count(DataDelivery), 0)
        self.assertEqual(await self.count(DataObject), 0)

    async def test_snapshot_promotion_rechecks_reference_source_grants(self):
        from app.integration.data_types import ObjectReference

        service, coll, env = await self.seed()
        other, other_coll, other_env = await self.seed()
        await other.ingest_one(other_coll, other_env)
        snapshot = msgspec.structs.replace(
            env, references=[ObjectReference(other_coll, other_env.external_id)]
        )
        accepted = await service.ingest_one(coll, snapshot)
        restricted = DataService(
            self.factory,
            AccessContext(
                service.context.tenant_id, "user", tuple(SCOPES), (env.source_id,)
            ),
        )
        change = msgspec.structs.replace(
            snapshot, operation="upsert", event_id="promote"
        )
        with self.assertRaises(HTTPException) as caught:
            await restricted.ingest_one(coll, change)
        self.assertEqual(caught.exception.status_code, 403)
        self.assertEqual(
            (await service.event(accepted.delivery_id)).operation, "snapshot"
        )
        self.assertEqual(await self.count(DataOutbox), 2)
        invalid = msgspec.structs.replace(
            env,
            external_id="invalid-ref",
            references=[ObjectReference(other_coll, "bad\x00id")],
        )
        with self.assertRaises(ContractError):
            await service.ingest_one(coll, invalid)

    async def test_erasure_clears_multiple_receipt_batches(self):
        service, coll, env = await self.seed("learning_sessions")
        async with self.factory.begin() as session:
            session.add_all(
                [
                    DataDelivery(
                        tenant_id=service.context.tenant_id,
                        user_id="user",
                        source_id=env.source_id,
                        collection_id=coll,
                        schema_version=1,
                        operation="observe",
                        event_id=f"batch-{i}",
                        payload=env.payload,
                        fingerprint="a" * 64,
                        state_fingerprint="b" * 64,
                        occurred_at=env.occurred_at,
                    )
                    for i in range(1101)
                ]
            )
        await service.erase_scope(collection_id=coll)
        async with self.factory() as session:
            self.assertEqual(
                await session.scalar(
                    select(func.count())
                    .select_from(DataDelivery)
                    .where(
                        DataDelivery.erased.is_(True), DataDelivery.payload.is_(None)
                    )
                ),
                1101,
            )
        self.assertEqual((await service.events(coll)).items, [])
