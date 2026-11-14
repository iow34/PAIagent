"""Isolated SQLite transaction tests with an async adapter and fake embeddings.

These do not replace integration tests against PostgreSQL/pgvector.
"""

import asyncio
import unittest
import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import httpx2
from litestar.testing import TestClient
from openai import APIConnectionError
from sqlalchemy import create_engine, event, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.asgi import create_app
from app.config.settings import Settings
from app.db.models import Base, Document, DocumentChunk, ServiceAPIKey, Tenant
from app.db.postgres import DatabaseResources
from app.ingestion.validation import InvalidDocumentError
from app.repositories.documents import DocumentRepository
from app.security.api_keys import hash_api_key
from app.security.context import AccessContext
from app.services.document_service import DocumentIndexingError, DocumentService
from app.services.embedding_service import (
    EmbeddingResponseError,
    EmbeddingResult,
    EmbeddingService,
)


class AsyncSessionAdapter:
    def __init__(self, session):
        self.session = session

    def get_bind(self):
        return self.session.get_bind()

    def add(self, obj):
        self.session.add(obj)

    def add_all(self, objects):
        self.session.add_all(objects)

    async def flush(self):
        self.session.flush()

    async def scalar(self, statement):
        return self.session.scalar(statement)

    async def scalars(self, statement):
        return self.session.scalars(statement)


class SQLiteFactory(async_sessionmaker):
    def __init__(self, engine):
        super().__init__()
        self.engine = engine
        self.active = 0
        self.transactions = 0
        self.fail_transactions = set()

    @asynccontextmanager
    async def begin(self):
        self.transactions += 1
        number = self.transactions
        self.active += 1
        try:
            with Session(self.engine, expire_on_commit=False) as session:
                with session.begin():
                    yield AsyncSessionAdapter(session)
                    if number in self.fail_transactions:
                        raise SQLAlchemyError("Simulated database failure")
        finally:
            self.active -= 1


class FakeEmbeddingService(EmbeddingService):
    def __init__(self, factory):
        self.factory = factory
        self.calls = 0
        self.error = None
        self.drop_last = False

    async def embed_texts(self, texts):
        assert self.factory.active == 0, "Network call inside a database transaction"
        with Session(self.factory.engine) as session:
            documents = list(
                session.scalars(select(Document).where(Document.status == "processing"))
            )
            assert documents, "Texts must be committed before the API call"
        self.calls += 1
        if self.error is not None:
            raise self.error
        vectors = [[float(i + 1)] + [0.0] * 1535 for i in range(len(texts))]
        if self.drop_last:
            vectors = vectors[:-1]
        return EmbeddingResult(vectors, "text-embedding-3-small", 1536, 12)


class DocumentIndexingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite:///:memory:",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )

        @event.listens_for(self.engine, "connect")
        def foreign_keys(connection, record):
            connection.execute("PRAGMA foreign_keys=ON")

        Base.metadata.create_all(
            self.engine,
            tables=[
                Base.metadata.tables[name]
                for name in (
                    "tenants",
                    "service_api_keys",
                    "documents",
                    "document_chunks",
                )
            ],
        )
        self.tenant_id = uuid.uuid4()
        with Session(self.engine) as session, session.begin():
            session.add(Tenant(id=self.tenant_id, name="Test"))
            session.flush()
            session.add(
                ServiceAPIKey(
                    tenant_id=self.tenant_id,
                    name="Test",
                    key_hash=hash_api_key("test-key"),
                )
            )
        self.context = AccessContext(self.tenant_id, "same-user")
        self.factory = SQLiteFactory(self.engine)
        self.embedding = FakeEmbeddingService(self.factory)
        self.service = DocumentService(self.factory, self.embedding)

    def tearDown(self):
        self.engine.dispose()

    async def ingest(self, data=b"First paragraph.\n\nSecond paragraph."):
        return await self.service.ingest(
            context=self.context, filename="file.txt", raw_bytes=data
        )

    def stored(self):
        with Session(self.engine) as session:
            return list(session.scalars(select(Document))), list(
                session.scalars(
                    select(DocumentChunk).order_by(DocumentChunk.chunk_index)
                )
            )

    async def test_success_with_vectors_metadata_and_no_transaction_during_api(self):
        result = await self.ingest(b"a" * 2500)
        documents, chunks = self.stored()
        self.assertEqual(result.chunks_created, 3)
        self.assertEqual(documents[0].status, "ready")
        self.assertEqual(documents[0].embedding_model, "text-embedding-3-small")
        self.assertEqual(documents[0].embedding_dimensions, 1536)
        self.assertEqual(documents[0].embedding_tokens, 12)
        self.assertIsNotNone(documents[0].indexed_at)
        self.assertIsNone(documents[0].indexing_error_code)
        self.assertEqual([float(c.embedding[0]) for c in chunks], [1, 2, 3])
        self.assertEqual(self.factory.transactions, 2)
        self.assertEqual(self.factory.active, 0)

    async def test_provider_timeout_and_response_errors_keep_source_texts(self):
        cases = [
            (
                APIConnectionError(
                    request=httpx2.Request("POST", "https://example.invalid")
                ),
                "embedding_provider_error",
            ),
            (TimeoutError(), "embedding_timeout"),
            (EmbeddingResponseError("bad"), "invalid_embedding_response"),
        ]
        for error, code in cases:
            self.embedding.error = error
            with self.assertRaises(DocumentIndexingError) as caught:
                await self.ingest()
            self.assertEqual(caught.exception.code, code)
            with Session(self.engine) as session:
                document = session.get(Document, caught.exception.document_id)
                chunks = list(
                    session.scalars(
                        select(DocumentChunk).where(
                            DocumentChunk.document_id == document.id
                        )
                    )
                )
                self.assertEqual(document.status, "failed")
                self.assertEqual(document.indexing_error_code, code)
                self.assertIsNone(document.indexed_at)
                self.assertTrue(chunks)
                self.assertTrue(all(c.embedding is None for c in chunks))

    async def test_storage_failure_rolls_back_all_vectors_and_ready(self):
        self.factory.fail_transactions = {2}
        with self.assertRaises(DocumentIndexingError) as caught:
            await self.ingest(b"a" * 2500)
        documents, chunks = self.stored()
        self.assertEqual(caught.exception.code, "index_storage_error")
        self.assertEqual(documents[0].status, "failed")
        self.assertIsNone(documents[0].embedding_model)
        self.assertIsNone(documents[0].indexed_at)
        self.assertEqual(len(chunks), 3)
        self.assertTrue(all(c.embedding is None for c in chunks))

    async def test_initial_storage_failure_never_calls_embeddings(self):
        self.factory.fail_transactions = {1}
        with self.assertRaises(SQLAlchemyError):
            await self.ingest()
        self.assertEqual(self.embedding.calls, 0)
        self.assertEqual(self.stored(), ([], []))

    async def test_invalid_file_creates_nothing(self):
        with self.assertRaises(InvalidDocumentError):
            await self.ingest(b"")
        self.assertEqual(self.factory.transactions, 0)
        self.assertEqual(self.embedding.calls, 0)

    async def test_wrong_vector_count_never_marks_ready(self):
        self.embedding.drop_last = True
        with self.assertRaises(DocumentIndexingError):
            await self.ingest()
        self.assertEqual(self.stored()[0][0].status, "failed")
        self.assertTrue(all(c.embedding is None for c in self.stored()[1]))

    async def test_cancellation_is_reraised_and_status_is_failed(self):
        self.embedding.error = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.ingest()
        self.assertEqual(self.stored()[0][0].indexing_error_code, "indexing_cancelled")
        self.assertEqual(self.stored()[0][0].status, "failed")

    async def test_unavailable_failure_storage_keeps_original_error(self):
        self.embedding.error = TimeoutError()
        self.factory.fail_transactions = {2}
        with self.assertLogs("app.services.document_service", level="ERROR"):
            with self.assertRaises(DocumentIndexingError) as caught:
                await self.ingest()
        self.assertEqual(caught.exception.code, "embedding_timeout")
        self.assertEqual(self.stored()[0][0].status, "processing")

    async def test_owner_filters_and_ready_protection(self):
        result = await self.ingest()
        attempt_id = self.stored()[0][0].indexing_attempt_id
        for context in [
            AccessContext(uuid.uuid4(), self.context.user_id),
            AccessContext(self.tenant_id, "different-user"),
        ]:
            with self.subTest(context=context):
                with self.assertRaises(LookupError):
                    async with self.factory.begin() as session:
                        await DocumentRepository(session).save_embeddings(
                            attempt_id=attempt_id,
                            context=context,
                            document_id=result.document_id,
                            vectors=[[1.0] * 1536],
                            model="text-embedding-3-small",
                            dimensions=1536,
                            total_tokens=1,
                        )
                async with self.factory.begin() as session:
                    changed = await DocumentRepository(session).mark_failed(
                        attempt_id=attempt_id,
                        context=context,
                        document_id=result.document_id,
                        error_code="test",
                    )
                    self.assertFalse(changed)
        async with self.factory.begin() as session:
            changed = await DocumentRepository(session).mark_failed(
                attempt_id=attempt_id,
                context=self.context,
                document_id=result.document_id,
                error_code="test",
            )
            self.assertFalse(changed)
        self.assertEqual(self.stored()[0][0].status, "ready")

    async def test_http_success_and_sanitized_failure(self):

        @asynccontextmanager
        async def fake_database(url):
            yield DatabaseResources(engine=self.engine, session_factory=self.factory)

        @asynccontextmanager
        async def fake_openai(app):
            app.state.embedding_service = self.embedding
            app.state.generation_service = AsyncMock()
            app.state.knowledge_generation_service = AsyncMock()
            yield

        def settings() -> Settings:
            return Settings(database_url="test", openai_api_key="test")

        with (
            patch("app.asgi.open_database", fake_database),
            patch("app.asgi.openai_lifespan", fake_openai),
            patch("app.asgi.get_settings", settings),
        ):
            with TestClient(create_app()) as client:
                headers = {
                    "Authorization": "Bearer test-key",
                    "X-User-ID": self.context.user_id,
                }
                response = client.post(
                    "/v1/documents",
                    files={"data": ("file.txt", b"Hello")},
                    headers=headers,
                )
                self.assertEqual(response.status_code, 201, response.text)
                self.embedding.error = TimeoutError("private provider details")
                response = client.post(
                    "/v1/documents",
                    files={"data": ("file.txt", b"Hello")},
                    headers=headers,
                )
                self.assertEqual(response.status_code, 504, response.text)
                self.assertEqual(response.json()["extra"]["code"], "embedding_timeout")
                self.assertNotIn("private provider details", response.text)
                self.assertTrue(uuid.UUID(response.json()["extra"]["document_id"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
