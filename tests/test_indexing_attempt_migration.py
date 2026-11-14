"""Migration safety tests; SQLite execution and offline PostgreSQL SQL only."""

import importlib
import io
import unittest
import uuid
from unittest.mock import patch

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.db.models import Document

migration = importlib.import_module(
    "app.db.migrations.versions.c4d8e2f9a631_add_indexing_attempt"
)


class IndexingAttemptMigrationTests(unittest.TestCase):
    def test_postgres_sql_model_types_and_revision_chain(self):
        self.assertEqual(migration.down_revision, "b7c2d9e4f610")
        self.assertEqual(migration.revision, "c4d8e2f9a631")
        output = io.StringIO()
        context = MigrationContext.configure(
            dialect_name="postgresql",
            opts={"as_sql": True, "output_buffer": output},
        )
        with patch.object(migration, "op", Operations(context)):
            migration.upgrade()
        sql = output.getvalue()
        self.assertIn("ADD COLUMN indexing_attempt_id UUID", sql)
        self.assertIn("ADD COLUMN indexing_expires_at TIMESTAMP WITH TIME ZONE", sql)
        self.assertEqual(sql.count("ALTER TABLE documents ADD COLUMN"), 2)
        self.assertNotIn("UPDATE ", sql)
        self.assertNotIn("DELETE ", sql)

        columns = Document.__table__.c
        self.assertIs(columns.indexing_attempt_id.type.python_type, uuid.UUID)
        self.assertTrue(columns.indexing_expires_at.type.timezone)
        for name in ("indexing_attempt_id", "indexing_expires_at"):
            self.assertTrue(columns[name].nullable)
            self.assertIsNone(columns[name].default)
            self.assertIsNone(columns[name].server_default)

    def test_upgrade_and_downgrade_preserve_existing_documents_and_chunks(self):
        engine = sa.create_engine("sqlite:///:memory:")
        self.addCleanup(engine.dispose)
        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    "CREATE TABLE documents (id INTEGER PRIMARY KEY, "
                    "status VARCHAR(50) NOT NULL, embedding_model VARCHAR(100))"
                )
            )
            connection.execute(
                sa.text(
                    "CREATE TABLE document_chunks (id INTEGER PRIMARY KEY, "
                    "document_id INTEGER, content TEXT, embedding TEXT)"
                )
            )
            connection.execute(
                sa.text(
                    "INSERT INTO documents VALUES "
                    "(1, 'ready', 'text-embedding-3-small'), "
                    "(2, 'failed', NULL), (3, 'processing', NULL), (4, 'pending', NULL)"
                )
            )
            connection.execute(
                sa.text(
                    "INSERT INTO document_chunks VALUES (1, 1, 'Source text', '[1,0]')"
                )
            )
            before_documents = connection.execute(
                sa.text("SELECT id, status, embedding_model FROM documents ORDER BY id")
            ).all()
            before_chunks = connection.execute(
                sa.text("SELECT * FROM document_chunks")
            ).all()
            operations = Operations(MigrationContext.configure(connection))
            with patch.object(migration, "op", operations):
                migration.upgrade()
            values = connection.execute(
                sa.text(
                    "SELECT indexing_attempt_id, indexing_expires_at FROM documents"
                )
            ).all()
            self.assertEqual([tuple(row) for row in values], [(None, None)] * 4)

            for stage in ("upgraded", "downgraded"):
                with self.subTest(stage=stage):
                    if stage == "downgraded":
                        with patch.object(migration, "op", operations):
                            migration.downgrade()
                        self.assertEqual(
                            {
                                c["name"]
                                for c in sa.inspect(connection).get_columns("documents")
                            },
                            {"id", "status", "embedding_model"},
                        )
                    self.assertEqual(
                        connection.execute(
                            sa.text(
                                "SELECT id, status, embedding_model FROM documents ORDER BY id"
                            )
                        ).all(),
                        before_documents,
                    )
                    self.assertEqual(
                        connection.execute(
                            sa.text("SELECT * FROM document_chunks")
                        ).all(),
                        before_chunks,
                    )


if __name__ == "__main__":
    unittest.main()
