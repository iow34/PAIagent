"""Step 9 acceptance: HTTP, profiles, metrics, provenance and lifecycle races."""

import asyncio
import importlib
import json
import unittest
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import UUID, uuid4

import httpx
import msgspec
import test_postgres_data_lifecycle as lifecycle
import test_postgres_indexing as fixture
from alembic.migration import MigrationContext
from alembic.operations import Operations
from litestar import Litestar
from litestar.di import Provide
from litestar.exceptions import HTTPException
from sqlalchemy import func, inspect, select, text

from app.api.controllers.capabilities import CapabilityController
from app.api.controllers.data import DataController
from app.api.controllers.integration import IntegrationController
from app.asgi import create_app
from app.capabilities.generation import GenerationError, OpenAIGenerator
from app.capabilities.types import (
    Budget,
    Filter,
    InlineInput,
    Metric,
    Period,
    ProfileInput,
    RunInput,
    Selection,
)
from app.db.models import AnalysisProfile, AnalysisResult, ServiceAPIKey, Tenant
from app.integration.validation import ContractError
from app.security.api_keys import hash_api_key
from app.security.context import AccessContext
from app.security.permissions import SCOPES
from app.services.capability_service import CapabilityService

OUTPUT = {
    "type": "object",
    "properties": {
        "category": {"type": "string", "maxLength": 30, "enum": ["work", "reading"]}
    },
    "required": ["category"],
    "additionalProperties": False,
}


class FakeGenerator(OpenAIGenerator):
    def __init__(self, callback=None, broken=None):
        self.calls = []
        self.callback, self.broken = callback, broken

    async def generate(self, *, request, schema, max_output_tokens):
        self.calls.append(request)
        if self.callback:
            await self.callback()
        if self.broken == "failure":
            raise GenerationError("provider failed")
        source = request["sources"][0]
        ids = source.get("support_ids", [source["id"]])
        result = {
            "insufficient_data": False,
            "claims": [{"text": "Підтверджене спостереження", "source_ids": ids}],
            "limitations": [],
        }
        if "data" in schema["properties"]:
            result.update(data={"category": "work"}, source_ids=ids)
        if self.broken == "refs":
            result["claims"][0]["source_ids"] = ["foreign-id"]
        if self.broken == "schema":
            result["unexpected"] = True
        if self.broken == "classification":
            result["data"]["category"] = "invented"
        return result


