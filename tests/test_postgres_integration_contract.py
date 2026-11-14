"""Registry, real PostgreSQL and authenticated HTTP. No paid model calls."""

import asyncio
import importlib
import json
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import httpx
import test_postgres_indexing as fixture
from alembic.migration import MigrationContext
from alembic.operations import Operations
from litestar import Litestar
from litestar.di import Provide
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.api.controllers.integration import IntegrationController
from app.asgi import create_app
from app.cli.create_api_key import issue_api_key
from app.db.models import (
    IntegrationCollection,
    ServiceAPIKey,
    Tenant,
)
from app.integration.quickstart import run as quickstart
from app.security.api_keys import hash_api_key
from app.security.permissions import SCOPES

EXAMPLES = Path(__file__).resolve().parents[1] / "app/integration/examples"


@unittest.skipUnless(
    fixture.TEST_URL,
    "Run tests/run_postgres_tests.py --test-file test_postgres_integration_contract.py",
)
class PostgreSQLIntegrationContractTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.PostgreSQLIndexingTests.asyncSetUp
    drop_schema = fixture.PostgreSQLIndexingTests.drop_schema

    @asynccontextmanager
    async def application(self):
        self.other_tenant = uuid4()
        async with self.factory.begin() as session:
            session.add(Tenant(id=self.other_tenant, name="Other"))
            await session.flush()
            for key, tenant, scopes in [
                ("admin", self.context.tenant_id, sorted(SCOPES)),
                ("other", self.other_tenant, sorted(SCOPES)),
                ("legacy", self.context.tenant_id, ["legacy:*", "catalog:read"]),
                (
                    "reader",
                    self.context.tenant_id,
                    ["sources:read", "collections:read", "schemas:read"],
                ),
            ]:
                session.add(
                    ServiceAPIKey(
                        tenant_id=tenant,
                        name=key,
                        key_hash=hash_api_key(key),
                        scopes=scopes,
                    )
                )

        async def factory():
            return self.factory

        # Test the real application routes/authentication, but no production lifespan.
        app = create_app()
        app = Litestar(
            route_handlers=[IntegrationController],
            dependencies={"db_session_factory": Provide(factory)},
            openapi_config=app.openapi_config,
        )
        self.app = app
        async with (
            app.lifespan(),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
                headers={"Authorization": "Bearer admin", "X-User-ID": "alice"},
            ) as client,
        ):
            yield client

    async def seed(self, client, example="tasks"):
        item = json.loads((EXAMPLES / (example + ".json")).read_text())
        source = await client.post("/v1/sources", json={"name": "s-" + uuid4().hex})
        self.assertEqual(source.status_code, 201, source.text)
        source = source.json()
        coll = await client.post(
            f"/v1/sources/{source['id']}/collections", json=item["collection"]
        )
        self.assertEqual(coll.status_code, 201, coll.text)
        coll = coll.json()
        path = f"/v1/collections/{coll['id']}"
        schema = await client.post(path + "/schemas", json=item["schema"])
        self.assertEqual(schema.status_code, 201, schema.text)
        envelope = {
            "api_version": "1",
            "source_id": source["id"],
            "collection_id": coll["id"],
            "schema_version": 1,
            "operation": item["operation"],
            "occurred_at": "2026-11-12T10:00:00Z",
            "payload": item["payload"],
        }
        if item["operation"] == "observe":
            envelope["event_id"] = "event-1"
        else:
            envelope.update(external_id="item-1", object_version=1)
        return source, coll, path, item, envelope

    async def test_three_domains_validate_and_are_discoverable(self):
        async with self.application() as client:
            for example in ("tasks", "books", "learning_sessions"):
                _source, _coll, path, item, envelope = await self.seed(client, example)
                result = await client.post(path + "/validate", json=envelope)
                self.assertEqual(result.status_code, 200, result.text)
                self.assertEqual(
                    result.json(),
                    {
                        "valid": True,
                        "schema_kind": item["schema"]["kind"],
                        "schema_version": 1,
                        "persisted": False,
                    },
                )
                self.assertEqual(
                    (await client.get(path + "/schemas")).json()["items"][0][
                        "json_schema"
                    ],
                    item["schema"]["json_schema"],
                )
                self.assertEqual(
                    (
                        await client.get(path + f"/schemas/{item['schema']['kind']}/1")
                    ).status_code,
                    200,
                )
            catalog = (await client.get("/v1/capabilities")).json()
            self.assertTrue(catalog["registry"]["record_persistence"])
            self.assertEqual(
                (await client.get("/v1/integration-contract")).status_code, 200
            )
            spec = (await client.get("/schema/openapi.json")).json()
            self.assertIn("/v1/collections/{collection_id}/validate", spec["paths"])
            self.assertNotIn("/v1/runs", spec["paths"])
            self.assertIn(
                "X-User-ID", str(spec["paths"]["/v1/sources"]["post"]["parameters"])
            )

    async def test_access_grants_tenant_and_source_boundaries(self):
        async with self.application() as client:
            source, _coll, path, _item, envelope = await self.seed(client)
            for key, status in [("other", 404), ("legacy", 403), ("reader", 200)]:
                response = await client.get(
                    path + "/schemas", headers={"Authorization": "Bearer " + key}
                )
                self.assertEqual(response.status_code, status, response.text)
            response = await client.get(path + "/schemas", headers={"X-User-ID": "bob"})
            self.assertEqual(
                response.status_code, 200
            )  # intentionally tenant-shared registry only
            self.assertEqual(
                (
                    await client.post(
                        path + "/validate",
                        json=envelope,
                        headers={"Authorization": "Bearer reader"},
                    )
                ).status_code,
                403,
            )
            self.assertEqual(
                (
                    await client.get(
                        "/v1/sources", headers={"Authorization": "Bearer invalid"}
                    )
                ).status_code,
                401,
            )
            self.assertEqual(
                (
                    await client.post(
                        "/v1/sources",
                        json={"name": "x", "tenant_id": str(self.other_tenant)},
                    )
                ).status_code,
                400,
            )
            self.assertEqual(
                (
                    await client.post(
                        "/v1/sources",
                        json={"name": "nul", "description": "bad\x00value"},
                    )
                ).status_code,
                400,
            )
            from uuid import UUID

            _, key = await issue_api_key(
                session_factory=self.factory,
                tenant_id=self.context.tenant_id,
                name="limited",
                scopes=tuple(SCOPES - {"legacy:*"}),
                source_ids=(UUID(source["id"]),),
            )
            headers = {"Authorization": "Bearer " + key}
            self.assertEqual(
                (await client.get(path + "/schemas", headers=headers)).status_code, 200
            )
            self.assertEqual(
                (
                    await client.post(
                        "/v1/sources", json={"name": "forbidden"}, headers=headers
                    )
                ).status_code,
                403,
            )
            source2, coll2, path2, _, _ = await self.seed(client)
            self.assertEqual(
                (await client.get(path2 + "/schemas", headers=headers)).status_code, 403
            )
            listed = (await client.get("/v1/sources", headers=headers)).json()["items"]
            self.assertEqual([s["id"] for s in listed], [source["id"]])
            with self.assertRaises(ValueError):
                await issue_api_key(
                    session_factory=self.factory,
                    tenant_id=self.other_tenant,
                    name="bad",
                    scopes=("schemas:read",),
                    source_ids=(UUID(source["id"]),),
                )
            forged = {**envelope, "source_id": source2["id"]}
            self.assertEqual(
                (await client.post(path + "/validate", json=forged)).status_code, 422
            )
            self.assertEqual(
                (
                    await client.post(
                        path + "/validate",
                        json={**envelope, "collection_id": coll2["id"]},
                    )
                ).status_code,
                422,
            )

    async def test_versions_conflicts_concurrency_and_cursor(self):
        async with self.application() as client:
            _source, _coll, path, item, envelope = await self.seed(client)
            self.assertEqual(
                (await client.post(path + "/schemas", json=item["schema"])).status_code,
                409,
            )
            new = {**item["schema"], "version": 2}
            responses = await asyncio.gather(
                *[client.post(path + "/schemas", json=new) for _ in range(2)]
            )
            self.assertEqual(sorted(r.status_code for r in responses), [201, 409])
            first = (await client.get(path + "/schemas?limit=1&kind=record")).json()
            second = (
                await client.get(
                    path + "/schemas",
                    params={
                        "limit": 1,
                        "kind": "record",
                        "cursor": first["next_cursor"],
                    },
                )
            ).json()
            self.assertNotEqual(first["items"][0]["id"], second["items"][0]["id"])
            self.assertIsNone(second["next_cursor"])
            self.assertEqual(
                (await client.get(path + "/schemas/record/1")).json()["json_schema"],
                item["schema"]["json_schema"],
            )
            for query in [
                "limit=0",
                "limit=101",
                "kind=unknown",
                "ignored=true",
                "limit=1&limit=2",
            ]:
                self.assertEqual(
                    (await client.get(path + "/schemas?" + query)).status_code,
                    400,
                    query,
                )
            self.assertEqual(
                (await client.get(path + "/schemas/record/0")).status_code, 400
            )
            self.assertEqual(
                (await client.get(path + "/schemas/nope/1")).status_code, 400
            )
            self.assertEqual(
                (
                    await client.post(
                        path + "/validate", json={**envelope, "schema_version": 99}
                    )
                ).status_code,
                404,
            )

    async def test_invalid_payloads_do_not_persist_or_leak(self):
        async with self.application() as client:
            _, _, path, item, envelope = await self.seed(client)
            bad = {**envelope, "payload": {"caption": "ok", "phase": "PRIVATE-VALUE"}}
            response = await client.post(path + "/validate", json=bad)
            self.assertEqual(response.status_code, 422, response.text)
            self.assertNotIn("PRIVATE-VALUE", response.text)
            self.assertEqual(
                response.json()["extra"]["code"], "payload_validation_failed"
            )
            for extras in [
                {"received_at": "2026-11-12T10:00:00Z"},
                {"user_id": "other"},
            ]:
                self.assertEqual(
                    (
                        await client.post(
                            path + "/validate", json={**envelope, **extras}
                        )
                    ).status_code,
                    400,
                )
            schema = {
                **item["schema"],
                "version": 2,
                "json_schema": {
                    "type": "object",
                    "$ref": "https://example.invalid/schema",
                },
            }
            self.assertEqual(
                (await client.post(path + "/schemas", json=schema)).status_code, 422
            )
            self.assertEqual(
                (
                    await client.post(
                        path + "/validate",
                        content="x" * 98305,
                        headers={"Content-Type": "application/json"},
                    )
                ).status_code,
                413,
            )

    async def test_migration_preserves_existing_key_and_enforces_tenant_foreign_keys(
        self,
    ):
        module = importlib.import_module(
            "app.db.migrations.versions.f3a6c9d2e710_add_integration_contract"
        )
        async with self.engine.begin() as conn:
            await conn.execute(
                text(f'SET LOCAL search_path TO "{self.schema}", public')
            )

            lifecycle = importlib.import_module(
                "app.db.migrations.versions.a7d9e3f1b820_add_data_lifecycle"
            )

            def downgrade(c):
                with Operations.context(MigrationContext.configure(c)):
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
                    lifecycle.downgrade()
                    module.downgrade()

            await conn.run_sync(downgrade)
            await conn.execute(
                text(
                    "INSERT INTO service_api_keys(id,tenant_id,name,key_hash) VALUES (:id,:tenant,'existing','old-hash')"
                ),
                {"id": uuid4(), "tenant": self.context.tenant_id},
            )

            def upgrade(c):
                with Operations.context(MigrationContext.configure(c)):
                    module.upgrade()
                    lifecycle.upgrade()
                    importlib.import_module(
                        "app.db.migrations.versions.b8e4f2a6c930_add_normalized_evidence"
                    ).upgrade()
                    importlib.import_module(
                        "app.db.migrations.versions.c9f5a3b7d041_analysis_profiles"
                    ).upgrade()
                    importlib.import_module(
                        "app.db.migrations.versions.d0a6b4c8e152_background_execution"
                    ).upgrade()
                    importlib.import_module(
                        "app.db.migrations.versions.e1b7c5d9f263_managed_memory"
                    ).upgrade()

            await conn.run_sync(upgrade)
            scopes = (
                await conn.execute(
                    text("SELECT scopes,source_ids FROM service_api_keys")
                )
            ).one()
            self.assertEqual(scopes[0], ["legacy:*", "catalog:read"])
            self.assertIsNone(scopes[1])
        async with self.application() as client:
            source, _coll, _, _, _ = await self.seed(client)
            with self.assertRaises(IntegrityError):
                async with self.factory.begin() as session:
                    session.add(
                        IntegrationCollection(
                            tenant_id=self.other_tenant,
                            source_id=source["id"],
                            name="cross-tenant",
                        )
                    )

    async def test_rate_limit_is_advertised_and_enforced(self):
        async with self.application() as client:
            for _ in range(120):
                response = await client.get("/v1/capabilities")
                self.assertEqual(response.status_code, 200, response.text)
            response = await client.get(
                "/v1/capabilities", headers={"X-Forwarded-For": "1.2.3.4"}
            )
            self.assertEqual(response.status_code, 429, response.text)
            self.assertIn("ratelimit-reset", response.headers)

    async def test_independent_quickstart_over_tcp(self):
        import socket

        import uvicorn

        async with self.application():
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
            server = uvicorn.Server(
                uvicorn.Config(
                    self.app, lifespan="off", log_level="error", access_log=False
                )
            )
            task = asyncio.create_task(server.serve(sockets=[sock]))
            try:
                async with asyncio.timeout(5):
                    while not server.started:
                        if task.done():
                            await task
                        await asyncio.sleep(0.01)

                def execute():
                    with httpx.Client(
                        base_url=f"http://127.0.0.1:{port}",
                        headers={
                            "Authorization": "Bearer admin",
                            "X-User-ID": "client-user",
                        },
                    ) as client:
                        return quickstart(client)

                result = await asyncio.to_thread(execute)
                self.assertEqual(len(result), 3)
                self.assertTrue(
                    all(row["valid"] and not row["persisted"] for row in result)
                )
            finally:
                server.should_exit = True
                await asyncio.wait_for(task, 5)
                sock.close()

    async def test_restricted_keys_cannot_bypass_through_legacy_routes(self):
        from app.api.controllers.search import SearchController

        async with self.application() as registry_client:
            source, _, _, _, _ = await self.seed(registry_client)
            async with self.factory.begin() as session:
                session.add(
                    ServiceAPIKey(
                        tenant_id=self.context.tenant_id,
                        name="restricted-legacy",
                        key_hash=hash_api_key("restricted"),
                        scopes=["legacy:*"],
                        source_ids=[source["id"]],
                    )
                )

            async def factory():
                return self.factory

            # Dependency order is framework-controlled; supply a sentinel whose
            # methods cannot execute rather than asserting DI evaluation order.
            class Embeddings:
                async def embed_texts(self, texts):
                    raise AssertionError("Unauthorized provider invocation")

            async def embeddings():
                return Embeddings()

            app = Litestar(
                route_handlers=[SearchController],
                dependencies={
                    "db_session_factory": Provide(factory),
                    "embedding_service": Provide(embeddings),
                },
            )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
                headers={"X-User-ID": "alice"},
            ) as client:
                for key in ("restricted", "reader"):
                    response = await client.post(
                        "/v1/search",
                        json={"query": "private"},
                        headers={"Authorization": "Bearer " + key},
                    )
                    self.assertEqual(response.status_code, 403, response.text)
