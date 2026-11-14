"""Attempt ownership tests. No OpenAI calls or PostgreSQL concurrency claims."""

import unittest
import uuid
from unittest.mock import AsyncMock, patch

import test_reindex_service as fixtures
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from app.api.controllers.documents import _indexing_http_exception
from app.db.models import Document
from app.repositories.documents import DocumentRepository, IndexingAttemptConflictError
from app.security.context import AccessContext
from app.services.document_service import DocumentIndexingError


class AttemptIdentityTests(unittest.IsolatedAsyncioTestCase):
    # Reuse only setup/helpers, without inheriting and rerunning the old tests.
    setUp = fixtures.ReindexServiceTests.setUp
    tearDown = fixtures.ReindexServiceTests.tearDown
    seed = fixtures.ReindexServiceTests.seed
    stored = fixtures.ReindexServiceTests.stored
    reindex = fixtures.ReindexServiceTests.reindex

    async def claim(self, document_id, attempt_id):
        async with self.factory.begin() as session:
            return await DocumentRepository(session).claim_for_reindex(
                context=self.context,
                document_id=document_id,
                attempt_id=attempt_id,
            )

    async def save(self, document_id, attempt_id):
        async with self.factory.begin() as session:
            await DocumentRepository(session).save_embeddings(
                context=self.context,
                document_id=document_id,
                attempt_id=attempt_id,
                vectors=[[1.0] + [0.0] * 1535, [2.0] + [0.0] * 1535],
                model="text-embedding-3-small",
                dimensions=1536,
                total_tokens=12,
            )

    async def fail(self, document_id, attempt_id, context=None):
        async with self.factory.begin() as session:
            return await DocumentRepository(session).mark_failed(
                context=context or self.context,
                document_id=document_id,
                attempt_id=attempt_id,
                error_code="test_failure",
            )

    def replace_attempt(self, document_id, attempt_id):
        # Simulate a future recovery operation. This is not production recovery code.
        with Session(self.engine) as session, session.begin():
            session.get(Document, document_id).indexing_attempt_id = attempt_id

    async def test_upload_and_each_retry_record_new_attempt_ids(self):
        uploaded = await self.service.ingest(
            context=self.context,
            filename="file.txt",
            raw_bytes=b"Source text",
        )
        upload_id = self.stored(uploaded.document_id)[0].indexing_attempt_id
        self.assertIsInstance(upload_id, uuid.UUID)

        document_id = self.seed()
        self.embedding.error = TimeoutError()
        with self.assertRaises(DocumentIndexingError):
            await self.reindex(document_id)
        failed, _ = self.stored(document_id)
        first_id = failed.indexing_attempt_id
        self.assertEqual(failed.status, "failed")
        self.assertIsInstance(first_id, uuid.UUID)
        self.embedding.error = None
        await self.reindex(document_id)
        ready, _ = self.stored(document_id)
        self.assertEqual(ready.status, "ready")
        self.assertEqual(len({upload_id, first_id, ready.indexing_attempt_id}), 3)

    async def test_stale_save_cannot_change_processing_or_ready_attempt(self):
        document_id = self.seed()
        old_id, current_id = uuid.uuid4(), uuid.uuid4()
        await self.claim(document_id, current_id)
        with self.assertRaises(IndexingAttemptConflictError):
            await self.save(document_id, old_id)
        document, chunks = self.stored(document_id)
        self.assertEqual(document.status, "processing")
        self.assertEqual(document.indexing_attempt_id, current_id)
        self.assertIsNone(document.indexed_at)
        self.assertTrue(all(c.embedding is None for c in chunks))

        await self.save(document_id, current_id)
        indexed_at = self.stored(document_id)[0].indexed_at
        for attempt_id in (old_id, current_id):
            with self.subTest(attempt_id=attempt_id):
                with self.assertRaises(IndexingAttemptConflictError):
                    await self.save(document_id, attempt_id)
        document, chunks = self.stored(document_id)
        self.assertEqual(document.status, "ready")
        self.assertEqual(document.indexed_at, indexed_at)
        self.assertEqual([float(c.embedding[0]) for c in chunks], [1.0, 2.0])

    async def test_failure_update_requires_owner_and_current_attempt(self):
        document_id, current_id = self.seed(), uuid.uuid4()
        await self.claim(document_id, current_id)
        cases = (
            (self.context, uuid.uuid4()),
            (AccessContext(uuid.uuid4(), self.context.user_id), current_id),
            (AccessContext(self.context.tenant_id, "other"), current_id),
        )
        for context, attempt_id in cases:
            self.assertFalse(await self.fail(document_id, attempt_id, context))
        document, _ = self.stored(document_id)
        self.assertEqual(document.status, "processing")
        self.assertIsNone(document.indexing_error_code)
        self.assertTrue(await self.fail(document_id, current_id))
        self.assertEqual(self.stored(document_id)[0].status, "failed")

    async def test_ready_attempt_cannot_be_marked_failed(self):
        document_id, attempt_id = self.seed(), uuid.uuid4()
        await self.claim(document_id, attempt_id)
        await self.save(document_id, attempt_id)
        self.assertFalse(await self.fail(document_id, attempt_id))
        self.assertEqual(self.stored(document_id)[0].status, "ready")

    async def test_stale_success_becomes_conflict_without_cleanup(self):
        document_id, replacement_id = self.seed(), uuid.uuid4()

        async def embed(texts):
            result = await self.embedding.embed_texts(texts)
            self.replace_attempt(document_id, replacement_id)
            return result

        with (
            patch.object(
                self.service, "_embedding_service", AsyncMock(embed_texts=embed)
            ),
            patch.object(self.service, "_try_mark_failed") as cleanup,
        ):
            with self.assertRaises(DocumentIndexingError) as caught:
                await self.reindex(document_id)
            cleanup.assert_not_awaited()
        self.assertEqual(caught.exception.code, "indexing_attempt_conflict")
        self.assertIsInstance(caught.exception.__cause__, IndexingAttemptConflictError)
        document, chunks = self.stored(document_id)
        self.assertEqual(document.indexing_attempt_id, replacement_id)
        self.assertEqual(document.status, "processing")
        self.assertTrue(all(c.embedding is None for c in chunks))

    async def test_stale_provider_error_cannot_fail_new_attempt(self):
        document_id, replacement_id = self.seed(), uuid.uuid4()

        async def embed(texts):
            self.replace_attempt(document_id, replacement_id)
            raise TimeoutError()

        with patch.object(
            self.service, "_embedding_service", AsyncMock(embed_texts=embed)
        ):
            with self.assertRaises(DocumentIndexingError) as caught:
                await self.reindex(document_id)
        self.assertEqual(caught.exception.code, "embedding_timeout")
        document, _ = self.stored(document_id)
        self.assertEqual(document.indexing_attempt_id, replacement_id)
        self.assertEqual(document.status, "processing")
        self.assertIsNone(document.indexing_error_code)

    async def test_failure_sql_includes_attempt_and_owner_conditions(self):
        session = AsyncMock()
        session.scalar.return_value = None
        attempt_id = uuid.uuid4()
        await DocumentRepository(session).mark_failed(
            context=self.context,
            document_id=uuid.uuid4(),
            attempt_id=attempt_id,
            error_code="test",
        )
        statement = session.scalar.await_args.args[0]
        compiled = statement.compile(dialect=postgresql.dialect())
        sql = str(compiled)
        for term in (
            "documents.id =",
            "documents.tenant_id =",
            "documents.user_id =",
            "documents.status =",
            "documents.indexing_attempt_id =",
        ):
            self.assertIn(term, sql)
        self.assertIn(attempt_id, compiled.params.values())

    async def test_attempt_conflict_maps_to_http_409(self):
        document_id = uuid.uuid4()
        error = DocumentIndexingError(document_id, "indexing_attempt_conflict")
        response_error = _indexing_http_exception(error)
        self.assertEqual(response_error.status_code, 409)
        self.assertEqual(
            response_error.extra,
            {
                "document_id": str(document_id),
                "code": "indexing_attempt_conflict",
            },
        )


if __name__ == "__main__":
    unittest.main()
