"""HTTP tests using real auth/service/repository with SQLite and fake embeddings.

No external API requests. PostgreSQL concurrency is not covered here.
"""

import unittest
import uuid
from contextlib import ExitStack, asynccontextmanager
from unittest.mock import AsyncMock, patch

import httpx2
from litestar.testing import TestClient
from openai import APIConnectionError
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from test_document_indexing import FakeEmbeddingService, SQLiteFactory

from app.asgi import create_app
from app.config.settings import Settings
from app.db.models import Base, Document, DocumentChunk, ServiceAPIKey, Tenant
from app.db.postgres import DatabaseResources
from app.security.api_keys import hash_api_key
from app.services.embedding_service import EmbeddingInputError, EmbeddingResponseError


class ReindexHTTPTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite:///:memory:",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        self.addCleanup(self.engine.dispose)

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
        self.tenant_id = uuid.uuid4()
        self.other_tenant_id = uuid.uuid4()
        with Session(self.engine) as session, session.begin():
            session.add_all(
                [
                    Tenant(id=self.tenant_id, name="First"),
                    Tenant(id=self.other_tenant_id, name="Second"),
                ]
            )
            session.flush()
            session.add_all(
                [
                    ServiceAPIKey(
                        tenant_id=self.tenant_id,
                        name="First",
                        key_hash=hash_api_key("first-key"),
                    ),
                    ServiceAPIKey(
                        tenant_id=self.other_tenant_id,
                        name="Second",
                        key_hash=hash_api_key("second-key"),
                    ),
                    ServiceAPIKey(
                        tenant_id=self.tenant_id,
                        name="Disabled",
                        key_hash=hash_api_key("disabled-key"),
                        is_active=False,
                    ),
                ]
            )

        self.factory = SQLiteFactory(self.engine)
        self.embedding = FakeEmbeddingService(self.factory)
        self.headers = {"Authorization": "Bearer first-key", "X-User-ID": "user"}

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

        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch("app.asgi.open_database", fake_database))
        stack.enter_context(patch("app.asgi.openai_lifespan", fake_openai))
        stack.enter_context(patch("app.asgi.get_settings", settings))
        self.app = create_app()
        self.client = stack.enter_context(TestClient(self.app))

    def seed(self, status="pending", texts=("First", "Second")):
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
                    indexing_error_code="old_error" if status == "failed" else None,
                )
            )
            session.flush()
            session.add_all(
                [
                    DocumentChunk(document_id=document_id, chunk_index=i, content=text)
                    for i, text in enumerate(texts)
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

    def post(self, document_id, headers=None):
        return self.client.post(
            f"/v1/documents/{document_id}/reindex",
            headers=self.headers if headers is None else headers,
        )

    def test_pending_and_failed_return_200_and_reuse_chunks(self):
        for status in ("pending", "failed"):
            with self.subTest(status=status):
                document_id = self.seed(status)
                _, before = self.stored(document_id)
                response = self.post(document_id)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(
                    response.json(),
                    {
                        "document_id": str(document_id),
                        "chunks_indexed": 2,
                    },
                )
                document, chunks = self.stored(document_id)
                self.assertEqual(document.status, "ready")
                self.assertEqual([c.id for c in chunks], [c.id for c in before])
                self.assertEqual([float(c.embedding[0]) for c in chunks], [1, 2])
                self.assertIsNone(document.indexing_error_code)

    def test_ready_and_processing_return_409_without_embeddings(self):
        for status in ("ready", "processing"):
            with self.subTest(status=status):
                document_id = self.seed(status)
                response = self.post(document_id)
                self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual(
                    response.json()["extra"]["code"], "document_reindex_conflict"
                )
                self.assertEqual(self.stored(document_id)[0].status, status)
        self.assertEqual(self.embedding.calls, 0)

    def test_invalid_stored_chunks_return_409_without_embeddings(self):
        for texts in ((), ("  ",)):
            with self.subTest(texts=texts):
                document_id = self.seed("failed", texts)
                response = self.post(document_id)
                self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual(
                    response.json()["extra"]["code"], "invalid_stored_chunks"
                )
                self.assertEqual(
                    self.stored(document_id)[0].indexing_error_code, "old_error"
                )
        self.assertEqual(self.embedding.calls, 0)

    def test_missing_and_foreign_documents_return_identical_404(self):
        document_id = self.seed()
        responses = [
            self.post(uuid.uuid4()),
            self.post(document_id, {**self.headers, "X-User-ID": "other-user"}),
            self.post(
                document_id, {**self.headers, "Authorization": "Bearer second-key"}
            ),
        ]
        for response in responses:
            self.assertEqual(response.status_code, 404, response.text)
            self.assertEqual(response.json(), responses[0].json())
        self.assertEqual(self.embedding.calls, 0)
        self.assertEqual(self.stored(document_id)[0].status, "pending")

    def test_missing_invalid_and_disabled_keys_return_401(self):
        document_id = self.seed()
        for authorization in (
            None,
            "Basic first-key",
            "Bearer wrong-key",
            "Bearer disabled-key",
        ):
            headers = {"X-User-ID": "user"}
            if authorization is not None:
                headers["Authorization"] = authorization
            with self.subTest(authorization=authorization):
                response = self.post(document_id, headers)
                self.assertEqual(response.status_code, 401, response.text)
                self.assertEqual(response.headers["www-authenticate"], "Bearer")
        self.assertEqual(self.embedding.calls, 0)
        self.assertEqual(self.stored(document_id)[0].status, "pending")

    def test_missing_user_header_returns_400(self):
        document_id = self.seed()
        response = self.post(document_id, {"Authorization": "Bearer first-key"})
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.embedding.calls, 0)
        self.assertEqual(self.stored(document_id)[0].status, "pending")

    def test_invalid_uuid_does_not_match_route(self):
        response = self.post("not-a-uuid")
        self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(self.factory.transactions, 0)
        self.assertEqual(self.embedding.calls, 0)

    def test_indexing_errors_have_safe_status_code_and_details(self):
        private = "private-provider-details"
        cases = (
            (TimeoutError(private), 504, "embedding_timeout"),
            (
                APIConnectionError(
                    message=private,
                    request=httpx2.Request("POST", "https://example.invalid"),
                ),
                503,
                "embedding_provider_error",
            ),
            (EmbeddingResponseError(private), 502, "invalid_embedding_response"),
            (EmbeddingInputError(private), 422, "invalid_embedding_input"),
            (RuntimeError(private), 500, "indexing_internal_error"),
        )
        for error, status, code in cases:
            with self.subTest(code=code):
                document_id = self.seed()
                self.embedding.error = error
                response = self.post(document_id)
                self.assertEqual(response.status_code, status, response.text)
                self.assertEqual(
                    response.json()["extra"],
                    {
                        "document_id": str(document_id),
                        "code": code,
                    },
                )
                self.assertNotIn(private, response.text)
                self.assertEqual(self.stored(document_id)[0].status, "failed")

    def test_retry_after_timeout_succeeds(self):
        document_id = self.seed()
        self.embedding.error = TimeoutError()
        self.assertEqual(self.post(document_id).status_code, 504)
        self.embedding.error = None
        self.assertEqual(self.post(document_id).status_code, 200)
        self.assertEqual(self.stored(document_id)[0].status, "ready")
        self.assertEqual(self.post(document_id).status_code, 409)
        self.assertEqual(self.embedding.calls, 2)

    def test_storage_failure_returns_500_and_rolls_back_vectors(self):
        document_id = self.seed()
        # Transactions: auth, claim, save, then best-effort failure marking.
        self.factory.fail_transactions = {3}
        response = self.post(document_id)
        self.assertEqual(response.status_code, 500, response.text)
        self.assertEqual(response.json()["extra"]["code"], "index_storage_error")
        document, chunks = self.stored(document_id)
        self.assertEqual(document.status, "failed")
        self.assertIsNone(document.indexed_at)
        self.assertTrue(all(c.embedding is None for c in chunks))

    def test_openapi_has_success_schema_auth_and_no_request_body(self):
        schema = self.app.openapi_schema.to_schema()
        operation = schema["paths"]["/v1/documents/{document_id}/reindex"]["post"]
        self.assertIn("200", operation["responses"])
        self.assertNotIn("201", operation["responses"])
        self.assertNotIn("requestBody", operation)
        self.assertEqual(operation["security"], [{"ServiceAPIKey": []}])
        parameters = {p["name"]: p for p in operation["parameters"]}
        self.assertEqual(parameters["document_id"]["in"], "path")
        self.assertTrue(parameters["X-User-ID"]["required"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
