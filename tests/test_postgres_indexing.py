"""Real PostgreSQL/pgvector tests. No OpenAI requests or working-database URL."""

import asyncio
import os
import unittest
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateSchema, DropSchema

from app.db.models import Base, Document, DocumentChunk, Tenant
from app.repositories.documents import (
    DocumentReindexConflictError,
    DocumentRepository,
    IndexingAttemptConflictError,
)
from app.security.context import AccessContext
from app.services.document_service import DocumentIndexingError, DocumentService
from app.services.embedding_service import EmbeddingResult, EmbeddingService

TEST_URL = os.environ.get("PAIAGENT_TEST_DATABASE_URL")


class FakeEmbeddings(EmbeddingService):
    def __init__(self, broken=False):
        self.broken = broken

    async def embed_texts(self, texts):
        vectors = [[float(i + 1)] + [0.0] * 1535 for i in range(len(texts))]
        if self.broken:
            vectors[-1] = [1.0, 2.0]  # Forces a vector serialization/storage failure.
        return EmbeddingResult(vectors, "text-embedding-3-small", 1536, 12)


@unittest.skipUnless(
    TEST_URL, "Set PAIAGENT_TEST_DATABASE_URL or use run_postgres_tests.py"
)
class PostgreSQLIndexingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        url = make_url(TEST_URL)
        if url.get_backend_name() != "postgresql" or not (url.database or "").endswith(
            "_test"
        ):
            raise ValueError("Use a PostgreSQL database whose name ends with '_test'.")
        self.schema = "paiagent_test_" + uuid.uuid4().hex
        self.engine = create_async_engine(
            url.set(drivername="postgresql+asyncpg"),
            isolation_level="READ COMMITTED",
            execution_options={"schema_translate_map": {None: self.schema}},
            connect_args={
                "server_settings": {
                    "search_path": "public",
                    "statement_timeout": "10000",
                    "lock_timeout": "5000",
                }
            },
        )
        self.addAsyncCleanup(self.engine.dispose)
        async with self.engine.begin() as connection:
            present = await connection.scalar(
                text(
                    "SELECT EXISTS (SELECT 1 FROM pg_extension e "
                    "JOIN pg_namespace n ON n.oid=e.extnamespace "
                    "WHERE e.extname='vector' AND n.nspname='public')"
                )
            )
            if not present:
                raise RuntimeError(
                    "Install the vector extension in public in the test database."
                )
            await connection.execute(CreateSchema(self.schema))
            await connection.run_sync(Base.metadata.create_all)
        self.addAsyncCleanup(self.drop_schema)
        self.factory = async_sessionmaker(self.engine, expire_on_commit=False)
        self.context = AccessContext(uuid.uuid4(), "user")
        async with self.factory.begin() as session:
            session.add(Tenant(id=self.context.tenant_id, name="Test"))

    async def drop_schema(self):
        async with self.engine.begin() as connection:
            await connection.execute(DropSchema(self.schema, cascade=True))

    async def seed_expired(self):
        document_id, old_id = uuid.uuid4(), uuid.uuid4()
        async with self.factory.begin() as session:
            session.add(
                Document(
                    id=document_id,
                    tenant_id=self.context.tenant_id,
                    user_id="user",
                    title="test.txt",
                    content_type="txt",
                    status="processing",
                    indexing_attempt_id=old_id,
                    indexing_expires_at=datetime.now(UTC) - timedelta(minutes=5),
                )
            )
            await session.flush()
            session.add_all(
                [
                    DocumentChunk(document_id=document_id, chunk_index=i, content=value)
                    for i, value in enumerate(("First", "Second"))
                ]
            )
        return document_id, old_id

    async def stored(self, document_id):
        async with self.factory() as session:
            document = await session.get(Document, document_id)
            chunks = list(
                await session.scalars(
                    select(DocumentChunk)
                    .where(DocumentChunk.document_id == document_id)
                    .order_by(DocumentChunk.chunk_index)
                )
            )
            return document, chunks

    async def wait_for_lock(self, waiting_pid, holding_pid, task):
        async with asyncio.timeout(3):
            async with self.engine.connect() as observer:
                while True:
                    blockers = await observer.scalar(
                        text("SELECT pg_blocking_pids(:pid)"), {"pid": waiting_pid}
                    )
                    if holding_pid in blockers:
                        return
                    if task.done():
                        self.fail(
                            "Second claim finished without waiting for the first transaction."
                        )
                    await asyncio.sleep(0.02)

    async def race_claims(self, rollback_first):
        document_id, _ = await self.seed_expired()
        first_id, second_id = uuid.uuid4(), uuid.uuid4()
        pid_future = asyncio.get_running_loop().create_future()

        async def second_claim():
            try:
                async with self.factory.begin() as session:
                    pid_future.set_result(
                        await session.scalar(text("SELECT pg_backend_pid()"))
                    )
                    await DocumentRepository(session).claim_for_reindex(
                        context=self.context,
                        document_id=document_id,
                        attempt_id=second_id,
                    )
                return "claimed"
            except DocumentReindexConflictError:
                return "conflict"

        task = None
        try:
            async with self.factory() as first:
                transaction = await first.begin()
                first_pid = await first.scalar(text("SELECT pg_backend_pid()"))
                await DocumentRepository(first).claim_for_reindex(
                    context=self.context,
                    document_id=document_id,
                    attempt_id=first_id,
                )
                task = asyncio.create_task(second_claim())
                second_pid = await asyncio.wait_for(
                    asyncio.shield(pid_future), timeout=3
                )
                self.assertNotEqual(first_pid, second_pid)
                await self.wait_for_lock(second_pid, first_pid, task)
                if rollback_first:
                    await transaction.rollback()
                else:
                    await transaction.commit()
            outcome = await asyncio.wait_for(task, timeout=5)
            self.assertEqual(outcome, "claimed" if rollback_first else "conflict")
            document, _ = await self.stored(document_id)
            self.assertEqual(
                document.indexing_attempt_id, second_id if rollback_first else first_id
            )
            self.assertEqual(document.status, "processing")
            self.assertIsNotNone(document.indexing_expires_at.tzinfo)
        finally:
            if task is not None:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_second_claim_waits_then_rejects_after_first_commit(self):
        await self.race_claims(rollback_first=False)

    async def test_second_claim_succeeds_after_first_rollback(self):
        await self.race_claims(rollback_first=True)

    async def test_service_persists_real_pgvector_and_ready_together(self):
        service = DocumentService(self.factory, FakeEmbeddings())
        result = await service.ingest(
            context=self.context,
            filename="test.txt",
            raw_bytes=b"a" * 2500,
        )
        document, chunks = await self.stored(result.document_id)
        self.assertGreater(len(chunks), 1)
        self.assertEqual(result.chunks_created, len(chunks))
        self.assertEqual(document.status, "ready")
        self.assertIsNone(document.indexing_expires_at)
        self.assertIsNotNone(document.indexed_at.tzinfo)
        self.assertEqual(document.embedding_model, "text-embedding-3-small")
        self.assertTrue(all(len(chunk.embedding) == 1536 for chunk in chunks))
        self.assertEqual(
            [float(c.embedding[0]) for c in chunks], list(range(1, len(chunks) + 1))
        )

    async def test_invalid_vector_rolls_back_vectors_and_ready(self):
        service = DocumentService(self.factory, FakeEmbeddings(broken=True))
        with self.assertRaises(DocumentIndexingError) as caught:
            await service.ingest(
                context=self.context,
                filename="test.txt",
                raw_bytes=b"a" * 2500,
            )
        document, chunks = await self.stored(caught.exception.document_id)
        self.assertEqual(caught.exception.code, "index_storage_error")
        self.assertEqual(document.status, "failed")
        self.assertIsNone(document.indexed_at)
        self.assertIsNone(document.embedding_model)
        self.assertIsNone(document.indexing_expires_at)
        self.assertTrue(all(chunk.embedding is None for chunk in chunks))

    async def test_recovery_rejects_old_attempt_and_preserves_owner_filters(self):
        document_id, old_id = await self.seed_expired()
        new_id = uuid.uuid4()
        for context in (
            AccessContext(uuid.uuid4(), "user"),
            AccessContext(self.context.tenant_id, "other"),
        ):
            with self.assertRaises(LookupError):
                async with self.factory.begin() as session:
                    await DocumentRepository(session).claim_for_reindex(
                        context=context,
                        document_id=document_id,
                        attempt_id=new_id,
                    )
        async with self.factory.begin() as session:
            await DocumentRepository(session).claim_for_reindex(
                context=self.context,
                document_id=document_id,
                attempt_id=new_id,
            )
        with self.assertRaises(IndexingAttemptConflictError):
            async with self.factory.begin() as session:
                await DocumentRepository(session).save_embeddings(
                    context=self.context,
                    document_id=document_id,
                    attempt_id=old_id,
                    vectors=[[1.0] * 1536] * 2,
                    model="text-embedding-3-small",
                    dimensions=1536,
                    total_tokens=1,
                )
        async with self.factory.begin() as session:
            changed = await DocumentRepository(session).mark_failed(
                context=self.context,
                document_id=document_id,
                attempt_id=old_id,
                error_code="late_failure",
            )
            self.assertFalse(changed)
        document, chunks = await self.stored(document_id)
        self.assertEqual(document.indexing_attempt_id, new_id)
        self.assertEqual(document.status, "processing")
        self.assertTrue(all(c.embedding is None for c in chunks))


if __name__ == "__main__":
    unittest.main()
