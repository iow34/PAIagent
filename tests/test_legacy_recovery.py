"""Legacy recovery tests. No working database or OpenAI requests."""

import io
import unittest
import uuid
from contextlib import asynccontextmanager, redirect_stderr, redirect_stdout
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import test_reindex_service as fixtures
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.cli import recover_legacy_indexing as cli
from app.db.models import Document, DocumentChunk
from app.repositories.documents import DocumentReindexConflictError, DocumentRepository
from app.security.context import AccessContext


class LegacyRecoveryTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.ReindexServiceTests.setUp
    tearDown = fixtures.ReindexServiceTests.tearDown
    seed = fixtures.ReindexServiceTests.seed
    stored = fixtures.ReindexServiceTests.stored

    def metadata(self, document_id, attempt_id, expires_at):
        with Session(self.engine) as session, session.begin():
            document = session.get(Document, document_id)
            document.indexing_attempt_id = attempt_id
            document.indexing_expires_at = expires_at
            document.indexing_error_code = "old_error"
            document.embedding_model = "text-embedding-3-small"
            for chunk in session.query(DocumentChunk).filter_by(
                document_id=document_id
            ):
                chunk.embedding = [1.0] + [0.0] * 1535

    def snapshot(self, document_id):
        document, chunks = self.stored(document_id)
        return (
            document.status,
            document.indexing_attempt_id,
            document.indexing_expires_at,
            document.indexing_error_code,
            document.embedding_model,
            document.tenant_id,
            document.user_id,
            document.title,
            [
                (
                    c.id,
                    c.content,
                    list(c.embedding) if c.embedding is not None else None,
                )
                for c in chunks
            ],
        )

    async def recover(self, document_id, *, apply=False, context=None):
        async with self.factory.begin() as session:
            await DocumentRepository(session).recover_legacy_processing(
                context=context or self.context,
                document_id=document_id,
                apply=apply,
            )

    async def test_preview_preserves_all_data(self):
        for attempt_id, expires_at in (
            (None, None),
            (uuid.uuid4(), None),
            (None, datetime.now(UTC)),
        ):
            document_id = self.seed("processing")
            self.metadata(document_id, attempt_id, expires_at)
            before = self.snapshot(document_id)
            await self.recover(document_id)
            self.assertEqual(self.snapshot(document_id), before)

    async def test_apply_changes_only_recovery_metadata(self):
        for attempt_id, expires_at in (
            (None, None),
            (uuid.uuid4(), None),
            (None, datetime.now(UTC)),
        ):
            document_id = self.seed("processing")
            self.metadata(document_id, attempt_id, expires_at)
            before = self.snapshot(document_id)
            await self.recover(document_id, apply=True)
            after = self.snapshot(document_id)
            self.assertEqual(
                after[:4], ("failed", None, None, "legacy_indexing_interrupted")
            )
            self.assertEqual(after[4:], before[4:])

    async def test_missing_and_foreign_documents_are_unavailable(self):
        document_id = self.seed("processing")
        before = self.snapshot(document_id)
        for apply in (False, True):
            for requested, context in (
                (uuid.uuid4(), self.context),
                (document_id, AccessContext(uuid.uuid4(), self.context.user_id)),
                (document_id, AccessContext(self.context.tenant_id, "other")),
            ):
                with self.assertRaises(LookupError):
                    await self.recover(requested, apply=apply, context=context)
        self.assertEqual(self.snapshot(document_id), before)

    async def test_complete_attempts_and_other_statuses_are_rejected(self):
        now = datetime.now(UTC)
        cases = [
            ("processing", uuid.uuid4(), now - timedelta(days=1)),
            ("processing", uuid.uuid4(), now + timedelta(days=1)),
            ("ready", None, None),
            ("failed", None, None),
            ("pending", None, None),
        ]
        for status, attempt_id, expires_at in cases:
            document_id = self.seed(status)
            self.metadata(document_id, attempt_id, expires_at)
            before = self.snapshot(document_id)
            for apply in (False, True):
                with self.assertRaises(DocumentReindexConflictError):
                    await self.recover(document_id, apply=apply)
            self.assertEqual(self.snapshot(document_id), before)

    async def test_recovered_document_can_be_claimed_normally(self):
        document_id = self.seed("processing")
        await self.recover(document_id, apply=True)
        with self.assertRaises(DocumentReindexConflictError):
            await self.recover(document_id, apply=True)
        attempt_id = uuid.uuid4()
        async with self.factory.begin() as session:
            texts = await DocumentRepository(session).claim_for_reindex(
                context=self.context,
                document_id=document_id,
                attempt_id=attempt_id,
            )
        self.assertEqual(texts, ["First", "Second"])
        document, _ = self.stored(document_id)
        self.assertEqual(document.status, "processing")
        self.assertEqual(document.indexing_attempt_id, attempt_id)
        self.assertIsNotNone(document.indexing_expires_at)
        self.assertIsNone(document.indexing_error_code)

    async def test_failed_commit_preserves_previous_state(self):
        document_id = self.seed("processing")
        self.metadata(document_id, uuid.uuid4(), None)
        before = self.snapshot(document_id)
        self.factory.fail_transactions = {1}
        with self.assertRaises(SQLAlchemyError):
            await self.recover(document_id, apply=True)
        self.assertEqual(self.snapshot(document_id), before)

    async def test_cli_run_reports_only_after_successful_transaction(self):
        document_id = self.seed("processing")

        @asynccontextmanager
        async def fake_database(url):
            self.assertEqual(url, "test-url")
            yield SimpleNamespace(session_factory=self.factory)

        with (
            patch.object(cli, "load_dotenv"),
            patch.object(cli.os, "environ", {"DATABASE_URL": "test-url"}),
            patch.object(cli, "open_database", fake_database),
        ):
            output = io.StringIO()
            with redirect_stdout(output):
                await cli.run(
                    context=self.context, document_id=document_id, apply=False
                )
            self.assertIn("no changes made", output.getvalue())
            self.assertEqual(self.stored(document_id)[0].status, "processing")
            self.factory.fail_transactions = {self.factory.transactions + 1}
            output = io.StringIO()
            with redirect_stdout(output), self.assertRaises(SQLAlchemyError):
                await cli.run(context=self.context, document_id=document_id, apply=True)
            self.assertEqual(output.getvalue(), "")
            self.assertEqual(self.stored(document_id)[0].status, "processing")


