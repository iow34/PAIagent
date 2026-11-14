import unittest
import uuid
from unittest.mock import AsyncMock

from sqlalchemy import create_engine
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from test_document_indexing import SQLiteFactory

from app.db.models import Base, Document, DocumentChunk, Tenant
from app.repositories.documents import (
    DocumentReindexConflictError,
    DocumentRepository,
    InvalidStoredChunksError,
)
from app.security.context import AccessContext


class ReindexClaimTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite:///:memory:",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
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
        self.context = AccessContext(self.tenant_id, "user")
        self.factory = SQLiteFactory(self.engine)
        with Session(self.engine) as session, session.begin():
            session.add(Tenant(id=self.tenant_id, name="Test tenant"))

    def tearDown(self):
        self.engine.dispose()

    def create_document(self, status="pending", chunks=((0, "First"), (1, "Second"))):
        document_id = uuid.uuid4()
        with Session(self.engine) as session, session.begin():
            session.add(
                Document(
                    id=document_id,
                    tenant_id=self.tenant_id,
                    user_id="user",
                    title="file.txt",
                    content_type="txt",
                    status=status,
                    indexing_error_code="earlier_error" if status == "failed" else None,
                )
            )
            session.flush()
            session.add_all(
                [
                    DocumentChunk(
                        document_id=document_id, chunk_index=index, content=text
                    )
                    for index, text in chunks
                ]
            )
        return document_id

    def read_status(self, document_id):
        with Session(self.engine) as session:
            doc = session.get(Document, document_id)
            return doc.status, doc.indexing_error_code

    async def claim(self, document_id, context=None):
        async with self.factory.begin() as session:
            return await DocumentRepository(session).claim_for_reindex(
                attempt_id=uuid.uuid4(),
                context=context or self.context,
                document_id=document_id,
            )

    async def test_pending_and_failed_return_ordered_texts_and_claim(self):
        for status in ("pending", "failed"):
            with self.subTest(status=status):
                doc_id = self.create_document(
                    status, chunks=((1, "Second"), (0, "First"))
                )
                self.assertEqual(await self.claim(doc_id), ["First", "Second"])
                self.assertEqual(self.read_status(doc_id), ("processing", None))

    async def test_missing_and_wrong_owner_are_unavailable(self):
        doc_id = self.create_document()
        for context, requested in (
            (self.context, uuid.uuid4()),
            (AccessContext(uuid.uuid4(), "user"), doc_id),
            (AccessContext(self.tenant_id, "other"), doc_id),
        ):
            with self.subTest(context=context), self.assertRaises(LookupError):
                await self.claim(requested, context)
        self.assertEqual(self.read_status(doc_id), ("pending", None))

    async def test_ready_and_processing_are_not_claimed(self):
        for status in ("ready", "processing"):
            doc_id = self.create_document(status)
            with (
                self.subTest(status=status),
                self.assertRaises(DocumentReindexConflictError),
            ):
                await self.claim(doc_id)
            self.assertEqual(self.read_status(doc_id), (status, None))

    async def test_second_sequential_claim_rejected(self):
        doc_id = self.create_document()
        await self.claim(doc_id)
        with self.assertRaises(DocumentReindexConflictError):
            await self.claim(doc_id)
        self.assertEqual(self.read_status(doc_id), ("processing", None))

    async def test_invalid_chunks_do_not_change_status(self):
        cases = (
            (),
            ((0, "First"), (2, "Gap")),
            ((0, "First"), (0, "Duplicate")),
            ((0, "  "),),
        )
        for chunks in cases:
            with self.subTest(chunks=chunks):
                doc_id = self.create_document("failed", chunks)
                with self.assertRaises(InvalidStoredChunksError):
                    await self.claim(doc_id)
                self.assertEqual(self.read_status(doc_id), ("failed", "earlier_error"))

    async def test_rollback_reverts_claim(self):
        doc_id = self.create_document()
        with self.assertRaisesRegex(RuntimeError, "abort"):
            async with self.factory.begin() as session:
                await DocumentRepository(session).claim_for_reindex(
                    attempt_id=uuid.uuid4(),
                    context=self.context,
                    document_id=doc_id,
                )
                raise RuntimeError("abort")
        self.assertEqual(self.read_status(doc_id), ("pending", None))

    async def test_postgres_query_includes_owner_and_for_update(self):
        session = AsyncMock()
        session.scalar.return_value = None
        with self.assertRaises(LookupError):
            await DocumentRepository(session).claim_for_reindex(
                attempt_id=uuid.uuid4(),
                context=self.context,
                document_id=uuid.uuid4(),
            )
        statement = session.scalar.await_args.args[0]
        sql = str(statement.compile(dialect=postgresql.dialect()))
        for term in (
            "documents.id =",
            "documents.tenant_id =",
            "documents.user_id =",
            "FOR UPDATE",
        ):
            self.assertIn(term, sql)


if __name__ == "__main__":
    unittest.main(verbosity=2)
