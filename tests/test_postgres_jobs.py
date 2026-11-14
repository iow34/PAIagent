"""Durable step10 execution, polling, quotas and lifecycle fencing on PostgreSQL."""

import asyncio
import importlib
import unittest
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import msgspec
import test_postgres_data_lifecycle as lifecycle
import test_postgres_indexing as fixture
from alembic.migration import MigrationContext
from alembic.operations import Operations
from litestar.exceptions import HTTPException
from sqlalchemy import func, select, text, update
from test_postgres_capabilities import FakeGenerator

from app.capabilities.types import (
    Budget,
    InlineInput,
    ProfileInput,
    RunInput,
    Selection,
)
from app.db.models import (
    AnalysisResult,
    AnalysisRun,
    AnalysisTenantUsage,
    AnalysisTrigger,
    ServiceAPIKey,
)
from app.jobs.runtime import LIMITS, Attempt, LeaseLost, TenantLimits
from app.jobs.types import CreateRun, TriggerInput
from app.security.api_keys import hash_api_key
from app.security.context import AccessContext
from app.security.permissions import SCOPES
from app.services.capability_service import CapabilityService
from app.services.job_service import JobService
from app.services.trigger_service import TriggerService, schedule


@unittest.skipUnless(fixture.TEST_URL, "Use temporary PostgreSQL runner")
class JobTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.PostgreSQLIndexingTests.asyncSetUp
    drop_schema = fixture.PostgreSQLIndexingTests.drop_schema
    seed = lifecycle.DataLifecycleTests.seed

    async def setup_job(self, *, limits=LIMITS, budget=None, context=None):
        base, coll, env = await self.seed(context=context)
        await base.ingest_one(coll, env)
        key_id = uuid4()
        async with self.factory.begin() as session:
            session.add(
                ServiceAPIKey(
                    id=key_id,
                    tenant_id=base.context.tenant_id,
                    name="job",
                    key_hash=hash_api_key("job-test-" + key_id.hex),
                    scopes=list(SCOPES),
                )
            )
        context = AccessContext(
            base.context.tenant_id, base.context.user_id, tuple(SCOPES), None, key_id
        )
        service = JobService(self.factory, context, limits=limits)
        config = ProfileInput(
            "analysis",
            "recommend",
            "Grounded advice",
            selections=[Selection(coll, 1, ["/caption", "/phase"])],
            budget=budget or Budget(),
        )
        pid = UUID((await service.create_profile(config))["profile_id"])
        return service, coll, env, config, pid

    async def enqueue(self, service, pid, key=None):
        return await service.enqueue(CreateRun(pid, RunInput(1), key or uuid4().hex))

    async def execute_one(self, service, generator=None):
        claim = await service.claim()
        self.assertIsNotNone(claim)
        await service.execute(
            claim, generator or FakeGenerator(), fixture.FakeEmbeddings()
        )
        return claim

    async def expire(self, rid):
        async with self.factory.begin() as session:
            await session.execute(
                update(AnalysisRun)
                .where(AnalysisRun.id == rid)
                .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )

    async def ready(self, rid):
        async with self.factory.begin() as session:
            await session.execute(
                update(AnalysisRun)
                .where(AnalysisRun.id == rid)
                .values(available_at=datetime.now(UTC) - timedelta(seconds=1))
            )

    async def test_durable_idempotent_manual_run_and_saved_result(self):
        service, _coll, _env, _cfg, pid = await self.setup_job()
        first = await self.enqueue(service, pid, "stable")
        again = await self.enqueue(service, pid, "stable")
        self.assertEqual(first["id"], again["id"])
        with self.assertRaises(HTTPException) as caught:
            await service.enqueue(
                CreateRun(pid, RunInput(1, query="different"), "stable")
            )
        self.assertEqual(caught.exception.status_code, 409)
        restarted = JobService(self.factory, service.context)
        await self.execute_one(restarted)
        result = await restarted.get_run(UUID(first["id"]))
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["attempts"], 1)
        self.assertTrue(result["input_boundary"])
        payload = await service.result(UUID(result["result_id"]))
        self.assertEqual(payload["run_id"], first["id"])
        self.assertEqual(
            (await self.enqueue(service, pid, "stable"))["result_id"],
            result["result_id"],
        )

    async def test_expired_attempt_cannot_commit_after_recovery(self):
        service, _coll, _env, _cfg, pid = await self.setup_job()
        row = await self.enqueue(service, pid)
        rid = UUID(row["id"])
        old = await service.claim()
        await self.expire(rid)
        self.assertIsNone(await service.claim())  # recovery schedules bounded backoff
        await self.ready(rid)
        new = await service.claim()
        self.assertNotEqual(old[1], new[1])
        with self.assertRaises(LeaseLost):
            await CapabilityService(self.factory, old[2]).run(
                pid,
                RunInput(1),
                FakeGenerator(),
                fixture.FakeEmbeddings(),
                attempt=Attempt(rid, old[1]),
            )
        await service.execute(new, FakeGenerator(), fixture.FakeEmbeddings())
        self.assertEqual((await service.get_run(rid))["attempts"], 2)
        async with self.factory() as session:
            self.assertEqual(
                await session.scalar(select(func.count()).select_from(AnalysisResult)),
                1,
            )

    async def test_retries_terminal_failure_and_cancellation(self):
        service, _coll, _env, _cfg, pid = await self.setup_job(
            budget=Budget(max_attempts=2, retry_seconds=1)
        )
        row = await self.enqueue(service, pid)
        rid = UUID(row["id"])
        await self.execute_one(service, FakeGenerator(broken="failure"))
        self.assertEqual((await service.get_run(rid))["status"], "pending")
        await self.ready(rid)
        await self.execute_one(service, FakeGenerator(broken="failure"))
        self.assertEqual((await service.get_run(rid))["status"], "failed")
        second = await self.enqueue(service, pid)
        self.assertEqual(
            (await service.cancel(UUID(second["id"])))["status"], "cancelled"
        )
        self.assertIsNone(await service.claim())

    async def test_cancel_running_prevents_result_and_stops_attempt(self):
        service, _coll, _env, _cfg, pid = await self.setup_job()
        row = await self.enqueue(service, pid)
        entered, release = asyncio.Event(), asyncio.Event()

        async def block():
            entered.set()
            await release.wait()

        claim = await service.claim()
        task = asyncio.create_task(
            service.execute(
                claim, FakeGenerator(callback=block), fixture.FakeEmbeddings()
            )
        )
        await asyncio.wait_for(entered.wait(), 3)
        await service.cancel(UUID(row["id"]))
        await asyncio.wait_for(task, 3)
        self.assertEqual(
            (await service.get_run(UUID(row["id"])))["status"], "cancelled"
        )
        async with self.factory() as session:
            self.assertEqual(
                await session.scalar(select(func.count()).select_from(AnalysisResult)),
                0,
            )

    async def test_on_change_coalescing_schedule_and_no_result_loop(self):
        service, coll, env, _cfg, pid = await self.setup_job()
        triggers = TriggerService(self.factory, service.context)
        await triggers.configure(
            pid, TriggerInput(1, on_change=True, debounce_seconds=1, max_wait_seconds=2)
        )
        self.assertEqual(await schedule(self.factory), 0)
        await service.ingest_one(
            coll, msgspec.structs.replace(env, external_id="second")
        )
        scheduled = await asyncio.gather(schedule(self.factory), schedule(self.factory))
        self.assertEqual(sum(scheduled), 1)
        await service.ingest_one(
            coll, msgspec.structs.replace(env, external_id="third")
        )
        self.assertEqual(await schedule(self.factory), 0)
        async with self.factory() as session:
            pending = (
                await session.scalars(
                    select(AnalysisRun).where(AnalysisRun.status == "pending")
                )
            ).one()
        self.assertLessEqual(
            pending.available_at, pending.created_at + timedelta(seconds=2)
        )
        await self.ready(pending.id)
        await self.execute_one(service)
        self.assertEqual(await schedule(self.factory), 0)
        await triggers.configure(
            pid,
            TriggerInput(
                1,
                interval_seconds=60,
                start_at=datetime.now(UTC) - timedelta(seconds=180),
            ),
        )
        self.assertEqual(await schedule(self.factory), 1)
        self.assertEqual(await schedule(self.factory), 0)
        view = await triggers.get_trigger(pid)
        self.assertGreater(view["next_due_at"], datetime.now(UTC))

    async def test_changes_during_generation_remain_pending(self):
        service, coll, env, _cfg, pid = await self.setup_job()
        await TriggerService(self.factory, service.context).configure(
            pid, TriggerInput(1, on_change=True, debounce_seconds=1)
        )
        await service.ingest_one(
            coll, msgspec.structs.replace(env, external_id="before")
        )
        await schedule(self.factory)
        async with self.factory() as session:
            first = await session.scalar(select(AnalysisRun))
        await self.ready(first.id)

        async def change():
            await service.ingest_one(
                coll, msgspec.structs.replace(env, external_id="during")
            )
            await schedule(self.factory)

        await self.execute_one(service, FakeGenerator(callback=change))
        self.assertEqual((await service.get_run(first.id))["status"], "cancelled")
        async with self.factory() as session:
            pending = (
                await session.scalars(
                    select(AnalysisRun).where(AnalysisRun.status == "pending")
                )
            ).one()
        await self.ready(pending.id)
        await self.execute_one(service)
        self.assertEqual((await service.get_run(pending.id))["status"], "succeeded")

    async def test_key_revoked_before_start_and_before_commit(self):
        service, _coll, _env, _cfg, pid = await self.setup_job()
        row = await self.enqueue(service, pid)
        async with self.factory.begin() as session:
            await session.execute(
                update(ServiceAPIKey)
                .where(ServiceAPIKey.id == service.context.api_key_id)
                .values(is_active=False)
            )
        self.assertIsNone(await service.claim())
        self.assertEqual((await service.get_run(UUID(row["id"])))["status"], "failed")
        async with self.factory.begin() as session:
            await session.execute(
                update(ServiceAPIKey)
                .where(ServiceAPIKey.id == service.context.api_key_id)
                .values(is_active=True)
            )
        row = await self.enqueue(service, pid)

        async def revoke():
            async with self.factory.begin() as session:
                await session.execute(
                    update(ServiceAPIKey)
                    .where(ServiceAPIKey.id == service.context.api_key_id)
                    .values(source_ids=[])
                )

        await self.execute_one(service, FakeGenerator(callback=revoke))
        self.assertEqual((await service.get_run(UUID(row["id"])))["status"], "failed")
        async with self.factory() as session:
            self.assertEqual(
                await session.scalar(select(func.count()).select_from(AnalysisResult)),
                0,
            )

    async def test_erasure_clears_jobs_and_cannot_resurrect_output(self):
        service, coll, env, _cfg, pid = await self.setup_job()
        ack = await service.ingest_one(coll, env)
        row = await self.enqueue(service, pid)

        async def erase():
            await service.erase_object(ack.object_id)

        await self.execute_one(service, FakeGenerator(callback=erase))
        self.assertEqual(
            (await service.get_run(UUID(row["id"])))["status"], "cancelled"
        )
        async with self.factory() as session:
            stored = await session.get(AnalysisRun, UUID(row["id"]))
            self.assertEqual(stored.input, {})
            self.assertEqual(
                await session.scalar(select(func.count()).select_from(AnalysisResult)),
                0,
            )
        await service.erase_scope(collection_id=coll)
        async with self.factory() as session:
            self.assertEqual(
                await session.scalar(select(func.count()).select_from(AnalysisRun)), 0
            )

    async def test_concurrent_claims_tenant_queue_and_daily_reservations(self):
        service, _coll, _env, _cfg, pid = await self.setup_job(
            limits=TenantLimits(running=1, pending=2, daily_attempts=1)
        )
        await self.enqueue(service, pid)
        await self.enqueue(service, pid)
        with self.assertRaises(HTTPException) as caught:
            await self.enqueue(service, pid)
        self.assertEqual(caught.exception.status_code, 429)
        claims = await asyncio.gather(service.claim(), service.claim())
        self.assertEqual(sum(c is not None for c in claims), 1)
        claim = next(c for c in claims if c)
        await service.execute(claim, FakeGenerator(), fixture.FakeEmbeddings())
        self.assertIsNone(await service.claim())
        async with self.factory() as session:
            usage = (await session.scalars(select(AnalysisTenantUsage))).one()
            self.assertEqual(usage.attempts, 1)
            delayed = (
                await session.scalars(
                    select(AnalysisRun).where(AnalysisRun.status == "pending")
                )
            ).one()
            self.assertEqual(delayed.error_code, "daily_budget")

    async def test_timeout_is_retryable_and_inline_remains_ephemeral(self):
        service, _coll, _env, _cfg, pid = await self.setup_job(
            budget=Budget(seconds=1, max_attempts=1)
        )
        row = await self.enqueue(service, pid)

        async def slow():
            await asyncio.sleep(2)

        await self.execute_one(service, FakeGenerator(callback=slow))
        self.assertEqual((await service.get_run(UUID(row["id"])))["status"], "failed")
        inline = UUID(
            (
                await service.create_profile(
                    ProfileInput("inline", "summarize", "Summarize")
                )
            )["profile_id"]
        )
        result = await service.sync(
            inline,
            RunInput(1, inline=[InlineInput("private ephemeral text")]),
            FakeGenerator(),
            fixture.FakeEmbeddings(),
        )
        self.assertFalse(result["persisted"])
        async with self.factory() as session:
            stored = await session.get(AnalysisRun, UUID(result["run_id"]))
            self.assertEqual(stored.input, {})
            self.assertEqual(stored.fingerprint, "")
            self.assertTrue(stored.ephemeral)

    async def test_background_migration_roundtrip(self):
        migration = importlib.import_module(
            "app.db.migrations.versions.d0a6b4c8e152_background_execution"
        )
        async with self.engine.begin() as connection:
            await connection.execute(
                text(f'SET LOCAL search_path TO "{self.schema}", public')
            )

            def migrate(conn):
                with Operations.context(MigrationContext.configure(conn)):
                    memory = importlib.import_module(
                        "app.db.migrations.versions.e1b7c5d9f263_managed_memory"
                    )
                    memory.downgrade()
                    migration.downgrade()
                    migration.upgrade()
                    memory.upgrade()
                from sqlalchemy import inspect

                from app.db.models import Base

                inspector = inspect(conn)
                for name in [
                    "analysis_runs",
                    "analysis_triggers",
                    "analysis_tenant_usage",
                    "analysis_results",
                ]:
                    actual = {
                        c["name"]: (
                            c["nullable"],
                            str(c["type"].compile(dialect=conn.dialect)),
                        )
                        for c in inspector.get_columns(name, schema=self.schema)
                    }
                    expected = {
                        c.name: (c.nullable, str(c.type.compile(dialect=conn.dialect)))
                        for c in Base.metadata.tables[name].columns
                    }
                    self.assertEqual(actual, expected, name)

            await connection.run_sync(migrate)
        service, _coll, _env, _cfg, pid = await self.setup_job()
        await self.enqueue(service, pid)
        await self.execute_one(service)

    async def test_tenant_fairness_and_profile_version_pin(self):
        from app.db.models import Tenant

        service, _coll, _env, config, pid = await self.setup_job(
            limits=TenantLimits(running=1)
        )
        await self.enqueue(service, pid)
        await self.enqueue(service, pid)
        tenant = uuid4()
        async with self.factory.begin() as session:
            session.add(Tenant(id=tenant, name="second tenant"))
        other, _c, _e, _cfg, other_pid = await self.setup_job(
            limits=TenantLimits(running=1),
            context=AccessContext(tenant, "user", tuple(SCOPES)),
        )
        await self.enqueue(other, other_pid)
        await service.create_profile(
            msgspec.structs.replace(
                config, goal="Newer version must not replace accepted work"
            ),
            pid,
            2,
        )
        claims = await asyncio.gather(service.claim(), service.claim())
        self.assertEqual(
            {claim[2].tenant_id for claim in claims},
            {service.context.tenant_id, tenant},
        )
        self.assertEqual({claim[3].goal for claim in claims}, {config.goal})
        self.assertIsNone(await service.claim())
        await asyncio.gather(
            *(
                service.execute(claim, FakeGenerator(), fixture.FakeEmbeddings())
                for claim in claims
            )
        )
        async with self.factory() as session:
            versions = list(
                await session.scalars(select(AnalysisResult.profile_version))
            )
        self.assertEqual(versions, [1, 1])

    async def test_worker_process_loss_recovers_after_restart(self):
        import os
        import sys

        service, _coll, _env, _cfg, pid = await self.setup_job()
        row = await self.enqueue(service, pid)
        rid = UUID(row["id"])
        environment = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONWARNINGS": "ignore",
            "PAIAGENT_TEST_DATABASE_URL": fixture.TEST_URL,
            "PAIAGENT_TEST_SCHEMA": self.schema,
        }
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "tests/job_worker_process.py",
            "block",
            env=environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            async with asyncio.timeout(5):
                while (await service.get_run(rid))["status"] != "running":
                    if process.returncode is not None:
                        _, stderr = await process.communicate()
                        self.fail(stderr.decode())
                    await asyncio.sleep(0.02)
            process.kill()  # actual process death, no Python cleanup/finish callback
            await asyncio.wait_for(process.communicate(), 3)
            self.assertEqual((await service.get_run(rid))["status"], "running")
        finally:
            if process.returncode is None:
                process.kill()
                await process.communicate()
        await self.expire(rid)
        await service.claim()  # reclaim expired lease, preserving bounded backoff
        await self.ready(rid)
        restarted = await asyncio.create_subprocess_exec(
            sys.executable,
            "tests/job_worker_process.py",
            "drain",
            env=environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(restarted.communicate(), 5)
            self.assertEqual(restarted.returncode, 0, stderr.decode())
        finally:
            if restarted.returncode is None:
                restarted.kill()
                await restarted.communicate()
        state = await service.get_run(rid)
        self.assertEqual(state["status"], "succeeded")
        self.assertEqual(state["attempts"], 2)
        self.assertTrue(state["result_id"])

    async def test_trigger_config_revision_cancels_running_attempt(self):
        service, coll, env, _cfg, pid = await self.setup_job()
        triggers = TriggerService(self.factory, service.context)
        await triggers.configure(
            pid, TriggerInput(1, on_change=True, debounce_seconds=1)
        )
        await service.ingest_one(coll, msgspec.structs.replace(env, external_id="new"))
        await schedule(self.factory)
        async with self.factory() as session:
            row = await session.scalar(select(AnalysisRun))
        await self.ready(row.id)

        async def disable():
            await triggers.configure(pid, TriggerInput(1, enabled=False))

        await self.execute_one(service, FakeGenerator(callback=disable))
        self.assertEqual((await service.get_run(row.id))["status"], "cancelled")
        self.assertEqual(await schedule(self.factory), 0)
        async with self.factory() as session:
            self.assertEqual(
                await session.scalar(select(func.count()).select_from(AnalysisResult)),
                0,
            )

    async def test_auto_input_change_retry_and_crash_retry_limit(self):
        service, coll, env, _cfg, pid = await self.setup_job(
            budget=Budget(max_attempts=2, retry_seconds=1)
        )
        row = await self.enqueue(service, pid)

        async def change():
            await service.ingest_one(
                coll, msgspec.structs.replace(env, external_id="during")
            )

        await self.execute_one(service, FakeGenerator(callback=change))
        rid = UUID(row["id"])
        self.assertEqual((await service.get_run(rid))["error_code"], "input_changed")
        await self.ready(rid)
        await self.execute_one(service)
        self.assertEqual((await service.get_run(rid))["status"], "succeeded")
        row = await self.enqueue(service, pid)
        rid = UUID(row["id"])
        await service.claim()
        await self.expire(rid)
        await service.claim()
        await self.ready(rid)
        await service.claim()
        await self.expire(rid)
        self.assertIsNone(await service.claim())
        self.assertEqual((await service.get_run(rid))["status"], "failed")

    async def test_trigger_queue_quota_does_not_lose_dirty_data(self):
        service, coll, env, _cfg, pid = await self.setup_job(
            limits=TenantLimits(pending=1)
        )
        await TriggerService(self.factory, service.context).configure(
            pid, TriggerInput(1, on_change=True)
        )
        manual = await self.enqueue(service, pid)
        await service.ingest_one(
            coll, msgspec.structs.replace(env, external_id="changed")
        )
        self.assertEqual(await schedule(self.factory, limits=service.limits), 0)
        self.assertEqual(
            (await TriggerService(self.factory, service.context).get_trigger(pid))[
                "error_code"
            ],
            "queue_limit",
        )
        await service.cancel(UUID(manual["id"]))
        self.assertEqual(await schedule(self.factory, limits=service.limits), 1)

    async def test_revoked_trigger_disabled_and_scope_isolation(self):
        service, _coll, _env, _cfg, pid = await self.setup_job()
        await TriggerService(self.factory, service.context).configure(
            pid, TriggerInput(1, interval_seconds=60)
        )
        async with self.factory.begin() as session:
            await session.execute(
                update(ServiceAPIKey)
                .where(ServiceAPIKey.id == service.context.api_key_id)
                .values(is_active=False)
            )
        await schedule(self.factory)
        async with self.factory() as session:
            trigger = await session.get(AnalysisTrigger, pid)
            self.assertFalse(trigger.enabled)
        other = JobService(
            self.factory,
            AccessContext(service.context.tenant_id, "other", tuple(SCOPES)),
        )
        row = await self.enqueue(service, pid)
        with self.assertRaises(HTTPException) as caught:
            await other.get_run(UUID(row["id"]))
        self.assertEqual(caught.exception.status_code, 404)
        self.assertEqual((await other.runs())["items"], [])

    async def http_context(self, service, generator):
        # A reusable context manager is returned to keep app lifespans explicit.
        from contextlib import asynccontextmanager

        import httpx
        from litestar import Litestar
        from litestar.di import Provide

        from app.api.controllers.capabilities import CapabilityController
        from app.api.controllers.data import DataController
        from app.api.controllers.integration import IntegrationController
        from app.api.controllers.jobs import JobController
        from app.asgi import create_app

        async def factory():
            return self.factory

        @asynccontextmanager
        async def application():
            self.app = Litestar(
                route_handlers=[
                    CapabilityController,
                    DataController,
                    JobController,
                    IntegrationController,
                ],
                dependencies={
                    "db_session_factory": Provide(factory),
                    "capability_generator": Provide(
                        lambda: generator, sync_to_thread=False
                    ),
                    "capability_embeddings": Provide(
                        lambda: fixture.FakeEmbeddings(), sync_to_thread=False
                    ),
                },
                openapi_config=create_app().openapi_config,
            )
            async with (
                self.app.lifespan(),
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=self.app),
                    base_url="http://test",
                    headers={
                        "Authorization": "Bearer job-test-"
                        + service.context.api_key_id.hex,
                        "X-User-ID": service.context.user_id,
                    },
                ) as client,
            ):
                yield client

        return application()

    async def test_http_manual_sync_status_filters_cancel_and_triggers(self):
        service, _coll, _env, _cfg, pid = await self.setup_job()
        generator = FakeGenerator()
        async with await self.http_context(service, generator) as client:
            body = {
                "profile_id": str(pid),
                "input": {"version": 1},
                "idempotency_key": "http",
            }
            response = await client.post("/v1/runs", json=body)
            self.assertEqual(response.status_code, 202, response.text)
            rid = response.json()["id"]
            self.assertEqual(
                (await client.post("/v1/runs", json=body)).json()["id"], rid
            )
            await self.execute_one(service, generator)
            status = (await client.get("/v1/runs/" + rid)).json()
            self.assertEqual(status["status"], "succeeded")
            calls = len(generator.calls)
            self.assertEqual(
                (await client.get("/v1/results/" + status["result_id"])).status_code,
                200,
            )
            self.assertEqual(calls, len(generator.calls))
            page = (
                await client.get(
                    "/v1/runs",
                    params={"status": "succeeded", "profile_id": str(pid), "limit": 1},
                )
            ).json()
            self.assertEqual(page["items"][0]["id"], rid)
            self.assertEqual(
                (
                    await client.get("/v1/runs/" + rid, headers={"X-User-ID": "other"})
                ).status_code,
                404,
            )
            second = await client.post(
                "/v1/runs", json={**body, "idempotency_key": "cancel"}
            )
            cancelled = await client.post("/v1/runs/" + second.json()["id"] + "/cancel")
            self.assertEqual(cancelled.json()["status"], "cancelled")
            config = await client.put(
                f"/v1/profiles/{pid}/triggers", json={"version": 1, "on_change": True}
            )
            self.assertEqual(config.status_code, 200, config.text)
            self.assertEqual(
                (await client.get(f"/v1/profiles/{pid}/triggers")).status_code, 200
            )
            sync = await client.post(
                "/v1/runs", json={**body, "mode": "sync", "idempotency_key": "sync"}
            )
            self.assertEqual(sync.status_code, 200, sync.text)
            self.assertEqual(sync.json()["status"], "succeeded")
            self.assertEqual(
                (await client.get("/v1/runs?status=unknown")).status_code, 400
            )
            self.assertEqual(
                (
                    await client.post("/v1/runs", json={**body, "sql": "bad"})
                ).status_code,
                400,
            )
            schema = self.app.openapi_schema.to_schema()
            self.assertIn("/v1/runs/{run_id}/cancel", schema["paths"])

    async def test_independent_client_process_over_real_http(self):
        import json
        import os
        import socket
        import sys

        import uvicorn

        from app.jobs.worker import serve

        service, _coll, _env, _cfg, pid = await self.setup_job()
        async with await self.http_context(service, FakeGenerator()):
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
            server = uvicorn.Server(
                uvicorn.Config(self.app, log_level="error", lifespan="off")
            )
            server_task = asyncio.create_task(server.serve(sockets=[sock]))
            worker = asyncio.create_task(
                serve(
                    self.factory,
                    FakeGenerator(),
                    fixture.FakeEmbeddings(),
                    poll_seconds=0.1,
                )
            )
            try:
                async with asyncio.timeout(5):
                    while not server.started:
                        await asyncio.sleep(0.01)
                env = os.environ.copy()
                env["PAIAGENT_SERVICE_KEY"] = (
                    "job-test-" + service.context.api_key_id.hex
                )
                process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    "app.integration.run_client",
                    "--base-url",
                    f"http://127.0.0.1:{port}",
                    "--user-id",
                    service.context.user_id,
                    "--profile-id",
                    str(pid),
                    "--version",
                    "1",
                    env=env,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await asyncio.wait_for(process.communicate(), 10)
                self.assertEqual(process.returncode, 0, stderr.decode())
                value = json.loads(stdout)
                self.assertEqual(value["run"]["status"], "succeeded")
                self.assertEqual(value["result"]["execution_status"], "succeeded")
                self.assertEqual(value["result"]["profile_id"], str(pid))
            finally:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
                server.should_exit = True
                await asyncio.wait_for(server_task, 5)
                sock.close()

    async def test_bundled_background_schemas_match_runtime(self):
        import json
        from importlib.resources import files

        from jsonschema import Draft202012Validator

        from app.jobs.types import (
            CreateRun,
            RunPage,
            RunView,
            TriggerInput,
            TriggerView,
        )

        contract = json.loads(
            files("app.integration").joinpath("contract.json").read_text()
        )
        for model in [CreateRun, RunView, RunPage, TriggerInput, TriggerView]:
            schema = contract["background_execution"]["schemas"][model.__name__]
            self.assertEqual(schema, msgspec.json.schema(model))
            Draft202012Validator.check_schema(schema)