class LegacyRecoveryCLITests(unittest.TestCase):
    def arguments(self):
        return [
            "recover",
            "--tenant-id",
            str(uuid.uuid4()),
            "--user-id",
            "user",
            "--document-id",
            str(uuid.uuid4()),
        ]

    def test_apply_requires_explicit_workers_stopped_flag(self):
        with (
            patch("sys.argv", self.arguments() + ["--apply"]),
            patch.object(cli, "run", new_callable=AsyncMock) as run,
            redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as caught,
        ):
            cli.main()
        self.assertEqual(caught.exception.code, 2)
        run.assert_not_called()

    def test_preview_default_and_confirmed_apply(self):
        for flags, expected_apply in (
            ([], False),
            (["--apply", "--workers-stopped"], True),
        ):
            args = self.arguments()
            with (
                patch("sys.argv", args + flags),
                patch.object(cli, "run", new_callable=AsyncMock) as run,
            ):
                cli.main()
            run.assert_awaited_once_with(
                context=AccessContext(uuid.UUID(args[2]), "user"),
                document_id=uuid.UUID(args[6]),
                apply=expected_apply,
            )

    def test_invalid_user_and_uuid_are_rejected_before_database_access(self):
        for value, position in ((" user ", 4), ("not-a-uuid", 6)):
            args = self.arguments()
            args[position] = value
            with (
                patch("sys.argv", args),
                patch.object(cli, "run", new_callable=AsyncMock) as run,
                redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                cli.main()
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
