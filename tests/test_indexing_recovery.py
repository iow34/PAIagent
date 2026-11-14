"""Expiry/recovery tests: SQLite and fake embeddings, not PostgreSQL locking."""

import asyncio
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import test_reindex_http as http_fixtures
import test_reindex_service as fixtures
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.db.models import Document
from app.repositories.documents import (
    INDEXING_LEASE_SECONDS,
    DocumentReindexConflictError,
    DocumentRepository,
    InvalidStoredChunksError,
)
from app.security.context import AccessContext
from app.services.document_service import DocumentIndexingError
from app.services.embedding_service import EMBEDDING_DEADLINE_SECONDS, EmbeddingResult

NOW = datetime(2030, 1, 1, tzinfo=UTC)


class IndexingRecoveryTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.ReindexServiceTests.setUp
    tearDown = fixtures.ReindexServiceTests.tearDown
    seed = fixtures.ReindexServiceTests.seed
    stored = fixtures.ReindexServiceTests.stored
    reindex = fixtures.ReindexServiceTests.reindex

    def metadata(self, document_id, attempt_id, expires_at):
        with Session(self.engine) as session, session.begin():
            document = session.get(Document, document_id)
            document.indexing_attempt_id = attempt_id
            document.indexing_expires_at = expires_at

    async def claim(self, document_id, attempt_id, context=None):
        async with self.factory.begin() as session:
            return await DocumentRepository(session).claim_for_reindex(
                context=context or self.context,
                document_id=document_id,
                attempt_id=attempt_id,
            )

    async def test_upload_records_expiry_before_embedding_call(self):
        self.assertGreater(INDEXING_LEASE_SECONDS, EMBEDDING_DEADLINE_SECONDS)
        original = self.embedding.embed_texts

        async def embed(texts):
            with Session(self.engine) as session:
                document = session.query(Document).one()
                # SQLite returns a naive datetime for this column.
                self.assertEqual(
                    document.indexing_expires_at,
                    (NOW + timedelta(seconds=INDEXING_LEASE_SECONDS)).replace(
                        tzinfo=None
                    ),
                )
                self.assertIsInstance(document.indexing_attempt_id, uuid.UUID)
            return await original(texts)

        with (
            patch("app.repositories.documents.datetime") as clock,
            patch.object(self.embedding, "embed_texts", side_effect=embed),
        ):
            clock.now.return_value = NOW
            await self.service.ingest(
                context=self.context,
                filename="file.txt",
                raw_bytes=b"Text",
            )

    async def test_expiry_boundary_and_second_claim(self):
        with patch("app.repositories.documents.datetime") as clock:
            clock.now.return_value = NOW
            for seconds in (1, 0, -1):
                with self.subTest(seconds=seconds):
                    document_id = self.seed("processing")
                    old_id, new_id = uuid.uuid4(), uuid.uuid4()
                    self.metadata(document_id, old_id, NOW + timedelta(seconds=seconds))
                    if seconds > 0:
                        with self.assertRaises(DocumentReindexConflictError):
                            await self.claim(document_id, new_id)
                        self.assertEqual(
                            self.stored(document_id)[0].indexing_attempt_id, old_id
                        )
                        continue
                    self.assertEqual(
                        await self.claim(document_id, new_id), ["First", "Second"]
                    )
                    document, _ = self.stored(document_id)
                    self.assertEqual(document.status, "processing")
                    self.assertEqual(document.indexing_attempt_id, new_id)
                    self.assertEqual(
                        document.indexing_expires_at,
                        (NOW + timedelta(seconds=INDEXING_LEASE_SECONDS)).replace(
                            tzinfo=None
                        ),
                    )
                    with self.assertRaises(DocumentReindexConflictError):
                        await self.claim(document_id, uuid.uuid4())

    async def test_legacy_processing_without_attempt_or_expiry_is_not_recovered(self):
        cases = ((None, None), (uuid.uuid4(), None), (None, NOW - timedelta(days=1)))
        with patch("app.repositories.documents.datetime") as clock:
            clock.now.return_value = NOW
            for attempt_id, expires_at in cases:
                with self.subTest(attempt_id=attempt_id, expires_at=expires_at):
                    document_id = self.seed("processing")
                    self.metadata(document_id, attempt_id, expires_at)
                    with self.assertRaises(DocumentReindexConflictError):
                        await self.claim(document_id, uuid.uuid4())
                    self.assertEqual(
                        self.stored(document_id)[0].indexing_attempt_id, attempt_id
                    )

    async def test_expiry_does_not_bypass_owner_or_ready_protection(self):
        document_id, old_id = self.seed("processing"), uuid.uuid4()
        self.metadata(document_id, old_id, NOW - timedelta(seconds=1))
        with patch("app.repositories.documents.datetime") as clock:
            clock.now.return_value = NOW
            for context in (
                AccessContext(uuid.uuid4(), self.context.user_id),
                AccessContext(self.context.tenant_id, "other"),
            ):
                with self.assertRaises(LookupError):
                    await self.claim(document_id, uuid.uuid4(), context)
            ready_id = self.seed("ready")
            self.metadata(ready_id, uuid.uuid4(), NOW - timedelta(seconds=1))
            with self.assertRaises(DocumentReindexConflictError):
                await self.claim(ready_id, uuid.uuid4())
        self.assertEqual(self.stored(document_id)[0].indexing_attempt_id, old_id)

    async def test_invalid_chunks_leave_old_attempt_untouched(self):
        document_id, old_id = self.seed("processing", texts=()), uuid.uuid4()
        expires_at = NOW - timedelta(seconds=1)
        self.metadata(document_id, old_id, expires_at)
        with patch("app.repositories.documents.datetime") as clock:
            clock.now.return_value = NOW
            with self.assertRaises(InvalidStoredChunksError):
                await self.claim(document_id, uuid.uuid4())
        document, _ = self.stored(document_id)
        self.assertEqual(document.indexing_attempt_id, old_id)
        self.assertEqual(document.indexing_expires_at, expires_at.replace(tzinfo=None))

    async def test_failed_claim_transaction_restores_previous_attempt(self):
        document_id, old_id = self.seed("processing"), uuid.uuid4()
        expires_at = NOW - timedelta(seconds=1)
        self.metadata(document_id, old_id, expires_at)
        self.factory.fail_transactions = {1}
        with patch("app.repositories.documents.datetime") as clock:
            clock.now.return_value = NOW
            with self.assertRaises(SQLAlchemyError):
                await self.reindex(document_id)
        document, _ = self.stored(document_id)
        self.assertEqual(document.indexing_attempt_id, old_id)
        self.assertEqual(document.indexing_expires_at, expires_at.replace(tzinfo=None))
        self.assertEqual(self.embedding.calls, 0)

    async def test_ready_and_failed_clear_expiry_but_keep_attempt_id(self):
        for fail in (False, True):
            with self.subTest(fail=fail):
                document_id = self.seed()
                self.embedding.error = TimeoutError() if fail else None
                if fail:
                    with self.assertRaises(DocumentIndexingError):
                        await self.reindex(document_id)
                else:
                    await self.reindex(document_id)
                document, _ = self.stored(document_id)
                self.assertEqual(document.status, "failed" if fail else "ready")
                self.assertIsNone(document.indexing_expires_at)
                self.assertIsInstance(document.indexing_attempt_id, uuid.UUID)

    async def test_late_result_or_error_cannot_overwrite_recovered_attempt(self):
        # Two interleaved tasks exercise the service protocol, not DB row locking.
        for late_error in (False, True):
            with self.subTest(late_error=late_error):
                document_id = self.seed()
                started, release = asyncio.Event(), asyncio.Event()
                calls = 0

                async def embed(texts):
                    nonlocal calls
                    self.assertEqual(self.factory.active, 0)
                    calls += 1
                    if calls == 1:
                        started.set()
                        await release.wait()
                        if late_error:
                            raise TimeoutError()
                        value = 1.0
                    else:
                        value = 7.0
                    return EmbeddingResult(
                        [[value] + [0.0] * 1535 for _ in texts],
                        "text-embedding-3-small",
                        1536,
                        12,
                    )

                with (
                    patch("app.repositories.documents.datetime") as clock,
                    patch.object(self.embedding, "embed_texts", side_effect=embed),
                ):
                    clock.now.return_value = NOW
                    old_task = asyncio.create_task(self.reindex(document_id))
                    try:
                        await asyncio.wait_for(started.wait(), timeout=2)
                        old_id = self.stored(document_id)[0].indexing_attempt_id
                        clock.now.return_value = NOW + timedelta(
                            seconds=INDEXING_LEASE_SECONDS
                        )
                        await self.reindex(document_id)
                        new_id = self.stored(document_id)[0].indexing_attempt_id
                        self.assertNotEqual(old_id, new_id)
                        release.set()
                        with self.assertRaises(DocumentIndexingError) as caught:
                            await asyncio.wait_for(old_task, timeout=2)
                        self.assertEqual(
                            caught.exception.code,
                            "embedding_timeout"
                            if late_error
                            else "indexing_attempt_conflict",
                        )
                    finally:
                        release.set()
                        if not old_task.done():
                            old_task.cancel()
                        await asyncio.gather(old_task, return_exceptions=True)
                document, chunks = self.stored(document_id)
                self.assertEqual(document.status, "ready")
                self.assertEqual(document.indexing_attempt_id, new_id)
                self.assertIsNone(document.indexing_error_code)
                self.assertIsNone(document.indexing_expires_at)
                self.assertEqual([float(c.embedding[0]) for c in chunks], [7.0, 7.0])


class RecoveryHTTPTests(unittest.TestCase):
    setUp = http_fixtures.ReindexHTTPTests.setUp
    seed = http_fixtures.ReindexHTTPTests.seed
    stored = http_fixtures.ReindexHTTPTests.stored
    post = http_fixtures.ReindexHTTPTests.post

    def test_http_recovers_expired_attempt_and_rejects_active_attempt(self):
        with patch("app.repositories.documents.datetime") as clock:
            clock.now.return_value = NOW
            for seconds, expected in ((-1, 200), (1, 409)):
                with self.subTest(seconds=seconds):
                    document_id = self.seed("processing")
                    with Session(self.engine) as session, session.begin():
                        document = session.get(Document, document_id)
                        document.indexing_attempt_id = uuid.uuid4()
                        document.indexing_expires_at = NOW + timedelta(seconds=seconds)
                    response = self.post(document_id)
                    self.assertEqual(response.status_code, expected, response.text)
            self.assertEqual(self.embedding.calls, 1)


if __name__ == "__main__":
    unittest.main()
