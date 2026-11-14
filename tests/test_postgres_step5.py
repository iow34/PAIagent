"""Real PostgreSQL, real HTTP/auth/LangGraph; deterministic model substitutes."""

import asyncio
import importlib
import unittest
from contextlib import ExitStack, asynccontextmanager
from unittest.mock import patch
from uuid import uuid4

import httpx
import test_postgres_indexing as postgres_fixture
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import func, select, text

from app.asgi import create_app
from app.config.settings import Settings
from app.db.models import (
    ConversationMessage,
    ConversationSession,
    Document,
    DocumentChunk,
    DocumentKnowledgeRecord,
    ServiceAPIKey,
    Tenant,
)
from app.db.postgres import DatabaseResources
from app.repositories.api_keys import APIKeysRepository
from app.repositories.knowledge import KnowledgeRepository, KnowledgeSourceConflictError
from app.security.api_keys import hash_api_key
from app.services.embedding_service import EmbeddingResult
from app.services.generation_service import GeneratedAnswer
from app.services.knowledge_context import prepare_knowledge_context
from app.services.knowledge_generation import (
    GeneratedDocumentKnowledge,
    GeneratedKnowledgeFact,
    KnowledgeResponseError,
)
from app.services.knowledge_validation import validate_knowledge


@unittest.skipUnless(
    postgres_fixture.TEST_URL,
    "Use tests/run_postgres_tests.py --test-file test_postgres_step5.py",
)
class PostgreSQLStep5Tests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = postgres_fixture.PostgreSQLIndexingTests.asyncSetUp
    drop_schema = postgres_fixture.PostgreSQLIndexingTests.drop_schema

    @asynccontextmanager
    async def application(self):
        case = self
        self.model_calls = []
        self.knowledge_error = None
        self.generation_barrier = None
        self.barrier_arrivals = 0

        class Embeddings:
            def __init__(self, client, settings):
                pass

            async def embed_texts(self, texts):
                if case.generation_barrier is None:
                    case.assertEqual(case.engine.pool.checkedout(), 0)
                return EmbeddingResult(
                    [[1.0] + [0.0] * 1535 for _ in texts],
                    "text-embedding-3-small",
                    1536,
                    1,
                )

        class Answers:
            def __init__(self, client, *, model):
                pass

            async def prepare_search_query(self, *, question, history):
                if case.generation_barrier is None:
                    case.assertEqual(case.engine.pool.checkedout(), 0)
                case.model_calls.append(("rewrite", history))
                return "повернення товару"

            async def generate(self, *, question, history, context):
                if case.generation_barrier is None:
                    case.assertEqual(case.engine.pool.checkedout(), 0)
                case.model_calls.append(("answer", history))
                if case.generation_barrier is not None:
                    case.barrier_arrivals += 1
                    if case.barrier_arrivals == 2:
                        case.generation_barrier.set()
                    await asyncio.wait_for(case.generation_barrier.wait(), timeout=5)
                return GeneratedAnswer(
                    answer="Повернення протягом 14 днів [S1].",
                    source_ids=["S1"],
                    status="answered",
                )

        class Knowledge:
            def __init__(self, client, *, model):
                self.model = model

            async def generate(self, *, context):
                if case.generation_barrier is None:
                    case.assertEqual(case.engine.pool.checkedout(), 0)
                case.model_calls.append(("knowledge", context))
                if case.knowledge_error:
                    raise case.knowledge_error
                return case.generated_knowledge()

        @asynccontextmanager
        async def database(url):
            yield DatabaseResources(case.engine, case.factory)

        def settings() -> Settings:
            return Settings(database_url="unused", openai_api_key="test-not-real")

        async with self.factory.begin() as session:
            other_tenant = Tenant(id=uuid4(), name="Other")
            session.add(other_tenant)
            await session.flush()
            session.add_all(
                [
                    ServiceAPIKey(
                        tenant_id=self.context.tenant_id,
                        name="Test",
                        key_hash=hash_api_key("first-key"),
                    ),
                    ServiceAPIKey(
                        tenant_id=other_tenant.id,
                        name="Other",
                        key_hash=hash_api_key("other-key"),
                    ),
                ]
            )
        self.headers = {
            "Authorization": "Bearer first-key",
            "X-User-ID": self.context.user_id,
        }
        with ExitStack() as stack:
            for name, value in (
                ("open_database", database),
                ("get_settings", settings),
                ("EmbeddingService", Embeddings),
                ("GenerationService", Answers),
                ("KnowledgeGenerationService", Knowledge),
            ):
                stack.enter_context(patch("app.asgi." + name, value))
            self.app = create_app()
            async with self.app.lifespan():
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=self.app),
                    base_url="http://test",
                    headers=self.headers,
                ) as client:
                    yield client

    def generated_knowledge(self):
        return GeneratedDocumentKnowledge(
            summary="Правила повернення",
            summary_source_ids=["K1"],
            topics=["повернення"],
            facts=[
                GeneratedKnowledgeFact(
                    text="Повернення протягом 14 днів.", source_ids=["K1"]
                )
            ],
        )

    async def seed(self, *, status="ready", chunks=1):
        document_id = uuid4()
        async with self.factory.begin() as session:
            session.add(
                Document(
                    id=document_id,
                    tenant_id=self.context.tenant_id,
                    user_id=self.context.user_id,
                    title="rules.txt",
                    content_type="txt",
                    status=status,
                    embedding_model="text-embedding-3-small",
                    embedding_dimensions=1536,
                )
            )
            await session.flush()
            session.add_all(
                [
                    DocumentChunk(
                        id=uuid4(),
                        document_id=document_id,
                        chunk_index=i,
                        content="Повернення товару можливе протягом 14 днів.",
                        embedding=[1.0] + [0.0] * 1535,
                    )
                    for i in range(chunks)
                ]
            )
        return document_id

    async def test_query_http_history_sources_and_owner_isolation(self):
        document_id = await self.seed()
        async with self.application() as client:
            response = await client.post("/v1/query", json={"message": "Які правила?"})
            self.assertEqual(response.status_code, 200, response.text)
            first = response.json()
            self.assertEqual(first["sources"][0]["document_id"], str(document_id))
            response = await client.post(
                "/v1/query",
                json={"message": "А винятки?", "session_id": first["session_id"]},
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["session_id"], first["session_id"])
            self.assertNotEqual(response.json()["request_id"], first["request_id"])
            self.assertEqual(self.model_calls[-1][1].message_count, 2)
            before = len(self.model_calls)
            for headers in (
                {"X-User-ID": "other"},
                {"Authorization": "Bearer other-key"},
            ):
                result = await client.post(
                    "/v1/query",
                    json={"message": "Чуже", "session_id": first["session_id"]},
                    headers=headers,
                )
                self.assertEqual(result.status_code, 404, result.text)
            self.assertEqual(len(self.model_calls), before)
            async with self.factory.begin() as session:
                messages = list(
                    await session.scalars(
                        select(ConversationMessage).order_by(
                            ConversationMessage.position
                        )
                    )
                )
                self.assertEqual([m.position for m in messages], [0, 1, 2, 3])
                self.assertEqual(
                    messages[1].sources[0]["document_id"], str(document_id)
                )

    async def test_no_sources_and_invalid_query_input(self):
        async with self.application() as client:
            result = await client.post("/v1/query", json={"message": "Питання"})
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(result.json()["status"], "insufficient_information")
            self.assertEqual(result.json()["sources"], [])
            self.assertFalse(any(name == "answer" for name, _ in self.model_calls))
            for payload in (
                {"message": " "},
                {"message": "x" * 2001},
                {"message": "x\x00"},
                {"message": "x", "tenant_id": str(uuid4())},
            ):
                self.assertEqual(
                    (await client.post("/v1/query", json=payload)).status_code, 400
                )

    async def test_concurrent_followups_reject_stale_history(self):
        await self.seed()
        async with self.application() as client:
            first = (await client.post("/v1/query", json={"message": "Початок"})).json()
            self.generation_barrier = asyncio.Event()
            responses = await asyncio.gather(
                *[
                    client.post(
                        "/v1/query",
                        json={
                            "message": f"Далі {i}",
                            "session_id": first["session_id"],
                        },
                    )
                    for i in range(2)
                ]
            )
            self.assertEqual(sorted(r.status_code for r in responses), [200, 409])
            async with self.factory.begin() as session:
                self.assertEqual(
                    await session.scalar(
                        select(func.count()).select_from(ConversationMessage)
                    ),
                    4,
                )

    async def test_knowledge_http_persistence_upsert_and_failures(self):
        document_id = await self.seed(chunks=13)
        path = f"/v1/documents/{document_id}/knowledge"
        async with self.application() as client:
            self.assertEqual((await client.get(path)).status_code, 404)
            response = await client.post(path)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["coverage"], "partial")
            self.assertEqual((await client.get(path)).json(), response.json())
            before = len(self.model_calls)
            for headers in (
                {"X-User-ID": "other"},
                {"Authorization": "Bearer other-key"},
            ):
                self.assertEqual(
                    (await client.get(path, headers=headers)).status_code, 404
                )
                self.assertEqual(
                    (await client.post(path, headers=headers)).status_code, 404
                )
            self.assertEqual(len(self.model_calls), before)
            self.assertEqual((await client.post(path)).status_code, 200)
            for error, code in (
                (KnowledgeResponseError("private-data"), 502),
                (TimeoutError("private-data"), 504),
            ):
                self.knowledge_error = error
                failed = await client.post(path)
                self.assertEqual(failed.status_code, code, failed.text)
                self.assertNotIn("private-data", failed.text)
                self.assertEqual((await client.get(path)).json(), response.json())
            async with self.factory.begin() as session:
                self.assertEqual(
                    await session.scalar(
                        select(func.count()).select_from(DocumentKnowledgeRecord)
                    ),
                    1,
                )
                self.assertEqual(
                    (await session.get(Document, document_id)).status, "ready"
                )

    async def test_nonready_document_and_missing_key_never_generate(self):
        document_id = await self.seed(status="processing")
        async with self.application() as client:
            path = f"/v1/documents/{document_id}/knowledge"
            self.assertEqual((await client.post(path)).status_code, 409)
            self.assertEqual(
                (await client.post(path, headers={"Authorization": ""})).status_code,
                401,
            )
            self.assertEqual(self.model_calls, [])

    async def test_deadline_includes_authentication(self):
        async def slow_lookup(*args, **kwargs):
            await asyncio.sleep(1)

        async with self.application() as client:
            with (
                patch("app.api.execution_timeout.REQUEST_TIMEOUT_SECONDS", 0.02),
                patch.object(APIKeysRepository, "get_active_key", slow_lookup),
            ):
                for path, payload in (
                    ("/v1/query", {"message": "Питання"}),
                    (f"/v1/documents/{uuid4()}/knowledge", None),
                ):
                    response = await client.post(path, json=payload)
                    self.assertEqual(response.status_code, 504, response.text)
            self.assertEqual(self.model_calls, [])
            async with self.factory.begin() as session:
                self.assertEqual(
                    await session.scalar(
                        select(func.count()).select_from(ConversationSession)
                    ),
                    0,
                )

    async def test_changed_sources_reject_save_and_cascade_delete(self):
        document_id = await self.seed()
        async with self.factory.begin() as session:
            chunks = await KnowledgeRepository(session).list_document_chunks(
                context=self.context, document_id=document_id
            )
            prepared = prepare_knowledge_context(document_id=document_id, chunks=chunks)
        knowledge = validate_knowledge(
            generated=self.generated_knowledge(), context=prepared
        )
        async with self.factory.begin() as session:
            repo = KnowledgeRepository(session)
            await repo.save(
                context=self.context, knowledge=knowledge, generation_model="test"
            )
        async with self.factory.begin() as session:
            chunk = await session.get(DocumentChunk, knowledge.sources[0].chunk_id)
            chunk.content = "Changed"
        with self.assertRaises(KnowledgeSourceConflictError):
            async with self.factory.begin() as session:
                await KnowledgeRepository(session).save(
                    context=self.context, knowledge=knowledge, generation_model="test"
                )
        async with self.factory.begin() as session:
            await session.delete(await session.get(Document, document_id))
        async with self.factory.begin() as session:
            self.assertEqual(
                await session.scalar(
                    select(func.count()).select_from(DocumentKnowledgeRecord)
                ),
                0,
            )

    async def test_step5_migrations_upgrade_and_downgrade(self):
        names = (
            "d9e3f6a2b845_add_conversation_tables",
            "e2f4b8c6a901_add_document_knowledge",
        )
        modules = [
            importlib.import_module("app.db.migrations.versions." + name)
            for name in names
        ]
        async with self.engine.begin() as connection:
            await connection.execute(
                text(f'SET LOCAL search_path TO "{self.schema}", public')
            )

            def migrate(sync_connection):
                with Operations.context(MigrationContext.configure(sync_connection)):
                    for module in reversed(modules):
                        module.downgrade()
                    for module in modules:
                        module.upgrade()

            await connection.run_sync(migrate)
        async with self.application() as client:
            document_id = await self.seed()
            self.assertEqual(
                (
                    await client.post(f"/v1/documents/{document_id}/knowledge")
                ).status_code,
                200,
            )
            self.assertEqual(
                (
                    await client.post("/v1/query", json={"message": "Питання"})
                ).status_code,
                200,
            )

    async def test_openapi_routes_and_named_dependencies(self):
        async with self.application():
            schema = self.app.openapi_schema.to_schema()
            path = schema["paths"]["/v1/documents/{document_id}/knowledge"]
            self.assertIn("get", path)
            self.assertIn("post", path)
            self.assertNotIn("requestBody", path["post"])
            self.assertEqual(path["post"]["security"], [{"ServiceAPIKey": []}])
            names = {p["name"] for p in path["post"]["parameters"]}
            self.assertEqual(names, {"document_id", "X-User-ID"})


if __name__ == "__main__":
    unittest.main()
