"""Service tests with SQLite and fake embeddings; no OpenAI requests.

These tests do not verify PostgreSQL concurrency or semantic search quality.
"""

import asyncio
import unittest
import uuid
from unittest.mock import AsyncMock, patch

from sqlalchemy import create_engine, event, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from test_document_indexing import FakeEmbeddingService, SQLiteFactory

from app.db.models import Base, Document, DocumentChunk, Tenant
from app.repositories.documents import (
    DocumentReindexConflictError,
    InvalidStoredChunksError,
)
from app.security.context import AccessContext
from app.services.document_service import DocumentIndexingError, DocumentService


class ReindexServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite:///:memory:",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )

        @event.listens_for(self.engine, "connect")
        def enable_foreign_keys(connection, record):
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
        self.context = AccessContext(uuid.uuid4(), "user")
        with Session(self.engine) as session, session.begin():
            session.add(Tenant(id=self.context.tenant_id, name="Test"))
        self.factory = SQLiteFactory(self.engine)
        self.embedding = FakeEmbeddingService(self.factory)
        self.service = DocumentService(self.factory, self.embedding)

    def tearDown(self):
        self.engine.dispose()

    def seed(self, status="pending", texts=("First", "Second")):
        document_id = uuid.uuid4()
        with Session(self.engine) as session, session.begin():
            session.add(
                Document(
                    id=document_id,
                    tenant_id=self.context.tenant_id,
                    user_id=self.context.user_id,
                    title="file.txt",
                    content_type="txt",
                    status=status,
                    indexing_error_code="earlier_error" if status == "failed" else None,
                )
            )
            session.flush()
            # Insert backwards to ensure the service receives ordered texts.
            session.add_all(
                [
                    DocumentChunk(
                        document_id=document_id, chunk_index=index, content=text
                    )
                    for index, text in reversed(list(enumerate(texts)))
                ]
            )
        return document_id

    def stored(self, document_id):
        with Session(self.engine) as session:
            document = session.get(Document, document_id)
            chunks = list(
                session.scalars(
                    select(DocumentChunk)
                    .where(DocumentChunk.document_id == document_id)
                    .order_by(DocumentChunk.chunk_index)
                )
            )
            return document, chunks

    async def reindex(self, document_id, context=None):
        return await self.service.reindex(
            context=context if context is not None else self.context,
            document_id=document_id,
        )

    async def test_success_reuses_chunks_and_commits_before_embeddings(self):
        for status in ("pending", "failed"):
            with self.subTest(status=status):
                document_id = self.seed(status)
                _, before = self.stored(document_id)
                transactions = self.factory.transactions

                async def embed(texts):
                    self.assertEqual(texts, ["First", "Second"])
                    document, _ = self.stored(document_id)
                    self.assertEqual(document.status, "processing")
                    self.assertIsNone(document.indexing_error_code)
                    return await self.embedding.embed_texts(texts)

                with patch.object(
                    self.service, "_embedding_service", new=AsyncMock(embed_texts=embed)
                ):
                    result = await self.reindex(document_id)

                document, chunks = self.stored(document_id)
                self.assertEqual(result.document_id, document_id)
                self.assertEqual(result.chunks_indexed, 2)
                self.assertEqual([c.id for c in chunks], [c.id for c in before])
                self.assertEqual([c.content for c in chunks], ["First", "Second"])
                self.assertEqual([float(c.embedding[0]) for c in chunks], [1.0, 2.0])
                self.assertEqual(document.status, "ready")
                self.assertEqual(document.embedding_model, "text-embedding-3-small")
                self.assertEqual(document.embedding_dimensions, 1536)
                self.assertEqual(document.embedding_tokens, 12)
                self.assertIsNotNone(document.indexed_at)
                self.assertIsNone(document.indexing_error_code)
                self.assertEqual(self.factory.transactions - transactions, 2)
                self.assertEqual(self.factory.active, 0)

    async def test_missing_and_wrong_owner_never_call_embeddings_or_cleanup(self):
        document_id = self.seed("failed")
        cases = (
            (self.context, uuid.uuid4()),
            (AccessContext(uuid.uuid4(), self.context.user_id), document_id),
            (AccessContext(self.context.tenant_id, "other"), document_id),
        )
        with patch.object(self.service, "_try_mark_failed") as cleanup:
            for context, requested in cases:
                with self.subTest(context=context), self.assertRaises(LookupError):
                    await self.reindex(requested, context)
            cleanup.assert_not_awaited()
        self.assertEqual(self.embedding.calls, 0)
        document, _ = self.stored(document_id)
        self.assertEqual(document.status, "failed")
        self.assertEqual(document.indexing_error_code, "earlier_error")

    async def test_ready_and_processing_rejected_without_cleanup(self):
        with patch.object(self.service, "_try_mark_failed") as cleanup:
            for status in ("ready", "processing"):
                document_id = self.seed(status)
                with self.subTest(status=status):
                    with self.assertRaises(DocumentReindexConflictError):
                        await self.reindex(document_id)
                    self.assertEqual(self.stored(document_id)[0].status, status)
            cleanup.assert_not_awaited()
        self.assertEqual(self.embedding.calls, 0)

    async def test_invalid_chunks_preserve_failed_status_and_previous_error(self):
        with patch.object(self.service, "_try_mark_failed") as cleanup:
            for texts in ((), ("  ",)):
                document_id = self.seed("failed", texts)
                with self.subTest(texts=texts):
                    with self.assertRaises(InvalidStoredChunksError):
                        await self.reindex(document_id)
                    document, _ = self.stored(document_id)
                    self.assertEqual(document.status, "failed")
                    self.assertEqual(document.indexing_error_code, "earlier_error")
            cleanup.assert_not_awaited()
        self.assertEqual(self.embedding.calls, 0)

    async def test_claim_commit_failure_rolls_back_and_never_calls_embeddings(self):
        document_id = self.seed("failed")
        self.factory.fail_transactions = {1}
        with patch.object(self.service, "_try_mark_failed") as cleanup:
            with self.assertRaises(SQLAlchemyError):
                await self.reindex(document_id)
            cleanup.assert_not_awaited()
        self.assertEqual(self.embedding.calls, 0)
        document, _ = self.stored(document_id)
        self.assertEqual(document.status, "failed")
        self.assertEqual(document.indexing_error_code, "earlier_error")

    async def test_timeout_then_successful_retry(self):
        document_id = self.seed()
        self.embedding.error = TimeoutError("simulated")
        with self.assertRaises(DocumentIndexingError) as caught:
            await self.reindex(document_id)
        self.assertEqual(caught.exception.document_id, document_id)
        self.assertEqual(caught.exception.code, "embedding_timeout")
        self.assertIsInstance(caught.exception.__cause__, TimeoutError)
        document, chunks = self.stored(document_id)
        self.assertEqual(document.status, "failed")
        self.assertEqual(document.indexing_error_code, "embedding_timeout")
        self.assertTrue(all(c.embedding is None for c in chunks))
        self.embedding.error = None
        await self.reindex(document_id)
        document, _ = self.stored(document_id)
        self.assertEqual(document.status, "ready")
        self.assertIsNone(document.indexing_error_code)
        self.assertEqual(self.embedding.calls, 2)

    async def test_final_commit_failure_rolls_back_vectors_and_metadata(self):
        document_id = self.seed()
        self.factory.fail_transactions = {2}
        with self.assertRaises(DocumentIndexingError) as caught:
            await self.reindex(document_id)
        self.assertEqual(caught.exception.code, "index_storage_error")
        document, chunks = self.stored(document_id)
        self.assertEqual(document.status, "failed")
        self.assertIsNone(document.embedding_model)
        self.assertIsNone(document.indexed_at)
        self.assertTrue(all(c.embedding is None for c in chunks))

    async def test_cancellation_propagates_and_marks_failed(self):
        document_id = self.seed()
        self.embedding.error = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.reindex(document_id)
        document, _ = self.stored(document_id)
        self.assertEqual(document.status, "failed")
        self.assertEqual(document.indexing_error_code, "indexing_cancelled")

    async def test_cleanup_failure_preserves_original_error(self):
        document_id = self.seed()
        self.embedding.error = TimeoutError()
        self.factory.fail_transactions = {2}
        with self.assertLogs("app.services.document_service", level="ERROR"):
            with self.assertRaises(DocumentIndexingError) as caught:
                await self.reindex(document_id)
        self.assertEqual(caught.exception.code, "embedding_timeout")
        self.assertEqual(self.stored(document_id)[0].status, "processing")

    async def test_wrong_vector_count_does_not_mark_ready(self):
        document_id = self.seed()
        self.embedding.drop_last = True
        with self.assertRaises(DocumentIndexingError):
            await self.reindex(document_id)
        document, chunks = self.stored(document_id)
        self.assertEqual(document.status, "failed")
        self.assertTrue(all(c.embedding is None for c in chunks))


if __name__ == "__main__":
    unittest.main(verbosity=2)
