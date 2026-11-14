"""Step11 consent, candidates, privacy, feedback effects and cross-session context."""

import importlib
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import msgspec
import test_postgres_data_lifecycle as lifecycle
import test_postgres_indexing as fixture
from alembic.migration import MigrationContext
from alembic.operations import Operations
from litestar.exceptions import HTTPException
from sqlalchemy import func, select, text
from test_postgres_capabilities import FakeGenerator

from app.capabilities.types import ProfileInput, RunInput, Selection
from app.db.models import AnalysisResult, MemoryItem, MemoryMessage
from app.integration.validation import ContractError
from app.personalization.types import (
    Definition,
    ExtractionInput,
    FeedbackInput,
    FieldRule,
    MemoryAction,
    MessageInput,
    MessageUpdate,
    Period,
    Personalization,
    PreferenceWrite,
    SettingsInput,
)
from app.security.context import AccessContext
from app.security.permissions import SCOPES
from app.services.capability_service import CapabilityService
from app.services.feedback_service import FeedbackService
from app.services.memory_service import MemoryService


@unittest.skipUnless(fixture.TEST_URL, "Use temporary PostgreSQL runner")
class MemoryTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.PostgreSQLIndexingTests.asyncSetUp
    drop_schema = fixture.PostgreSQLIndexingTests.drop_schema
    seed = lifecycle.DataLifecycleTests.seed

    async def prepare(self):
        base, coll, env = await self.seed()
        ack = await base.ingest_one(coll, env)
        service = FeedbackService(self.factory, base.context)
        settings = SettingsInput(
            (await service.get_settings())["version"],
            enabled=True,
            collections=[coll],
            definitions=[
                Definition(
                    "style", {"type": "string", "maxLength": 100}, ["I prefer: "]
                ),
                Definition("phase", {"type": "string", "maxLength": 20}),
            ],
            field_rules=[FieldRule(coll, 1, "/phase", "phase")],
        )
        await service.configure(settings)
        config = ProfileInput(
            "personal",
            "recommend",
            "Useful grounded advice",
            selections=[Selection(coll, 1, ["/caption", "/phase"])],
            personalization=Personalization(
                enabled=True, collections=[coll], keys=["style", "phase"]
            ),
        )
        cap = CapabilityService(self.factory, base.context)
        pid = UUID((await cap.create_profile(config))["profile_id"])
        return service, cap, coll, env, ack, config, pid

    async def run_cap(self, cap, pid, generator=None, version=1):
        return await cap.run(
            pid,
            RunInput(version),
            generator or FakeGenerator(),
            fixture.FakeEmbeddings(),
        )

    async def test_explicit_preference_between_sessions_opt_in_and_disable(self):
        service, cap, coll, _env, _ack, config, pid = await self.prepare()
        memory = await service.put_preference(
            PreferenceWrite(coll, "style", "short answers")
        )
        restarted = CapabilityService(self.factory, cap.context)
        generator = FakeGenerator()
        result = await self.run_cap(restarted, pid, generator)
        self.assertEqual(
            generator.calls[0]["personalization"][0]["value"], "short answers"
        )
        self.assertEqual(result["personalization"]["memory"][0]["id"], memory["id"])
        await cap.create_profile(
            msgspec.structs.replace(config, personalization=Personalization()), pid, 2
        )
        disabled = FakeGenerator()
        result = await self.run_cap(cap, pid, disabled, 2)
        self.assertEqual(disabled.calls[0]["personalization"], [])
        self.assertEqual(result["personalization"]["status"], "disabled")
        current = await service.get_settings()
        await service.configure(
            SettingsInput(
                current["version"],
                enabled=False,
                collections=[coll],
                definitions=[
                    Definition(
                        "style", {"type": "string", "maxLength": 100}, ["I prefer: "]
                    )
                ],
            )
        )
        generator = FakeGenerator()
        await self.run_cap(cap, pid, generator)
        self.assertEqual(generator.calls[0]["personalization"], [])
        self.assertEqual(
            (await service.get_item(UUID(memory["id"])))["value"], "short answers"
        )

    async def test_user_messages_candidates_confirmation_conflict_and_correction(self):
        service, cap, coll, _env, _ack, _config, pid = await self.prepare()
        explicit = await service.put_preference(PreferenceWrite(coll, "style", "short"))
        message = MessageInput(coll, "m1", "I prefer: detailed")
        extracted = await service.message(message)
        candidate = extracted["items"][0]
        self.assertEqual(candidate["state"], "candidate")
        again = await service.message(message)
        self.assertEqual(again["items"][0]["id"], candidate["id"])
        with self.assertRaises(HTTPException) as caught:
            await service.confirm(
                UUID(candidate["id"]), MemoryAction(candidate["version"])
            )
        self.assertEqual(caught.exception.status_code, 409)
        corrected = await service.put_preference(
            PreferenceWrite(
                coll, "style", "detailed", expected_version=explicit["version"]
            )
        )
        self.assertEqual(corrected["origin"], "explicit")
        self.assertGreater(corrected["version"], explicit["version"])
        with self.assertRaises(HTTPException):
            await service.put_preference(
                PreferenceWrite(
                    coll, "style", "stale", expected_version=explicit["version"]
                )
            )
        result = await self.run_cap(cap, pid)
        self.assertEqual(
            result["personalization"]["memory"][0]["version"], corrected["version"]
        )

    async def test_message_edit_delete_and_no_resurrection(self):
        service, _cap, coll, _env, _ack, _config, _pid = await self.prepare()
        data = MessageInput(coll, "user-1", "I prefer: morning")
        source = await service.message(data)
        candidate = source["items"][0]
        confirmed = await service.confirm(
            UUID(candidate["id"]), MemoryAction(candidate["version"])
        )
        updated = await service.edit_message(
            UUID(source["message_id"]), MessageUpdate(1, "I prefer: evening")
        )
        self.assertEqual(updated["items"][0]["state"], "candidate")
        self.assertGreater(updated["items"][0]["version"], confirmed["version"])
        deleted = await service.delete_item(UUID(candidate["id"]))
        self.assertIsNone(deleted["value"])
        self.assertEqual(
            (await service.message(MessageInput(coll, "new-id", "I prefer: evening")))[
                "items"
            ],
            [],
        )
        with self.assertRaises(HTTPException) as caught:
            await service.put_preference(PreferenceWrite(coll, "style", "evening"))
        self.assertEqual(caught.exception.status_code, 410)
        restored = await service.put_preference(
            PreferenceWrite(coll, "style", "night", restore=True)
        )
        self.assertEqual(restored["value"], "night")
        await service.delete_message(UUID(source["message_id"]))
        with self.assertRaises(HTTPException):
            await service.message(MessageInput(coll, "user-1", "I prefer: evening"))

    async def test_fields_are_candidates_source_updates_invalidate(self):
        service, _cap, coll, env, ack, _config, _pid = await self.prepare()
        extracted = await service.extract(ExtractionInput(ack.delivery_id))
        candidate = extracted["items"][0]
        self.assertEqual(candidate["value"], "todo")
        self.assertEqual(
            (await service.extract(ExtractionInput(ack.delivery_id)))["items"][0]["id"],
            candidate["id"],
        )
        await service.confirm(UUID(candidate["id"]), MemoryAction(candidate["version"]))
        next_ack = await service.ingest_one(
            coll,
            msgspec.structs.replace(
                env,
                operation="upsert",
                object_version=2,
                event_id="updated",
                payload={**env.payload, "phase": "done"},
            ),
        )
        invalid = await service.get_item(UUID(candidate["id"]))
        self.assertEqual(invalid["state"], "invalidated")
        self.assertIsNone(invalid["value"])
        newer = (await service.extract(ExtractionInput(next_ack.delivery_id)))["items"][
            0
        ]
        self.assertEqual(newer["id"], candidate["id"])
        self.assertEqual(newer["value"], "done")
        self.assertEqual(newer["state"], "candidate")

    async def test_observation_period_and_file_content_not_personal_facts(self):
        from app.services.evidence_service import EvidenceService

        base, coll, env = await self.seed("learning_sessions")
        ack = await base.ingest_one(coll, env, "events")
        service = MemoryService(self.factory, base.context)
        await service.configure(
            SettingsInput(
                0,
                enabled=True,
                collections=[coll],
                definitions=[Definition("elapsed", {"type": "integer"})],
                field_rules=[
                    FieldRule(
                        coll,
                        1,
                        "/elapsed",
                        "elapsed",
                        kind="observation",
                        source_kind="event",
                    )
                ],
            )
        )
        with self.assertRaises(ContractError):
            await service.extract(ExtractionInput(ack.delivery_id))
        p = Period(
            env.occurred_at - timedelta(seconds=1),
            env.occurred_at + timedelta(seconds=1),
        )
        candidate = (await service.extract(ExtractionInput(ack.delivery_id, p)))[
            "items"
        ][0]
        self.assertEqual(candidate["kind"], "observation")
        self.assertEqual(candidate["provenance"]["unit"], "second")
        self.assertEqual(candidate["provenance"]["sample_count"], 1)
        await service.confirm(UUID(candidate["id"]), MemoryAction(candidate["version"]))
        cap = CapabilityService(self.factory, service.context)
        profile = ProfileInput(
            "observations",
            "analyze",
            "Describe current evidence",
            period=p,
            selections=[
                Selection(
                    coll, 1, ["/subject", "/elapsed"], kind="event", mode="history"
                )
            ],
            personalization=Personalization(
                enabled=True, collections=[coll], keys=["elapsed"], observations=True
            ),
        )
        pid = UUID((await cap.create_profile(profile))["profile_id"])
        generator = FakeGenerator()
        await self.run_cap(cap, pid, generator)
        observed = generator.calls[0]["personalization"][0]
        self.assertEqual(observed["sample_count"], 1)
        self.assertEqual(observed["unit"], "second")
        self.assertIn("not an immutable personal trait", observed["applicability"])
        await service.correct_event(
            ack.delivery_id, msgspec.structs.replace(env, event_id="correction")
        )
        self.assertEqual(
            (await service.get_item(UUID(candidate["id"])))["state"], "invalidated"
        )
        other, _cap, books, env, _ack, _cfg, _pid = await self.prepare()
        file = await EvidenceService(self.factory, other.context).attach(
            books,
            msgspec.structs.replace(env, external_id="book"),
            b"I prefer: fictional narrator",
            "txt",
        )
        with self.assertRaises(ContractError):
            await other.extract(ExtractionInput(file["ack"].delivery_id))

    async def test_feedback_suppresses_repeat_and_acceptance_is_not_execution(self):
        service, cap, _coll, _env, _ack, _config, pid = await self.prepare()
        first = await self.run_cap(cap, pid)
        rid = UUID(first["result_id"])
        feedback = await service.submit(
            rid, FeedbackInput("reject", decision="rejected", claim_index=0)
        )
        replay = await service.submit(
            rid, FeedbackInput("reject", decision="rejected", claim_index=0)
        )
        self.assertEqual(feedback["id"], replay["id"])
        with self.assertRaises(HTTPException):
            await service.submit(rid, FeedbackInput("reject", decision="accepted"))
        second = await self.run_cap(cap, pid)
        self.assertEqual(second["payload"]["claims"], [])
        self.assertEqual(second["personalization"]["suppressed_recommendations"], 1)
        self.assertEqual(second["data_state"], "insufficient_data")
        await service.submit(
            rid,
            FeedbackInput(
                "accept", decision="accepted", claim_index=0, rating="helpful"
            ),
        )
        third = await self.run_cap(cap, pid)
        self.assertTrue(third["payload"]["claims"])
        scores = await service.profile_scores()
        self.assertEqual(scores["items"][0]["accepted"], 1)
        self.assertNotIn("executed", scores["items"][0])

    async def test_feedback_explicit_preference_and_stale_prior_result(self):
        service, cap, coll, _env, _ack, _cfg, pid = await self.prepare()
        pref = await service.put_preference(PreferenceWrite(coll, "style", "brief"))
        result = await self.run_cap(cap, pid)
        await service.submit(
            UUID(result["result_id"]),
            FeedbackInput(
                "correction",
                rating="unhelpful",
                preference=PreferenceWrite(
                    coll, "style", "detailed", expected_version=pref["version"]
                ),
            ),
        )
        self.assertEqual(
            (await cap.result(UUID(result["result_id"])))["freshness"], "stale"
        )
        generator = FakeGenerator()
        await self.run_cap(cap, pid, generator)
        self.assertEqual(generator.calls[0]["personalization"][0]["value"], "detailed")

    async def test_isolation_scopes_source_caps_and_personal_result_rights(self):
        service, cap, coll, _env, _ack, _cfg, pid = await self.prepare()
        pref = await service.put_preference(PreferenceWrite(coll, "style", "private"))
        result = await self.run_cap(cap, pid)
        foreign = MemoryService(
            self.factory,
            AccessContext(service.context.tenant_id, "other", tuple(SCOPES)),
        )
        with self.assertRaises(HTTPException) as caught:
            await foreign.get_item(UUID(pref["id"]))
        self.assertEqual(caught.exception.status_code, 404)
        restricted = MemoryService(
            self.factory,
            AccessContext(service.context.tenant_id, "user", tuple(SCOPES), ()),
        )
        self.assertEqual((await restricted.items())["items"], [])
        with self.assertRaises(HTTPException):
            await restricted.get_item(UUID(pref["id"]))
        scopes = tuple(s for s in SCOPES if s != "memory:read")
        no_memory = CapabilityService(
            self.factory, AccessContext(service.context.tenant_id, "user", scopes)
        )
        generator = FakeGenerator()
        await self.run_cap(no_memory, pid, generator)
        self.assertEqual(generator.calls[0]["personalization"], [])
        with self.assertRaises(HTTPException) as caught:
            await no_memory.result(UUID(result["result_id"]))
        self.assertEqual(caught.exception.status_code, 403)

    async def test_erasure_removes_personalized_results_and_inflight_cannot_save(self):
        service, cap, coll, _env, _ack, _cfg, pid = await self.prepare()
        pref = await service.put_preference(PreferenceWrite(coll, "style", "private"))
        first = await self.run_cap(cap, pid)

        async def erase():
            await service.delete_item(UUID(pref["id"]))

        with self.assertRaises(HTTPException) as caught:
            await self.run_cap(cap, pid, FakeGenerator(callback=erase))
        self.assertEqual(caught.exception.status_code, 409)
        with self.assertRaises(HTTPException):
            await cap.result(UUID(first["result_id"]))
        async with self.factory() as session:
            self.assertEqual(
                await session.scalar(select(func.count()).select_from(AnalysisResult)),
                0,
            )
        await service.erase_scope(collection_id=coll)
        async with self.factory() as session:
            self.assertEqual(
                await session.scalar(select(func.count()).select_from(MemoryItem)), 0
            )

    async def test_optional_memory_read_failure_returns_base_result(self):
        _service, cap, _coll, _env, _ack, _cfg, pid = await self.prepare()
        with patch(
            "app.personalization.context.load_context",
            new=AsyncMock(side_effect=RuntimeError("no memory")),
        ):
            result = await self.run_cap(cap, pid)
        self.assertEqual(result["execution_status"], "succeeded")
        self.assertEqual(result["personalization"]["status"], "unavailable")

        async def database_failure(repo, *args):
            await repo.session.execute(text("SELECT 1 / 0"))

        with patch(
            "app.personalization.context.load_context", side_effect=database_failure
        ):
            recovered = await self.run_cap(cap, pid)
        self.assertEqual(recovered["execution_status"], "succeeded")
        self.assertEqual(recovered["personalization"]["status"], "unavailable")

    async def test_ambiguous_messages_and_forget_all(self):
        service, _cap, coll, _env, _ack, _cfg, _pid = await self.prepare()
        result = await service.message(
            MessageInput(coll, "ambiguous", "I prefer: morning\nI prefer: evening")
        )
        self.assertEqual(result["items"], [])
        self.assertEqual(result["ambiguous_keys"], ["style"])
        await service.put_preference(PreferenceWrite(coll, "style", "morning"))
        await service.forget_all()
        self.assertFalse((await service.get_settings())["enabled"])
        async with self.factory() as session:
            self.assertTrue(
                all(r.value is None for r in await session.scalars(select(MemoryItem)))
            )
            self.assertTrue(
                all(
                    r.content is None
                    for r in await session.scalars(select(MemoryMessage))
                )
            )

    async def test_memory_migration_roundtrip_columns(self):
        module = importlib.import_module(
            "app.db.migrations.versions.e1b7c5d9f263_managed_memory"
        )
        async with self.engine.begin() as connection:
            await connection.execute(
                text(f'SET LOCAL search_path TO "{self.schema}", public')
            )

            def migrate(conn):
                with Operations.context(MigrationContext.configure(conn)):
                    module.downgrade()
                    module.upgrade()
                from sqlalchemy import inspect

                from app.db.models import Base

                inspector = inspect(conn)
                for name in [
                    "memory_settings",
                    "memory_messages",
                    "memory_items",
                    "memory_blocks",
                    "result_feedback",
                    "personalization_uses",
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
        await self.prepare()

    async def test_durable_context_triggers_retries_and_revoked_memory_scope(self):
        from test_postgres_jobs import JobTests

        from app.db.models import AnalysisRun, ServiceAPIKey
        from app.jobs.types import CreateRun, TriggerInput
        from app.services.trigger_service import TriggerService, schedule

        service, coll, _env, cfg, pid = await JobTests.setup_job(self)
        memory = FeedbackService(self.factory, service.context)
        await memory.configure(
            SettingsInput(
                0,
                enabled=True,
                collections=[coll],
                definitions=[Definition("style", {"type": "string", "maxLength": 100})],
            )
        )
        personal = Personalization(enabled=True, collections=[coll], keys=["style"])
        await service.create_profile(
            msgspec.structs.replace(cfg, personalization=personal), pid, 2
        )
        await TriggerService(self.factory, service.context).configure(
            pid, TriggerInput(2, on_change=True, debounce_seconds=1)
        )
        await memory.put_preference(PreferenceWrite(coll, "style", "brief"))
        self.assertEqual(
            await schedule(self.factory), 1
        )  # no source mutation: memory revision is a durable change intent
        async with self.factory.begin() as session:
            pending = await session.scalar(select(AnalysisRun))
            pending.available_at = datetime.now(UTC) - timedelta(seconds=1)
        claim = await service.claim()
        generator = FakeGenerator()
        await service.execute(claim, generator, fixture.FakeEmbeddings())
        self.assertEqual(generator.calls[0]["personalization"][0]["value"], "brief")
        self.assertEqual(await schedule(self.factory), 0)
        manual = await service.enqueue(CreateRun(pid, RunInput(2), "memory-revoke"))

        async def revoke():
            async with self.factory.begin() as session:
                key = await session.get(ServiceAPIKey, service.context.api_key_id)
                key.scopes = [s for s in key.scopes if s != "memory:read"]

        claim = await service.claim()
        await service.execute(
            claim, FakeGenerator(callback=revoke), fixture.FakeEmbeddings()
        )
        self.assertEqual(
            (await service.get_run(UUID(manual["id"])))["status"], "pending"
        )
        async with self.factory.begin() as session:
            row = await session.get(AnalysisRun, UUID(manual["id"]))
            row.available_at = datetime.now(UTC) - timedelta(seconds=1)
        claim = await service.claim()
        generator = FakeGenerator()
        await service.execute(claim, generator, fixture.FakeEmbeddings())
        self.assertEqual(generator.calls[0]["personalization"], [])
        self.assertEqual(
            (await service.get_run(UUID(manual["id"])))["status"], "succeeded"
        )

    async def test_dedicated_memory_collection_feedback_and_scope_erasure(self):
        from app.integration.types import NamedInput

        service, cap, coll, _env, _ack, cfg, pid = await self.prepare()
        source = await service.create_source(NamedInput("preferences"))
        collection = await service.create_collection(
            source.id, NamedInput("preferences")
        )
        current = await service.get_settings()
        await service.configure(
            SettingsInput(
                current["version"],
                enabled=True,
                collections=[coll, collection.id],
                definitions=[Definition("style", {"type": "string", "maxLength": 100})],
            )
        )
        personal = Personalization(
            enabled=True, collections=[collection.id], keys=["style"]
        )
        await cap.create_profile(
            msgspec.structs.replace(cfg, personalization=personal), pid, 2
        )
        result = await self.run_cap(cap, pid, version=2)
        await service.submit(
            UUID(result["result_id"]),
            FeedbackInput(
                "dedicated", preference=PreferenceWrite(collection.id, "style", "brief")
            ),
        )
        generator = FakeGenerator()
        result = await self.run_cap(cap, pid, generator, 2)
        self.assertEqual(
            generator.calls[0]["personalization"][0]["collection_id"],
            str(collection.id),
        )
        await service.erase_scope(collection_id=collection.id)
        with self.assertRaises(HTTPException):
            await cap.result(UUID(result["result_id"]))
        with self.assertRaises(HTTPException):
            await cap.get_profile(pid)

    async def test_tenant_isolation_and_explicit_restore_after_forget(self):
        from app.db.models import Tenant

        service, cap, coll, _env, _ack, _cfg, pid = await self.prepare()
        await service.put_preference(PreferenceWrite(coll, "style", "private"))
        result = await self.run_cap(cap, pid)
        tenant = uuid4()
        async with self.factory.begin() as session:
            session.add(Tenant(id=tenant, name="other"))
        foreign = FeedbackService(
            self.factory, AccessContext(tenant, "user", tuple(SCOPES))
        )
        self.assertEqual((await foreign.items())["items"], [])
        with self.assertRaises(HTTPException):
            await foreign.submit(
                UUID(result["result_id"]), FeedbackInput("foreign", rating="helpful")
            )
        await service.forget_all()
        current = await service.get_settings()
        await service.configure(
            SettingsInput(
                current["version"],
                enabled=True,
                collections=[coll],
                definitions=[
                    Definition(
                        "style", {"type": "string", "maxLength": 100}, ["I prefer: "]
                    )
                ],
            )
        )
        self.assertEqual(
            (
                await service.message(
                    MessageInput(coll, "after-forget", "I prefer: private")
                )
            )["items"],
            [],
        )
        with self.assertRaises(HTTPException):
            await service.put_preference(PreferenceWrite(coll, "style", "private"))
        explicit = await service.put_preference(
            PreferenceWrite(coll, "style", "new", restore=True)
        )
        self.assertEqual(explicit["value"], "new")

    async def test_http_memory_feedback_contract_and_untrusted_origins(self):
        import httpx
        from litestar import Litestar
        from litestar.di import Provide

        from app.api.controllers.capabilities import CapabilityController
        from app.api.controllers.memory import MemoryController
        from app.asgi import create_app
        from app.db.models import ServiceAPIKey
        from app.security.api_keys import hash_api_key

        service, _cap, coll, _env, _ack, _cfg, pid = await self.prepare()
        async with self.factory.begin() as session:
            session.add(
                ServiceAPIKey(
                    tenant_id=service.context.tenant_id,
                    name="memory",
                    key_hash=hash_api_key("memory-test"),
                    scopes=list(SCOPES),
                )
            )

        async def factory():
            return self.factory

        generator = FakeGenerator()
        app = Litestar(
            route_handlers=[MemoryController, CapabilityController],
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
            app.lifespan(),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
                headers={"Authorization": "Bearer memory-test", "X-User-ID": "user"},
            ) as client,
        ):
            self.assertEqual((await client.get("/v1/memory/settings")).status_code, 200)
            value = await client.put(
                "/v1/memory/preferences",
                json={"collection_id": str(coll), "key": "style", "value": "brief"},
            )
            self.assertEqual(value.status_code, 200, value.text)
            self.assertEqual(
                (await client.get("/v1/memory/items?state=confirmed")).status_code, 400
            )
            self.assertEqual(
                (await client.get("/v1/memory/items?status=confirmed")).json()["items"][
                    0
                ]["value"],
                "brief",
            )
            source = await client.post(
                "/v1/memory/messages",
                json={
                    "collection_id": str(coll),
                    "external_id": "assistant",
                    "text": "I prefer: evil",
                    "author": "assistant",
                },
            )
            self.assertEqual(source.status_code, 400)
            candidate = await client.post(
                "/v1/memory/messages",
                json={
                    "collection_id": str(coll),
                    "external_id": "user",
                    "text": "I prefer: brief",
                },
            )
            self.assertEqual(candidate.status_code, 200, candidate.text)
            row = candidate.json()["items"][0]
            confirmed = await client.post(
                "/v1/memory/items/" + row["id"] + "/confirm",
                json={"expected_version": row["version"]},
            )
            self.assertEqual(confirmed.status_code, 200, confirmed.text)
            first = await client.post(f"/v1/profiles/{pid}/runs", json={"version": 1})
            self.assertEqual(first.status_code, 200, first.text)
            rid = first.json()["result_id"]
            feedback = await client.post(
                f"/v1/results/{rid}/feedback",
                json={"request_id": "http", "decision": "rejected", "claim_index": 0},
            )
            self.assertEqual(feedback.status_code, 200, feedback.text)
            again = await client.post(f"/v1/profiles/{pid}/runs", json={"version": 1})
            self.assertEqual(again.status_code, 200, again.text)
            self.assertEqual(again.json()["payload"]["claims"], [])
            self.assertEqual(
                (await client.get("/v1/feedback/profiles")).status_code, 200
            )
            self.assertEqual(
                (await client.get(f"/v1/results/{rid}/feedback")).json()["items"][0][
                    "decision"
                ],
                "rejected",
            )
            self.assertEqual(
                (
                    await client.get(
                        "/v1/memory/items/" + value.json()["id"],
                        headers={"X-User-ID": "other"},
                    )
                ).status_code,
                404,
            )
            self.assertEqual((await client.delete("/v1/memory")).status_code, 200)
            self.assertIn(
                "MemoryView", app.openapi_schema.to_schema()["components"]["schemas"]
            )

    async def test_disabled_memory_ignores_later_feedback_change_triggers(self):
        from test_postgres_jobs import JobTests

        from app.db.models import AnalysisRun
        from app.jobs.types import TriggerInput
        from app.services.trigger_service import TriggerService, schedule

        service, coll, _env, config, pid = await JobTests.setup_job(self)
        memory = FeedbackService(self.factory, service.context)
        definitions = [Definition("style", {"type": "string", "maxLength": 100})]
        await memory.configure(
            SettingsInput(0, enabled=True, collections=[coll], definitions=definitions)
        )
        await service.create_profile(
            msgspec.structs.replace(
                config,
                personalization=Personalization(
                    enabled=True, collections=[coll], keys=["style"]
                ),
            ),
            pid,
            2,
        )
        first = await self.run_cap(service, pid, version=2)
        await TriggerService(self.factory, service.context).configure(
            pid, TriggerInput(2, on_change=True)
        )
        current = await memory.get_settings()
        await memory.configure(
            SettingsInput(
                current["version"],
                enabled=False,
                collections=[coll],
                definitions=definitions,
            )
        )
        self.assertEqual(await schedule(self.factory), 1)
        async with self.factory() as session:
            pending = await session.scalar(select(AnalysisRun))
        await service.cancel(pending.id)
        await memory.submit(
            UUID(first["result_id"]), FeedbackInput("disabled", decision="rejected")
        )
        self.assertEqual(await schedule(self.factory), 0)

    async def test_bundled_memory_schemas_match_runtime(self):
        import json
        from importlib.resources import files

        from jsonschema import Draft202012Validator

        from app.personalization.types import (
            ExtractionInput,
            ExtractionView,
            FeedbackInput,
            FeedbackPage,
            FeedbackView,
            MemoryAction,
            MemoryPage,
            MemoryView,
            MessageCandidates,
            MessageInput,
            MessageUpdate,
            MessageView,
            PreferenceWrite,
            SettingsInput,
            SettingsView,
        )

        contract = json.loads(
            files("app.integration").joinpath("contract.json").read_text()
        )
        for model in [
            SettingsInput,
            PreferenceWrite,
            MessageInput,
            MessageUpdate,
            ExtractionInput,
            MemoryAction,
            FeedbackInput,
            MemoryView,
            MemoryPage,
            SettingsView,
            MessageView,
            MessageCandidates,
            ExtractionView,
            FeedbackView,
            FeedbackPage,
        ]:
            schema = contract["managed_memory"]["schemas"][model.__name__]
            self.assertEqual(schema, msgspec.json.schema(model))
            Draft202012Validator.check_schema(schema)
