import unittest
from uuid import UUID

import msgspec
import test_postgres_data_lifecycle as lifecycle
import test_postgres_indexing as fixture
from litestar.exceptions import HTTPException
from sqlalchemy import func, select

from app.db.models import EvidenceFragment, FileProvenance
from app.security.context import AccessContext
from app.security.permissions import SCOPES
from app.services.evidence_service import EvidenceService


@unittest.skipUnless(fixture.TEST_URL, "Use temporary PostgreSQL runner")
class EvidenceTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.PostgreSQLIndexingTests.asyncSetUp
    drop_schema = fixture.PostgreSQLIndexingTests.drop_schema
    seed = lifecycle.DataLifecycleTests.seed
    http = lifecycle.DataLifecycleTests.http

    async def test_current_context_index_search_and_erasure(self):
        base, coll, env = await self.seed()
        service = EvidenceService(self.factory, base.context)
        ack = await service.ingest_one(coll, env)
        context = await service.context_page(coll)
        self.assertTrue(context["items"])
        identifier = UUID(context["items"][0]["id"])
        await service.index(coll, fixture.FakeEmbeddings())
        self.assertTrue(
            (await service.search(coll, "звіт", fixture.FakeEmbeddings()))["items"]
        )
        foreign = EvidenceService(
            self.factory, AccessContext(base.context.tenant_id, "other", tuple(SCOPES))
        )
        with self.assertRaises(HTTPException):
            await foreign.get_evidence(identifier)
        await service.ingest_one(
            coll,
            msgspec.structs.replace(
                env,
                object_version=2,
                operation="upsert",
                event_id="e2",
                payload={**env.payload, "caption": "New text"},
            ),
        )
        with self.assertRaises(HTTPException):
            await service.get_evidence(identifier)
        await service.erase_object(ack.object_id)
        async with self.factory() as session:
            self.assertEqual(
                await session.scalar(
                    select(func.count()).select_from(EvidenceFragment)
                ),
                0,
            )

    async def test_multipart_file_and_csv(self):
        service, coll, env = await self.seed()
        async with self.http(service) as client:
            route = f"/v1/collections/{coll}"
            response = await client.post(
                route + "/files",
                files={
                    "file": ("book.md", b"# Hello book", "text/markdown"),
                    "envelope": (None, msgspec.json.encode(env).decode()),
                },
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["fragments"], 1)
            context = (await client.get(route + "/context")).json()
            self.assertIn("Hello book", context["items"][0]["content"])
            duplicate = await client.post(
                route + "/files",
                files={
                    "file": ("book.md", b"# Hello book", "text/markdown"),
                    "envelope": (None, msgspec.json.encode(env).decode()),
                },
            )
            self.assertEqual(
                duplicate.json()["ack"]["status"], "duplicate", duplicate.text
            )
            mismatch = await client.post(
                route + "/files",
                files={
                    "file": ("book.md", b"Changed file", "text/markdown"),
                    "envelope": (None, msgspec.json.encode(env).decode()),
                },
            )
            self.assertEqual(mismatch.status_code, 409, mismatch.text)
            mapping = {
                "columns": [
                    {"column": "title", "field": "caption", "type": "string"},
                    {"column": "phase", "field": "phase", "type": "string"},
                ],
                "id_column": "id",
                "version_column": "v",
                "occurred_at": env.occurred_at.isoformat(),
            }
            template = msgspec.structs.replace(env, external_id="unused")
            result = await client.post(
                route + "/csv",
                files={
                    "file": (
                        "rows.csv",
                        b"id,v,title,phase\nb,1,CSV title,todo\nc,no,Bad,todo\n",
                        "text/csv",
                    ),
                    "envelope": (None, msgspec.json.encode(template).decode()),
                    "mapping": (None, msgspec.json.encode(mapping).decode()),
                },
            )
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(result.json()["items"][0]["ack"]["status"], "accepted")
            self.assertIn("error", result.json()["items"][1])
            self.assertEqual((await client.post(route + "/index")).status_code, 200)
            self.assertEqual(
                (
                    await client.post(route + "/search", json={"query": "Hello"})
                ).status_code,
                200,
            )
            await client.delete("/v1/users/me/data")
            async with self.factory() as session:
                self.assertEqual(
                    await session.scalar(
                        select(func.count()).select_from(FileProvenance)
                    ),
                    0,
                )

    async def test_existing_normalization_and_index_race(self):
        from sqlalchemy import delete

        base, coll, env = await self.seed()
        service = EvidenceService(self.factory, base.context)
        await service.ingest_one(coll, env)
        async with self.factory.begin() as session:
            await session.execute(delete(EvidenceFragment))
        self.assertEqual((await service.normalize_existing(coll))["normalized"], 1)

        class ChangingEmbeddings(fixture.FakeEmbeddings):
            async def embed_texts(inner, texts):
                result = await super().embed_texts(texts)
                await service.ingest_one(
                    coll,
                    msgspec.structs.replace(
                        env,
                        object_version=2,
                        operation="upsert",
                        event_id="new",
                        payload={**env.payload, "caption": "Updated"},
                    ),
                )
                return result

        with self.assertRaises(HTTPException) as caught:
            await service.index(coll, ChangingEmbeddings())
        self.assertEqual(caught.exception.status_code, 409)
        async with self.factory() as session:
            self.assertEqual(
                await session.scalar(
                    select(func.count())
                    .select_from(EvidenceFragment)
                    .where(EvidenceFragment.embedding.is_not(None))
                ),
                0,
            )