@unittest.skipUnless(fixture.TEST_URL, "Use temporary PostgreSQL runner")
class CapabilityTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.PostgreSQLIndexingTests.asyncSetUp
    drop_schema = fixture.PostgreSQLIndexingTests.drop_schema
    seed = lifecycle.DataLifecycleTests.seed

    async def prepare(self, capability="analyze", **kwargs):
        base, coll, env = await self.seed()
        await base.ingest_one(coll, env)
        service = CapabilityService(self.factory, base.context)
        config = ProfileInput(
            "profile",
            capability,
            "Provide grounded insights",
            selections=[Selection(coll, 1, ["/caption", "/phase", "/estimate"])],
            **kwargs,
        )
        profile = await service.create_profile(config)
        return service, coll, env, config, UUID(profile["profile_id"])

    async def execute(self, service, pid, generator=None, run=None):
        return await service.run(
            pid,
            run or RunInput(1),
            generator or FakeGenerator(),
            fixture.FakeEmbeddings(),
        )

    async def count_results(self):
        async with self.factory() as session:
            return await session.scalar(
                select(func.count()).select_from(AnalysisResult)
            )

    async def test_six_capabilities_three_domains_no_domain_code(self):
        for domain, fields in [
            ("tasks", ["/caption", "/phase"]),
            ("books", ["/heading", "/pages"]),
            ("learning_sessions", ["/subject", "/elapsed"]),
        ]:
            base, coll, env = await self.seed(domain)
            await base.ingest_one(
                coll, env, "events" if env.operation == "observe" else "records"
            )
            service = CapabilityService(self.factory, base.context)
            for capability in (
                "search",
                "query",
                "summarize",
                "extract",
                "analyze",
                "recommend",
            ):
                selection = Selection(
                    coll,
                    1,
                    fields,
                    kind="event" if env.operation == "observe" else "record",
                    mode="history" if env.operation == "observe" else "current",
                )
                config = ProfileInput(
                    domain,
                    capability,
                    "Describe supplied evidence",
                    selections=[selection],
                    output_schema=OUTPUT if capability == "extract" else None,
                )
                profile = await service.create_profile(config)
                keyword = str(env.payload[fields[0][1:]]).split()[0]
                result = await self.execute(
                    service, UUID(profile["profile_id"]), run=RunInput(1, keyword)
                )
                self.assertEqual(result["execution_status"], "succeeded")
                self.assertNotEqual(
                    result["data_state"],
                    "insufficient_data",
                    (domain, capability, result),
                )
                self.assertTrue(result["sources"])
                self.assertTrue(result["persisted"])
                self.assertEqual(
                    (await service.result(UUID(result["result_id"])))["freshness"],
                    "current",
                )
                if capability == "extract":
                    self.assertEqual(result["payload"]["data"]["category"], "work")
        self.assertEqual(await self.count_results(), 18)

    async def test_metrics_null_filters_units_time_and_comparison(self):
        primary = Period(
            datetime(2026, 11, 1, tzinfo=UTC), datetime(2026, 11, 8, tzinfo=UTC)
        )
        previous = Period(
            datetime(2026, 10, 25, tzinfo=UTC), datetime(2026, 11, 1, tzinfo=UTC)
        )
        metrics = [
            Metric("n", 0, "count"),
            Metric("sum", 0, "sum", "/estimate"),
            Metric("avg", 0, "average", "/estimate"),
            Metric("min", 0, "min", "/estimate"),
            Metric("max", 0, "max", "/estimate"),
            Metric("groups", 0, "sum", "/estimate", "/phase"),
            Metric("daily", 0, "count", interval="day"),
        ]
        service, coll, env, config, pid = await self.prepare(
            period=primary,
            comparison_period=previous,
            timezone="Europe/Warsaw",
            metrics=metrics,
        )
        # Initial row lies outside these fixed periods.
        for i, (when, estimate, phase) in enumerate(
            [
                ("2026-11-01T00:00:00+00:00", 0.1, "todo"),
                ("2026-11-01T23:30:00+00:00", 0.2, "todo"),
                ("2026-11-03T00:00:00+00:00", None, "done"),
                ("2026-10-30T00:00:00+00:00", 2, "done"),
                ("2026-11-08T00:00:00+00:00", 999, "done"),
            ]
        ):
            await service.ingest_one(
                coll,
                msgspec.structs.replace(
                    env,
                    external_id=f"x{i}",
                    occurred_at=datetime.fromisoformat(when),
                    payload={**env.payload, "estimate": estimate, "phase": phase},
                ),
            )
        result = await self.execute(service, pid)
        data = result["payload"]["metrics"]
        get = lambda name, period="primary": next(
            m for m in data if m["name"] == name and m.get("period") == period
        )
        self.assertEqual(get("n")["value"], "3")
        self.assertEqual(get("sum")["value"], "0.3")
        self.assertEqual(get("avg")["value"], "0.15")
        self.assertEqual(get("avg")["denominator"], 2)
        self.assertEqual(get("avg")["null_count"], 1)
        self.assertEqual(get("min")["value"], "0.1")
        self.assertEqual(get("max")["value"], "0.2")
        self.assertEqual(get("sum")["unit"], "hour")
        difference = next(
            m for m in data if m["name"] == "sum" and m["operation"] == "difference"
        )
        self.assertEqual(difference["value"], "-1.7")
        self.assertIn("2026-11-02", [m["bucket"] for m in data if m["name"] == "daily"])
        selected = msgspec.structs.replace(
            config.selections[0],
            filters=[Filter("/estimate", "not_null"), Filter("/phase", "eq", "todo")],
        )
        await service.create_profile(
            msgspec.structs.replace(config, selections=[selected]), pid, 2
        )
        filtered = await self.execute(service, pid, run=RunInput(2))
        self.assertEqual(
            filtered["data_state"], "insufficient_data"
        )  # comparison has no todo rows
        self.assertEqual(
            next(
                m
                for m in filtered["payload"]["metrics"]
                if m["name"] == "n" and m.get("period") == "primary"
            )["value"],
            "2",
        )

    async def test_profiles_immutable_private_and_sources_never_grant_access(self):
        service, _coll, _env, config, pid = await self.prepare()
        await service.create_profile(
            msgspec.structs.replace(config, goal="Different goal"), pid, 2
        )
        self.assertEqual(
            (await service.get_profile(pid, 1))["config"]["goal"], config.goal
        )
        self.assertEqual((await service.get_profile(pid))["version"], 2)
        with self.assertRaises(HTTPException):
            await service.create_profile(config, pid, 2)
        foreign = CapabilityService(
            self.factory,
            AccessContext(service.context.tenant_id, "other", tuple(SCOPES)),
        )
        with self.assertRaises(HTTPException) as caught:
            await foreign.get_profile(pid)
        self.assertEqual(caught.exception.status_code, 404)
        self.assertEqual((await foreign.profiles())["items"], [])
        tenant = uuid4()
        async with self.factory.begin() as session:
            session.add(Tenant(id=tenant, name="foreign"))
        alien = CapabilityService(
            self.factory, AccessContext(tenant, "user", tuple(SCOPES))
        )
        with self.assertRaises(HTTPException):
            await alien.get_profile(pid)
        restricted = CapabilityService(
            self.factory,
            AccessContext(service.context.tenant_id, "user", tuple(SCOPES), ()),
        )
        with self.assertRaises(HTTPException) as caught:
            await restricted.get_profile(pid)
        self.assertEqual(caught.exception.status_code, 403)
        self.assertEqual((await restricted.profiles())["items"], [])
        missing = CapabilityService(
            self.factory,
            AccessContext(
                service.context.tenant_id, "user", ("profiles:read", "ai:run")
            ),
        )
        with self.assertRaises(HTTPException) as caught:
            await self.execute(missing, pid)
        self.assertEqual(caught.exception.status_code, 403)
        results = await asyncio.gather(
            service.create_profile(config, pid, 3),
            service.create_profile(config, pid, 3),
            return_exceptions=True,
        )
        self.assertEqual(sum(isinstance(r, dict) for r in results), 1)

    async def test_provider_failures_bad_refs_and_schemas_preserve_success(self):
        service, _coll, _env, _config, pid = await self.prepare(
            "extract", output_schema=OUTPUT
        )
        success = await self.execute(service, pid)
        for broken in ["failure", "refs", "schema", "classification"]:
            with self.assertRaises(HTTPException) as caught:
                await self.execute(service, pid, FakeGenerator(broken=broken))
            self.assertEqual(caught.exception.status_code, 502)
        self.assertEqual(await self.count_results(), 1)
        self.assertEqual(
            (await service.result(UUID(success["result_id"])))["payload"],
            success["payload"],
        )

    async def test_stale_results_update_and_erasure_during_generation(self):
        service, coll, env, _config, pid = await self.prepare("recommend")
        success = await self.execute(service, pid)

        async def update():
            await service.ingest_one(
                coll,
                msgspec.structs.replace(
                    env, object_version=2, operation="upsert", event_id="changed"
                ),
            )

        with self.assertRaises(HTTPException) as caught:
            await self.execute(service, pid, FakeGenerator(callback=update))
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(
            (await service.result(UUID(success["result_id"])))["freshness"], "stale"
        )
        ack = await service.ingest_one(
            coll,
            msgspec.structs.replace(
                env, object_version=2, operation="upsert", event_id="changed"
            ),
        )

        async def erase():
            await service.erase_object(ack.object_id)

        with self.assertRaises(HTTPException):
            await self.execute(service, pid, FakeGenerator(callback=erase))
        self.assertEqual(await self.count_results(), 0)
        with self.assertRaises(HTTPException):
            await service.result(UUID(success["result_id"]))

    async def test_erasure_cannot_alias_same_count_and_timestamp_snapshot(self):
        from sqlalchemy import update

        from app.db.models import DataOutbox

        service, coll, env, _config, pid = await self.prepare("recommend")
        ack = await service.ingest_one(coll, env)
        original = await self.execute(service, pid)
        async with self.factory() as session:
            timestamp = await session.scalar(select(DataOutbox.created_at))

        async def erase_with_equal_timestamp():
            await service.erase_object(ack.object_id)
            # Simulate a waiting transaction whose server now() precedes the
            # previous owner's commit. Row count/timestamp can stay identical.
            async with self.factory.begin() as session:
                await session.execute(update(DataOutbox).values(created_at=timestamp))

        with self.assertRaises(HTTPException) as caught:
            await self.execute(
                service, pid, FakeGenerator(callback=erase_with_equal_timestamp)
            )
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(await self.count_results(), 0)
        with self.assertRaises(HTTPException):
            await service.result(UUID(original["result_id"]))

    async def test_inline_long_summary_ephemeral_budget_and_minimum(self):
        base, _coll, _env = await self.seed()
        service = CapabilityService(self.factory, base.context)
        config = ProfileInput(
            "inline",
            "summarize",
            "Summarize all parts",
            budget=Budget(
                context_chars=4000,
                input_tokens=100000,
                output_tokens=32000,
                model_calls=32,
            ),
        )
        profile = await service.create_profile(config)
        pid = UUID(profile["profile_id"])
        generator = FakeGenerator()
        result = await self.execute(
            service,
            pid,
            generator,
            RunInput(1, inline=[InlineInput("Important book text. " * 700)]),
        )
        self.assertGreater(len(generator.calls), 2)
        self.assertFalse(result["persisted"])
        self.assertFalse(result["coverage"]["text_partial"])
        self.assertEqual(
            result["coverage"]["processed_source_count"],
            result["coverage"]["selected_source_count"],
        )
        self.assertEqual(await self.count_results(), 0)
        self.assertTrue(all(s["id"].startswith("inline:") for s in result["sources"]))
        with self.assertRaises(HTTPException):
            await service.result(UUID(result["result_id"]))
        await service.create_profile(
            msgspec.structs.replace(config, budget=Budget(model_calls=1)), pid, 2
        )
        with self.assertRaises(ContractError) as caught:
            await self.execute(
                service,
                pid,
                FakeGenerator(),
                RunInput(2, inline=[InlineInput("Long text. " * 6000)]),
            )
        self.assertEqual(caught.exception.code, "generation_budget")
        await service.create_profile(
            msgspec.structs.replace(config, minimum_samples=2), pid, 3
        )
        insufficient = await self.execute(
            service, pid, run=RunInput(3, inline=[InlineInput("One input")])
        )
        self.assertEqual(insufficient["data_state"], "insufficient_data")
        self.assertEqual(insufficient["usage"]["model_calls"], 0)
        await service.create_profile(
            msgspec.structs.replace(config, capability="search", minimum_samples=2),
            pid,
            4,
        )
        empty_search = await self.execute(
            service, pid, run=RunInput(4, "One", inline=[InlineInput("One input")])
        )
        self.assertEqual(empty_search["payload"], {"items": []})
        self.assertEqual(empty_search["usage"]["model_calls"], 0)
        await service.erase_scope()
        async with self.factory() as session:
            self.assertEqual(
                await session.scalar(select(func.count()).select_from(AnalysisProfile)),
                0,
            )

    async def test_bounded_sampling_exact_refusal_and_field_projection(self):
        service, coll, env, config, pid = await self.prepare(
            "analyze",
            metrics=[Metric("count", 0, "count")],
            budget=Budget(max_records=1),
        )
        await service.ingest_one(
            coll, msgspec.structs.replace(env, external_id="another")
        )
        with self.assertRaises(ContractError) as caught:
            await self.execute(service, pid)
        self.assertEqual(caught.exception.code, "exact_input_limit")
        config = msgspec.structs.replace(
            config,
            capability="recommend",
            metrics=[],
            selections=[Selection(coll, 1, ["/phase"])],
        )
        await service.create_profile(config, pid, 2)
        generator = FakeGenerator()
        result = await self.execute(service, pid, generator, RunInput(2))
        self.assertEqual(result["data_state"], "partial")
        self.assertNotIn("За жовтень", json.dumps(generator.calls, ensure_ascii=False))
        self.assertNotIn(
            "Підготувати звіт", json.dumps(generator.calls, ensure_ascii=False)
        )
        await service.erase_scope(collection_id=coll)
        self.assertEqual(await self.count_results(), 0)
        with self.assertRaises(HTTPException):
            await service.get_profile(pid)

    @asynccontextmanager
    async def http(self, service, generator):
        async with self.factory.begin() as session:
            session.add(
                ServiceAPIKey(
                    tenant_id=service.context.tenant_id,
                    name="admin",
                    key_hash=hash_api_key("admin"),
                    scopes=list(SCOPES),
                )
            )

        async def factory():
            return self.factory

        app = Litestar(
            route_handlers=[
                CapabilityController,
                IntegrationController,
                DataController,
            ],
            dependencies={
                "db_session_factory": Provide(factory),
                "capability_embeddings": Provide(
                    lambda: fixture.FakeEmbeddings(), sync_to_thread=False
                ),
                "capability_generator": Provide(
                    lambda: generator, sync_to_thread=False
                ),
            },
            openapi_config=create_app().openapi_config,
        )
        self.app = app
        async with (
            app.lifespan(),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
                headers={"Authorization": "Bearer admin", "X-User-ID": "user"},
            ) as client,
        ):
            yield client

    async def test_http_contract_auth_validation_discovery_and_result_get(self):
        base, coll, env = await self.seed()
        await base.ingest_one(coll, env)
        config = ProfileInput(
            "http",
            "query",
            "Answer with sources",
            selections=[Selection(coll, 1, ["/caption"])],
        )
        generator = FakeGenerator()
        async with self.http(base, generator) as client:
            response = await client.post(
                "/v1/profiles", json=msgspec.to_builtins(config)
            )
            self.assertEqual(response.status_code, 201, response.text)
            pid = response.json()["profile_id"]
            self.assertEqual(
                (await client.get("/v1/profiles")).json()["items"][0]["profile_id"], pid
            )
            result = await client.post(
                f"/v1/profiles/{pid}/runs",
                json={
                    "version": 1,
                    "query": "звіт",
                    "history": [{"role": "user", "content": "What about the report?"}],
                },
            )
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(result.json()["data_state"], "partial")
            rid = result.json()["result_id"]
            calls = len(generator.calls)
            self.assertEqual((await client.get(f"/v1/results/{rid}")).status_code, 200)
            self.assertEqual(len(generator.calls), calls)
            self.assertEqual(
                (await client.get(f"/v1/profiles/{pid}/results")).json()["items"][0][
                    "result_id"
                ],
                rid,
            )
            self.assertEqual(
                (
                    await client.get(
                        f"/v1/results/{rid}", headers={"X-User-ID": "other"}
                    )
                ).status_code,
                404,
            )
            self.assertEqual(
                (
                    await client.post(
                        f"/v1/profiles/{pid}/runs",
                        json={"version": 1, "query": "x", "sql": "SELECT 1"},
                    )
                ).status_code,
                400,
            )
            self.assertEqual(
                (await client.get("/v1/profiles?limit=0")).status_code, 400
            )
            self.assertEqual(
                (await client.get("/v1/profiles?unknown=x")).status_code, 400
            )
            catalog = (await client.get("/v1/capabilities")).json()
            for name in [
                "search",
                "query",
                "summarize",
                "extract",
                "analyze",
                "recommend",
            ]:
                self.assertTrue(
                    next(c for c in catalog["capabilities"] if c["name"] == name)[
                        "available"
                    ]
                )
            schema = self.app.openapi_schema.to_schema()
            self.assertIn("/v1/profiles/{profile_id}/runs", schema["paths"])
            self.assertEqual(
                (await client.delete(f"/v1/profiles/{pid}")).status_code, 200
            )
            self.assertEqual((await client.get(f"/v1/results/{rid}")).status_code, 404)

    async def test_profile_rejects_arbitrary_paths_formulas_and_units(self):
        service, coll, _env, config, _pid = await self.prepare()
        invalid = [
            msgspec.structs.replace(config, timezone="Unknown/Timezone"),
            msgspec.structs.replace(
                config, metrics=[Metric("bad", 0, "sum", "/caption")]
            ),
            msgspec.structs.replace(
                config, selections=[Selection(coll, 1, ["/missing"])]
            ),
            msgspec.structs.replace(
                config,
                selections=[
                    Selection(
                        coll, 1, ["/caption"], filters=[Filter("/phase", "eq", "todo")]
                    )
                ],
            ),
            msgspec.structs.replace(
                config,
                selections=[
                    Selection(
                        coll,
                        1,
                        ["/estimate"],
                        filters=[Filter("/estimate", "gt", "bad")],
                    )
                ],
            ),
            msgspec.structs.replace(
                config,
                capability="extract",
                output_schema={"type": "object", "$ref": "https://example.com"},
            ),
        ]
        for candidate in invalid:
            with self.assertRaises(ContractError):
                await service.create_profile(candidate)

    async def test_migration_roundtrip_columns_constraints_and_legacy_preserved(self):
        base, coll, env = await self.seed()
        await base.ingest_one(coll, env)
        module = importlib.import_module(
            "app.db.migrations.versions.c9f5a3b7d041_analysis_profiles"
        )
        async with self.engine.begin() as connection:
            await connection.execute(
                text(f'SET LOCAL search_path TO "{self.schema}", public')
            )

            def migrate(conn):
                with Operations.context(MigrationContext.configure(conn)):
                    background = importlib.import_module(
                        "app.db.migrations.versions.d0a6b4c8e152_background_execution"
                    )
                    memory = importlib.import_module(
                        "app.db.migrations.versions.e1b7c5d9f263_managed_memory"
                    )
                    memory.downgrade()
                    background.downgrade()
                    module.downgrade()
                    module.upgrade()
                    background.upgrade()
                    memory.upgrade()
                from app.db.models import Base

                inspector = inspect(conn)
                for name in [
                    "analysis_profiles",
                    "analysis_profile_versions",
                    "analysis_results",
                ]:
                    table = Base.metadata.tables[name]
                    actual = {
                        c["name"]: (
                            c["nullable"],
                            str(c["type"].compile(dialect=conn.dialect)),
                        )
                        for c in inspector.get_columns(name, schema=self.schema)
                    }
                    expected = {
                        c.name: (c.nullable, str(c.type.compile(dialect=conn.dialect)))
                        for c in table.columns
                    }
                    self.assertEqual(actual, expected)

            await connection.run_sync(migrate)
        self.assertEqual(len((await base.records(coll)).items), 1)

    async def test_history_versions_and_schema_pin(self):
        service, coll, env, config, pid = await self.prepare(
            "analyze", metrics=[Metric("rows", 0, "count")]
        )
        await service.ingest_one(
            coll,
            msgspec.structs.replace(
                env,
                operation="upsert",
                object_version=2,
                event_id="changed",
                payload={**env.payload, "estimate": 3},
            ),
        )
        current = await self.execute(service, pid)
        self.assertEqual(current["payload"]["metrics"][0]["value"], "1")
        history = msgspec.structs.replace(config.selections[0], mode="history")
        await service.create_profile(
            msgspec.structs.replace(config, selections=[history]), pid, 2
        )
        prior = await self.execute(service, pid, run=RunInput(2))
        self.assertEqual(prior["payload"]["metrics"][0]["value"], "2")
        self.assertEqual({s["object_version"] for s in prior["sources"]}, {1, 2})

    async def test_event_time_dst_buckets_and_correction(self):
        base, coll, env = await self.seed("learning_sessions")
        service = CapabilityService(self.factory, base.context)
        for i, started in enumerate(
            [
                "2026-10-25T02:30:00+02:00",
                "2026-10-25T02:30:00+01:00",
                "2026-10-26T00:00:00+01:00",
            ]
        ):
            await service.ingest_one(
                coll,
                msgspec.structs.replace(
                    env,
                    event_id=f"dst-{i}",
                    payload={
                        **env.payload,
                        "started": started,
                        "elapsed": 60 * (i + 1),
                    },
                ),
                "events",
            )
        selection = Selection(
            coll,
            1,
            ["/subject", "/started", "/elapsed"],
            kind="event",
            mode="history",
            time_field="/started",
        )
        period = Period(
            datetime.fromisoformat("2026-10-25T00:00:00+02:00"),
            datetime.fromisoformat("2026-10-26T00:00:00+01:00"),
        )
        config = ProfileInput(
            "dst",
            "analyze",
            "Compare received observations",
            selections=[selection],
            period=period,
            timezone="Europe/Warsaw",
            metrics=[Metric("elapsed", 0, "sum", "/elapsed", interval="day")],
        )
        pid = UUID((await service.create_profile(config))["profile_id"])
        result = await self.execute(service, pid)
        metric = result["payload"]["metrics"][0]
        self.assertEqual(
            (metric["value"], metric["sample_count"], metric["bucket"]),
            ("180", 2, "2026-10-25"),
        )
        first = next(
            e for e in (await service.events(coll)).items if e.event_id == "dst-0"
        )
        await service.correct_event(
            first.id,
            msgspec.structs.replace(
                env,
                event_id="correction",
                payload={
                    **env.payload,
                    "started": "2026-10-25T02:30:00+02:00",
                    "elapsed": 180,
                },
            ),
        )
        updated = await self.execute(service, pid)
        self.assertEqual(updated["payload"]["metrics"][0]["value"], "300")
        self.assertNotIn(str(first.id), updated["payload"]["metrics"][0]["source_ids"])

    async def test_two_long_files_summary_and_filtered_vector_retrieval(self):
        from app.services.evidence_service import EvidenceService

        base, coll, env = await self.seed()
        evidence = EvidenceService(self.factory, base.context)
        for i, (word, phase) in enumerate([("Alpha", "todo"), ("Beta", "done")]):
            envelope = msgspec.structs.replace(
                env,
                external_id=f"book-{i}",
                payload={**env.payload, "caption": word, "phase": phase},
            )
            await evidence.attach(
                coll, envelope, ((word + " book chapter. ") * 150).encode(), "txt"
            )
        service = CapabilityService(self.factory, base.context)
        config = ProfileInput(
            "library",
            "summarize",
            "Summarize all supplied books",
            selections=[Selection(coll, 1, ["/caption", "/phase"], include_text=True)],
            budget=Budget(
                context_chars=4000,
                model_calls=32,
                output_tokens=32000,
                input_tokens=200000,
            ),
        )
        pid = UUID((await service.create_profile(config))["profile_id"])
        generator = FakeGenerator()
        result = await self.execute(service, pid, generator)
        self.assertGreater(len(generator.calls), 2)
        self.assertFalse(result["coverage"]["text_partial"])
        self.assertEqual(len({s["delivery_id"] for s in result["sources"]}), 2)
        self.assertTrue(
            any(
                "format" not in s and "extraction_coverage" in s
                for s in result["sources"]
            )
        )
        await evidence.index(coll, fixture.FakeEmbeddings())
        filtered = msgspec.structs.replace(
            config.selections[0], filters=[Filter("/phase", "eq", "done")]
        )
        await service.create_profile(
            msgspec.structs.replace(config, capability="search", selections=[filtered]),
            pid,
            2,
        )
        search = await self.execute(service, pid, run=RunInput(2, "Alpha"))
        self.assertTrue(
            search["sources"]
        )  # vector matches are only from the allowed filtered book
        self.assertTrue(all("Alpha" not in s["content"] for s in search["sources"]))
        self.assertEqual(search["usage"]["model_calls"], 1)

    async def test_deadline_preserves_last_result(self):
        service, _coll, _env, _config, pid = await self.prepare(
            "recommend", budget=Budget(seconds=1)
        )
        result = await self.execute(service, pid)

        async def slow():
            await asyncio.sleep(2)

        with self.assertRaises(HTTPException) as caught:
            await self.execute(service, pid, FakeGenerator(callback=slow))
        self.assertEqual(caught.exception.status_code, 504)
        self.assertEqual(await self.count_results(), 1)
        self.assertEqual(
            (await service.result(UUID(result["result_id"])))["freshness"], "current"
        )


class ProviderBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_responses_sdk_wire_format_refusal_incomplete_and_no_retry(self):
        from openai import APIStatusError, AsyncOpenAI

        from app.capabilities.generation import Meter, response_schema

        seen = []
        response_state = {"status": "completed"}

        def handle(request):
            seen.append(json.loads(request.content))
            if response_state["status"] == "http_error":
                return httpx.Response(503, json={"error": {"message": "temporary"}})
            value = {
                "insufficient_data": False,
                "claims": [{"text": "supported", "source_ids": ["a"]}],
                "limitations": [],
            }
            return httpx.Response(
                200,
                json={
                    "id": "resp_test",
                    "object": "response",
                    "created_at": 1,
                    "model": "test-model",
                    "status": response_state["status"],
                    "output": [
                        {
                            "type": "message",
                            "id": "msg_test",
                            "role": "assistant",
                            "status": "completed",
                            "content": [
                                {"type": "refusal", "refusal": "no"}
                                if response_state.get("refused")
                                else {
                                    "type": "output_text",
                                    "text": json.dumps(value),
                                    "annotations": [],
                                }
                            ],
                        }
                    ],
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            client = AsyncOpenAI(api_key="local-test-only", http_client=http)
            provider = OpenAIGenerator(client, "test-model")
            kwargs = {
                "request": {"sources": [{"id": "a", "content": "untrusted"}]},
                "schema": response_schema("query"),
                "max_output_tokens": 256,
            }
            result = await provider.generate(**kwargs)
            self.assertEqual(result["claims"][0]["source_ids"], ["a"])
            self.assertFalse(seen[0]["store"])
            self.assertNotIn("tools", seen[0])
            self.assertTrue(seen[0]["text"]["format"]["strict"])
            response_state["status"] = "incomplete"
            with self.assertRaises(GenerationError):
                await provider.generate(**kwargs)
            response_state.update(status="completed", refused=True)
            with self.assertRaises(GenerationError):
                await provider.generate(**kwargs)
            response_state["status"] = "http_error"
            count = len(seen)
            with self.assertRaises(APIStatusError):
                await provider.generate(**kwargs)
            self.assertEqual(len(seen), count + 1)
            meter = Meter(Budget(input_tokens=1000), provider)
            with self.assertRaises(ContractError):
                await meter.generate(
                    {"sources": [{"id": "a", "content": "x" * 1001}]},
                    response_schema("query"),
                    {"a"},
                )

    async def test_bundled_contract_and_examples_match_runtime(self):
        from importlib.resources import files

        from jsonschema import Draft202012Validator

        from app.capabilities.results import SOURCE, payload_schema
        from app.capabilities.types import (
            CapabilityResult,
            ProfilePage,
            ProfileView,
            ResultPage,
        )
        from app.capabilities.validation import validate_profile, validate_selection

        contract = json.loads(
            files("app.integration").joinpath("contract.json").read_text()
        )
        api = contract["capability_contract"]
        for name, model in [
            ("profile_input", ProfileInput),
            ("run_input", RunInput),
            ("profile_view", ProfileView),
            ("profile_page", ProfilePage),
            ("result", CapabilityResult),
            ("result_page", ResultPage),
        ]:
            self.assertEqual(api[name + "_json_schema"], msgspec.json.schema(model))
            Draft202012Validator.check_schema(api[name + "_json_schema"])
        self.assertEqual(api["source_json_schema"], SOURCE)
        registry = json.loads(
            files("app.integration").joinpath("examples/tasks.json").read_text()
        )["schema"]
        for capability in [
            "search",
            "query",
            "summarize",
            "extract",
            "analyze",
            "recommend",
        ]:
            self.assertEqual(
                api["payload_json_schemas"][capability], payload_schema(capability)
            )
            Draft202012Validator.check_schema(api["payload_json_schemas"][capability])
            raw = json.loads(
                files("app.integration")
                .joinpath("profile_examples/" + capability + ".json")
                .read_text()
            )
            config = msgspec.convert(raw, type=ProfileInput)
            validate_profile(config)
            validate_selection(
                config.selections[0],
                registry["json_schema"],
                registry["field_mappings"],
                config.metrics,
            )
