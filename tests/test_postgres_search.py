"""Search integration checks using temporary PostgreSQL and fake embeddings."""

import unittest
from uuid import UUID, uuid4

import test_postgres_indexing as postgres_fixture

from app.db.models import EMBEDDING_DIM, Document, DocumentChunk, Tenant
from app.repositories.search import SearchRepository
from app.security.context import AccessContext
from app.services.embedding_service import EmbeddingResult, EmbeddingService
from app.services.search_service import SearchInputError, SearchService

MODEL = "text-embedding-3-small"


class FakeQueryEmbeddings(EmbeddingService):
    def __init__(self):
        self.calls = []

    async def embed_texts(self, texts):
        self.calls.append(texts)
        vector = [1.0] + [0.0] * (EMBEDDING_DIM - 1)
        return EmbeddingResult([vector], MODEL, EMBEDDING_DIM, 1)


@unittest.skipUnless(
    postgres_fixture.TEST_URL,
    "Use run_postgres_tests.py --test-file test_postgres_search.py",
)
class PostgreSQLSearchTests(unittest.IsolatedAsyncioTestCase):
    # Reuse only setup/cleanup, without inheriting the five indexing tests.
    asyncSetUp = postgres_fixture.PostgreSQLIndexingTests.asyncSetUp
    drop_schema = postgres_fixture.PostgreSQLIndexingTests.drop_schema

    async def seed_document(
        self,
        *,
        chunk_id=None,
        context=None,
        content="PAIagent зберігає документи.",
        vector=(1.0, 0.0),
        status="ready",
        model=MODEL,
        dimensions=EMBEDDING_DIM,
    ):
        context = context or self.context
        document_id = uuid4()
        chunk_id = chunk_id or uuid4()
        embedding = (
            None
            if vector is None
            else list(vector) + [0.0] * (EMBEDDING_DIM - len(vector))
        )

        async with self.factory.begin() as session:
            session.add(
                Document(
                    id=document_id,
                    tenant_id=context.tenant_id,
                    user_id=context.user_id,
                    title="search-test.txt",
                    content_type="txt",
                    status=status,
                    embedding_model=model,
                    embedding_dimensions=dimensions,
                )
            )
            await session.flush()
            session.add(
                DocumentChunk(
                    id=chunk_id,
                    document_id=document_id,
                    chunk_index=0,
                    content=content,
                    embedding=embedding,
                )
            )
        return document_id, chunk_id

    async def find(self, method_name, *, document_id=None, limit=20):
        arguments = {
            "context": self.context,
            "model": MODEL,
            "limit": limit,
            "document_id": document_id,
        }
        if method_name == "search_vector":
            arguments["query_vector"] = [1.0] + [0.0] * (EMBEDDING_DIM - 1)
        else:
            arguments["query"] = "документи"

        async with self.factory.begin() as session:
            method = getattr(SearchRepository(session), method_name)
            return await method(**arguments)

    async def test_both_searches_enforce_filters_order_and_limit(self):
        _, first = await self.seed_document(chunk_id=UUID(int=1))
        second_document, second = await self.seed_document(chunk_id=UUID(int=2))

        other_context = AccessContext(uuid4(), self.context.user_id)
        async with self.factory.begin() as session:
            session.add(Tenant(id=other_context.tenant_id, name="Other tenant"))

        foreign_document, _ = await self.seed_document(context=other_context)
        await self.seed_document(
            context=AccessContext(self.context.tenant_id, "other-user")
        )
        for status in ("pending", "processing", "failed"):
            await self.seed_document(status=status)
        await self.seed_document(vector=None)
        await self.seed_document(model="different-model")
        await self.seed_document(dimensions=3)

        for method_name in ("search_vector", "search_full_text"):
            with self.subTest(method=method_name):
                hits = await self.find(method_name)
                self.assertEqual([hit.chunk_id for hit in hits], [first, second])
                self.assertEqual(hits[0].content, "PAIagent зберігає документи.")
                self.assertEqual(hits[0].chunk_index, 0)

                limited = await self.find(method_name, limit=1)
                self.assertEqual([hit.chunk_id for hit in limited], [first])

                filtered = await self.find(method_name, document_id=second_document)
                self.assertEqual([hit.chunk_id for hit in filtered], [second])
                self.assertEqual(filtered[0].document_id, second_document)

                for unavailable_id in (foreign_document, uuid4()):
                    self.assertEqual(
                        await self.find(method_name, document_id=unavailable_id),
                        [],
                    )

    async def test_service_validates_input_and_fuses_rankings(self):
        await self.seed_document(chunk_id=UUID(int=1), content="Лише вектор.")
        _, second = await self.seed_document(
            chunk_id=UUID(int=2), content="документи", vector=(0.8, 0.6)
        )
        _, third = await self.seed_document(
            chunk_id=UUID(int=3), content="документи", vector=(0.0, 1.0)
        )

        embeddings = FakeQueryEmbeddings()
        service = SearchService(self.factory, embeddings)

        with self.assertRaises(SearchInputError):
            await service.search(context=self.context, query="   ")
        self.assertEqual(embeddings.calls, [])

        hits = await service.search(
            context=self.context, query="  документи  ", limit=2
        )

        self.assertEqual(embeddings.calls, [["документи"]])
        self.assertEqual([hit.chunk_id for hit in hits], [second, third])
        self.assertAlmostEqual(hits[0].score, 1 / 62 + 1 / 61)
        self.assertAlmostEqual(hits[1].score, 1 / 63 + 1 / 62)


if __name__ == "__main__":
    unittest.main()
